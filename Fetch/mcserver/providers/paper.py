"""Paper Minecraft server provider (papermc.io Fill v3 API).

Standalone: `python -m mcserver.providers.paper` prints the version list.

Flow is two hops --
  1. /v3/projects/paper                       -> versions, grouped by major
     release ("1.21": ["1.21.11", "1.21.10", ...], newest first at both the
     group and the per-group level)
  2. /v3/projects/paper/versions/{id}/builds  -> every build for that
     version: a bare JSON array, each entry carrying its own channel
     (STABLE/RECOMMENDED/BETA/ALPHA), build id, and a download entry keyed
     "server:default"

This targets papermc.io's current "Fill" API. The older `api.papermc.io/v2`
endpoint this module used to hit is deprecated -- Fill is its replacement,
same idea, different shapes (versions are grouped, not flat; builds come
back as a bare array; the download key changed from "application" to
"server:default"). Parsing below is deliberately defensive about a couple of
fields (checksum location, filename) since Fill's exact schema wasn't fully
pinned down while writing this.

Unlike Spigot, Paper publishes prebuilt jars directly, so this is a plain
download-and-verify like vanilla.py -- no BuildTools, no compiling.

Fill doesn't reliably expose the required Java version in a place this
module could confirm, so `required_java()` piggybacks on VanillaProvider's
per-version metadata instead (Paper runs on the same JVM as the vanilla
version it's built from). That lookup is best-effort: if it fails for any
reason, the install proceeds without a Java note.

## Subclassing

Fill serves several projects through one shape, so everything that names
"paper" specifically is a class attribute rather than a module constant:
`project`, `cache_file`, `download_key`, plus `name` / `content_dir` from the
Provider protocol and an `install_notes` hook for anything a fork needs to
say at the end of an install. `folia.py` is the whole of that mechanism's
current use -- same API, same install, different project id and a very
different set of warnings -- following neoforge.py subclassing forge.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional, Sequence

from .base import (
    CHANNEL_LATEST,
    CHANNEL_RECOMMENDED,
    STAGE_DOWNLOAD,
    STAGE_FINALIZE,
    STAGE_INDEX,
    STAGE_RESOLVE,
    STAGE_VERIFY,
    USER_AGENT,
    InstallResult,
    channel_entries,
    check_cancelled,
    ProgressCB,
    ProviderError,
    Version,
    fetch,
)

BASE_URL = "https://fill.papermc.io/v3"
PROJECT = "paper"
DOWNLOAD_KEY = "server:default"

# Fill's stable-ish channels, best to worst. "RECOMMENDED" only shows up for
# Velocity today, but there's no harm treating it as promoted if it appears.
_STABLE_CHANNELS = {"STABLE", "RECOMMENDED"}

CACHE_TTL = 60 * 60 * 6  # 6h -- the *list* of MC versions rarely changes; builds are always fetched fresh


def _cache_path(filename: str) -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "mc-server-manager")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)


class PaperProvider:
    name = "Paper"
    content_dir = "plugins"
    compiles_from_source = False

    # ---- Fill project identity. Overridden by forks; see the module header.
    project = PROJECT
    cache_file = "paper_versions.json"
    download_key = DOWNLOAD_KEY
    #: Appended verbatim to every successful install's notes.
    install_notes: Sequence[str] = ()

    def __init__(self) -> None:
        self._project: Optional[dict] = None
        self._builds_cache: dict[str, list[dict]] = {}

    # ---------------- index ----------------

    def _load_project(self, progress: Optional[ProgressCB] = None) -> dict:
        if self._project is not None:
            return self._project

        cache = _cache_path(self.cache_file)
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < CACHE_TTL:
            try:
                with open(cache, "r", encoding="utf-8") as fh:
                    self._project = json.load(fh)
                return self._project
            except (OSError, ValueError):
                pass  # corrupt cache -> refetch

        if progress:
            progress(STAGE_INDEX, 0, None, f"Fetching {self.name} version list...")
        try:
            raw = fetch(f"{BASE_URL}/projects/{self.project}")
            data = json.loads(raw)
        except ProviderError:
            if os.path.exists(cache):
                with open(cache, "r", encoding="utf-8") as fh:
                    self._project = json.load(fh)
                return self._project
            raise

        try:
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass  # cache is a nicety, not a requirement

        self._project = data
        return data

    @staticmethod
    def _flatten_versions(data: dict) -> list[str]:
        """`versions` groups ids by major release, e.g. {"1.21": ["1.21.11", ...]}.

        Both the group order and each group's own list are newest-first, so a
        straight flatten preserves overall newest-first order -- no sorting
        needed (and version strings aren't reliably sortable as text anyway).
        """
        groups = data.get("versions", {})
        if isinstance(groups, dict):
            out: list[str] = []
            for ids in groups.values():
                out.extend(ids)
            return out
        # Defensive fallback in case a future response is a flat list instead.
        if isinstance(groups, list):
            return list(groups)
        return []

    def list_versions(
        self, include_unstable: bool = False, progress: Optional[ProgressCB] = None
    ) -> list[Version]:
        """Minecraft versions Paper builds for, newest first.

        Fill only lists versions Paper actually targets and doesn't mark any of
        them as snapshots, so the checkbox has nothing to filter here -- what it
        does is split each version into "Recommended" (a STABLE build) and
        "Latest" (any channel). With it off, a version that only has BETA builds
        installs the newest one rather than refusing; that used to be a dead end
        that told you to enable snapshots.

        Which builds exist costs a request per version, so the split resolves at
        install time (`resolved=False`).
        """
        data = self._load_project(progress)
        out: list[Version] = []
        for vid in self._flatten_versions(data):
            out.extend(channel_entries(vid, include_unstable, ref=vid, resolved=False))
        return out

    def latest_release(self) -> Optional[str]:
        ids = self._flatten_versions(self._load_project())
        return ids[0] if ids else None

    # ---------------- resolve ----------------

    def _builds(self, version_id: str, progress: Optional[ProgressCB] = None) -> list[dict]:
        if version_id in self._builds_cache:
            return self._builds_cache[version_id]
        if progress:
            progress(STAGE_RESOLVE, 0, None, f"Fetching {self.name} builds for {version_id}...")
        url = f"{BASE_URL}/projects/{self.project}/versions/{version_id}/builds"
        data = json.loads(fetch(url))
        # Fill returns a bare array; tolerate a {"builds": [...]} wrapper too
        # in case that changes.
        builds = data if isinstance(data, list) else data.get("builds", [])
        if not builds:
            raise ProviderError(
                f"No {self.name} builds published for Minecraft {version_id}."
            )
        self._builds_cache[version_id] = builds
        return builds

    def _select_build(
        self,
        version_id: str,
        progress: Optional[ProgressCB] = None,
        channel: Optional[str] = None,
    ) -> dict:
        """The build a dropdown entry means. See list_versions for the channels.

        Fill's own `channel` field (STABLE/RECOMMENDED/BETA/ALPHA) is what
        "recommended" resolves against; `latest` ignores it entirely and takes
        the highest build id.
        """
        builds = self._builds(version_id, progress)
        newest = lambda pool: max(pool, key=lambda b: b.get("id") or b.get("build") or 0)

        if channel == CHANNEL_LATEST:
            return newest(builds)

        stable = [b for b in builds if str(b.get("channel", "")).upper() in _STABLE_CHANNELS]
        if stable:
            return newest(stable)
        if channel == CHANNEL_RECOMMENDED:
            raise ProviderError(
                f"{self.name} {version_id} has no stable build yet -- every build so far "
                'is a beta or alpha. Pick the "Latest" entry for this version '
                "instead."
            )
        return newest(builds)

    @classmethod
    def _download_info(cls, build: dict) -> tuple[str, str, Optional[str]]:
        """-> (url, filename, sha256_or_None). Tolerant of minor schema drift."""
        downloads = build.get("downloads") or {}
        entry = downloads.get(cls.download_key) or downloads.get("application")
        if not entry or not entry.get("url"):
            raise ProviderError(
                f"{cls.name} build {build.get('id', '?')} has no "
                f"'{cls.download_key}' download."
            )
        url = entry["url"]
        name = entry.get("name") or posixpath.basename(urllib.parse.urlsplit(url).path) or "server.jar"
        sha256 = (
            entry.get("sha256")
            or (entry.get("checksums") or {}).get("sha256")
            or entry.get("checksum")
        )
        return url, name, sha256

    def required_java(self, version_id: str) -> Optional[int]:
        """Best-effort: ask VanillaProvider what the same MC version needs."""
        try:
            from .vanilla import VanillaProvider  # local import, avoids a hard dependency

            vp = VanillaProvider()
            for v in vp.list_versions(include_unstable=True):
                if v.id == version_id:
                    return vp.required_java(v)
        except Exception:
            pass
        return None

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

        report(STAGE_RESOLVE, 0, None, f"Resolving {self.name} {version.id}...")
        build = self._select_build(version.id, progress, version.channel)
        build_id = build.get("id") or build.get("build")
        download_url, jar_name, expected_sha256 = self._download_info(build)

        java_major = self.required_java(version.id)

        os.makedirs(dest_dir, exist_ok=True)
        jar_path = os.path.join(dest_dir, "server.jar")

        fd, tmp_path = tempfile.mkstemp(prefix=".server-", suffix=".part", dir=dest_dir)
        os.close(fd)
        digest = hashlib.sha256()
        try:
            req = urllib.request.Request(download_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp_path, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0) or None
                got = 0
                report(STAGE_DOWNLOAD, 0, total, f"Downloading {jar_name}...")
                while True:
                    check_cancelled(cancel)
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
                    report(STAGE_DOWNLOAD, got, total, f"Downloading {jar_name}... ({got:,} bytes)")

            report(STAGE_VERIFY, 0, None, "Verifying checksum...")
            notes: list[str] = [
                f"{self.name} build {build_id} ({build.get('channel', 'unknown').lower()})."
            ]
            if expected_sha256:
                if digest.hexdigest() != expected_sha256:
                    raise ProviderError(
                        "SHA-256 mismatch on downloaded jar -- the file is corrupt or was "
                        "tampered with. Nothing was installed."
                    )
            else:
                notes.append("No checksum was published for this build; integrity was not verified.")

            os.replace(tmp_path, jar_path)
            tmp_path = None  # ownership transferred
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        report(STAGE_FINALIZE, 0, None, "Writing server files...")

        if self.content_dir:
            os.makedirs(os.path.join(dest_dir, self.content_dir), exist_ok=True)

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
        notes.extend(self.install_notes)

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
    p = PaperProvider()
    versions = p.list_versions(include_unstable=False)
    print(f"{len(versions)} versions, latest = {p.latest_release()}")
    for v in versions[:15]:
        print(" ", v)
    print("\nwith pre-releases:")
    for v in p.list_versions(include_unstable=True)[:6]:
        print(" ", v)


if __name__ == "__main__":
    _cli()
