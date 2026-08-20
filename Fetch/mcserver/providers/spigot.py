"""Spigot and CraftBukkit providers, via BuildTools.

Unlike every other provider here, these do not download a server jar -- they
compile one. SpigotMC cannot legally redistribute compiled binaries, so
BuildTools exists to build them on the end user's machine from CraftBukkit and
Spigot sources. Third-party sites offering prebuilt spigot jars are of dubious
legality; compiling locally is the compliant path, which is why this module
does it the slow way.

Consequences the other providers do not have:
  * builds take 10-30 minutes on a first run
  * Git must be on PATH (BuildTools shells out to it)
  * the JDK must fall inside a per-version range, or the build fails deep
    inside Maven with a useless error
  * ~1-2 GB of scratch space is needed

The work directory is deliberately persistent and shared between builds -- it
holds the git clones, so a second build of a different version takes a few
minutes instead of twenty.

## Two providers, one module

Spigot *is* CraftBukkit plus a patch set, and a Spigot build compiles
CraftBukkit on the way there regardless -- see the stage hints below, which
watch for exactly that. So the version index, the Java range, the prerequisite
checks and the build machinery are identical between the two, and they differ
only in the `--compile` target and the jar name that lands in the output
directory. Those are class attributes (`build_target`, `artifact_glob`), which
is why `CraftBukkitProvider` at the bottom of this file is a dozen lines rather
than a second copy of everything above it. Same arrangement as sponge.py, where
three providers share one module for the same reason.

Note that since 1.14 BuildTools does not *emit* the CraftBukkit jar unless it
is the requested target, so `--compile craftbukkit` is a real second build
rather than a matter of picking a different file out of one build's output.

Everything written into the shared work directory is keyed by target as well as
by revision. Without that, building CraftBukkit 1.21.8 would delete a
previously built Spigot 1.21.8's staging directory and overwrite the log file
that a Spigot build failure had just told the user to go read.
"""

from __future__ import annotations

import glob
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
from typing import Optional

from . import javafind
from .base import (
    STAGE_BUILD,
    STAGE_DOWNLOAD,
    STAGE_FINALIZE,
    STAGE_INDEX,
    STAGE_RESOLVE,
    USER_AGENT,
    Cancelled,
    InstallResult,
    ProgressCB,
    ProviderError,
    Version,
    check_cancelled,
    fetch,
)

VERSIONS_URL = "https://hub.spigotmc.org/versions/"
BUILDTOOLS_URL = (
    "https://hub.spigotmc.org/jenkins/job/BuildTools/"
    "lastSuccessfulBuild/artifact/target/BuildTools.jar"
)
CACHE_TTL = 60 * 60 * 12
BUILDTOOLS_MAX_AGE = 60 * 60 * 24 * 7  # refresh weekly; it self-updates rarely

# Matches 1.8, 1.16.5, and the odd 1.13-pre7 that shows up in the listing.
_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:-(?:pre|rc)\d+)?$")

# BuildTools' own milestone lines. Maven's output is a firehose, so rather than
# guessing a percentage we surface the current stage and let the GUI run an
# indeterminate bar.
_STAGE_HINTS = (
    (re.compile(r"Attempting to build version", re.I), "Preparing sources..."),
    (re.compile(r"Pulling updates|Cloning into|Checking out", re.I), "Fetching sources (git)..."),
    (re.compile(r"Applying patches|Patching", re.I), "Applying patches..."),
    (re.compile(r"Decompiling|Remapping|remap", re.I), "Decompiling and remapping Minecraft..."),
    (re.compile(r"Compiling Bukkit", re.I), "Compiling Bukkit..."),
    (re.compile(r"Compiling CraftBukkit", re.I), "Compiling CraftBukkit..."),
    # Only ever printed by a --compile spigot run; a CraftBukkit build stops at
    # the line above. Harmless either way -- a hint that never matches just
    # leaves the previous stage on screen.
    (re.compile(r"Compiling Spigot", re.I), "Compiling Spigot..."),
    (re.compile(r"Success! Everything (completed|worked)", re.I), "Build finished."),
)

_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def _cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "mc-server-manager")
    os.makedirs(d, exist_ok=True)
    return d


