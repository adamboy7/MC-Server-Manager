"""Fabric Minecraft server provider (meta.fabricmc.net v2).

Standalone: `python -m mcserver.providers.fabric` prints the version list.

Fabric is the odd one out in this codebase. It neither ships a runnable server
jar (vanilla, Paper) nor compiles anything locally (Spigot) nor runs a heavy
installer (Forge). What Fabric publishes is a *launcher*: a small jar that,
given a Minecraft version + loader version + installer version, boots the
vanilla server with Fabric's classloader patched in. Everything else it needs
it pulls from Maven the first time it runs.

Four endpoints, all on meta.fabricmc.net/v2 --

  /versions/game          [{"version": "1.21.1", "stable": true}, ...]
                          Every Minecraft version Fabric *knows about*,
                          newest first. `stable` is the release/snapshot flag.
  /versions/intermediary  [{"maven": ..., "version": "1.21.1", ...}, ...]
                          Every version Fabric actually has mappings for.
                          This is the real supported set: /game is a superset
                          by a couple dozen entries. Its own `stable` field is
                          true for everything and carries no information --
                          release/snapshot comes from /game.
  /versions/loader/{game} [{"loader": {...}, "intermediary": {...},
                            "launcherMeta": {...}}, ...]
                          Loader builds compatible with that Minecraft
                          version, newest first. `launcherMeta.min_java_version`
                          is the one machine-readable Java hint any provider in
                          this project gets for free.
  /versions/installer     [{"url": ..., "version": "1.1.2", "stable": true}, ...]

...plus the download itself, which is a URL shape rather than JSON:

  /versions/loader/{game}/{loader}/{installer}/server/jar

The dropdown lists Minecraft versions, not loader builds -- same treatment
Forge gets, including the Recommended/Latest split (see forge.py's header for
why it exists). "Recommended" here means the newest loader Fabric flagged
`stable`; with the checkbox off a version whose loaders are all betas still
lists, and installs the newest of them rather than refusing. Unlike Forge the
loader list costs a request per Minecraft version, so the split is resolved at
install time rather than while listing. `builds_for()` returns the full list if
a future UI wants a second dropdown.

## Why the vanilla jar is downloaded here

Left alone, the Fabric launcher fetches the vanilla server jar itself on first
start. This module downloads it up front instead, through `VanillaProvider`,
and writes `fabric-server-launcher.properties` pointing at it. Two reasons:
the jar arrives SHA-1 verified against Mojang's manifest like every other jar
this project installs, and the finished directory doesn't depend on a network
round-trip happening correctly at first launch on someone else's machine.

The launcher still fetches Fabric's own libraries (loader, ASM, mixin -- a few
MB) into `.fabric/` on first start, so the first launch is not fully offline.
There is no way around that short of reimplementing the installer.

## Checksums

Mojang publishes a SHA-1 for the vanilla jar and it is verified. Fabric's meta
API publishes no checksum for the launcher jar at the `/server/jar` endpoint,
so that one is size-checked only and a note says so. (`launcherMeta.libraries`
does carry hashes, but those are for artifacts the launcher fetches itself, not
for the launcher.)
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
    CHANNEL_LATEST,
    CHANNEL_RECOMMENDED,
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

BASE_URL = "https://meta.fabricmc.net/v2"

LAUNCHER_JAR = "fabric-server-launch.jar"
VANILLA_JAR = "server.jar"
PROPERTIES = "fabric-server-launcher.properties"

CACHE_TTL = 60 * 60 * 6  # 6h, matching vanilla/paper


def _cache_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "mc-server-manager")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "fabric_versions.json")


class FabricProvider:
    name = "Fabric"
    content_dir = "mods"
    compiles_from_source = False

    def __init__(self) -> None:
        self._index: Optional[dict] = None          # {"game": [...], "intermediary": [...]}
        self._loaders_cache: dict[str, list[dict]] = {}
        self._installer: Optional[dict] = None

    # ---------------- index ----------------

    def _load_index(self, progress: Optional[ProgressCB] = None) -> dict:
        if self._index is not None:
            return self._index

        cache = _cache_path()
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < CACHE_TTL:
            try:
                with open(cache, "r", encoding="utf-8") as fh:
                    self._index = json.load(fh)
                return self._index
            except (OSError, ValueError):
                pass  # corrupt cache -> refetch

        if progress:
            progress(STAGE_INDEX, 0, None, "Fetching Fabric version list...")
        try:
            data = {
                "game": json.loads(fetch(f"{BASE_URL}/versions/game")),
                "intermediary": json.loads(fetch(f"{BASE_URL}/versions/intermediary")),
            }
        except ProviderError:
            # Offline but with a stale cache beats an empty dropdown.
            if os.path.exists(cache):
                with open(cache, "r", encoding="utf-8") as fh:
                    self._index = json.load(fh)
                return self._index
            raise

        try:
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass  # cache is a nicety, not a requirement

        self._index = data
        return data

    def list_versions(
        self, include_unstable: bool = False, progress: Optional[ProgressCB] = None
    ) -> list[Version]:
        """Minecraft versions Fabric has mappings for, newest first.

        Same Recommended/Latest split as Forge, but Fabric's loader list costs a
        request per Minecraft version, so which builds exist isn't known while
        listing (`resolved=False`). With the checkbox off that makes no
        difference -- one entry, stable loader preferred, newest loader if
        there is no stable one. With it on, both entries are offered for every
        version and `_select_loader` says plainly if the stable one turns out
        not to exist.
        """
        data = self._load_index(progress)

        supported = {
            entry.get("version")
            for entry in data.get("intermediary", [])
            if entry.get("version")
        }

        out: list[Version] = []
        for entry in data.get("game", []):
            vid = entry.get("version")
            if not vid or vid not in supported:
                # Known to Fabric but with no mappings published -- the loader
                # endpoint returns [] for these, so they'd fail at install time.
                continue
            out.extend(
                channel_entries(
                    vid,
                    include_unstable,
                    kind="release" if entry.get("stable") else "snapshot",
                    ref=vid,
                    resolved=False,
                )
            )

        # /versions/game is newest-first already, and Minecraft version ids are
        # not sortable as text (1.21.1 / 26.3-snapshot-8 / 1.14_combat-0), so
        # the upstream order is the only ordering available. Unlike Mojang's
        # manifest there are no release dates here to sort by.
        return out

    def latest_release(self) -> Optional[str]:
        for entry in self._load_index().get("game", []):
            if entry.get("stable"):
                return entry.get("version")
        return None

    # ---------------- resolve ----------------

    def _loaders(self, game_id: str, progress: Optional[ProgressCB] = None) -> list[dict]:
        if game_id in self._loaders_cache:
            return self._loaders_cache[game_id]
        if progress:
            progress(STAGE_RESOLVE, 0, None, f"Fetching Fabric loaders for {game_id}...")
        entries = json.loads(fetch(f"{BASE_URL}/versions/loader/{game_id}"))
        if not isinstance(entries, list) or not entries:
            raise ProviderError(
                f"Fabric publishes no loader for Minecraft {game_id}."
            )
        self._loaders_cache[game_id] = entries
        return entries

    def builds_for(self, game_id: str) -> list[str]:
        """Every loader version usable with this Minecraft version, newest first."""
        return [e.get("loader", {}).get("version", "") for e in self._loaders(game_id)]

    def _select_loader(
        self,
        game_id: str,
        progress: Optional[ProgressCB] = None,
        channel: Optional[str] = None,
    ) -> dict:
        """The loader build a dropdown entry means. Upstream order is newest-first.

        channel None      -- newest stable, falling back to newest of any. A
                             version with only beta loaders is installable
                             without turning pre-releases on; that fallback is
                             the point of the split.
        CHANNEL_LATEST    -- newest of any stability, deliberately.
        CHANNEL_RECOMMENDED -- newest stable, and an error rather than a quiet
                             downgrade if there is none.
        """
        entries = self._loaders(game_id, progress)
        if channel == CHANNEL_LATEST:
            return entries[0]

        stable = [e for e in entries if e.get("loader", {}).get("stable")]
        if stable:
            return stable[0]
        if channel == CHANNEL_RECOMMENDED:
            raise ProviderError(
                f"Fabric has no stable loader for Minecraft {game_id} -- every "
                'build so far is a beta. Pick the "Latest" entry for this '
                "version instead."
            )
        return entries[0]

    def _select_installer(self, progress: Optional[ProgressCB] = None) -> str:
        """Newest stable installer version, else newest of any.

        The installer version only decides how the launcher jar is *built*; an
        unstable one here is far less consequential than an unstable loader, so
        this ignores `include_unstable` and just prefers stable when it exists.
        """
        if self._installer is None:
            if progress:
                progress(STAGE_RESOLVE, 0, None, "Fetching Fabric installer list...")
            entries = json.loads(fetch(f"{BASE_URL}/versions/installer"))
            if not entries:
                raise ProviderError("Fabric published no installer versions.")
            stable = [e for e in entries if e.get("stable")]
            self._installer = (stable or entries)[0]
        return self._installer["version"]

    def required_java(self, game_id: str, loader_entry: Optional[dict] = None) -> Optional[int]:
        """Highest of what Fabric's loader needs and what the MC version needs.

        Fabric states its own floor in `launcherMeta.min_java_version`, but that
        floor is about the loader, not the game -- it still reads 8 for a 1.21
        entry that Mojang requires Java 21 for. Mojang's number is the binding
        one in practice; take the max so neither is understated.
        """
        fabric_min: Optional[int] = None
        try:
            entry = loader_entry or self._select_loader(game_id)
            fabric_min = entry.get("launcherMeta", {}).get("min_java_version")
        except Exception:
            pass

        mojang: Optional[int] = None
        try:
            from .vanilla import VanillaProvider  # local import, avoids a hard dependency

            vp = VanillaProvider()
            for v in vp.list_versions(include_unstable=True):
                if v.id == game_id:
                    mojang = vp.required_java(v)
                    break
        except Exception:
            pass

        candidates = [j for j in (fabric_min, mojang) if j]
        return max(candidates) if candidates else None

    # ---------------- download helper ----------------

    @staticmethod
    def _download(
        url: str,
        dest_path: str,
        label: str,
        progress: Optional[ProgressCB],
        cancel: Optional[threading.Event],
        expected_sha1: Optional[str] = None,
        expected_size: int = 0,
    ) -> None:
        """Stream to a sibling temp file, verify, then atomically rename.

        Same shape as vanilla.py's inline download -- kept local rather than
        pushed into base.py so this module doesn't force a change on the other
        providers.
        """
        def report(stage: str, done: int, total: Optional[int], msg: str) -> None:
            if progress:
                progress(stage, done, total, msg)

        dest_dir = os.path.dirname(dest_path)
        fd, tmp_path = tempfile.mkstemp(prefix=".fabric-", suffix=".part", dir=dest_dir)
        os.close(fd)
        digest = hashlib.sha1()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp_path, "wb") as out:
                total = expected_size or int(resp.headers.get("Content-Length") or 0) or None
                got = 0
                report(STAGE_DOWNLOAD, 0, total, f"Downloading {label}...")
                while True:
                    check_cancelled(cancel)
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
                    report(STAGE_DOWNLOAD, got, total, f"Downloading {label}... ({got:,} bytes)")

            if expected_sha1 or expected_size:
                report(STAGE_VERIFY, 0, None, f"Verifying {label}...")
            if expected_sha1 and digest.hexdigest() != expected_sha1:
                raise ProviderError(
                    f"SHA-1 mismatch on {label} -- the file is corrupt or was tampered "
                    "with. Nothing was installed."
                )
            if expected_size and got != expected_size:
                raise ProviderError(
                    f"Size mismatch on {label}: expected {expected_size}, got {got}."
                )
            if not got:
                raise ProviderError(f"{label} downloaded as an empty file.")

            os.replace(tmp_path, dest_path)
            tmp_path = None  # ownership transferred
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

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

        game_id = version.id
        report(STAGE_RESOLVE, 0, None, f"Resolving Fabric {game_id}...")

        loader_entry = self._select_loader(game_id, progress, version.channel)
        loader_version = loader_entry["loader"]["version"]
        loader_stable = bool(loader_entry["loader"].get("stable"))
        installer_version = self._select_installer(progress)
        java_major = self.required_java(game_id, loader_entry)

        # The vanilla jar is resolved *before* anything is written, so a version
        # Mojang has no server download for fails cleanly instead of leaving a
        # half-built directory behind.
        vanilla_dl = self._vanilla_download(game_id)

        os.makedirs(dest_dir, exist_ok=True)
        check_cancelled(cancel)

        notes: list[str] = [
            f"Fabric loader {loader_version}"
            + ("" if loader_stable else " (beta)")
            + f", installer {installer_version}."
        ]

        # 1. vanilla server jar -- checksummed against Mojang's manifest
        self._download(
            vanilla_dl["url"],
            os.path.join(dest_dir, VANILLA_JAR),
            f"Minecraft {game_id} server",
            progress,
            cancel,
            expected_sha1=vanilla_dl.get("sha1"),
            expected_size=int(vanilla_dl.get("size") or 0),
        )

        # 2. Fabric launcher jar -- no checksum published for this endpoint
        launcher_url = (
            f"{BASE_URL}/versions/loader/{game_id}/{loader_version}/"
            f"{installer_version}/server/jar"
        )
        self._download(
            launcher_url,
            os.path.join(dest_dir, LAUNCHER_JAR),
            LAUNCHER_JAR,
            progress,
            cancel,
        )
        notes.append(
            "Fabric publishes no checksum for the launcher jar, so only the "
            "Minecraft server jar was integrity-verified."
        )

        report(STAGE_FINALIZE, 0, None, "Writing server files...")

        os.makedirs(os.path.join(dest_dir, "mods"), exist_ok=True)

        # Points the launcher at the jar downloaded above instead of letting it
        # fetch its own copy.
        with open(os.path.join(dest_dir, PROPERTIES), "w", encoding="utf-8") as fh:
            fh.write(
                "# Written by mc-server-manager.\n"
                f"serverJar={VANILLA_JAR}\n"
            )

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

        notes.append(
            "First launch downloads Fabric's loader libraries into .fabric/ "
            "and needs an internet connection. Later launches do not."
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
            jar_path=os.path.join(dest_dir, LAUNCHER_JAR),
            launch_argv=launch,
            java_major=java_major,
            notes=notes,
        )

    @staticmethod
    def _vanilla_download(game_id: str) -> dict:
        """Mojang's server download entry for this version. Raises if absent."""
        try:
            from .vanilla import VanillaProvider

            vp = VanillaProvider()
            for v in vp.list_versions(include_unstable=True):
                if v.id == game_id:
                    return vp.server_download(v)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Could not reach Mojang to fetch the Minecraft {game_id} server "
                f"jar that Fabric runs on top of: {exc}"
            ) from None
        raise ProviderError(
            f"Mojang's manifest has no entry for {game_id}, so the vanilla server "
            "jar Fabric needs could not be downloaded."
        )

    @staticmethod
    def _write_start_scripts(dest_dir: str, java_major: Optional[int]) -> None:
        hint = f"REM Requires Java {java_major}\n" if java_major else ""
        bat = os.path.join(dest_dir, "start.bat")
        with open(bat, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(
                f"@echo off\n{hint}java -Xms1G -Xmx2G -jar {LAUNCHER_JAR} nogui\npause\n"
            )

        sh_hint = f"# Requires Java {java_major}\n" if java_major else ""
        sh = os.path.join(dest_dir, "start.sh")
        with open(sh, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                f"#!/bin/sh\n{sh_hint}exec java -Xms1G -Xmx2G -jar {LAUNCHER_JAR} nogui\n"
            )
        try:
            os.chmod(sh, os.stat(sh).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass


def _cli() -> None:
    p = FabricProvider()
    versions = p.list_versions(include_unstable=False)
    print(f"{len(versions)} releases, latest = {p.latest_release()}")
    for v in versions[:15]:
        print(" ", v.id)

    if versions:
        newest = versions[0].id
        builds = p.builds_for(newest)
        print(f"\nloaders for {newest}: {builds[:5]}")
        entry = p._select_loader(newest)
        print("  selected loader:", entry["loader"]["version"],
              "stable" if entry["loader"].get("stable") else "beta")
        print("  installer:", p._select_installer())
        print("  java:", p.required_java(newest, entry))


if __name__ == "__main__":
    _cli()
