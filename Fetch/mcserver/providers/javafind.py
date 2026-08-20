"""Locating usable JDKs.

BuildTools is picky in a way nothing else here is: each Minecraft version can
only be compiled by a JDK inside a specific range. Building 1.8.8 under Java 21
fails deep inside Maven with an unhelpful error, so we detect what's installed
and pick a match *before* starting a twenty-minute compile.

Spigot expresses the range as class-file major versions (52 = Java 8), so the
conversion is `java_version = class_major - 44`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable, Optional

CLASSFILE_OFFSET = 44

# Silences the console window that Popen would otherwise flash on Windows.
_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


@dataclass(frozen=True)
class Jdk:
    path: str          # path to the java executable
    major: int         # 8, 17, 21, ...
    raw: str           # full version string as reported
    has_javac: bool    # a JRE cannot compile; BuildTools needs a real JDK

    def __str__(self) -> str:
        kind = "JDK" if self.has_javac else "JRE"
        return f"Java {self.major} ({kind}) - {self.path}"


def classfile_to_java(major: int) -> int:
    return major - CLASSFILE_OFFSET


def java_range_from_classfile(pair: Iterable[int]) -> tuple[int, int]:
    vals = [classfile_to_java(v) for v in pair]
    return min(vals), max(vals)


def _parse_version(text: str) -> Optional[int]:
    """`java -version` writes to stderr and has two historical formats.

    Legacy: 1.8.0_402  -> 8
    Modern: 17.0.9     -> 17
    """
    m = re.search(r'version "(\d+)(?:\.(\d+))?[^"]*"', text)
    if not m:
        return None
    major, minor = int(m.group(1)), m.group(2)
    if major == 1 and minor is not None:
        return int(minor)  # 1.8 -> 8
    return major


def probe_java(exe: str) -> Optional[Jdk]:
    try:
        proc = subprocess.run(
            [exe, "-version"], capture_output=True, text=True, timeout=15, **_NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = (proc.stderr or "") + (proc.stdout or "")
    major = _parse_version(blob)
    if major is None:
        return None

    javac = os.path.join(os.path.dirname(exe), "javac" + (".exe" if sys.platform == "win32" else ""))
    return Jdk(path=exe, major=major, raw=blob.strip().splitlines()[0], has_javac=os.path.exists(javac))


def _candidate_dirs() -> list[str]:
    """Common JDK install roots, so we find Javas that aren't on PATH."""
    roots: list[str] = []
    if sys.platform == "win32":
        for base in filter(None, [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]):
            for vendor in ("Java", "Eclipse Adoptium", "Microsoft", "Amazon Corretto",
                           "AdoptOpenJDK", "Zulu", "BellSoft", "Eclipse Foundation"):
                roots.append(os.path.join(base, vendor))
    elif sys.platform == "darwin":
        roots.append("/Library/Java/JavaVirtualMachines")
        roots.append(os.path.expanduser("~/Library/Java/JavaVirtualMachines"))
    else:
        roots += ["/usr/lib/jvm", "/usr/java", "/opt/java", os.path.expanduser("~/.sdkman/candidates/java")]
    return [r for r in roots if os.path.isdir(r)]


def find_jdks() -> list[Jdk]:
    """Every distinct Java we can find, newest first. Results are de-duplicated."""
    exe_name = "java.exe" if sys.platform == "win32" else "java"
    seen: dict[tuple[int, bool], Jdk] = {}
    candidates: list[str] = []

    on_path = shutil.which("java")
    if on_path:
        candidates.append(on_path)

    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(os.path.join(java_home, "bin", exe_name))

    for root in _candidate_dirs():
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            for sub in (("bin", exe_name), ("Contents", "Home", "bin", exe_name)):
                candidates.append(os.path.join(root, entry, *sub))

    for exe in candidates:
        if not os.path.exists(exe):
            continue
        jdk = probe_java(exe)
        if not jdk:
            continue
        key = (jdk.major, jdk.has_javac)
        # Prefer whichever we saw first; PATH/JAVA_HOME are checked first on purpose.
        seen.setdefault(key, jdk)

    out = list(seen.values())
    out.sort(key=lambda j: (j.major, j.has_javac), reverse=True)
    return out


def select_jdk(lo: int, hi: int, jdks: Optional[list[Jdk]] = None) -> Optional[Jdk]:
    """Best JDK within [lo, hi]. Prefers a real JDK, then the newest in range."""
    pool = jdks if jdks is not None else find_jdks()
    fits = [j for j in pool if lo <= j.major <= hi]
    if not fits:
        return None
    fits.sort(key=lambda j: (j.has_javac, j.major), reverse=True)
    return fits[0]


def describe_missing(lo: int, hi: int, jdks: list[Jdk]) -> str:
    want = f"Java {lo}" if lo == hi else f"Java {lo}-{hi}"
    if not jdks:
        return (
            f"No Java installation was found. Building Spigot needs a JDK "
            f"({want}) and Git on PATH."
        )
    have = ", ".join(sorted({str(j.major) for j in jdks}, key=int))
    return (
        f"This version must be compiled with {want}, but the only Java "
        f"versions found are: {have}.\n\n"
        f"Install a matching JDK (adoptium.net) and try again."
    )
