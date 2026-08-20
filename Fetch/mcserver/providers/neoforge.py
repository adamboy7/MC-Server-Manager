"""NeoForge provider.

Standalone: `python -m mcserver.providers.neoforge` prints the version list.

NeoForge is a fork of Forge, made by most of Forge's former maintainers. The
installer is a fork too -- same `--installServer` flag, same processors, same
post-install layout -- so everything from "download the installer" onwards is
inherited from ForgeProvider unchanged and only the index layer lives here.

The index is one hop and one flat list:

    GET https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge
    -> {"isSnapshot": false, "versions": ["20.2.3-beta", ..., "26.2.0.59-beta"]}

oldest first. There is no promotions file -- NeoForge has no "recommended"
concept at all, so the only stability signal is the version string itself: a
`-beta` suffix means unstable, anything else is a release. That maps onto
Forge's channel split cleanly enough: "newest non-beta" plays the recommended
build, "newest of any" plays latest, and the release/snapshot state of the
Minecraft version itself comes from Mojang's manifest, which this module
already fetches to map versions in the first place.

Versions do not name their Minecraft version outright. The scheme is "the
Minecraft version with the leading `1.` stripped, plus a build number":

    MC 1.20.2  -> 20.2.<build>
    MC 1.21    -> 21.0.<build>      (a zero patch component is inserted)
    MC 1.21.11 -> 21.11.<build>
    MC 26.1.2  -> 26.1.2.<build>    (new-style MC ids keep all three parts)
    MC 26.2    -> 26.2.0.<build>

Reversing that is ambiguous in two directions at once: `26.2.0.59` could mean MC
`26.2.0` or MC `26.2`, and `21.0.167` could mean `1.21.0` or `1.21`. Rather than
guess a rule that will break the next time Mojang changes its numbering, we
generate the candidates in most-likely-first order and keep the first one that
actually exists in Mojang's manifest (via VanillaProvider). Versions whose
Minecraft id resolves to nothing -- the `0.25w14craftmine.*` April Fools builds,
for instance -- are dropped rather than shown as a bogus dropdown entry.

Layout after install is always the argfile form: NeoForge only exists for
Minecraft 1.20.2 and newer, well past the 1.17 cutoff where Forge stopped
shipping a runnable jar, so `legacy_jar_globs` is empty and `launch_argv` is
always `java @libraries/net/neoforged/neoforge/<ver>/unix_args.txt nogui`.
"""

from __future__ import annotations

import re
from typing import Optional

from .base import (
    CHANNEL_LATEST,
    CHANNEL_RECOMMENDED,
    ProgressCB,
    ProviderError,
    Version,
    channel_entries,
)
from .forge import ForgeProvider, _mc_key

VERSIONS_URL = (
    "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
)
MAVEN_BASE = "https://maven.neoforged.net/releases/net/neoforged/neoforge"

BETA_SUFFIX = "-beta"

# NeoForge starts at Minecraft 1.20.2, so Mojang always states a javaVersion for
# these; this only applies when the manifest itself couldn't be fetched.
FALLBACK_JAVA = 21


