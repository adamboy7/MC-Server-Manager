"""Sponge providers -- SpongeVanilla, SpongeForge and SpongeNeo.

Standalone: `python -m mcserver.providers.sponge` prints all three version lists.

Three providers live in one module because they are one API with three artifact
ids, not three APIs. Everything except the last step of `install()` is shared.

## The downloads API

Everything hangs off `dl-api.spongepowered.org/v2/groups/org.spongepowered`:

  /artifacts                      {"artifactIds": ["spongevanilla", "spongeforge",
                                   "spongeneo"], "type": "group"}
  /artifacts/{id}                 project metadata, and the bit that matters:
                                  "tags": {"api": [...], "minecraft": [...],
                                           "forge": [...] | "neoforge": [...]}
                                  -- every value each tag has ever taken.
  /artifacts/{id}/versions        paginated build list. Filter with
                                  ?tags=minecraft:1.21.1 and ?recommended=true.
                                  -> {"artifacts": {"<build>": {"recommended": bool,
                                       "tagValues": {"minecraft": "1.21.1",
                                                     "api": "12.0.4",
                                                     "forge": "60.0.1"}}},
                                      "offset": 0, "limit": 25, "size": 285}
  /artifacts/{id}/versions/{v}    -> {"assets": [{"classifier", "extension",
                                       "downloadUrl", "md5", "sha1"}, ...], ...}

The asset list carries a dozen entries per build (sources, mixins, accessors,
applaunch, the pom). The only one that matters is `classifier == "universal"`,
`extension == "jar"`: for SpongeVanilla that is the runnable server, for the
other two it is the mod jar.

## Picking a build

`recommended` is rare. Minecraft 1.21.1 has 285 SpongeVanilla builds and not one
is flagged -- they are all release candidates. So the rule is the same one Paper
and Forge use here (see forge.py's header): prefer a recommended build, fall
back to the newest release candidate, and say in the install notes which one you
got. Requiring the pre-release checkbox for that fallback -- as this used to --
made almost every Sponge version un-installable without also pouring every
Minecraft snapshot into the dropdown. With the checkbox on, "Recommended" and
"Latest" become separate entries.

The API returns builds newest-first within a version, so build selection is
"first entry of the filtered page" rather than a sort -- Sponge's build strings
(`1.21.1-12.0.4-RC2684`) are not reliably sortable as text.

## The version dropdown

Built from the artifact's `minecraft` tag values. Whether an entry is a release
or a snapshot, and what order the entries go in, are *not* parsed out of the
version string -- `1.21.11`, `25w41a` and `26.1.2` have no common grammar and no
sortable shape. Instead each id is looked up in Mojang's manifest through
`VanillaProvider`, which already has both the type and the release date. Ids
Mojang doesn't know about are kept but treated as snapshots and sorted last, so
a new Sponge target never vanishes from the list just because this lookup
lagged.

## What each implementation installs

SpongeVanilla is self-contained: the universal jar is the server, and it
downloads Minecraft and its libraries itself on first start. Nothing else to do.

SpongeForge and SpongeNeo are mod jars. They need a Forge / NeoForge server
underneath at *exactly* the build named in the Sponge build's `forge` /
`neoforge` tag -- Sponge's docs are emphatic that the build number must match,
because it patches Forge internals. So those two delegate the whole base install
to `ForgeProvider.install_build()` / `NeoForgeProvider.install_build()` with the
build pinned, then drop the universal jar into `mods/`. The launch shape,
start scripts and Java requirement all come back from the base provider
unchanged; this module only appends its own notes.

`content_dir` is `plugins` for all three. That trips people up on the Forge
variants, where the *Sponge jar itself* goes in `mods/` but Sponge *plugins* go
in `plugins/` -- so both directories get created.
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
    ProgressCB,
    ProviderError,
    Version,
    channel_entries,
    check_cancelled,
    fetch,
)
from .forge import ForgeProvider, _mc_key
from .neoforge import NeoForgeProvider
from .vanilla import VanillaProvider

BASE_URL = "https://dl-api.spongepowered.org/v2/groups/org.spongepowered/artifacts"

UNIVERSAL_CLASSIFIER = "universal"
PAGE_LIMIT = 25  # only ever need the first entry; keep the response small

CACHE_TTL = 60 * 60 * 6  # 6h, matching the other providers


def _cache_path(artifact_id: str) -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "mc-server-manager")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"sponge_{artifact_id}.json")


class _SpongeBase:
    """Shared dl-api client. Not a provider on its own -- see the three below."""

    name = "Sponge"
    content_dir = "plugins"
    compiles_from_source = False

    artifact_id = ""          # spongevanilla | spongeforge | spongeneo
    platform_tag: Optional[str] = None   # "forge" | "neoforge" | None

    def __init__(self) -> None:
        self._meta: Optional[dict] = None
        self._builds_cache: dict[tuple[str, bool], dict] = {}
        self._mojang: Optional[dict[str, Version]] = None

    # ---------------- index ----------------

    def _load_meta(self, progress: Optional[ProgressCB] = None) -> dict:
        if self._meta is not None:
            return self._meta

        cache = _cache_path(self.artifact_id)
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < CACHE_TTL:
            try:
                with open(cache, "r", encoding="utf-8") as fh:
                    self._meta = json.load(fh)
                return self._meta
            except (OSError, ValueError):
                pass  # corrupt cache -> refetch

        if progress:
            progress(STAGE_INDEX, 0, None, f"Fetching {self.name} version list...")
        try:
            data = json.loads(fetch(f"{BASE_URL}/{self.artifact_id}"))
        except ProviderError:
            # Offline but with a stale cache beats an empty dropdown.
            if os.path.exists(cache):
                with open(cache, "r", encoding="utf-8") as fh:
                    self._meta = json.load(fh)
                return self._meta
            raise
        except ValueError as exc:
            raise ProviderError(f"Malformed response from the Sponge API: {exc}") from None

        try:
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass  # cache is a nicety, not a requirement

        self._meta = data
        return data

    def _mojang_versions(self) -> dict[str, Version]:
        """Mojang's manifest keyed by version id, fetched at most once.

        An empty dict means "tried and failed" so an offline run doesn't retry
        the lookup once per dropdown entry.
        """
        if self._mojang is None:
            try:
                vp = VanillaProvider()
                self._mojang = {
                    v.id: v for v in vp.list_versions(include_unstable=True)
                }
            except Exception:
                self._mojang = {}
        return self._mojang

    def list_versions(
        self, include_unstable: bool = False, progress: Optional[ProgressCB] = None
    ) -> list[Version]:
        """Minecraft versions this Sponge artifact targets, newest first.

        Two axes, as everywhere else: the checkbox admits snapshot and
        pre-release *Minecraft* versions, and splits each version into
        "Recommended" and "Latest" *builds*. Recommended builds are rare here
        (see the header), so with the checkbox off almost every entry resolves
        to the newest release candidate -- which now just installs, instead of
        telling you to enable snapshots. The build channel is resolved at
        install time because it costs a request per version.
        """
        meta = self._load_meta(progress)
        ids = meta.get("tags", {}).get("minecraft", [])
        mojang = self._mojang_versions()

        out: list[Version] = []
        for vid in ids:
            known = mojang.get(vid)
            # Unknown to Mojang -> treat as a snapshot rather than dropping it.
            # Sponge occasionally targets a version before this lookup catches
            # up, and a hidden entry is worse than a conservatively-labelled one.
            out.extend(
                channel_entries(
                    vid,
                    include_unstable,
                    kind=known.kind if known else "snapshot",
                    released=known.released if known else None,
                    ref=vid,
                    resolved=False,
                )
            )

        # Release date first, because Minecraft ids have no sortable grammar
        # across schemes (1.21.11 / 25w41a / 26.1.2). _mc_key is the tiebreak
        # for anything Mojang didn't give us a date for.
        out.sort(key=lambda v: (v.released or "", _mc_key(v.id)), reverse=True)
        return out

    def latest_release(self) -> Optional[str]:
        versions = self.list_versions(include_unstable=False)
        return versions[0].id if versions else None

    # ---------------- resolve ----------------

    def _versions_page(
        self, mc_version: str, recommended: bool, progress: Optional[ProgressCB] = None
    ) -> dict:
        key = (mc_version, recommended)
        if key in self._builds_cache:
            return self._builds_cache[key]

        if progress:
            progress(
                STAGE_RESOLVE, 0, None,
                f"Fetching {self.name} builds for Minecraft {mc_version}...",
            )
        query = {"tags": f"minecraft:{mc_version}", "limit": str(PAGE_LIMIT), "offset": "0"}
        if recommended:
            query["recommended"] = "true"
        url = f"{BASE_URL}/{self.artifact_id}/versions?{urllib.parse.urlencode(query)}"
        try:
            data = json.loads(fetch(url))
        except ValueError as exc:
            raise ProviderError(f"Malformed response from {url}: {exc}") from None

        artifacts = data.get("artifacts") or {}
        self._builds_cache[key] = artifacts
        return artifacts

    def builds_for(self, mc_version: str) -> list[str]:
        """Newest builds for a Minecraft version, newest first.

        Capped at one page -- 1.21.1 alone has 285 builds and no UI here wants
        them all. Exposed for a future second dropdown, same as Forge's.
        """
        return list(self._versions_page(mc_version, recommended=False))

    def resolve_build(
        self,
        mc_version: str,
        progress: Optional[ProgressCB] = None,
        channel: Optional[str] = None,
    ) -> tuple[str, dict]:
        """-> (build id, its entry) for the channel a dropdown entry asked for.

        channel None      -- recommended if one exists, else the newest release
                             candidate.
        CHANNEL_LATEST    -- newest build, recommended or not.
        CHANNEL_RECOMMENDED -- recommended only, and an error if there is none.
        """
        def newest(recommended: bool) -> tuple[Optional[str], dict]:
            page = self._versions_page(mc_version, recommended, progress=progress)
            if not page:
                return None, {}
            build = next(iter(page))  # the API returns newest-first
            return build, page[build]

        if channel != CHANNEL_LATEST:
            build, entry = newest(recommended=True)
            if build:
                return build, entry
            if channel == CHANNEL_RECOMMENDED:
                raise ProviderError(
                    f"{self.name} has no recommended build for Minecraft "
                    f"{mc_version} -- every build so far is a release candidate. "
                    'Pick the "Latest" entry for this version instead.'
                )

        build, entry = newest(recommended=False)
        if not build:
            raise ProviderError(
                f"{self.name} has no builds at all for Minecraft {mc_version}."
            )
        return build, entry

    def _universal_asset(self, build: str, progress: Optional[ProgressCB] = None) -> dict:
        """The one asset out of a dozen that is actually the jar we want."""
        if progress:
            progress(STAGE_RESOLVE, 0, None, f"Resolving {self.name} {build}...")
        url = f"{BASE_URL}/{self.artifact_id}/versions/{urllib.parse.quote(build)}"
        try:
            data = json.loads(fetch(url))
        except ValueError as exc:
            raise ProviderError(f"Malformed response from {url}: {exc}") from None

        for asset in data.get("assets", []):
            if (
                asset.get("classifier") == UNIVERSAL_CLASSIFIER
                and asset.get("extension") == "jar"
                and asset.get("downloadUrl")
            ):
                return asset
        raise ProviderError(
            f"{self.name} build {build} publishes no '{UNIVERSAL_CLASSIFIER}' jar. "
            "Nothing was installed."
        )

    def required_java(self, mc_version: str) -> Optional[int]:
        """Sponge states no Java requirement; the Minecraft version's applies."""
        known = self._mojang_versions().get(mc_version)
        if known is None:
            return None
        try:
            return VanillaProvider().required_java(known)
        except Exception:
            return None

    # ---------------- download ----------------

    @staticmethod
    def _download_asset(
        asset: dict,
        dest_path: str,
        label: str,
        progress: Optional[ProgressCB],
        cancel: Optional[threading.Event],
    ) -> list[str]:
        """Stream to a sibling temp file, verify SHA-1, atomically rename.

        -> notes about what could not be verified. Every Sponge asset carries a
        sha1 today, so the unverified branch should be dead code; it exists
        because silently skipping verification is the failure mode that matters.
        """
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        expected = (asset.get("sha1") or "").strip().lower() or None

        fd, tmp_path = tempfile.mkstemp(prefix=".sponge-", suffix=".part",
                                        dir=os.path.dirname(dest_path))
        os.close(fd)
        digest = hashlib.sha1()
        try:
            req = urllib.request.Request(asset["downloadUrl"], headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp_path, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0) or None
                got = 0
                if progress:
                    progress(STAGE_DOWNLOAD, 0, total, f"Downloading {label}...")
                while True:
                    check_cancelled(cancel)
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
                    if progress:
                        progress(
                            STAGE_DOWNLOAD, got, total,
                            f"Downloading {label}... ({got:,} bytes)",
                        )

            notes: list[str] = []
            if expected:
                if progress:
                    progress(STAGE_VERIFY, 0, None, "Verifying checksum...")
                if digest.hexdigest() != expected:
                    raise ProviderError(
                        f"SHA-1 mismatch on {label} -- the file is corrupt or was "
                        "tampered with. Nothing was installed."
                    )
            else:
                notes.append(
                    f"No checksum was published for {label}; it could not be verified."
                )
            if not got:
                raise ProviderError(f"{label} downloaded as an empty file.")

            os.replace(tmp_path, dest_path)
            tmp_path = None  # ownership transferred
            return notes
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _asset_filename(asset: dict, fallback: str) -> str:
        name = posixpath.basename(urllib.parse.urlsplit(asset["downloadUrl"]).path)
        return name or fallback

    @staticmethod
    def _write_eula(dest_dir: str, accept_eula: bool, notes: list[str]) -> None:
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


