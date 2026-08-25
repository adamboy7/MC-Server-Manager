#!/usr/bin/env python3
"""Borrow Fetch's decompiler to produce a workspace for the schema extractor.

`extract_mc_schema.py` needs a decompiled Minecraft tree to read. Keeping one
around by hand works, but it goes stale the moment Mojang ships a version that
moves a tag -- which is the exact moment the table most needs regenerating. So
`--fetch` builds one on demand.

This is the only place the two projects touch. `Fetch/mcserver/providers/
decompiled.py` already does everything hard here -- resolving the version
index, unbundling the server jar, picking a mapping era, running Vineflower,
laying out the tree -- so this module is a thin adapter, not a reimplementation.
Delete this file and the extractor still works with `--workspace`; that is the
intended blast radius of the coupling.

Three choices worth knowing about, all made here rather than in the extractor:

**Server, not client.** Every class the extractor reads (`Entity`,
`LivingEntity`, `Player`, `ServerPlayer`, `FoodData`, `PrimaryLevelData`, and
the whole `util/datafix` tree) ships in the server jar. The client workspace is
roughly twice the disk and pulls assets and LWJGL natives the extractor never
opens.

**No compile pass.** `DecompiledProvider.repair_workspace` compiles the tree and
quarantines whatever will not build, so the workspace opens green in an IDE.
That is several minutes of javac for a result we never use -- we read sources,
never build them -- so it is switched off. The extractor searches both
`src/main/java/` and `.decompiled/quarantine/`, so it does not care which side
of that line a file ends up on.

**Latest release only.** There is no version argument. The extractor's parser
tracks the *current* serialisation shape, and older versions are covered
through the fix registry rather than by re-parsing old sources. A pinned-version
mode would need the parser to understand pre-ValueInput sources, which it
deliberately does not.

Stdlib only, like the rest of tools/.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
from pathlib import Path
from typing import Optional


class FetchUnavailable(RuntimeError):
    """Fetch could not be imported, or refused the job. Carries a message the
    caller can print as-is -- callers fall back to --workspace."""


# A file that only exists once a decompile actually finished, used to tell a
# complete workspace from one a cancelled run left half-written.
COMPLETION_MARKER = Path("src") / "main" / "java" / "net" / "minecraft" / "world" / "entity" / "Entity.java"


def default_cache_dir() -> Path:
    """Alongside Fetch's own download cache, so the two clean up together and
    a user who knows where one lives can find the other."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return Path(base) / "mc-server-manager" / "schema-workspaces"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _import_fetch(repo_root: Optional[Path] = None):
    """Return Fetch's decompiled-provider module, or raise FetchUnavailable.

    Fetch is a sibling project with its own package root rather than an
    installed dependency, so its directory goes on sys.path here and nowhere
    else."""
    root = repo_root or _repo_root()
    fetch_dir = root / "Fetch"
    if not (fetch_dir / "mcserver" / "providers" / "decompiled.py").is_file():
        raise FetchUnavailable(
            f"Fetch not found at {fetch_dir}. --fetch borrows its decompiler; "
            f"either check out Fetch/ beside tools/, or pass --workspace with a "
            f"decompiled tree you already have."
        )
    if str(fetch_dir) not in sys.path:
        sys.path.insert(0, str(fetch_dir))
    try:
        from mcserver.providers import decompiled  # noqa: E402
    except ImportError as exc:
        raise FetchUnavailable(f"Could not import Fetch's decompiler: {exc}") from exc
    return decompiled


def _latest_release_server(decompiled):
    """Newest stable release, server side. list_versions returns newest first
    and yields a Client and a Server row per version."""
    provider = decompiled.DecompiledProvider()
    try:
        versions = provider.list_versions(include_unstable=False)
    except decompiled.ProviderError as exc:
        # Reaching Mojang's manifest is the first thing that can fail and the
        # most likely -- offline, a proxy, a firewall. Wrap it so the caller
        # prints a message and falls back to --workspace instead of showing a
        # urllib traceback.
        raise FetchUnavailable(
            f"Could not reach Mojang's version index: {exc}"
        ) from exc
    for version in versions:
        if version.variant == decompiled.SIDE_SERVER and version.is_stable:
            return provider, version
    raise FetchUnavailable(
        "Fetch's version index returned no stable server build. If Mojang's "
        "manifest is reachable this should not happen -- try again, or pass "
        "--workspace."
    )


# check_prerequisites is written for the GUI's use case, where the workspace
# gets compiled and opened in an IDE, so it insists on a JDK. We only run
# Vineflower, and _java_exe falls back to any Java it can find, so those two
# complaints do not apply to a decompile-only run.
#
# Matching another module's message text is fragile, so this fails safe: a
# problem that does not match stays a hard error rather than being waved
# through.
_JAVAC_ONLY_PROBLEMS = ("No JDK was found", "or newer to compile")


def _split_problems(problems: list) -> tuple[list, list]:
    """(fatal, ignorable) split of check_prerequisites output."""
    fatal, ignorable = [], []
    for problem in problems:
        if any(fragment in problem for fragment in _JAVAC_ONLY_PROBLEMS):
            ignorable.append(problem)
        else:
            fatal.append(problem)
    return fatal, ignorable


