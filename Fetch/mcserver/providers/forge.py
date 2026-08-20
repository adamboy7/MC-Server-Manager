"""Minecraft Forge provider.

Standalone: `python -m mcserver.providers.forge` prints the version list.

Forge sits between vanilla and Spigot in awkwardness. It does not compile from
source (so it is far faster than BuildTools), but it also does not ship a
ready-to-run server jar. What you download is an *installer*, which must be run
locally with `--installServer`; it then pulls the vanilla server jar plus a few
hundred megabytes of libraries and lays out the server directory itself.

Two hops for the index:
  1. maven-metadata.json     -> {mc_version: [every forge artifact version]}
  2. promotions_slim.json    -> {"1.20.1-recommended": "47.4.10", ...}

The promos file gives bare Forge versions; maven-metadata gives full artifact
coordinates, which for older releases carry a branch suffix
(`1.7.10-10.13.4.1614-1.7.10`). So the promo is resolved *through* the metadata
list rather than string-formatted directly, or every pre-1.8 install would 404.

Forge is the reason `Version.channel` exists. A Minecraft release can go weeks
with builds but no *recommended* promotion, and the dropdown used to hide those
versions behind the snapshots checkbox -- which then also poured every Minecraft
snapshot into the list, so getting at an early mod loader for a plain release
meant wading through dev builds. Recommended-vs-latest and release-vs-snapshot
are now separate axes: the checkbox only controls the Minecraft side, and an
unpromoted version stays listed, marked `(latest)`. Tick the box and each
version splits into "Recommended" and "Latest" entries.

Two launch shapes come out the other end:
  * 1.16.5 and older -- a runnable `forge-<ver>.jar` in the server directory.
  * 1.17 and newer   -- no runnable jar at all. Forge writes per-platform
    argfiles under libraries/net/minecraftforge/forge/<ver>/ and the server is
    started with `java @that_file nogui`. This is the reason
    `InstallResult.launch_argv` is a list instead of a jar path.

Everything from "download an installer" onwards is identical for NeoForge,
whose installer is a fork of this one, so the coordinates and the product name
live in class attributes (`maven_base`, `maven_group_path`, `cache_subdir`,
`installer_name_fmt`, `legacy_jar_globs`, `name`) rather than in the method
bodies. neoforge.py subclasses this and only replaces the index layer.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Sequence

from . import javafind
from .base import (
    CHANNEL_LATEST,
    CHANNEL_RECOMMENDED,
    STAGE_BUILD,
    STAGE_DOWNLOAD,
    STAGE_FINALIZE,
    STAGE_INDEX,
    STAGE_RESOLVE,
    STAGE_VERIFY,
    USER_AGENT,
    Cancelled,
    InstallResult,
    ProgressCB,
    ProviderError,
    Version,
    channel_entries,
    check_cancelled,
    fetch,
)
from .vanilla import VanillaProvider

INDEX_URL = "https://files.minecraftforge.net/net/minecraftforge/forge/maven-metadata.json"
PROMOS_URL = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
MAVEN_BASE = "https://maven.minecraftforge.net/net/minecraftforge/forge"

CACHE_TTL = 60 * 60 * 6
INSTALLER_MAX_AGE = 60 * 60 * 24 * 30  # installers are immutable; keep a month

# Forge dropped runnable jars here. Everything at or above needs the argfile form.
ARGFILE_ERA = (1, 17)

_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

# The installer is quiet for long stretches while it downloads libraries, so the
# GUI runs an indeterminate bar and we just surface whatever stage it announced.
_STAGE_HINTS = (
    (re.compile(r"Considering|Target Directory", re.I), "Preparing installer..."),
    (re.compile(r"Extracting|Extracted", re.I), "Extracting Forge files..."),
    (re.compile(r"Downloading|Considering library", re.I), "Downloading libraries..."),
    (re.compile(r"MINECRAFT_SERVER|Downloading minecraft server", re.I), "Downloading vanilla server jar..."),
    (re.compile(r"Building Processors|Executing: |Task ", re.I), "Patching and remapping..."),
    (re.compile(r"The server installed successfully", re.I), "Install finished."),
)


def _cache_dir(subdir: str = "forge") -> str:
    """Per-product cache directory. Subclasses pass their own `cache_subdir` so
    NeoForge's installers and index never collide with Forge's."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "mc-server-manager", subdir)
    os.makedirs(d, exist_ok=True)
    return d