class SpongeVanillaProvider(_SpongeBase):
    """Sponge's own server. No Forge, no NeoForge, nothing underneath."""

    name = "SpongeVanilla"
    artifact_id = "spongevanilla"
    platform_tag = None

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

        mc = version.ref or version.id
        report(STAGE_RESOLVE, 0, None, f"Resolving SpongeVanilla for Minecraft {mc}...")

        build, entry = self.resolve_build(mc, progress, version.channel)
        asset = self._universal_asset(build, progress)
        java_major = self.required_java(mc)

        os.makedirs(dest_dir, exist_ok=True)
        jar_name = self._asset_filename(asset, f"spongevanilla-{build}-universal.jar")
        jar_path = os.path.join(dest_dir, jar_name)

        notes: list[str] = [
            f"SpongeVanilla {build}"
            + (" (recommended)" if entry.get("recommended") else " (release candidate)")
            + f", SpongeAPI {entry.get('tagValues', {}).get('api', '?')}."
        ]
        notes += self._download_asset(asset, jar_path, jar_name, progress, cancel)

        report(STAGE_FINALIZE, 0, None, "Writing server files...")
        os.makedirs(os.path.join(dest_dir, "plugins"), exist_ok=True)
        self._write_eula(dest_dir, accept_eula, notes)

        notes.append(
            "First launch downloads Minecraft and Sponge's libraries and needs an "
            "internet connection. Later launches do not."
        )
        notes.append("Drop Sponge plugins into the plugins/ folder.")
        if java_major:
            notes.append(f"This version requires Java {java_major}.")

        launch = [
            "{java}", "-Xms{min_ram}", "-Xmx{max_ram}",
            "-jar", "{jar}", "nogui",
        ]
        _write_jar_start_scripts(dest_dir, jar_name, java_major)

        return InstallResult(
            server_dir=dest_dir,
            jar_path=jar_path,
            launch_argv=launch,
            java_major=java_major,
            notes=notes,
        )