class _Progress:
    """One rewriting line on stderr. Deliberately not a progress bar: the
    stages have wildly different lengths and a bar that sits at 4% through the
    whole Vineflower run reads as a hang."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet or not sys.stderr.isatty()
        self._width = 0
        self._last_stage = None

    def __call__(self, stage: str, done: int, total: Optional[int], msg: str) -> None:
        if self.quiet:
            # Without a tty, print each stage change once instead of spamming a
            # log file with thousands of carriage returns.
            if stage != self._last_stage:
                self._last_stage = stage
                print(f"  [{stage}] {msg}", file=sys.stderr, flush=True)
            return
        counter = f" {done:,}/{total:,}" if total else (f" {done:,}" if done else "")
        line = f"  [{stage}]{counter} {msg}"[:100]
        pad = " " * max(0, self._width - len(line))
        self._width = len(line)
        print(f"\r{line}{pad}", end="", file=sys.stderr, flush=True)

    def done(self) -> None:
        if not self.quiet and self._width:
            print(f"\r{' ' * self._width}\r", end="", file=sys.stderr, flush=True)
        self._width = 0


def cached_workspaces(cache_dir: Path) -> list:
    """Complete workspaces already in the cache, newest-built first."""
    if not cache_dir.is_dir():
        return []
    found = [
        child for child in cache_dir.iterdir()
        if child.is_dir() and (child / COMPLETION_MARKER).is_file()
    ]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def ensure_workspace(
    cache_dir: Optional[Path] = None,
    *,
    force: bool = False,
    quiet: bool = False,
    repo_root: Optional[Path] = None,
) -> Path:
    """Path to a decompiled server workspace for the latest Minecraft release,
    fetching one only if the cache does not already hold it.

    Raises FetchUnavailable with a printable message when Fetch is missing or
    the environment cannot support a decompile. Ctrl-C cancels through the
    provider's own cancel event, so a partial tree is left recognisably
    incomplete rather than looking finished."""
    decompiled = _import_fetch(repo_root)
    cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()

    try:
        provider, version = _latest_release_server(decompiled)
    except FetchUnavailable as exc:
        # Resolving "latest" needs the network even when the cache could answer
        # the question, so an offline run would otherwise fail with a usable
        # workspace sitting right there. Fall back to it, and say plainly that
        # it might not be the newest version any more.
        cached = cached_workspaces(cache_dir)
        if force or not cached:
            raise
        print(f"  {exc}", file=sys.stderr)
        print(f"  falling back to the newest cached workspace: {cached[0].name}\n"
              f"  ⚠ this may be behind the current Minecraft release.",
              file=sys.stderr)
        return cached[0]

    provider.repair_workspace = False   # see the module docstring
    dest = cache_dir / f"{version.id}-server"

    if dest.is_dir() and (dest / COMPLETION_MARKER).is_file() and not force:
        print(f"  cached workspace: {dest}", file=sys.stderr)
        return dest

    problems = provider.check_prerequisites(version, str(dest))
    fatal, ignorable = _split_problems(problems)
    if fatal:
        raise FetchUnavailable(
            "Fetch cannot decompile here:\n"
            + "\n".join(f"    - {p}" for p in fatal)
        )
    for problem in ignorable:
        print(f"  (ignored, decompile-only run) {problem}", file=sys.stderr)

    if dest.is_dir():
        # Either a cancelled run or --force. Either way a decompile into a
        # populated tree would silently mix two versions' sources.
        print(f"  clearing incomplete workspace: {dest}", file=sys.stderr)
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"  fetching Minecraft {version.id} (server) -> {dest}\n"
        f"  first run downloads and decompiles; later runs reuse this tree.",
        file=sys.stderr,
    )

    cancel = threading.Event()
    previous_sigint = signal.getsignal(signal.SIGINT)

    def _on_sigint(_signum, _frame):
        if not cancel.is_set():
            print("\n  cancelling...", file=sys.stderr, flush=True)
            cancel.set()

    progress = _Progress(quiet=quiet)
    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except ValueError:
        previous_sigint = None      # not on the main thread; Ctrl-C stays default

    try:
        provider.install(version, str(dest), progress=progress, cancel=cancel)
    except decompiled.Cancelled:
        progress.done()
        raise FetchUnavailable(
            f"Cancelled. The partial tree at {dest} is ignored on the next run."
        ) from None
    except decompiled.ProviderError as exc:
        progress.done()
        raise FetchUnavailable(f"Fetch failed: {exc}") from exc
    finally:
        progress.done()
        if previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)

    if not (dest / COMPLETION_MARKER).is_file():
        raise FetchUnavailable(
            f"Fetch reported success but {COMPLETION_MARKER.as_posix()} is not in "
            f"{dest}. The layout may have changed -- check the tree by hand."
        )
    print(f"  workspace ready: {dest}", file=sys.stderr)
    return dest


def main(argv=None) -> int:
    """Fetching on its own, for warming the cache or checking the plumbing."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help=f"default: {default_cache_dir()}")
    parser.add_argument("--force", action="store_true",
                        help="re-decompile even if the cache already has it")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        print(ensure_workspace(args.cache_dir, force=args.force, quiet=args.quiet))
    except FetchUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