def _mc_key(vid: str) -> tuple:
    """Sortable key for a Minecraft version id.

    Purely numeric, so both the old `1.20.1` scheme and the newer `26.1.2` one
    order correctly against each other without special-casing either.
    """
    nums = [int(n) for n in re.findall(r"\d+", vid)]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def _needs_argfile(mc_version: str) -> bool:
    return _mc_key(mc_version)[:2] >= ARGFILE_ERA


class ForgeProvider:
    name = "Forge"
    content_dir = "mods"
    compiles_from_source = False  # downloads, but the installer still runs locally

    # Retargetable coordinates. A subclass that publishes a Forge-shaped
    # installer under different maven coordinates only needs these six.
    cache_subdir = "forge"
    maven_base = MAVEN_BASE
    maven_group_path = ("net", "minecraftforge", "forge")
    installer_name_fmt = "forge-{artifact}-installer.jar"
    # Pre-1.17 shipped a runnable jar under one of these names; a product that
    # never did (NeoForge) sets this empty.
    legacy_jar_globs = ("forge-*.jar", "minecraftforge-universal-*.jar")

    def __init__(self) -> None:
        self._index: Optional[dict[str, list[str]]] = None
        self._promos: Optional[dict[str, str]] = None
        self._vanilla = VanillaProvider()
        # None means "not looked up yet"; an empty dict means "looked it up and
        # Mojang was unreachable", so the fallback fires once per process
        # rather than once per version.
        self._mojang: Optional[dict[str, Version]] = None

    # ---------------- index ----------------

    def _cache_dir(self) -> str:
        return _cache_dir(self.cache_subdir)

    def _args_dir(self, dest_dir: str) -> str:
        """Where the installer drops its per-platform argfiles, one directory
        per build."""
        return os.path.join(dest_dir, "libraries", *self.maven_group_path)

    def _cached_json(
        self,
        url: str,
        filename: str,
        progress: Optional[ProgressCB],
        message: str,
    ) -> dict:
        """Fetch JSON with an on-disk cache and a stale-beats-nothing fallback."""
        cache = os.path.join(self._cache_dir(), filename)
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < CACHE_TTL:
            try:
                with open(cache, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass  # corrupt cache -> refetch

        if progress:
            progress(STAGE_INDEX, 0, None, message)

        try:
            data = json.loads(fetch(url))
        except ValueError as exc:
            raise ProviderError(f"Malformed response from {url}: {exc}") from None
        except ProviderError:
            if os.path.exists(cache):
                with open(cache, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            raise

        try:
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass
        return data

    def _load_index(self, progress: Optional[ProgressCB] = None) -> dict[str, list[str]]:
        if self._index is None:
            data = self._cached_json(
                INDEX_URL, "forge_index.json", progress, "Fetching Forge version index..."
            )
            self._index = {k: list(v) for k, v in data.items() if isinstance(v, list) and v}
        return self._index

    def _load_promos(self, progress: Optional[ProgressCB] = None) -> dict[str, str]:
        if self._promos is None:
            data = self._cached_json(
                PROMOS_URL, "forge_promos.json", progress, "Fetching Forge promotions..."
            )
            promos = data.get("promos", data)
            self._promos = {str(k): str(v) for k, v in promos.items()}
        return self._promos

    def _mojang_versions(self) -> dict[str, Version]:
        """Mojang's manifest keyed by version id, fetched at most once.

        Forge's index says nothing about whether `1.21.11` is a release or
        `1.21.11-pre1` a pre-release, and that distinction is now what the
        pre-release checkbox filters on, so it has to come from Mojang.
        """
        if self._mojang is None:
            try:
                self._mojang = {
                    v.id: v for v in self._vanilla.list_versions(include_unstable=True)
                }
            except Exception:
                self._mojang = {}  # offline; _mc_kind falls back
        return self._mojang

    def _mc_kind(self, mc: str) -> str:
        """-> "release" / "snapshot" / ... for a Minecraft id.

        An id Mojang doesn't know about counts as a release. Hiding a version
        the loader clearly supports because a lookup lagged (or the manifest
        fetch failed) is the worse failure of the two.
        """
        known = self._mojang_versions().get(mc)
        return known.kind if known else "release"

    def _promos_safe(self) -> dict[str, str]:
        """Promotions, or {} when they can't be fetched.

        The index alone is still usable -- every version just becomes
        latest-only, which is now a first-class state rather than a reason to
        hide the version.
        """
        try:
            return self._load_promos()
        except ProviderError:
            return {}

    def _match_promo(self, mc: str, bare: Optional[str]) -> Optional[str]:
        """Resolve a bare promo version against the metadata listing.

        Promos hold '47.4.10', but the artifact may carry a trailing branch
        ('1.7.10-10.13.4.1614-1.7.10'), so the promo is matched rather than
        formatted into a coordinate directly.
        """
        if not bare:
            return None
        prefix = f"{mc}-{bare}"
        for artifact in self._load_index().get(mc) or ():
            if artifact == prefix or artifact.startswith(prefix + "-"):
                return artifact
        # Promoted build missing from the metadata listing -- shouldn't happen,
        # but a mirror that lags by a build shouldn't take the entry down.
        return None

    def recommended_build(self, mc: str) -> Optional[str]:
        """-> the promoted artifact for a Minecraft version, or None."""
        return self._match_promo(mc, self._promos_safe().get(f"{mc}-recommended"))

    def latest_build(self, mc: str) -> Optional[str]:
        """-> the newest artifact for a Minecraft version, or None.

        Forge promotes a `-latest` too; it is preferred over the tail of the
        metadata list because the two disagree occasionally and the promotion
        is the maintainers' word.
        """
        promoted = self._match_promo(mc, self._promos_safe().get(f"{mc}-latest"))
        if promoted:
            return promoted
        builds = self._load_index().get(mc)
        return builds[-1] if builds else None

    def list_versions(
        self, include_unstable: bool = False, progress: Optional[ProgressCB] = None
    ) -> list[Version]:
        """Minecraft versions Forge supports, newest first.

        The dropdown lists Minecraft versions, not Forge builds -- picking a
        specific Forge build by hand is a power-user concern and the GUI has one
        combo. What the checkbox controls is two separate things that used to be
        one:

          off -- Minecraft releases only, one entry each, the recommended Forge
                 build where there is one and the latest build (marked
                 `(latest)`) where there isn't. A Minecraft release Forge has
                 not promoted a build for is still installable from here; that
                 is the whole point of the split.
          on  -- snapshots and pre-releases join the list, and each version
                 splits into "Recommended" and "Latest" entries so an early mod
                 loader build can be picked deliberately. Versions where both
                 resolve to the same artifact stay a single entry.
        """
        index = self._load_index(progress)
        try:
            self._load_promos(progress)
        except ProviderError:
            pass  # _promos_safe() below degrades to latest-only

        out: list[Version] = []
        for mc in index:
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
        # Sort by Minecraft version, keeping each version's Recommended entry
        # ahead of its Latest one (channel_entries emits them in that order and
        # the sort is stable).
        out.sort(key=lambda v: _mc_key(v.id), reverse=True)
        return out

    # ---------------- resolve ----------------

    def builds_for(self, mc_version: str) -> list[str]:
        """Every Forge artifact version for a Minecraft version, oldest first."""
        builds = self._load_index().get(mc_version)
        if not builds:
            raise ProviderError(f"{self.name} has no builds for Minecraft {mc_version}.")
        return builds

    def resolve_build(self, version: Version, allow_unpromoted: bool = True) -> str:
        """-> full artifact version, e.g. '1.20.1-47.4.10'.

        The dropdown entry already carries the answer when it was listed
        (`Version.build`); this recomputes it for entries built by hand, and
        honours `Version.channel` so a "Latest" entry never quietly installs
        the recommended build or the other way round.
        """
        mc = version.ref or version.id
        builds = self.builds_for(mc)  # raises if the version is unknown

        if version.build:
            return version.build

        if version.channel == CHANNEL_LATEST:
            return self.latest_build(mc) or builds[-1]

        recommended = self.recommended_build(mc)
        if recommended:
            return recommended
        if version.channel == CHANNEL_RECOMMENDED:
            raise ProviderError(
                f"{self.name} has no recommended build for Minecraft {mc} -- the "
                'promotion was withdrawn since this list was loaded. Pick the '
                '"Latest" entry for this version instead.'
            )
        if not allow_unpromoted:
            raise ProviderError(
                f"{self.name} has no recommended build for Minecraft {mc} yet."
            )
        return self.latest_build(mc) or builds[-1]

    def find_build(self, mc_version: str, bare_version: str) -> str:
        """-> full artifact version for a bare build number.

        ('1.21.10', '60.0.1') -> '1.21.10-60.0.1'. Matched through builds_for()
        rather than formatted into a URL for the same reason resolve_build()
        does it that way: older artifacts carry a trailing branch suffix, so the
        string a human pins is a *prefix* of the real coordinate, not the whole
        of it.
        """
        builds = self.builds_for(mc_version)
        prefix = f"{mc_version}-{bare_version}"
        for artifact in builds:
            if artifact == prefix or artifact.startswith(prefix + "-"):
                return artifact
        # Also accept a build given as its full artifact coordinate already.
        if bare_version in builds:
            return bare_version
        raise ProviderError(
            f"{self.name} {bare_version} is not a published build for Minecraft "
            f"{mc_version}."
        )

    def installer_url(self, artifact: str) -> str:
        name = self.installer_name_fmt.format(artifact=artifact)
        return f"{self.maven_base}/{artifact}/{name}"

    def required_java(self, version: Version) -> Optional[int]:
        """Forge publishes no machine-readable Java requirement.

        Mojang does, for the same Minecraft version, so reuse that; everything
        before 1.17 predates the field and is Java 8 territory.
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
        return None if _needs_argfile(mc) else 8

    # ---------------- prerequisites ----------------

    def check_prerequisites(self, version: Version) -> list[str]:
        """Human-readable blockers. Empty list means good to go.

        The installer is itself a Java program, and it runs the same processors
        the client does -- launching it under a too-old JVM fails with an
        UnsupportedClassVersionError several minutes in.
        """
        problems: list[str] = []

        need = self.required_java(version)
        jdks = javafind.find_jdks()
        if not jdks:
            problems.append(
                f"No Java installation was found. The {self.name} installer is a Java "
                "program and the server needs a JVM to run"
                + (f" (Java {need} for this version)." if need else ".")
                + "\n\nInstall a JDK from adoptium.net and try again."
            )
        elif need and not any(j.major >= need for j in jdks):
            have = ", ".join(sorted({str(j.major) for j in jdks}, key=int))
            problems.append(
                f"Minecraft {version.id} needs Java {need} or newer, but the only "
                f"Java versions found are: {have}.\n\n"
                "Install a matching JDK (adoptium.net) and try again."
            )

        try:
            free = shutil.disk_usage(os.path.dirname(os.path.abspath(self._cache_dir()))).free
            if free < 1024**3:
                problems.append(
                    f"Only {free / 1024**3:.1f} GB free. A {self.name} server directory "
                    "runs to roughly 500 MB once libraries are downloaded."
                )
        except OSError:
            pass

        return problems

    def _pick_java(self, version: Version) -> javafind.Jdk:
        need = self.required_java(version)
        jdks = javafind.find_jdks()
        if not jdks:
            raise ProviderError(
                "No Java installation was found. Install a JDK from adoptium.net."
            )
        if need:
            fits = [j for j in jdks if j.major >= need]
            if not fits:
                raise ProviderError(javafind.describe_missing(need, need, jdks))
            # Oldest JVM that still satisfies the requirement. The installer's
            # processors have historically broken on JVMs much newer than the
            # target.
            fits.sort(key=lambda j: (j.major, not j.has_javac))
            return fits[0]
        return jdks[0]

    # ---------------- installer download ----------------

    def _ensure_installer(
        self,
        artifact: str,
        progress: Optional[ProgressCB],
        cancel: Optional[threading.Event],
    ) -> str:
        path = os.path.join(
            self._cache_dir(), self.installer_name_fmt.format(artifact=artifact)
        )
        stamp = path + ".verified"
        if os.path.exists(path) and os.path.getsize(path) > 0:
            if time.time() - os.path.getmtime(path) < INSTALLER_MAX_AGE:
                # The sidecar records that *this* copy was checksum-verified when
                # it was downloaded, so a cache hit doesn't silently downgrade
                # the note we show the user.
                self._verified_checksum = os.path.exists(stamp)
                return path

        url = self.installer_url(artifact)

        # Maven serves a sibling .sha1 for every artifact. It's advisory here --
        # if the mirror doesn't have it, the install still proceeds, just
        # unverified, and says so in the notes.
        expected: Optional[str] = None
        try:
            expected = fetch(url + ".sha1").decode("ascii", "ignore").strip().split()[0].lower()
            if not re.fullmatch(r"[0-9a-f]{40}", expected):
                expected = None
        except (ProviderError, IndexError, UnicodeDecodeError):
            expected = None

        tmp = path + ".part"
        digest = hashlib.sha1()
        got = 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0) or None
                if progress:
                    progress(STAGE_DOWNLOAD, 0, total, f"Downloading {self.name} installer...")
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
                            f"Downloading {self.name} installer... ({got:,} bytes)",
                        )
        except urllib.error.HTTPError as exc:
            self._cleanup(tmp)
            if exc.code == 404:
                # Name the host we actually asked, so a subclass on a different
                # maven doesn't blame minecraftforge.net for its own 404.
                host = urllib.parse.urlsplit(self.maven_base).netloc
                raise ProviderError(
                    f"{self.name} {artifact} has no installer on {host} "
                    f"(404).\n\n{url}"
                ) from None
            raise ProviderError(f"Could not download the {self.name} installer: {exc}") from None
        except (OSError, urllib.error.URLError) as exc:
            self._cleanup(tmp)
            raise ProviderError(f"Could not download the {self.name} installer: {exc}") from None
        except BaseException:
            self._cleanup(tmp)
            raise

        if expected:
            if progress:
                progress(STAGE_VERIFY, 0, None, "Verifying installer checksum...")
            if digest.hexdigest() != expected:
                self._cleanup(tmp)
                raise ProviderError(
                    f"SHA-1 mismatch on the {self.name} installer -- the download is "
                    "corrupt or was tampered with. Nothing was installed."
                )
        self._verified_checksum = bool(expected)

        os.replace(tmp, path)
        if expected:
            try:
                with open(stamp, "w", encoding="utf-8") as fh:
                    fh.write(expected + "\n")
            except OSError:
                pass
        else:
            self._cleanup(stamp)
        return path

    @staticmethod
    def _cleanup(path: str) -> None:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    # ---------------- running the installer ----------------

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """Kill the installer and anything it spawned.

        The installer's processors run as child JVMs; a plain kill() orphans
        them and they keep writing into the half-built server directory.
        """
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=20, **_NO_WINDOW,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass

    def _run_installer(
        self,
        installer: str,
        java_exe: str,
        artifact: str,
        dest_dir: str,
        progress: Optional[ProgressCB],
        cancel: Optional[threading.Event],
    ) -> str:
        log_path = os.path.join(self._cache_dir(), f"install-{artifact}.log")
        cmd = [java_exe, "-jar", installer, "--installServer", dest_dir]

        popen_kw = dict(_NO_WINDOW)
        if sys.platform != "win32":
            popen_kw["start_new_session"] = True  # own process group, so the tree is killable

        started = time.time()
        if progress:
            progress(STAGE_BUILD, 0, None, f"Running the {self.name} installer...")

        try:
            proc = subprocess.Popen(
                cmd, cwd=dest_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace", **popen_kw,
            )
        except OSError as exc:
            raise ProviderError(f"Could not start the {self.name} installer: {exc}") from None

        stage = "Installing..."
        try:
            with open(log_path, "w", encoding="utf-8") as log:
                assert proc.stdout is not None
                for line in proc.stdout:
                    log.write(line)

                    if cancel is not None and cancel.is_set():
                        self._terminate(proc)
                        raise Cancelled("Install cancelled.")

                    for pattern, label in _STAGE_HINTS:
                        if pattern.search(line):
                            stage = label
                            break

                    if progress:
                        mins, secs = divmod(int(time.time() - started), 60)
                        progress(STAGE_BUILD, 0, None, f"{stage}  [{mins}m {secs:02d}s]")
        finally:
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self._terminate(proc)

        if proc.returncode != 0:
            raise ProviderError(
                f"The {self.name} installer exited with code {proc.returncode}.\n\n"
                f"Full log:\n{log_path}\n\n"
                f"Common causes: the JVM is too old or too new for this {self.name} "
                "version, no network access to Mojang's library servers, or the "
                "target folder is not writable."
            )
        return log_path

    # ---------------- install ----------------

    def install(
        self,
        version: Version,
        dest_dir: str,
        progress: Optional[ProgressCB] = None,
        accept_eula: bool = False,
        cancel: Optional[threading.Event] = None,
    ) -> InstallResult:
        mc = version.ref or version.id
        if progress:
            progress(STAGE_RESOLVE, 0, None, f"Resolving {self.name} for Minecraft {mc}...")

        artifact = self.resolve_build(version)
        return self.install_build(
            artifact, mc, dest_dir,
            progress=progress, accept_eula=accept_eula, cancel=cancel,
            extra_notes=[self._channel_note(mc, artifact)],
        )

    def _channel_note(self, mc: str, artifact: str) -> str:
        """Which of the two builds you actually got.

        With Recommended and Latest as separate dropdown entries, that stops
        being obvious from the Minecraft version alone.
        """
        if artifact == self.recommended_build(mc):
            return f"{artifact} is the recommended build for Minecraft {mc}."
        return (
            f"{artifact} is the latest build for Minecraft {mc}; {self.name} has "
            "not promoted a recommended build for it."
        )

    def install_build(
        self,
        artifact: str,
        mc_version: str,
        dest_dir: str,
        progress: Optional[ProgressCB] = None,
        accept_eula: bool = False,
        cancel: Optional[threading.Event] = None,
        extra_notes: Optional[Sequence[str]] = None,
    ) -> InstallResult:
        """Everything install() does, with the build already chosen.

        Split out for providers that pin an exact build rather than letting us
        resolve one (Sponge ships against a specific Forge coordinate), and for
        those the STAGE_RESOLVE announcement belongs to the caller -- there is
        nothing left to resolve by the time we get here. `extra_notes` land at
        the end of InstallResult.notes, after ours.
        """
        def report(stage: str, done: int, total: Optional[int], msg: str) -> None:
            if progress:
                progress(stage, done, total, msg)

        mc = mc_version
        # required_java()/_pick_java() speak Version; ref==id is what install()
        # would have passed for a dropdown entry anyway.
        version = Version(id=mc, ref=mc)

        java_major = self.required_java(version)
        jdk = self._pick_java(version)
        check_cancelled(cancel)

        self._verified_checksum = False
        installer = self._ensure_installer(artifact, progress, cancel)
        check_cancelled(cancel)

        os.makedirs(dest_dir, exist_ok=True)
        self._run_installer(installer, jdk.path, artifact, dest_dir, progress, cancel)
        check_cancelled(cancel)

        report(STAGE_FINALIZE, 0, None, "Writing server files...")
        notes: list[str] = [f"{self.name} {artifact} (installer run with Java {jdk.major})."]
        if not getattr(self, "_verified_checksum", False):
            notes.append(
                "No SHA-1 was published alongside the installer, so it could not "
                "be checksum-verified."
            )

        jar_path, launch = self._detect_layout(dest_dir, artifact)
        if launch is None:
            raise ProviderError(
                f"The {self.name} installer reported success but produced neither a "
                f"runnable jar nor an argfile in\n{dest_dir}\n\n"
                f"The directory layout may have changed in this {self.name} version."
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

        os.makedirs(os.path.join(dest_dir, "mods"), exist_ok=True)
        notes.append("Drop mods into the mods/ folder.")
        if jar_path is None:
            notes.append(
                f"{self.name} 1.17+ has no runnable jar -- start.bat/start.sh launch it "
                "through the generated argfile instead."
            )
        if java_major:
            notes.append(f"This version requires Java {java_major}.")
        if extra_notes:
            notes.extend(extra_notes)

        self._write_start_scripts(dest_dir, jar_path, launch, java_major)

        return InstallResult(
            server_dir=dest_dir,
            jar_path=jar_path,
            launch_argv=launch,
            java_major=java_major,
            notes=notes,
        )

    # ---------------- post-install layout ----------------

    def _detect_layout(
        self, dest_dir: str, artifact: str
    ) -> tuple[Optional[str], Optional[list[str]]]:
        """-> (jar_path or None, launch_argv or None).

        Detection is by what is actually on disk rather than by version number,
        because the exact release where the layout flipped is fuzzier than the
        changelog suggests and some builds ship both.
        """
        args_name = "win_args.txt" if sys.platform == "win32" else "unix_args.txt"
        args_dir = self._args_dir(dest_dir)
        candidates = sorted(glob.glob(os.path.join(args_dir, "*", args_name)))
        exact = [c for c in candidates if os.path.basename(os.path.dirname(c)) == artifact]
        argfile = (exact or candidates or [None])[-1]

        if argfile:
            rel = os.path.relpath(argfile, dest_dir).replace(os.sep, "/")
            return None, [
                "{java}", "-Xms{min_ram}", "-Xmx{max_ram}", f"@{rel}", "nogui",
            ]

        # Legacy: a runnable jar sits in the server root. Skip the installer
        # itself and the vanilla jar it downloaded alongside.
        jars: list[str] = []
        for pattern in self.legacy_jar_globs:
            jars += [
                p for p in glob.glob(os.path.join(dest_dir, pattern))
                if "installer" not in os.path.basename(p)
            ]
        if jars:
            jar = sorted(jars)[-1]
            return jar, ["{java}", "-Xms{min_ram}", "-Xmx{max_ram}", "-jar", "{jar}", "nogui"]
        return None, None

    def _write_start_scripts(
        self,
        dest_dir: str,
        jar_path: Optional[str],
        launch: list[str],
        java_major: Optional[int],
    ) -> None:
        """Both platforms' scripts, regardless of which one we're running on.

        The argfile is platform-specific, so each script points at its own -- a
        server directory built on Windows and copied to a Linux box still works.
        """
        args_dir = self._args_dir(dest_dir)

        def argfile_for(name: str) -> Optional[str]:
            found = sorted(glob.glob(os.path.join(args_dir, "*", name)))
            if not found:
                return None
            return os.path.relpath(found[-1], dest_dir).replace(os.sep, "/")

        jar_name = os.path.basename(jar_path) if jar_path else None

        win_arg = argfile_for("win_args.txt")
        if jar_name:
            win_cmd = f'java -Xms1G -Xmx4G -jar "{jar_name}" nogui'
        elif win_arg:
            win_cmd = f'java -Xms1G -Xmx4G @{win_arg.replace("/", chr(92))} nogui'
        else:
            win_cmd = " ".join(launch)  # unreachable in practice; keeps the file honest
        hint = f"REM Requires Java {java_major}\n" if java_major else ""
        with open(os.path.join(dest_dir, "start.bat"), "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(f"@echo off\n{hint}{win_cmd}\npause\n")

        nix_arg = argfile_for("unix_args.txt")
        if jar_name:
            nix_cmd = f'exec java -Xms1G -Xmx4G -jar "{jar_name}" nogui'
        elif nix_arg:
            nix_cmd = f"exec java -Xms1G -Xmx4G @{nix_arg} nogui"
        else:
            nix_cmd = "exec " + " ".join(launch)
        sh_hint = f"# Requires Java {java_major}\n" if java_major else ""
        sh = os.path.join(dest_dir, "start.sh")
        with open(sh, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"#!/bin/sh\n{sh_hint}cd \"$(dirname \"$0\")\"\n{nix_cmd}\n")
        try:
            os.chmod(sh, os.stat(sh).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass


def _cli() -> None:
    p = ForgeProvider()
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