class _SpongeModBase(_SpongeBase):
    """Shared by SpongeForge and SpongeNeo: a mod jar on top of a base server.

    The base provider does all the heavy lifting -- installer download, running
    `--installServer`, layout detection, start scripts. This class only pins the
    build, then drops one jar into mods/.
    """

    base_factory = ForgeProvider    # overridden below
    base_label = "Forge"

    def _base(self):
        return self.base_factory()

    def check_prerequisites(self, version: Version) -> list[str]:
        """Delegated to the base provider -- its installer is what needs a JVM.

        The GUI calls this before starting, which is the whole point: a missing
        or too-old JDK should be reported now, not several minutes into a
        `--installServer` run.
        """
        mc = version.ref or version.id
        return self._base().check_prerequisites(Version(id=mc, ref=mc))

    def _pinned_build(self, entry: dict, build: str) -> str:
        tags = entry.get("tagValues") or {}
        pinned = tags.get(self.platform_tag)
        if not pinned:
            raise ProviderError(
                f"{self.name} build {build} does not say which {self.base_label} "
                f"build it needs (no '{self.platform_tag}' tag), so the base "
                "server could not be installed."
            )
        return pinned

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

        mc = version.ref or version.id
        report(STAGE_RESOLVE, 0, None, f"Resolving {self.name} for Minecraft {mc}...")

        build, entry = self.resolve_build(mc, progress, version.channel)
        pinned = self._pinned_build(entry, build)
        asset = self._universal_asset(build, progress)

        # Resolve the base build before anything is written, so a Sponge release
        # pinned to a base build that has vanished from maven fails cleanly
        # instead of leaving a half-built server directory behind.
        base = self._base()
        artifact = base.find_build(mc, pinned)
        check_cancelled(cancel)

        report(
            STAGE_RESOLVE, 0, None,
            f"Installing {self.base_label} {artifact} for {self.name} {build}...",
        )
        result = base.install_build(
            artifact,
            mc,
            dest_dir,
            progress=progress,
            accept_eula=accept_eula,
            cancel=cancel,
            extra_notes=[
                f"{self.name} {build}"
                + (" (recommended)" if entry.get("recommended") else " (release candidate)")
                + f", SpongeAPI {entry.get('tagValues', {}).get('api', '?')}.",
                f"{self.base_label} {artifact} was pinned by that Sponge build -- "
                f"Sponge patches {self.base_label} internals, so the build number "
                "has to match exactly.",
            ],
        )
        check_cancelled(cancel)

        # The Sponge jar is a mod, so it goes in mods/ -- which install_build
        # has already created.
        jar_name = self._asset_filename(asset, f"{self.artifact_id}-{build}-universal.jar")
        mod_path = os.path.join(dest_dir, "mods", jar_name)
        notes = list(result.notes)
        notes += self._download_asset(asset, mod_path, jar_name, progress, cancel)

        report(STAGE_FINALIZE, 0, None, "Writing server files...")
        os.makedirs(os.path.join(dest_dir, "plugins"), exist_ok=True)
        notes.append(
            "Sponge plugins go in plugins/, not mods/ -- mods/ holds the Sponge "
            f"jar itself and any ordinary {self.base_label} mods."
        )

        return InstallResult(
            server_dir=dest_dir,
            jar_path=result.jar_path,
            launch_argv=result.launch_argv,
            java_major=result.java_major,
            notes=notes,
        )