class NeoForgeProvider(ForgeProvider):
    name = "NeoForge"
    content_dir = "mods"
    compiles_from_source = False

    cache_subdir = "neoforge"
    maven_base = MAVEN_BASE
    maven_group_path = ("net", "neoforged", "neoforge")
    installer_name_fmt = "neoforge-{artifact}-installer.jar"
    legacy_jar_globs = ()  # argfile-only; NeoForge has never shipped a runnable jar

    # ---------------- version -> Minecraft mapping ----------------

    @staticmethod
    def _candidate_mc_ids(version: str) -> list[str]:
        """Minecraft ids `version` could belong to, most likely first.

        Empty when the string isn't build-numbered at all (the April Fools
        `0.25w14craftmine.3-beta` line), which is how those get dropped.
        """
        base = version[: -len(BETA_SUFFIX)] if version.endswith(BETA_SUFFIX) else version
        parts = base.split(".")
        if len(parts) < 2 or not all(re.fullmatch(r"\d+", p) for p in parts):
            return []

        prefix = parts[:-1]  # everything but the build number
        # A trailing zero is the inserted patch component, so try without it
        # first: Mojang publishes "1.21", never "1.21.0".
        trimmed = prefix[:-1] if len(prefix) > 1 and prefix[-1] == "0" else prefix

        out: list[str] = []
        # Old-style ids are "1.x" or "1.x.y", so only a prefix of at most two
        # components can be one -- "26.2.0.59" is never MC "1.26.2".
        if len(prefix) <= 2:
            out += ["1." + ".".join(c) for c in (trimmed, prefix)]
        out += [".".join(c) for c in (trimmed, prefix)]

        seen: set[str] = set()
        return [c for c in out if not (c in seen or seen.add(c))]

    def _known_mc_ids(self) -> set[str]:
        """Every Minecraft id Mojang has ever published, snapshots included.

        Shares ForgeProvider's one-shot manifest lookup, which also supplies the
        release/snapshot kind each dropdown entry is filtered on. Empty means
        the fetch failed, which is what makes the offline fallback in
        _minecraft_for() fire once per process instead of per version.
        """
        return set(self._mojang_versions())

    def _minecraft_for(self, version: str) -> Optional[str]:
        """-> Minecraft id for a NeoForge version, or None if it isn't one."""
        candidates = self._candidate_mc_ids(version)
        if not candidates:
            return None
        known = self._known_mc_ids()
        for candidate in candidates:
            if candidate in known:
                return candidate
        # No manifest (offline) -> trust the most likely candidate, so the
        # dropdown still populates. With a manifest, a version that matches
        # nothing is a curiosity we shouldn't offer to install.
        return candidates[0] if not known else None

    # ---------------- index ----------------

    def _load_index(self, progress: Optional[ProgressCB] = None) -> dict[str, list[str]]:
        """-> {minecraft_id: [neoforge versions, oldest first]}.

        Derived in memory rather than cached as its own file: the raw version
        list already goes through _cached_json, and a second cache file would
        expire independently of both it and Mojang's manifest, so a stale
        mapping could outlive the inputs that produced it.
        """
        if self._index is None:
            data = self._cached_json(
                VERSIONS_URL,
                "neoforge_versions.json",
                progress,
                "Fetching NeoForge version index...",
            )
            index: dict[str, list[str]] = {}
            for version in data.get("versions", []):
                mc = self._minecraft_for(str(version))
                if mc:
                    index.setdefault(mc, []).append(str(version))
            self._index = index
        return self._index

    def list_versions(
        self, include_unstable: bool = False, progress: Optional[ProgressCB] = None
    ) -> list[Version]:
        """Minecraft versions NeoForge supports, newest first.

        Same two-axis split as Forge (see forge.list_versions), with "not a
        beta" standing in for "recommended" since there is no promotions file.
        A version that has only betas is no longer hidden -- it lists as
        `(latest)` with the checkbox off, and splits into Recommended/Latest
        with it on.
        """
        out: list[Version] = []
        for mc in self._load_index(progress):
            out.extend(
                channel_entries(
                    mc,
                    include_unstable,
                    kind=self._mc_kind(mc),
                    ref=mc,
                    recommended=self.recommended_build(mc),
                    latest=self.latest_build(mc),
                )
            )
        out.sort(key=lambda v: _mc_key(v.id), reverse=True)
        return out

    # ---------------- resolve ----------------

    def builds_for(self, mc_version: str) -> list[str]:
        """Every NeoForge version for a Minecraft version, oldest first.

        Unlike Forge these are plain versions, not maven artifact coordinates:
        the Minecraft version is encoded in them rather than prefixed to them.
        """
        builds = self._load_index().get(mc_version)
        if not builds:
            raise ProviderError(f"{self.name} has no builds for Minecraft {mc_version}.")
        return builds

    def recommended_build(self, mc: str) -> Optional[str]:
        """-> newest non-beta build, or None if every build is a beta.

        There is nothing to look up in a promotions file, so "promoted" here
        just means "not a beta".
        """
        builds = self._load_index().get(mc) or ()
        stable = [b for b in builds if not b.endswith(BETA_SUFFIX)]
        return stable[-1] if stable else None

    def latest_build(self, mc: str) -> Optional[str]:
        """-> newest build of any stability, or None."""
        builds = self._load_index().get(mc)
        return builds[-1] if builds else None

    def resolve_build(self, version: Version, allow_unpromoted: bool = True) -> str:
        """-> the NeoForge version to install, e.g. '21.1.72'.

        Overridden only for the error text; the channel logic is Forge's.
        """
        mc = version.ref or version.id
        builds = self.builds_for(mc)

        if version.build:
            return version.build
        if version.channel == CHANNEL_LATEST:
            return builds[-1]

        stable = self.recommended_build(mc)
        if stable:
            return stable
        if version.channel == CHANNEL_RECOMMENDED:
            raise ProviderError(
                f"{self.name} has only beta builds for Minecraft {mc}, so there is "
                'no stable one to install. Pick the "Latest" entry for this '
                "version instead."
            )
        if not allow_unpromoted:
            raise ProviderError(
                f"{self.name} has only beta builds for Minecraft {mc} so far."
            )
        return builds[-1]

    def find_build(self, mc_version: str, bare_version: str) -> str:
        """-> the exact NeoForge version, validated against the index.

        Overridden because Forge's prefix match assumes the artifact starts with
        the Minecraft version; NeoForge versions never do.
        """
        builds = self.builds_for(mc_version)
        if bare_version in builds:
            return bare_version
        raise ProviderError(
            f"{self.name} {bare_version} is not a published build for Minecraft "
            f"{mc_version}."
        )

    def _channel_note(self, mc: str, artifact: str) -> str:
        if artifact == self.recommended_build(mc):
            return f"{artifact} is the newest stable build for Minecraft {mc}."
        return (
            f"{artifact} is a beta build -- {self.name} has published no stable "
            f"build for Minecraft {mc} yet."
        )

    def required_java(self, version: Version) -> Optional[int]:
        """NeoForge publishes no Java requirement either, so reuse Mojang's.

        Nothing older than 1.20.2 exists here, and Mojang has stated javaVersion
        since 1.17, so the field is always there in practice -- the fallback is
        for a failed manifest fetch, and it is 21 rather than Forge's 8 because
        no NeoForge-era Minecraft has ever run on 8.
        """
        mc = version.ref or version.id
        candidate = self._mojang_versions().get(mc)
        if candidate is not None:
            try:
                found = self._vanilla.required_java(candidate)
                if found:
                    return found
            except Exception:
                pass  # best effort; never block an install on this
        return FALLBACK_JAVA

    # _load_promos is inherited but dead: NeoForge publishes no promotions file,
    # and list_versions/resolve_build above are the only two callers Forge had.


def _cli() -> None:
    p = NeoForgeProvider()
    for unstable in (False, True):
        versions = p.list_versions(include_unstable=unstable)
        label = "with pre-releases" if unstable else "releases only"
        print(f"\n{len(versions)} entries ({label})")
        for v in versions[:15]:
            print(f"  {str(v):<32} -> {p.resolve_build(v)}")
    print("\nInstaller URL for the newest:")
    newest = p.list_versions()[0]
    print(" ", p.installer_url(p.resolve_build(newest)))


if __name__ == "__main__":
    _cli()
