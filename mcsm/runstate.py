"""Is a server currently running?

There is no reliable answer to this in general -- we cannot ping a host we
don't know the address of, and RCON needs credentials we don't have. What we
can do is read the two pieces of evidence the world folder leaves behind, and
be honest about which of them are trustworthy.

**session.lock is the strong signal, in one direction only.**

The file itself carries nothing useful: pre-1.13 servers write an 8-byte
`currentTimeMillis`, 1.13+ write three bytes -- E2 98 83, UTF-8 for a snowman.
The signal is that the server holds an *OS-level byte-range lock* on the open
handle for as long as the world is loaded. Two consequences follow, and both
are the opposite of what the mtime-based check this replaces assumed:

  * The lock dies with the process. SIGKILL, a pulled plug, a blue screen --
    the kernel drops it either way, so a hard shutdown can never leave a
    stale lock behind. What it *does* leave is the file, with a recent mtime.
  * The file is written once at world load and never touched again. Measured
    on the sample servers: one wrote session.lock nine seconds after start
    and left it alone for the following ten minutes. So mtime says when the
    world was *opened*, not whether it is open now, and any check with a
    freshness window reports a long-running server as stopped -- a false
    negative in the direction that destroys worlds.

**And the lock only travels as far as the filesystem carries it.** A lock
taken over SMB, NFS, a FUSE bridge or a Docker mount may never reach the
process that actually holds the world open. Verified: probing a file across
a FUSE mount succeeded happily while the real file lived on another machine
entirely. So a *refused* lock is proof something holds the world, anywhere;
an *acquired* lock only proves anything on genuinely local storage.

**The log is the fallback.** When no lock is held we read the tail of
latest.log looking for a shutdown sequence. Only shutdown-exclusive lines
count -- "All dimensions are saved" fires on every autosave and means
nothing on its own.

Nothing here ever gates a *player file* edit. Editing an offline player's
data on a running server is fine, so that path warns rather than checks.
"""

import errno
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .world import get_world_dir, get_nether_dir, get_the_end_dir, has_satellite_dimension_folders

# Filesystems where a byte-range lock either is not propagated to whoever else
# has the file open, or is propagated too unreliably to bet a world on. A
# successful lock on any of these tells us nothing.
NON_LOCAL_FSTYPES = frozenset({
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "9p", "virtiofs", "fuse", "fuseblk",
    "afs", "ncpfs", "glusterfs", "ceph", "lustre", "vboxsf", "prl_fs", "davfs",
    "sshfs", "overlay",
})

# Lines that only ever appear during a shutdown sequence. Deliberately narrow:
# "Saving chunks", "All dimensions are saved" and friends all fire on a routine
# autosave, so treating them as stop markers would call a running server
# stopped.
# Every server log line is "[stamp] [Thread/LEVEL]( [logger])*: message", so
# the message begins immediately after the bracket run. Anchoring there is
# what stops a player pasting a shutdown line into chat: their text always
# lands *after* the real separator, and this requires the phrase to be at it.
_LOG_MESSAGE_START = r"^(?:\[[^\]]*\]\s*)+:\s*"

# Lines that only ever appear during a shutdown sequence. Deliberately narrow:
# "Saving chunks", "Saving players" and "All dimensions are saved" all fire on
# a routine autosave, so treating them as stop markers would call a running
# server stopped.
LOG_STOP_PATTERNS = (
    re.compile(_LOG_MESSAGE_START + r"Stopping (the )?server\b", re.I),
    re.compile(_LOG_MESSAGE_START + r"Closing FML Loader\b", re.I),
    re.compile(_LOG_MESSAGE_START + r"Clearing ModLoader\b", re.I),
)

# Lines that mark a server coming *up*. A tail can straddle a stop followed by
# a restart, in which case the stop is stale and the server is running now.
LOG_START_PATTERNS = (
    re.compile(_LOG_MESSAGE_START + r"Starting minecraft server version", re.I),
    re.compile(_LOG_MESSAGE_START + r"Starting Minecraft server on", re.I),
    re.compile(_LOG_MESSAGE_START + r"Done \([0-9.]+s\)!", re.I),
    re.compile(_LOG_MESSAGE_START + r"Starting FancyModLoader", re.I),
    re.compile(_LOG_MESSAGE_START + r"Loading Minecraft [\w.]+ with (Fabric|Quilt) Loader", re.I),
    # ModLauncher writes its banner before the bracket convention settles, so
    # this one stays unanchored -- it is a start marker, and a false start
    # only ever pushes the verdict toward "unknown", which is the safe side.
    re.compile(r"ModLauncher running:", re.I),
)

# Enough for any shutdown sequence -- the longest in the sample ran 31 lines
# after its marker -- without reading a multi-megabyte log off a network share.
LOG_TAIL_BYTES = 64 * 1024