def _work_dir() -> str:
    d = os.path.join(_cache_dir(), "buildtools")
    os.makedirs(d, exist_ok=True)
    return d


def _version_key(vid: str) -> tuple:
    nums = [int(n) for n in re.findall(r"\d+", vid.split("-")[0])]
    while len(nums) < 3:
        nums.append(0)
    # A plain "1.13" outranks "1.13-pre7".
    return (*nums[:3], 0 if "-" in vid else 1)


class SpigotProvider:
    name = "Spigot"
    content_dir = "plugins"
    compiles_from_source = True  # GUI warns the user this is slow

    # --- the only three things CraftBukkit changes ------------------------
    # BuildTools --compile value.
    build_target = "spigot"
    # What to pick up from the output dir. Prefix-anchored on purpose: it must
    # not match the other target's jar if both ever land in one directory.
    artifact_glob = "spigot-*.jar"
    # Appended to every install's notes.
    extra_notes: tuple = ()
    # ----------------------------------------------------------------------

    def __init__(self) -> None:
        self._index: Optional[list[str]] = None
        self._meta: dict[str, dict] = {}

    # ---------------- shared work-dir paths ----------------

    def _staging_dir(self, rev: str) -> str:
        """Where BuildTools drops the finished jar. Wiped before each build."""
        return os.path.join(_work_dir(), f"out-{self.build_target}-{rev}")

    def _log_path(self, rev: str) -> str:
        """Build log. Named in the error message, so it must not be clobbered."""
        return os.path.join(_work_dir(), f"build-{self.build_target}-{rev}.log")

    # ---------------- index ----------------

    def _load_index(self, progress: Optional[ProgressCB] = None) -> list[str]:
        if self._index is not None:
            return self._index

        cache = os.path.join(_cache_dir(), "spigot_versions.json")
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < CACHE_TTL:
            try:
                with open(cache, "r", encoding="utf-8") as fh:
                    self._index = json.load(fh)
                return self._index
            except (OSError, ValueError):
                pass

        if progress:
            progress(STAGE_INDEX, 0, None, "Fetching Spigot version index...")

        try:
            html = fetch(VERSIONS_URL).decode("utf-8", "replace")
        except ProviderError:
            if os.path.exists(cache):
                with open(cache, "r", encoding="utf-8") as fh:
                    self._index = json.load(fh)
                return self._index
            raise

        # No JSON index exists for this listing -- it is a plain autoindex page.
        # Ugly, but the format has been stable for years, and the cache plus the
        # stale-fallback above means we hit it at most twice a day.
        names = re.findall(r'href="([^"]+)\.json"', html)
        versions = sorted({n for n in names if _VERSION_RE.match(n)}, key=_version_key, reverse=True)
        if not versions:
            raise ProviderError(
                "Spigot's version listing returned no usable entries. "
                "The page format may have changed."
            )

        try:
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(versions, fh)
        except OSError:
            pass

        self._index = versions
        return versions

    def list_versions(
        self, include_unstable: bool = False, progress: Optional[ProgressCB] = None
    ) -> list[Version]:
        ids = self._load_index(progress)
        out = []
        for vid in ids:
            unstable = "-" in vid
            if unstable and not include_unstable:
                continue
            out.append(Version(id=vid, kind="pre" if unstable else "release", ref=vid))
        return out

    # ---------------- per-version metadata ----------------

    def _version_meta(self, version: Version) -> dict:
        vid = version.ref or version.id
        if vid in self._meta:
            return self._meta[vid]
        try:
            data = json.loads(fetch(f"{VERSIONS_URL}{vid}.json"))
        except ValueError as exc:
            raise ProviderError(f"Malformed metadata for Spigot {vid}: {exc}") from None
        self._meta[vid] = data
        return data

    def java_range(self, version: Version) -> tuple[int, int]:
        """Buildable Java range, from Spigot's classfile-major pair."""
        meta = self._version_meta(version)
        pair = meta.get("javaVersions")
        if not pair or len(pair) < 2:
            # Pre-2021 entries omit the field; those are all Java 8 era.
            return (8, 8)
        return javafind.java_range_from_classfile(pair[:2])

    def required_java(self, version: Version) -> Optional[int]:
        return self.java_range(version)[1]

    # ---------------- prerequisites ----------------

    def check_prerequisites(self, version: Version) -> list[str]:
        """Human-readable blockers. Empty list means good to go."""
        problems: list[str] = []

        if not shutil.which("git"):
            problems.append(
                "Git was not found on PATH. BuildTools runs git directly, so it "
                "must be installed (git-scm.com)."
            )

        lo, hi = self.java_range(version)
        jdks = javafind.find_jdks()
        chosen = javafind.select_jdk(lo, hi, jdks)
        if chosen is None:
            problems.append(javafind.describe_missing(lo, hi, jdks))
        elif not chosen.has_javac:
            problems.append(
                f"The only Java {chosen.major} found is a JRE, not a JDK. "
                "BuildTools compiles from source and needs javac."
            )

        try:
            free = shutil.disk_usage(_work_dir()).free
            if free < 2 * 1024**3:
                problems.append(
                    f"Only {free / 1024**3:.1f} GB free where builds run. "
                    "BuildTools needs roughly 2 GB of scratch space."
                )
        except OSError:
            pass

        return problems

    # ---------------- BuildTools ----------------

    def _ensure_buildtools(
        self, progress: Optional[ProgressCB], cancel: Optional[threading.Event]
    ) -> str:
        path = os.path.join(_work_dir(), "BuildTools.jar")
        fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < BUILDTOOLS_MAX_AGE
        if fresh and os.path.getsize(path) > 0:
            return path

        if progress:
            progress(STAGE_DOWNLOAD, 0, None, "Downloading BuildTools.jar...")
        try:
            data = fetch(BUILDTOOLS_URL, timeout=60)
        except ProviderError:
            if os.path.exists(path):
                return path  # stale copy beats no copy
            raise
        check_cancelled(cancel)

        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return path

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """Kill BuildTools *and* the Maven children it spawns.

        A plain proc.kill() orphans the JVM children, which keep churning CPU
        and holding file locks in the work dir long after 'cancel'.
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

    def _run_buildtools(
        self,
        jar: str,
        java_exe: str,
        rev: str,
        out_dir: str,
        progress: Optional[ProgressCB],
        cancel: Optional[threading.Event],
    ) -> None:
        work = _work_dir()
        log_path = self._log_path(rev)
        cmd = [
            java_exe, "-Xmx2G", "-jar", jar,
            "--rev", rev,
            "--output-dir", out_dir,
            "--compile", self.build_target,
        ]

        popen_kw = dict(_NO_WINDOW)
        if sys.platform != "win32":
            popen_kw["start_new_session"] = True  # own process group, so we can kill the tree

        started = time.time()
        if progress:
            progress(
                STAGE_BUILD, 0, None,
                f"Starting BuildTools for {self.name} (this takes 10-30 minutes)...",
            )

        try:
            proc = subprocess.Popen(
                cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace", **popen_kw,
            )
        except OSError as exc:
            raise ProviderError(f"Could not start BuildTools: {exc}") from None

        stage = "Building..."
        try:
            with open(log_path, "w", encoding="utf-8") as log:
                assert proc.stdout is not None
                for line in proc.stdout:
                    log.write(line)

                    if cancel is not None and cancel.is_set():
                        self._terminate(proc)
                        raise Cancelled("Build cancelled.")

                    for pattern, label in _STAGE_HINTS:
                        if pattern.search(line):
                            stage = label
                            break

                    if progress:
                        mins, secs = divmod(int(time.time() - started), 60)
                        progress(STAGE_BUILD, 0, None, f"{stage}  [{mins}m {secs:02d}s]")
        finally:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._terminate(proc)

        if proc.returncode != 0:
            raise ProviderError(
                f"BuildTools exited with code {proc.returncode}.\n\n"
                f"Full build log:\n{log_path}\n\n"
                "Common causes: no Git on PATH, wrong JDK version, or an "
                "interrupted download of the Minecraft server jar."
            )

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

        rev = version.ref or version.id
        report(STAGE_RESOLVE, 0, None, f"Checking requirements for {self.name} {rev}...")

        problems = self.check_prerequisites(version)
        if problems:
            raise ProviderError("\n\n".join(problems))

        lo, hi = self.java_range(version)
        jdk = javafind.select_jdk(lo, hi)
        if jdk is None:  # re-checked because probing is racy across a slow UI
            raise ProviderError(javafind.describe_missing(lo, hi, javafind.find_jdks()))
        check_cancelled(cancel)

        jar = self._ensure_buildtools(progress, cancel)

        os.makedirs(dest_dir, exist_ok=True)
        staging = self._staging_dir(rev)
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)

        self._run_buildtools(jar, jdk.path, rev, staging, progress, cancel)
        check_cancelled(cancel)

        report(STAGE_FINALIZE, 0, None, "Installing server files...")
        built = sorted(glob.glob(os.path.join(staging, self.artifact_glob)))
        if not built:
            raise ProviderError(
                f"BuildTools reported success but produced no {self.build_target} "
                f"jar in\n{staging}\n\nCheck the build log:\n{self._log_path(rev)}"
            )

        jar_path = os.path.join(dest_dir, "server.jar")
        shutil.copy2(built[-1], jar_path)
        shutil.rmtree(staging, ignore_errors=True)

        notes: list[str] = [f"Compiled from source with Java {jdk.major}."]

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

        os.makedirs(os.path.join(dest_dir, "plugins"), exist_ok=True)
        self._write_start_scripts(dest_dir, hi)
        notes.append("Drop plugins into the plugins/ folder.")
        notes.extend(self.extra_notes)

        return InstallResult(
            server_dir=dest_dir,
            jar_path=jar_path,
            launch_argv=["{java}", "-Xms{min_ram}", "-Xmx{max_ram}", "-jar", "{jar}", "nogui"],
            java_major=hi,
            notes=notes,
        )

    @staticmethod
    def _write_start_scripts(dest_dir: str, java_major: Optional[int]) -> None:
        hint = f"REM Requires Java {java_major}\n" if java_major else ""
        with open(os.path.join(dest_dir, "start.bat"), "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(f"@echo off\n{hint}java -Xms1G -Xmx2G -jar server.jar nogui\npause\n")

        sh_hint = f"# Requires Java {java_major}\n" if java_major else ""
        sh = os.path.join(dest_dir, "start.sh")
        with open(sh, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"#!/bin/sh\n{sh_hint}exec java -Xms1G -Xmx2G -jar server.jar nogui\n")
        try:
            os.chmod(sh, os.stat(sh).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass


class CraftBukkitProvider(SpigotProvider):
    """Spigot's upstream: the same build, stopped one step earlier.

    Everything that makes Spigot slow and fussy applies here unchanged -- same
    version index, same Java range, same Git and scratch-space requirements --
    because it is the same BuildTools run with a different `--compile` target.

    Why you would pick it over Spigot, which is otherwise a strict superset:
    Spigot's patches change observable behaviour (mob spawning, item merging,
    a handful of other mechanics), so a plugin that misbehaves only under
    Spigot is worth retesting against unpatched CraftBukkit, and a server that
    wants vanilla mechanics *with* a plugin API has nowhere else to go. Absent
    one of those reasons, Spigot is the better default, which is what the
    install note says.
    """

    name = "CraftBukkit"
    build_target = "craftbukkit"
    artifact_glob = "craftbukkit-*.jar"
    extra_notes = (
        "CraftBukkit is Spigot without Spigot's patches: closer to vanilla "
        "behaviour, but with none of Spigot's performance work and no "
        "spigot.yml. If you did not specifically want that, Spigot is a strict "
        "superset of this.",
    )


def _cli(provider: Optional[SpigotProvider] = None) -> None:
    p = provider or SpigotProvider()
    versions = p.list_versions()
    print(f"{len(versions)} {p.name} revisions available (--compile {p.build_target})")
    for v in versions[:10]:
        lo, hi = p.java_range(v)
        print(f"  {v.id:<10} Java {lo}-{hi}")


if __name__ == "__main__":
    # Both targets share one index, so one flag is cheaper than a second module
    # entry point: python -m mcserver.providers.spigot [--craftbukkit]
    _cli(CraftBukkitProvider() if "--craftbukkit" in sys.argv[1:] else SpigotProvider())
