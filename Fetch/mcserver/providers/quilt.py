"""Quilt Minecraft server provider (meta.quiltmc.org v3 + quilt-installer).

Standalone: `python -m mcserver.providers.quilt` prints the version list.

Quilt is a fork of Fabric and its meta API is recognisably Fabric's, which
makes the differences easy to miss and expensive to get wrong. This module is
*not* fabric.py with a different base URL, and the four reasons why are the
whole design:

**1. There is no `/server/jar`.** Fabric hands you a prebuilt launcher jar at
`/versions/loader/{game}/{loader}/{installer}/server/jar`; that endpoint is
what lets fabric.py be a plain download. Quilt has no equivalent -- its meta
serves `/server/json` (launch metadata: `launcherMainClass` plus 14 maven
coordinates with no checksums) and expects `quilt-installer` to turn that into
a runnable directory. So this provider *runs an installer*, which puts it in
forge.py's family rather than fabric.py's: it needs a JVM on the machine,
it reports `STAGE_BUILD`, and `check_prerequisites` refuses early if Java is
missing rather than failing several minutes in.

Reimplementing the installer was the alternative and was rejected: the 14
libraries in `/server/json` carry `name` and `url` and nothing else -- no
sha1, no size -- so a hand-rolled version would perform fourteen unverified
downloads and hand-assemble a manifest whose exact expected shape is not
documented anywhere. Running the real installer is one download that *is*
checksummed (see 4).

**2. Loader builds carry no `stable` flag.** Fabric's loader entries have
`"stable": true|false` and fabric.py's channel split reads it. Quilt's do not
have the field at all -- stability lives in the version string, as
`0.30.1-beta.3`. So the Recommended/Latest split here tests for a `-beta`
marker, the same way neoforge.py does for a loader index with no promotions
file. Reading Fabric's missing flag as falsy would have quietly marked *every*
Quilt loader unstable and made "Recommended" permanently unreachable.

**3. `launcherMeta` has no `min_java_version`.** That field is the one
machine-readable Java hint fabric.py gets for free, and Quilt does not publish
it. Java comes from Mojang's per-version metadata via `VanillaProvider`, the
same best-effort lookup paper.py and purpur.py use.

**4. The installer jar is checksummed, and that is a step up.**
`/v3/versions/installer` publishes `url`, `file_size` and `hashes`
(sha1/sha256/sha512) for every installer release. Fabric publishes no checksum
for its launcher jar at all, and fabric.py has to say so in its notes. Here the
one executable we fetch is verified against a published SHA-256 before it is
ever run -- which matters more than usual, because unlike a server jar this one
is handed straight to a JVM.

## What could not be verified from here, and how it degrades

The documented server command is exactly:

    java -jar quilt-installer-<version>.jar install server <MINECRAFT_VERSION> \\
        --download-server

Two things about it are inference rather than documentation, so both are
written to fail safely rather than silently:

- **Pinning a loader version.** Quilt's own docs show only the Minecraft
  version, but the Recommended/Latest split is meaningless unless a specific
  loader can be requested. This passes the resolved loader as an optional
  positional argument (`install server <mc> <loader>`), mirroring
  fabric-installer. If the installer rejects it, `_run_installer` retries once
  *without* the positional and adds a note saying the pin could not be honoured
  and which loader the installer chose instead. A wrong guess therefore costs
  one extra run, not a failed install.
- **Where the files land.** The docs say the install "generates a Minecraft
  server installation in the `server/` directory", and `--install-dir` is not
  documented. Rather than guess, the installer runs with `cwd=dest_dir` and
  `_flatten_server_dir` afterwards looks for the launch jar in `dest_dir` *or*
  `dest_dir/server/`, hoisting the latter's contents up a level if that is
  where they went. Correct under either behaviour.

**⚠️ Neither of those paths has been executed.** They were written against the
published docs and the installer's source with no way to run a JVM against the
live API from the machine this was written on. `python -m
mcserver.providers.quilt` lists versions and resolves a loader without
installing anything; do that first, then one real install, before relying on
this. Same caveat paper.py carries, for the same reason.

## Known upstream bug worth knowing about

quilt-installer issue #20: the generated `quilt-server-launch.jar` embeds
library paths using the host's path separator, so a directory installed on
Windows does not run on Linux. Not fixed upstream as far as could be
determined, and not something this module can fix without rewriting the jar it
just asked the installer to build. The install notes mention it, because
"install on my desktop, upload to the VPS" is exactly what people do with this
tool.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from typing import Optional

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

BASE_URL = "https://meta.quiltmc.org/v3"

LAUNCH_JAR = "quilt-server-launch.jar"
VANILLA_JAR = "server.jar"

# Quilt encodes loader stability in the version string; there is no `stable`
# field on a loader entry. See point 2 in the module header.
BETA_MARKER = "-beta"

CACHE_TTL = 60 * 60 * 6          # 6h, matching vanilla/paper/fabric
INSTALLER_MAX_AGE = 60 * 60 * 24 * 30   # re-verify a cached installer monthly

# Silences the console window Popen would otherwise flash on Windows.
_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def _cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "mc-server-manager", "quilt")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(filename: str) -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "mc-server-manager")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)


def _is_beta(loader_version: str) -> bool:
    return BETA_MARKER in (loader_version or "").lower()


class QuiltProvider:
    name = "Quilt"
    content_dir = "mods"
    # Not "compiles" in Spigot's sense, but it does run a long opaque external
    # process, which is what the GUI uses this flag to warn about.
    compiles_from_source = False

    def __init__(self) -> None:
        self._index: Optional[dict] = None            # {"game": [...], "hashed": [...]}
        self._loaders_cache: dict[str, list[dict]] = {}
        self._installer: Optional[dict] = None
        self._verified_checksum = False

    # ---------------- index ----------------

    def _load_index(self, progress: Optional[ProgressCB] = None) -> dict:
        if self._index is not None:
            return self._index

        cache = _cache_path("quilt_versions.json")
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < CACHE_TTL:
            try:
                with open(cache, "r", encoding="utf-8") as fh:
                    self._index = json.load(fh)
                return self._index
            except (OSError, ValueError):
                pass  # corrupt cache -> refetch

        if progress:
            progress(STAGE_INDEX, 0, None, "Fetching Quilt version list...")
        try:
            data = {
                "game": json.loads(fetch(f"{BASE_URL}/versions/game")),
                # Quilt's own mappings. Same role Fabric's /intermediary plays:
                # /game is a superset that includes versions with no mappings,
                # for which /versions/loader/{game} comes back empty.
                "hashed": json.loads(fetch(f"{BASE_URL}/versions/hashed")),
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
        """Minecraft versions Quilt has mappings for, newest first.

        Same treatment as Fabric: the dropdown lists Minecraft versions, and
        each splits into Recommended (newest non-beta loader) and Latest
        (newest loader of any stability). The loader list costs a request per
        Minecraft version so the split resolves at install time
        (`resolved=False`); `_select_loader` says plainly when the non-beta one
        turns out not to exist.
        """
        data = self._load_index(progress)

        supported = {
            entry.get("version")
            for entry in data.get("hashed", [])
            if isinstance(entry, dict) and entry.get("version")
        }

        out: list[Version] = []
        for entry in data.get("game", []):
            vid = entry.get("version")
            if not vid:
                continue
            # An empty/unreadable hashed list must not blank the dropdown --
            # fall back to offering everything /game knows about.
            if supported and vid not in supported:
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

        # /versions/game is newest-first already and Minecraft ids are not
        # sortable as text, so upstream order is the only ordering available.
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
            progress(STAGE_RESOLVE, 0, None, f"Fetching Quilt loaders for {game_id}...")
        entries = json.loads(fetch(f"{BASE_URL}/versions/loader/{game_id}"))
        if not isinstance(entries, list) or not entries:
            raise ProviderError(f"Quilt publishes no loader for Minecraft {game_id}.")
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

        Stability is read off the version string, not a `stable` field -- see
        point 2 in the module header. Otherwise the policy is fabric.py's:

        channel None        -- newest non-beta, falling back to newest of any,
                               so a version with only betas still installs.
        CHANNEL_LATEST      -- newest of any stability, deliberately.
        CHANNEL_RECOMMENDED -- newest non-beta, and an error rather than a quiet
                               downgrade if there is none.
        """
        entries = self._loaders(game_id, progress)
        if channel == CHANNEL_LATEST:
            return entries[0]

        stable = [
            e for e in entries if not _is_beta(e.get("loader", {}).get("version", ""))
        ]
        if stable:
            return stable[0]
        if channel == CHANNEL_RECOMMENDED:
            raise ProviderError(
                f"Quilt has no stable loader for Minecraft {game_id} -- every build "
                'so far is a beta. Pick the "Latest" entry for this version instead.'
            )
        return entries[0]

    def _select_installer(self, progress: Optional[ProgressCB] = None) -> dict:
        """Newest installer release, with its published url + hashes.

        Unlike Fabric's installer list these entries have no `stable` flag
        either, and the installer only decides *how* the server directory is
        assembled, so newest is simply taken.
        """
        if self._installer is None:
            if progress:
                progress(STAGE_RESOLVE, 0, None, "Fetching Quilt installer list...")
            entries = json.loads(fetch(f"{BASE_URL}/versions/installer"))
            if not isinstance(entries, list) or not entries:
                raise ProviderError("Quilt published no installer versions.")
            self._installer = entries[0]
        return self._installer

    def required_java(self, game_id: str) -> Optional[int]:
        """Best-effort, from Mojang. Quilt publishes no Java floor of its own.

        fabric.py can take the max of Mojang's number and the loader's
        `launcherMeta.min_java_version`; Quilt's launcherMeta has no such field,
        so Mojang's is all there is.
        """
        try:
            from .vanilla import VanillaProvider  # local import, avoids a hard dependency

            vp = VanillaProvider()
            for v in vp.list_versions(include_unstable=True):
                if v.id == game_id:
                    return vp.required_java(v)
        except Exception:
            pass
        return None

    # ---------------- prerequisites ----------------

    def check_prerequisites(self, version: Version) -> list[str]:
        """Human-readable blockers. Empty list means good to go.

        Called by the GUI before the install starts. The installer is a Java
        program and the finished server needs a JVM anyway, so a machine with
        no Java can never succeed -- far better to say so now than after a
        download.
        """
        problems: list[str] = []

        need = self.required_java(version.ref or version.id)
        jdks = javafind.find_jdks()
        if not jdks:
            problems.append(
                "No Java installation was found. The Quilt installer is a Java "
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

        return problems

    def _pick_java(self, game_id: str) -> javafind.Jdk:
        need = self.required_java(game_id)
        jdks = javafind.find_jdks()
        if not jdks:
            raise ProviderError(
                "No Java installation was found. Install a JDK from adoptium.net."
            )
        if need:
            fits = [j for j in jdks if j.major >= need]
            if not fits:
                raise ProviderError(javafind.describe_missing(need, need, jdks))
            # Oldest JVM that still satisfies the requirement, matching forge.py:
            # newer JVMs are likelier to trip an installer than older-but-adequate
            # ones.
            fits.sort(key=lambda j: (j.major, not j.has_javac))
            return fits[0]
        return jdks[0]

    # ---------------- installer download ----------------

    def _ensure_installer(
        self,
        progress: Optional[ProgressCB],
        cancel: Optional[threading.Event],
    ) -> str:
        """Download (or reuse) the installer jar, verified against its SHA-256.

        This jar gets handed to a JVM, so unlike a server jar an unverified copy
        is not merely a corruption risk. Quilt publishes the hash, so it is
        checked and a mismatch aborts before anything is executed.
        """
        entry = self._select_installer(progress)
        version = entry.get("version") or "unknown"
        url = entry.get("url")
        if not url:
            raise ProviderError(
                f"Quilt installer {version} has no download URL in the meta API."
            )
        expected = (entry.get("hashes") or {}).get("sha256")
        expected_size = int(entry.get("file_size") or 0)

        path = os.path.join(_cache_dir(), f"quilt-installer-{version}.jar")
        stamp = path + ".verified"
        if os.path.exists(path) and os.path.getsize(path) > 0:
            if time.time() - os.path.getmtime(path) < INSTALLER_MAX_AGE:
                # The sidecar records that *this* copy was checksum-verified when
                # it was downloaded, so a cache hit can't silently downgrade the
                # note shown to the user.
                self._verified_checksum = os.path.exists(stamp)
                return path

        if progress:
            progress(STAGE_DOWNLOAD, 0, expected_size or None,
                     f"Downloading Quilt installer {version}...")

        fd, tmp_path = tempfile.mkstemp(prefix=".quilt-installer-", suffix=".part",
                                        dir=_cache_dir())
        os.close(fd)
        digest = hashlib.sha256()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp_path, "wb") as out:
                total = expected_size or int(resp.headers.get("Content-Length") or 0) or None
                got = 0
                while True:
                    check_cancelled(cancel)
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
                    if progress:
                        progress(STAGE_DOWNLOAD, got, total,
                                 f"Downloading Quilt installer... ({got:,} bytes)")

            if progress:
                progress(STAGE_VERIFY, 0, None, "Verifying the installer...")
            if expected:
                if digest.hexdigest().lower() != str(expected).lower():
                    raise ProviderError(
                        "SHA-256 mismatch on the Quilt installer -- it is corrupt or "
                        "was tampered with. It was NOT run and nothing was installed."
                    )
                self._verified_checksum = True
            else:
                self._verified_checksum = False
            if expected_size and got != expected_size:
                raise ProviderError(
                    f"Size mismatch on the Quilt installer: expected {expected_size}, "
                    f"got {got}. It was not run."
                )
            if not got:
                raise ProviderError("The Quilt installer downloaded as an empty file.")

            os.replace(tmp_path, path)
            tmp_path = None  # ownership transferred
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        try:
            if self._verified_checksum:
                with open(stamp, "w", encoding="utf-8") as fh:
                    fh.write(digest.hexdigest())
            elif os.path.exists(stamp):
                os.remove(stamp)
        except OSError:
            pass

        return path

    # ---------------- running the installer ----------------

    @staticmethod
    def _terminate(proc: "subprocess.Popen") -> None:
        """Kill the installer and anything it spawned.

        Same reasoning as forge.py: the installer's work can involve child
        JVMs, and a plain kill() orphans them to keep writing into a
        half-built directory.
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

    def _invoke(
        self,
        cmd: list[str],
        dest_dir: str,
        log_path: str,
        progress: Optional[ProgressCB],
        cancel: Optional[threading.Event],
    ) -> int:
        """One installer run. -> exit code. Streams output to `log_path`."""
        popen_kw = dict(_NO_WINDOW)
        if sys.platform != "win32":
            popen_kw["start_new_session"] = True  # own process group, so the tree is killable

        started = time.time()
        try:
            proc = subprocess.Popen(
                cmd, cwd=dest_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace", **popen_kw,
            )
        except OSError as exc:
            raise ProviderError(f"Could not start the Quilt installer: {exc}") from None

        try:
            with open(log_path, "w", encoding="utf-8") as log:
                assert proc.stdout is not None
                for line in proc.stdout:
                    log.write(line)
                    if cancel is not None and cancel.is_set():
                        self._terminate(proc)
                        raise Cancelled("Install cancelled.")
                    if progress:
                        mins, secs = divmod(int(time.time() - started), 60)
                        progress(STAGE_BUILD, 0, None,
                                 f"Running the Quilt installer...  [{mins}m {secs:02d}s]")
        finally:
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self._terminate(proc)

        return proc.returncode

    def _run_installer(
        self,
        installer: str,
        java_exe: str,
        game_id: str,
        loader_version: str,
        dest_dir: str,
        progress: Optional[ProgressCB],
        cancel: Optional[threading.Event],
    ) -> tuple[str, Optional[str]]:
        """-> (log_path, note_or_None).

        Runs `install server <mc> <loader> --download-server`. The loader
        positional is the one argument inferred rather than documented (see the
        module header), so a non-zero exit triggers exactly one retry without
        it; the returned note records that the pin was dropped. Any second
        failure is reported with the installer's own log.
        """
        log_path = os.path.join(_cache_dir(), f"install-{game_id}-{loader_version}.log")
        base = [java_exe, "-jar", installer, "install", "server", game_id]

        if progress:
            progress(STAGE_BUILD, 0, None, "Running the Quilt installer...")

        code = self._invoke(
            base + [loader_version, "--download-server"], dest_dir, log_path, progress, cancel
        )
        if code == 0:
            return log_path, None

        # Retry without the loader positional. Only worth doing when the
        # argument we're unsure about was actually present.
        if progress:
            progress(STAGE_BUILD, 0, None,
                     "Retrying without a pinned loader version...")
        retry_log = log_path + ".retry"
        code = self._invoke(base + ["--download-server"], dest_dir, retry_log, progress, cancel)
        if code == 0:
            return retry_log, (
                f"This installer build would not accept a pinned loader version, so "
                f"Quilt {loader_version} was not requested explicitly -- the installer "
                "chose its own default loader. Check quilt-server-launch.jar if you "
                "needed that exact build."
            )

        raise ProviderError(
            f"The Quilt installer exited with code {code}, both with and without a "
            f"pinned loader version.\n\nFull logs:\n{log_path}\n{retry_log}\n\n"
            "Common causes: the JVM is too old for this Minecraft version, no network "
            "access to Mojang's and Quilt's servers, or the target folder is not "
            "writable."
        )

    @staticmethod
    def _flatten_server_dir(dest_dir: str) -> bool:
        """Hoist `dest_dir/server/*` up a level if that is where the install went.

        Quilt's docs say the install "generates a Minecraft server installation
        in the `server/` directory" and document no flag to change it, so rather
        than assume either behaviour this looks for the launch jar in both
        places. -> True if anything was moved.
        """
        if os.path.exists(os.path.join(dest_dir, LAUNCH_JAR)):
            return False
        nested = os.path.join(dest_dir, "server")
        if not os.path.isdir(nested) or not os.path.exists(os.path.join(nested, LAUNCH_JAR)):
            return False

        for entry in os.listdir(nested):
            src = os.path.join(nested, entry)
            dst = os.path.join(dest_dir, entry)
            if os.path.exists(dst):
                # Only the eula/properties the caller may have written can
                # collide; the installer's copy is the newer truth.
                if os.path.isdir(dst) and not os.path.islink(dst):
                    shutil.rmtree(dst, ignore_errors=True)
                else:
                    try:
                        os.remove(dst)
                    except OSError:
                        continue
            shutil.move(src, dst)
        try:
            os.rmdir(nested)
        except OSError:
            pass  # something unmovable stayed behind; harmless
        return True

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

        game_id = version.ref or version.id
        report(STAGE_RESOLVE, 0, None, f"Resolving Quilt {game_id}...")

        loader_entry = self._select_loader(game_id, progress, version.channel)
        loader_version = loader_entry["loader"]["version"]
        loader_beta = _is_beta(loader_version)
        installer_entry = self._select_installer(progress)
        java = self._pick_java(game_id)
        java_major = self.required_java(game_id)

        os.makedirs(dest_dir, exist_ok=True)
        check_cancelled(cancel)

        installer = self._ensure_installer(progress, cancel)
        check_cancelled(cancel)

        log_path, pin_note = self._run_installer(
            installer, java.path, game_id, loader_version, dest_dir, progress, cancel
        )

        report(STAGE_FINALIZE, 0, None, "Writing server files...")
        hoisted = self._flatten_server_dir(dest_dir)

        jar_path = os.path.join(dest_dir, LAUNCH_JAR)
        if not os.path.exists(jar_path):
            raise ProviderError(
                f"The Quilt installer reported success but {LAUNCH_JAR} is not in "
                f"{dest_dir} (nor in a server/ subfolder).\n\nInstaller log:\n{log_path}"
            )

        notes: list[str] = [
            f"Quilt loader {loader_version}" + (" (beta)" if loader_beta else "")
            + f", installer {installer_entry.get('version', '?')}."
        ]
        if pin_note:
            notes.append(pin_note)
        if hoisted:
            notes.append(
                "The installer wrote into a server/ subfolder; its contents were "
                "moved up into the folder you chose."
            )
        notes.append(
            "The Quilt installer jar was verified against the SHA-256 published by "
            "meta.quiltmc.org before it was run."
            if self._verified_checksum else
            "No checksum was published for this installer build, so it was run "
            "without integrity verification."
        )

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

        notes.append(
            "Known upstream bug (quilt-installer #20): the generated "
            f"{LAUNCH_JAR} records library paths using this machine's path "
            "separator, so a folder installed on Windows may not start on Linux. "
            "Install on the machine that will run the server."
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
            fh.write(
                f"@echo off\n{hint}java -Xms1G -Xmx2G -jar {LAUNCH_JAR} nogui\npause\n"
            )

        sh_hint = f"# Requires Java {java_major}\n" if java_major else ""
        sh = os.path.join(dest_dir, "start.sh")
        with open(sh, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                f"#!/bin/sh\n{sh_hint}exec java -Xms1G -Xmx2G -jar {LAUNCH_JAR} nogui\n"
            )
        try:
            os.chmod(sh, os.stat(sh).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass


def _cli() -> None:
    p = QuiltProvider()
    versions = p.list_versions(include_unstable=False)
    print(f"{len(versions)} releases, latest = {p.latest_release()}")
    for v in versions[:15]:
        print(" ", v.id)

    if versions:
        newest = versions[0].id
        builds = p.builds_for(newest)
        print(f"\nloaders for {newest}: {builds[:5]}")
        entry = p._select_loader(newest)
        lv = entry["loader"]["version"]
        print("  selected loader:", lv, "beta" if _is_beta(lv) else "stable")
        inst = p._select_installer()
        print("  installer:", inst.get("version"), inst.get("url"))
        print("  java:", p.required_java(newest))
    print("\nNothing was installed. See the module header before the first real run.")


if __name__ == "__main__":
    _cli()