@dataclass
class RunState:
    """What we could work out, and how much of it we actually know.

    Only a held lock is treated as knowledge. Everything else carries a
    caveat, because everything else is inference -- so "stopped" still says
    so in the confirm dialog rather than passing silently."""
    state: str                       # "running" | "stopped" | "unknown"
    reason: str = ""                 # one line, shown to the user
    detail: list = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def severity(self) -> str:
        """How hard a destructive action should push back.

        "block"   -- a lock is held; this is the one case we actually know.
        "caution" -- nothing found and nothing confirms a shutdown either.
        "note"    -- the log says it stopped cleanly; likely fine, still said
                     out loud, since no lock was found to corroborate it.
        """
        return {"running": "block", "unknown": "caution"}.get(self.state, "note")


def _posix_fstype(path: Path) -> Optional[str]:
    """Filesystem type for the longest mount point containing `path`."""
    try:
        target = str(path.resolve())
    except OSError:
        target = str(path)
    best, best_type = "", None
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1].replace("\\040", " ")
                if target == mount or target.startswith(mount.rstrip("/") + "/"):
                    if len(mount) > len(best):
                        best, best_type = mount, parts[2]
    except OSError:
        return None
    return best_type


def storage_is_local(path: Path):
    """(is_local, reason). is_local is None when it can't be determined --
    which is treated the same as "not local": we only trust an acquired lock
    when we are certain the storage is local."""
    path = Path(path)
    if os.name == "nt":
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved.startswith("\\\\") or resolved.startswith("//"):
            return False, "UNC network path"
        drive = os.path.splitdrive(resolved)[0]
        if not drive:
            return None, "no drive letter"
        try:
            import ctypes
            kind = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
        except Exception as e:
            return None, f"could not query drive type ({e})"
        if kind == 4:            # DRIVE_REMOTE
            return False, "mapped network drive"
        if kind in (3, 6):       # DRIVE_FIXED, DRIVE_RAMDISK
            return True, "local disk"
        if kind == 2:            # DRIVE_REMOVABLE
            return True, "removable drive"
        return None, f"unrecognised drive type {kind}"
    fstype = _posix_fstype(path)
    if fstype is None:
        return None, "could not read /proc/mounts"
    if fstype in NON_LOCAL_FSTYPES:
        return False, f"{fstype} filesystem"
    return True, f"{fstype} filesystem"


# Errnos that genuinely mean "somebody else holds this". Everything else --
# ENOLCK from NFS without lockd, EINVAL/EOPNOTSUPP from a filesystem with no
# lock support -- means the question could not be asked. Reporting those as
# "locked" turned into a permanent, un-overridable refusal to restore, because
# a held lock is the one verdict with no override.
LOCK_CONTENTION_ERRNOS = frozenset({
    errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EDEADLOCK,
})


def _classify_lock_error(exc: OSError, path: Path):
    if exc.errno in LOCK_CONTENTION_ERRNOS:
        return "locked", f"{path.name} is locked by another process"
    return "unknown", (f"could not test the lock on {path.name} "
                       f"({exc.strerror or exc})")


