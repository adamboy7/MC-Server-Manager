"""Vanilla Minecraft server provider (Mojang piston-meta).

Standalone: `python -m mcserver.providers.vanilla` prints the release list.

Flow is two hops --
  1. version_manifest_v2.json  -> every version id + a URL to its own metadata
  2. <that url>                -> downloads.server.{url,sha1,size}

Not every version has a server download; the server jar only starts appearing
around 1.2.5, so anything older is filtered out of the dropdown rather than
failing at install time.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
import time
import urllib.request
from typing import Optional

from .base import (
    STAGE_DOWNLOAD,
    STAGE_FINALIZE,
    STAGE_INDEX,
    STAGE_RESOLVE,
    STAGE_VERIFY,
    USER_AGENT,
    InstallResult,
    check_cancelled,
    ProgressCB,
    ProviderError,
    Version,
    fetch,
)

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
CACHE_TTL = 60 * 60 * 6  # 6h; Mojang ships snapshots weekly at most


def _cache_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "mc-server-manager")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "vanilla_manifest.json")


class VanillaProvider:
    name = "Vanilla"
    content_dir = None  # no plugins/ or mods/
    compiles_from_source = False

    def __init__(self) -> None:
        self._manifest: Optional[dict] = None
        self._meta_cache: dict[str, dict] = {}

    # ---------------- index ----------------

    def _load_manifest(self, progress: Optional[ProgressCB] = None) -> dict:
        if self._manifest is not None:
            return self._manifest

        cache = _cache_path()
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < CACHE_TTL:
            try:
                with open(cache, "r", encoding="utf-8") as fh:
                    self._manifest = json.load(fh)
                return self._manifest
            except (OSError, ValueError):
                pass  # corrupt cache -> refetch

        if progress:
            progress(STAGE_INDEX, 0, None, "Fetching version manifest...")
        try:
            raw = fetch(MANIFEST_URL)
            data = json.loads(raw)
        except ProviderError:
            # Offline but with a stale cache is far better than an empty dropdown.
            if os.path.exists(cache):
                with open(cache, "r", encoding="utf-8") as fh:
                    self._manifest = json.load(fh)
                return self._manifest
            raise

        try:
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass  # cache is a nicety, not a requirement

        self._manifest = data
        return data

    def list_versions(
        self, include_unstable: bool = False, progress: Optional[ProgressCB] = None
    ) -> list[Version]:
        data = self._load_manifest(progress)
        out: list[Version] = []
        for entry in data.get("versions", []):
            kind = entry.get("type", "release")
            if not include_unstable and kind != "release":
                continue
            out.append(
                Version(
                    id=entry["id"],
                    kind=kind,
                    released=entry.get("releaseTime"),
                    ref=entry.get("url"),
                )
            )
        # Mojang already returns newest-first, but don't rely on it -- version ids
        # are unsortable (1.2.5 / 23w13a / a1.2.6) so sort by release date.
        out.sort(key=lambda v: v.released or "", reverse=True)
        return out

    def latest_release(self) -> Optional[str]:
        return self._load_manifest().get("latest", {}).get("release")

    # ---------------- resolve ----------------

    def _version_meta(self, version: Version) -> dict:
        if not version.ref:
            raise ProviderError(f"No metadata URL for {version.id}")
        if version.ref in self._meta_cache:
            return self._meta_cache[version.ref]
        meta = json.loads(fetch(version.ref))
        self._meta_cache[version.ref] = meta
        return meta

    def server_download(self, version: Version) -> dict:
        """-> {'url','sha1','size'}. Raises if this version has no server jar."""
        meta = self._version_meta(version)
        server = meta.get("downloads", {}).get("server")
        if not server or not server.get("url"):
            raise ProviderError(
                f"Minecraft {version.id} has no official server download. "
                "Mojang only began publishing server jars around 1.2.5."
            )
        return server

    def required_java(self, version: Version) -> Optional[int]:
        """Mojang states this directly for 1.17+; older versions omit it."""
        meta = self._version_meta(version)
        return meta.get("javaVersion", {}).get("majorVersion")

    # ---------------- install ----------------

    def install(
        self,
        version: Version,
        dest_dir: str,
        progress: Optional[ProgressCB] = None,
        accept_eula: bool = False,
        cancel: Optional[threading.Event] = None,
    ) -> InstallResult:
        def report(stage: str, done: int, total: Optional[int], msg: str) -> None:
            if progress:
                progress(stage, done, total, msg)

        report(STAGE_RESOLVE, 0, None, f"Resolving Minecraft {version.id}...")
        dl = self.server_download(version)
        java_major = self.required_java(version)

        os.makedirs(dest_dir, exist_ok=True)
        jar_path = os.path.join(dest_dir, "server.jar")
        expected_size = int(dl.get("size") or 0)

        # Download to a temp file in the same directory so the final move is
        # atomic on the same filesystem, and a cancelled/failed run never leaves
        # a half-written server.jar that looks valid.
        fd, tmp_path = tempfile.mkstemp(prefix=".server-", suffix=".part", dir=dest_dir)
        os.close(fd)
        digest = hashlib.sha1()
        try:
            req = urllib.request.Request(dl["url"], headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp_path, "wb") as out:
                total = expected_size or int(resp.headers.get("Content-Length") or 0) or None
                got = 0
                report(STAGE_DOWNLOAD, 0, total, "Downloading server.jar...")
                while True:
                    check_cancelled(cancel)
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
                    report(STAGE_DOWNLOAD, got, total, f"Downloading server.jar... ({got:,} bytes)")

            report(STAGE_VERIFY, 0, None, "Verifying checksum...")
            if dl.get("sha1") and digest.hexdigest() != dl["sha1"]:
                raise ProviderError(
                    "SHA-1 mismatch on downloaded jar -- the file is corrupt or was "
                    "tampered with. Nothing was installed."
                )
            if expected_size and got != expected_size:
                raise ProviderError(f"Size mismatch: expected {expected_size}, got {got}.")

            os.replace(tmp_path, jar_path)
            tmp_path = None  # ownership transferred
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        report(STAGE_FINALIZE, 0, None, "Writing server files...")
        notes: list[str] = []

        eula_path = os.path.join(dest_dir, "eula.txt")
        if accept_eula:
            with open(eula_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "# Accepted via mc-server-manager on the user's behalf.\n"
                    "# https://aka.ms/MinecraftEULA\n"
                    "eula=true\n"
                )
        elif not os.path.exists(eula_path):
            notes.append(
                "eula.txt was not written. The server will exit on first launch "
                "until you accept the Minecraft EULA."
            )

        launch = [
            "{java}", "-Xms{min_ram}", "-Xmx{max_ram}",
            "-jar", "{jar}", "nogui",
        ]
        self._write_start_scripts(dest_dir, java_major)

        if java_major:
            notes.append(f"This version requires Java {java_major}.")

        return InstallResult(
            server_dir=dest_dir,
            jar_path=jar_path,
            launch_argv=launch,
            java_major=java_major,
            notes=notes,
        )

    @staticmethod
    def _write_start_scripts(dest_dir: str, java_major: Optional[int]) -> None:
        hint = f"REM Requires Java {java_major}\n" if java_major else ""
        bat = os.path.join(dest_dir, "start.bat")
        with open(bat, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(f"@echo off\n{hint}java -Xms1G -Xmx2G -jar server.jar nogui\npause\n")

        sh_hint = f"# Requires Java {java_major}\n" if java_major else ""
        sh = os.path.join(dest_dir, "start.sh")
        with open(sh, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"#!/bin/sh\n{sh_hint}exec java -Xms1G -Xmx2G -jar server.jar nogui\n")
        try:
            os.chmod(sh, os.stat(sh).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass


def _cli() -> None:
    p = VanillaProvider()
    versions = p.list_versions(include_unstable=False)
    print(f"{len(versions)} releases, latest = {p.latest_release()}")
    for v in versions[:15]:
        print(" ", v.id, v.released)


if __name__ == "__main__":
    _cli()