class SpongeForgeProvider(_SpongeModBase):
    name = "SpongeForge"
    artifact_id = "spongeforge"
    platform_tag = "forge"
    base_factory = ForgeProvider
    base_label = "Forge"


class SpongeNeoProvider(_SpongeModBase):
    name = "SpongeNeo"
    artifact_id = "spongeneo"
    platform_tag = "neoforge"
    base_factory = NeoForgeProvider
    base_label = "NeoForge"


def _write_jar_start_scripts(
    dest_dir: str, jar_name: str, java_major: Optional[int]
) -> None:
    """Plain `-jar` start scripts, for SpongeVanilla only.

    The Forge/Neo variants never reach this -- their scripts come from the base
    provider, which knows about argfiles.
    """
    hint = f"REM Requires Java {java_major}\n" if java_major else ""
    bat = os.path.join(dest_dir, "start.bat")
    with open(bat, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(f'@echo off\n{hint}java -Xms1G -Xmx4G -jar "{jar_name}" nogui\npause\n')

    sh_hint = f"# Requires Java {java_major}\n" if java_major else ""
    sh = os.path.join(dest_dir, "start.sh")
    with open(sh, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            f'#!/bin/sh\n{sh_hint}cd "$(dirname "$0")"\n'
            f'exec java -Xms1G -Xmx4G -jar "{jar_name}" nogui\n'
        )
    try:
        os.chmod(sh, os.stat(sh).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _cli() -> None:
    for cls in (SpongeVanillaProvider, SpongeForgeProvider, SpongeNeoProvider):
        p = cls()
        versions = p.list_versions(include_unstable=False)
        print(f"\n{p.name}: {len(versions)} releases, latest = {p.latest_release()}")
        for v in versions[:8]:
            print("  ", v)
        if versions:
            build, entry = p.resolve_build(versions[0].id)
            tags = entry.get("tagValues", {})
            extra = f", {p.platform_tag} {tags.get(p.platform_tag)}" if p.platform_tag else ""
            print(f"   -> {build} (recommended={entry.get('recommended')}"
                  f", api {tags.get('api')}{extra})")


if __name__ == "__main__":
    _cli()