def probe_file_lock(path: Path):
    """(result, detail) where result is "locked", "absent", "free" or
    "unknown".

    "locked" means another process holds this file -- trustworthy wherever it
    comes from, since nothing else produces it.

    "absent" is the *strongest* negative answer, stronger than "free". A
    server creates session.lock when it opens a world (DirectoryLock.create
    uses StandardOpenOption.CREATE) and never deletes it -- close() releases
    the lock and closes the channel, nothing more. So a world with no
    session.lock has never been opened by anything, and unlike a lock we
    merely acquired, that conclusion survives network storage: a server on
    another machine would have *created* the file, and file existence
    propagates over SMB and NFS even where lock semantics do not. Mojang
    reads it the same way -- DirectoryLock.isLocked catches
    NoSuchFileException and returns false.

    "free" means we took the lock ourselves, which only rules anything out on
    local storage; the caller is responsible for that check."""
    path = Path(path)
    if not path.is_file():
        return "absent", f"{path.name} does not exist -- world never opened"
    try:
        handle = open(path, "r+b")
    except OSError as e:
        # Can't open for writing: read-only mount, permissions, or -- on
        # Windows -- a sharing mode that already excludes us, which is itself
        # a hint that something has it open. Not conclusive either way.
        return "unknown", f"could not open {path.name} ({e.strerror or e})"
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                return _classify_lock_error(e, path)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            return "free", f"{path.name} is not locked"
        import fcntl
        try:
            fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            return _classify_lock_error(e, path)
        try:
            fcntl.lockf(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        return "free", f"{path.name} is not locked"
    except ImportError as e:
        return "unknown", f"no locking support on this platform ({e})"
    finally:
        handle.close()


def expected_lock_paths(info: "ServerInfo") -> list:
    """Every session.lock this server's world *would* span, whether or not it
    exists yet. A split (Bukkit) world has one per dimension folder and the
    server locks all of them, so checking only the overworld would sail
    straight past a locked nether.

    Unfiltered on purpose: an absent session.lock is a verdict, not a gap
    (see probe_file_lock), and filtering it out here is what used to throw
    that answer away."""
    paths = [get_world_dir(info) / "session.lock"]
    if has_satellite_dimension_folders(info):
        paths.append(get_nether_dir(info) / "session.lock")
        paths.append(get_the_end_dir(info) / "session.lock")
    return paths


def world_lock_paths(info: "ServerInfo") -> list:
    """The subset of expected_lock_paths that is actually on disk."""
    return [p for p in expected_lock_paths(info) if p.is_file()]


def read_log_tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    """The last `limit` bytes of a log, minus the partial first line. Bounded
    so an active server's multi-megabyte log costs the same as a small one."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                fh.readline()          # discard the partial line
            raw = fh.read()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def log_shows_clean_stop(info: "ServerInfo"):
    """(stopped, detail). True only when the tail of latest.log ends in a
    shutdown sequence with no later start.

    The ordering check matters: a tail can span a stop followed immediately
    by a restart, and reading only "is there a stop marker" would call that
    server stopped while it is very much running."""
    log_path = info.path / "logs" / "latest.log"
    if not log_path.is_file():
        return False, "no logs/latest.log to check"
    tail = read_log_tail(log_path)
    if not tail:
        return False, "logs/latest.log is empty or unreadable"
    last_stop = last_start = -1
    # split("\n"), not splitlines(): the latter also breaks on U+2028 and
    # U+0085, both of which vanilla allows in chat, so a player could
    # synthesise a line that looks like a shutdown.
    for i, line in enumerate(tail.split("\n")):
        if any(p.search(line) for p in LOG_STOP_PATTERNS):
            last_stop = i
        if any(p.search(line) for p in LOG_START_PATTERNS):
            last_start = i
    if last_stop < 0:
        return False, "no shutdown in the last of the log -- it may have been killed rather than stopped"
    if last_start > last_stop:
        return False, "the log shows a restart after the last shutdown"
    return True, "the log ends with a clean shutdown"


def check_server_running(info: "ServerInfo") -> RunState:
    """Best available answer, with the reasoning attached.

    Four rungs, and only the last is inference:

      no session.lock at all   -> stopped, anywhere. Nothing has ever opened
                                  this world, and that survives a network
                                  share because file existence propagates
                                  even where lock semantics do not.
      lock refused             -> running, anywhere. Proof.
      lock acquired + local    -> stopped. Proof, on storage that can carry a
                                  lock; the log is read for the detail trail
                                  but does not change the verdict.
      anything else            -> the log. A clean shutdown reads as stopped
                                  with the caveat stated; otherwise unknown.
    """
    locks = expected_lock_paths(info)
    detail = []
    results = []
    for lock_path in locks:
        result, why = probe_file_lock(lock_path)
        detail.append(why)
        if result == "locked":
            return RunState("running",
                            "The world is locked by a running process.", detail)
        results.append(result)

    # Nothing has ever opened this world -- the strongest negative available,
    # and the only confident answer that does not need local storage. Reached
    # by a freshly unzipped world, and by anything restored from an MCSM
    # export, which strips session.lock (world.EXPORT_SKIP_FILES).
    if results and all(r == "absent" for r in results):
        return RunState(
            "stopped",
            "No session.lock exists, so no server has ever opened this world.",
            detail)

    local, why_local = storage_is_local(get_world_dir(info))
    detail.append(f"storage: {why_local}")

    # "absent" is at least as good as "free" here: a dimension folder with no
    # lock file was never opened, which is exactly what "free" is trying to
    # establish. A split world part-way through its first start can show one
    # of each.
    acquired_all = bool(results) and all(r in ("absent", "free") for r in results)

    if acquired_all and local:
        stopped, log_why = log_shows_clean_stop(info)
        detail.append(log_why)
        return RunState("stopped",
                        "No process holds this world and it is on local storage.",
                        detail)

    # Either the lock told us nothing, or it did but over storage that can't
    # carry one. Fall back to the log.
    stopped, log_why = log_shows_clean_stop(info)
    detail.append(log_why)
    if stopped:
        return RunState(
            "stopped",
            "Nothing here holds this world and the log ends with a clean "
            "shutdown -- but this world is not on local storage, so a lock "
            "held by a server elsewhere would not be visible from here.",
            detail)
    return RunState(
        "unknown",
        "Could not confirm whether a server has this world open: nothing "
        "here holds it, and the log does not end with a clean shutdown.",
        detail)
