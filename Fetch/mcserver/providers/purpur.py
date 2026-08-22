"""Purpur Minecraft server provider (api.purpurmc.org v2).

Standalone: `python -m mcserver.providers.purpur` prints the version list.

Purpur is a fork of Paper, so the *install* is the same shape as paper.py --
a prebuilt jar, downloaded and verified, no BuildTools and no installer. The
API in front of it is a different animal though, and simpler:

  1. /v2/purpur                        -> {"project", "versions": [...]}
  2. /v2/purpur/{version}              -> {"builds": {"latest": "2416",
                                                      "all": ["2380", ...]}}
  3. /v2/purpur/{version}/{build}      -> that build's metadata: `result`
                                          ("SUCCESS"/"FAILURE"), `timestamp`,
                                          `commits`, and an `md5` checksum
  4. /v2/purpur/{version}/{build}/download  -> the jar

Two things about that shape drive the code below.

**There is no promotion channel.** Paper tags each build STABLE/BETA/ALPHA;
Purpur tags nothing. What it does have is builds that failed to compile, which
are still listed and still numbered. So the Recommended/Latest split (see
`base.channel_entries`) resolves against `result` instead of a channel:
Recommended is the newest build that actually built, Latest is the newest
build full stop. Most of the time they are the same build and the distinction
costs nothing; when the newest build is a FAILURE, Recommended walks back to
the last good one and says so in the install notes, and Latest raises a
pointed error rather than downloading a jar that isn't there. Finding that out
costs a request per build probed, so entries are listed with `resolved=False`
and settled at install time -- same as Paper, Fabric and Sponge.

**The checksum is MD5, not SHA-256.** That is what the API publishes, so that
is what gets verified. It catches a truncated or corrupted download, which is
the failure this is actually guarding against; it is not a meaningful defence
against a tampered jar, and the install notes say so rather than implying a
stronger guarantee than the upstream data supports.

Purpur doesn't publish its required Java version either, so `required_java()`
piggybacks on `VanillaProvider` exactly as paper.py does -- a Purpur build for
Minecraft 1.21.4 runs on whatever JVM Mojang's 1.21.4 needs. Best-effort: if
the lookup fails for any reason the install proceeds without a Java note.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import urllib.request
from typing import Optional

from .base import (
    CHANNEL_LATEST,
    STAGE_DOWNLOAD,
    STAGE_FINALIZE,
    STAGE_INDEX,
    STAGE_RESOLVE,
    STAGE_VERIFY,
    USER_AGENT,
    InstallResult,
    ProgressCB,
    ProviderError,
    Version,
    channel_entries,
    check_cancelled,
    fetch,
)

BASE_URL = "https://api.purpurmc.org/v2"
PROJECT = "purpur"

# How far back to walk looking for a build that compiled. Each step is one
# request, and a run of failures this long means something is broken upstream
# rather than "the newest build is a bit fresh" -- at which point erroring out
# is more useful than silently installing a jar from weeks ago.
MAX_BUILD_PROBES = 12

CACHE_TTL = 60 * 60 * 6  # 6h -- the *list* of MC versions rarely changes; builds are always fetched fresh

_NUMERIC = re.compile(r"\d+")


def _cache_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "mc-server-manager")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "purpur_versions.json")


def _version_key(vid: str) -> Optional[tuple[int, ...]]:
    """Loose numeric key for a Minecraft version id, or None if it isn't one.

    Only used to work out which end of the API's list is the newest (see
    `_newest_first`), never to sort the list -- Purpur targets plain releases,
    so the ids are dotted numbers, but relying on that to *order* them would
    break the moment one isn't.
    """
    parts = _NUMERIC.findall(vid)
    return tuple(int(p) for p in parts) if parts else None


class PurpurProvider:
    name = "Purpur"
    content_dir = "plugins"
    compiles_from_source = False

    def __init__(self) -> None:
        self._project: Optional[dict] = None
        self._version_meta: dict[str, dict] = {}
        self._build_meta: dict[tuple[str, str], dict] = {}

    # ---------------- index ----------------

    def _load_project(self, progress: Optional[ProgressCB] = None) -> dict:
        if self._project is not None:
            return self._project

        cache = _cache_path()
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < CACHE_TTL:
            try:
                with open(cache, "r", encoding="utf-8") as fh:
                    self._project = json.load(fh)
                return self._project
            except (OSError, ValueError):
                pass  # corrupt cache -> refetch

        if progress:
            progress(STAGE_INDEX, 0, None, "Fetching Purpur version list...")
        try:
            data = json.loads(fetch(f"{BASE_URL}/{PROJECT}"))
        except ProviderError:
            # Stale beats nothing when the API is down mid-session.
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
    def _newest_first(data: dict) -> list[str]:
        """Version ids, newest first.

        v2 hands back `versions` oldest-first, the opposite of what the
        dropdown wants and the opposite of Paper's Fill API -- an easy thing
        to get backwards and a silent one when you do, since both ends of the
        list are real version numbers. Rather than hardcoding a `reverse()`
        that breaks quietly if upstream ever flips, the direction is measured:
        compare the first and last ids that parse as numbers and reverse only
        if the list is genuinely ascending.
        """
        ids = data.get("versions") or []
        if not isinstance(ids, list):
            return []
        ids = [str(v) for v in ids]

        head = next((k for k in map(_version_key, ids) if k), None)
        tail = next((k for k in map(_version_key, reversed(ids)) if k), None)
        if head and tail and tail > head:
            ids = list(reversed(ids))
        return ids

    def list_versions(
        self, include_unstable: bool = False, progress: Optional[ProgressCB] = None
    ) -> list[Version]:
        """Minecraft versions Purpur builds for, newest first.

        Purpur only publishes for Minecraft releases and marks none of them as
        snapshots, so -- as with Paper -- the checkbox has nothing to filter
        here. What it does is split each version into "Recommended" (the newest
        build that compiled) and "Latest" (the newest build, period). Which
        builds succeeded costs a request each, so the split resolves at install
        time (`resolved=False`).
        """
        data = self._load_project(progress)
        out: list[Version] = []
        for vid in self._newest_first(data):
            out.extend(channel_entries(vid, include_unstable, ref=vid, resolved=False))
        return out

    def latest_release(self) -> Optional[str]:
        ids = self._newest_first(self._load_project())
        return ids[0] if ids else None

    # ---------------- resolve ----------------

    def _builds(self, version_id: str, progress: Optional[ProgressCB] = None) -> list[str]:
        """Build ids for one Minecraft version, newest first."""
        meta = self._version_meta.get(version_id)
        if meta is None:
            if progress:
                progress(STAGE_RESOLVE, 0, None, f"Fetching Purpur builds for {version_id}...")
            meta = json.loads(fetch(f"{BASE_URL}/{PROJECT}/{version_id}"))
            self._version_meta[version_id] = meta

        builds = meta.get("builds") or {}
        # Documented shape is {"latest": "2416", "all": [...]}; tolerate a bare
        # list in case that ever collapses.
        if isinstance(builds, list):
            all_ids, latest = [str(b) for b in builds], None
        else:
            all_ids = [str(b) for b in (builds.get("all") or [])]
            latest = builds.get("latest")

        # `all` is ascending; sort numerically rather than reversing so a
        # non-numeric id can't wreck the order, and keep `latest` authoritative
        # about which build is newest.
        all_ids.sort(key=lambda b: _version_key(b) or (), reverse=True)
        if latest:
            latest = str(latest)
            all_ids = [latest] + [b for b in all_ids if b != latest]

        if not all_ids:
            raise ProviderError(f"No Purpur builds published for Minecraft {version_id}.")
        return all_ids

    def _build_info(
        self, version_id: str, build: str, progress: Optional[ProgressCB] = None
    ) -> dict:
        key = (version_id, build)
        if key not in self._build_meta:
            if progress:
                progress(STAGE_RESOLVE, 0, None, f"Checking Purpur build {build}...")
            self._build_meta[key] = json.loads(
                fetch(f"{BASE_URL}/{PROJECT}/{version_id}/{build}")
            )
        return self._build_meta[key]

    @staticmethod
    def _succeeded(info: dict) -> bool:
        # A missing `result` is treated as success: the field is there to flag
        # failures, and refusing to install because a key went absent would be
        # the wrong way to be wrong.
        return str(info.get("result", "SUCCESS")).upper() == "SUCCESS"

    def _select_build(
        self,
        version_id: str,
        progress: Optional[ProgressCB] = None,
        channel: Optional[str] = None,
        cancel: Optional[threading.Event] = None,
    ) -> tuple[str, dict]:
        """-> (build_id, build_metadata). See the module docstring for the split."""
        builds = self._builds(version_id, progress)

        if channel == CHANNEL_LATEST:
            build = builds[0]
            info = self._build_info(version_id, build, progress)
            if not self._succeeded(info):
                raise ProviderError(
                    f"Purpur build {build} for Minecraft {version_id} did not compile "
                    f"({str(info.get('result', '?')).lower()}), so there is no jar to "
                    'download. Pick the "Recommended" entry for this version instead -- '
                    "it installs the newest build that did."
                )
            return build, info

        # Recommended (and the collapsed checkbox-off entry, channel=None) walk
        # back to the last build that compiled rather than erroring -- Paper's
        # equivalent raises here because a missing *promotion* is a real state
        # the user has to choose around, whereas a failed Purpur build is just
        # noise from CI. The install notes say how far back it went.
        for build in builds[:MAX_BUILD_PROBES]:
            check_cancelled(cancel)
            info = self._build_info(version_id, build, progress)
            if self._succeeded(info):
                return build, info

        raise ProviderError(
            f"None of the last {min(len(builds), MAX_BUILD_PROBES)} Purpur builds for "
            f"Minecraft {version_id} compiled successfully. This is an upstream "
            "problem; try another Minecraft version."
        )

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

        report(STAGE_RESOLVE, 0, None, f"Resolving Purpur {version.id}...")
        builds = self._builds(version.id, progress)
        build_id, info = self._select_build(version.id, progress, version.channel, cancel)

        download_url = f"{BASE_URL}/{PROJECT}/{version.id}/{build_id}/download"
        jar_name = f"purpur-{version.id}-{build_id}.jar"
        expected_md5 = info.get("md5")

        java_major = self.required_java(version.id)

        os.makedirs(dest_dir, exist_ok=True)
        jar_path = os.path.join(dest_dir, "server.jar")

        notes: list[str] = [f"Purpur build {build_id} for Minecraft {version.id}."]
        skipped = builds.index(build_id) if build_id in builds else 0
        if skipped:
            notes.append(
                f"The {skipped} newer build(s) for this version did not compile; "
                "this is the most recent one that did."
            )

        fd, tmp_path = tempfile.mkstemp(prefix=".server-", suffix=".part", dir=dest_dir)
        os.close(fd)
        digest = hashlib.md5()
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
            if expected_md5:
                if digest.hexdigest().lower() != str(expected_md5).lower():
                    raise ProviderError(
                        "MD5 mismatch on downloaded jar -- the download was corrupted or "
                        "truncated. Nothing was installed."
                    )
                notes.append(
                    "Verified against the MD5 the Purpur API publishes -- that catches a "
                    "bad download, not a tampered one."
                )
            else:
                notes.append(
                    "No checksum was published for this build; integrity was not verified."
                )

            os.replace(tmp_path, jar_path)
            tmp_path = None  # ownership transferred
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        report(STAGE_FINALIZE, 0, None, "Writing server files...")

        os.makedirs(os.path.join(dest_dir, "plugins"), exist_ok=True)

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
        notes.append(
            "Purpur is a Paper fork: Bukkit/Spigot/Paper plugins go in plugins/, and "
            "its own settings land in purpur.yml after the first launch."
        )

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
    p = PurpurProvider()
    versions = p.list_versions(include_unstable=False)
    print(f"{len(versions)} versions, latest = {p.latest_release()}")
    for v in versions[:15]:
        print(" ", v)
    print("\nwith pre-releases:")
    for v in p.list_versions(include_unstable=True)[:6]:
        print(" ", v)
    if versions:
        newest = versions[0]
        build, info = p._select_build(newest.id, channel=newest.channel)
        print(f"\n{newest.id} resolves to build {build} "
              f"({info.get('result', '?')}, md5={info.get('md5', 'none')})")


if __name__ == "__main__":
    _cli()
