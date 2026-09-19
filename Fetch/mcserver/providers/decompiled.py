"""Decompiled Minecraft workspace provider.

Standalone: `python -m mcserver.providers.decompiled` prints the version list.

This core is the odd one out. Every other provider ends with "a server you can
run"; this one ends with "a source tree you can edit, compile and launch". The
client is the interesting side -- that is where you want a breakpoint -- so
client and server are separate rows in the version dropdown rather than a
setting hidden somewhere else.

What changed since the MCP / gradlew-setupDecompWorkspace days
--------------------------------------------------------------
Three eras, and the provider has to straddle all of them, so the whole design
is organised around one question: *how do we get from the jar Mojang ships to
readable names?*

  1. Pre-1.14.4 -- no official mappings at all. Community mappings only. We use
     Legacy Fabric's yarn (which covers 1.3 -> 1.13.2) applied with
     tiny-remapper. This is the only path that depends on a third party.

  2. 1.14.4 -> the last obfuscated build -- Mojang publishes ProGuard mapping
     files next to the jar in the version JSON (`downloads.client_mappings`).
     Those are *named -> obfuscated*, and SpecialSource wants TSRG going the
     other way, so `proguard_to_tsrg()` below inverts them. This is what
     DecompilerMC did, and it is still correct for this range.

  3. Post-deobfuscation -- Mojang removed obfuscation from Java Edition; the
     first snapshot after Mounts of Mayhem ships client and server jars with
     the original names, including parameter names, and drops the mapping
     entries from the version JSON entirely. There is nothing to remap. The jar
     goes straight to the decompiler.

Rather than hard-coding a cutoff date that will be wrong the moment Mojang
backfills something, `_mapping_plan()` decides by *looking at the jar*: if most
of its classes already live under `net/minecraft/`, it is era 3. Mappings in
the version JSON mean era 2. Anything else falls through to yarn.

Fernflower and CFR have both been superseded by Vineflower for this job -- it
is a Fernflower fork whose output on Minecraft is close enough to the original
source to be worth the extra minute it costs.

What lands in the destination folder
------------------------------------
    src/main/java/         decompiled, editable sources
    src/main/resources/    everything in the jar that wasn't a class
    libs/                  dependency jars, plus the remapped Minecraft jar
    natives/              extracted LWJGL natives (client only)
    assets/                Minecraft asset objects + index (client only)
    run/                   working directory for the launch configs
    mappings/              whatever mapping file was used, kept for reference
    build.gradle, settings.gradle
    .project, .classpath, *.launch    -- Eclipse imports this with no Gradle
    start-client.(bat|sh) or start-server.(bat|sh)
    WORKSPACE.md

The remapped Minecraft jar is deliberately left on the classpath *after* the
compiled sources. Decompiler output for a whole game is never 100% javac-clean;
when a handful of files refuse to compile you delete them and the original
classes are picked up from the jar instead of the workspace becoming unusable.
That escape hatch is the difference between "a source dump" and "a modding
environment".
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from typing import Iterable, Optional, Sequence

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
from .vanilla import VanillaProvider

# A stage of our own. base.py explicitly allows this -- the GUI just shows the
# message and does not switch on the name.
STAGE_DECOMPILE = "decompile"

SIDE_CLIENT = "client"
SIDE_SERVER = "server"

# How a given version's names get recovered. See the module docstring.
MAP_NONE = "none"       # jar already ships unobfuscated
MAP_MOJANG = "mojang"   # official ProGuard mappings in the version JSON
MAP_YARN = "yarn"       # community mappings, pre-1.14.4

MAVEN_CENTRAL = "https://repo1.maven.org/maven2"
FABRIC_MAVEN = "https://maven.fabricmc.net"
LEGACY_FABRIC_MAVEN = "https://repo.legacyfabric.net/legacyfabric"
LEGACY_FABRIC_META = "https://meta.legacyfabric.net/v2"
FABRIC_META = "https://meta.fabricmc.net/v2"
RESOURCES_BASE = "https://resources.download.minecraft.net"

# Pinned as a floor, not a ceiling: `_maven_latest` asks the repository what the
# newest release is and only falls back to these when it cannot ask. Pinning
# alone would rot; resolving alone would break offline.
TOOL_VINEFLOWER = ("org.vineflower", "vineflower", "1.12.0", None, MAVEN_CENTRAL)
TOOL_SPECIALSOURCE = ("net.md-5", "SpecialSource", "1.11.6", "shaded", MAVEN_CENTRAL)
TOOL_TINY_REMAPPER = ("net.fabricmc", "tiny-remapper", "0.14.0", "fat", FABRIC_MAVEN)

# Annotations Minecraft is compiled against but does not ship. They have CLASS
# retention, so they survive into the bytecode and come back out of the
# decompiler as imports -- but their jars are build-time only and appear nowhere
# in the version JSON. (group, artifact, pinned fallback version)
COMPILE_EXTRAS = [
    # javax.annotation.CheckReturnValue, javax.annotation.concurrent.Immutable
    ("com.google.code.findbugs", "jsr305", "3.0.2"),
    # org.jetbrains.annotations.Contract, @NotNull, @Nullable
    ("org.jetbrains", "annotations", "26.0.2"),
]

# How many compile-and-quarantine rounds to allow, and how much of the tree may
# be quarantined before we call it a failed decompile rather than a tidy-up.
# Removing a file breaks its dependents, so this converges downwards -- but if
# it is eating the game, something upstream went wrong and silence would be the
# worst possible answer.
REPAIR_MAX_ROUNDS = 12
REPAIR_MAX_FRACTION = 0.05

# Mojang's first server jar shipped with 1.2.5. Everything older is client-only,
# so the Server rows are simply not offered rather than failing at install time.
FIRST_SERVER_RELEASE = "2012-03-29"

# Decompiling the whole game is memory-hungry; below this Vineflower spends its
# time in GC instead of decompiling.
DECOMPILE_HEAP = "3G"

_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

# Windows caps a command line at ~32k. Vineflower gets one -e= per library and
# a modern client has enough of them to get close, so past this we hand it a
# java @argfile instead.
_ARGV_LIMIT = 30000


# --------------------------------------------------------------------------
# cache / download plumbing
# --------------------------------------------------------------------------

def _cache_dir(*parts: str) -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "mc-server-manager", "decompiled", *parts)
    os.makedirs(d, exist_ok=True)
    return d


def _sha1_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(
    url: str,
    dest: str,
    *,
    sha1: Optional[str] = None,
    size: Optional[int] = None,
    label: str = "",
    progress: Optional[ProgressCB] = None,
    cancel: Optional[threading.Event] = None,
    stage: str = STAGE_DOWNLOAD,
) -> str:
    """Download to `dest`, atomically, verifying sha1/size when known.

    Returns `dest`. A file that is already present and matches is left alone --
    this is what makes a second install of the same version take seconds, and
    what makes a cancelled asset download resumable.
    """
    if os.path.exists(dest):
        if sha1:
            if _sha1_file(dest) == sha1:
                return dest
        elif size is None or os.path.getsize(dest) == size:
            return dest

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    # Unique per call: the asset pass runs sixteen of these at once and two
    # entries in the index can point at the same content hash.
    tmp = f"{dest}.{os.getpid()}.{threading.get_ident()}.part"
    digest = hashlib.sha1()
    got = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as out:
            total = size or int(resp.headers.get("Content-Length") or 0) or None
            while True:
                check_cancelled(cancel)
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                if progress and label:
                    progress(stage, got, total, f"{label} ({got:,} bytes)")
    except Cancelled:
        _quiet_remove(tmp)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _quiet_remove(tmp)
        raise ProviderError(f"Could not download {url}: {exc}") from None

    if sha1 and digest.hexdigest() != sha1:
        _quiet_remove(tmp)
        raise ProviderError(f"SHA-1 mismatch downloading {os.path.basename(dest)}.")
    if size and got != size:
        _quiet_remove(tmp)
        raise ProviderError(f"Size mismatch downloading {os.path.basename(dest)}.")

    os.replace(tmp, dest)
    return dest


def _quiet_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _maven_path(group: str, artifact: str, ver: str, classifier: Optional[str]) -> str:
    tail = f"{artifact}-{ver}" + (f"-{classifier}" if classifier else "") + ".jar"
    return "/".join(group.split(".") + [artifact, ver, tail])


def _maven_latest(base: str, group: str, artifact: str, fallback: str) -> str:
    """Newest release from maven-metadata.xml, or `fallback` when offline."""
    url = f"{base}/{'/'.join(group.split('.'))}/{artifact}/maven-metadata.xml"
    try:
        root = ET.fromstring(fetch(url, timeout=12, retries=1))
    except (ProviderError, ET.ParseError):
        return fallback
    for tag in ("release", "latest"):
        node = root.find(f"./versioning/{tag}")
        if node is not None and (node.text or "").strip():
            return node.text.strip()
    return fallback


# --------------------------------------------------------------------------
# ProGuard -> TSRG
# --------------------------------------------------------------------------

_PRIMITIVES = {
    "void": "V", "boolean": "Z", "byte": "B", "char": "C",
    "short": "S", "int": "I", "long": "J", "float": "F", "double": "D",
}

_PG_CLASS = re.compile(r"^(?P<named>[\w.$]+) -> (?P<obf>[\w.$]+):$")
_PG_FIELD = re.compile(r"^\s+(?P<type>[\w.$\[\]]+) (?P<named>[\w$]+) -> (?P<obf>[\w$]+)$")
_PG_METHOD = re.compile(
    r"^\s+(?:\d+:\d+:)?(?P<ret>[\w.$\[\]]+) (?P<named>[\w$<>]+)"
    # `<init>` / `<clinit>` appear on both sides, so the obfuscated group has to
    # accept angle brackets too or every constructor is silently dropped.
    r"\((?P<args>[\w.$\[\], ]*)\)(?::\d+(?::\d+)?)? -> (?P<obf>[\w$<>]+)$"
)


def _type_descriptor(java_type: str, classes: dict) -> str:
    """`java.lang.String[]` -> `[Ljava/lang/String;`, obfuscating the class."""
    java_type = java_type.strip()
    arrays = 0
    while java_type.endswith("[]"):
        arrays += 1
        java_type = java_type[:-2]
    if java_type in _PRIMITIVES:
        core = _PRIMITIVES[java_type]
    else:
        core = "L" + classes.get(java_type, java_type).replace(".", "/") + ";"
    return "[" * arrays + core


def proguard_to_tsrg(text: str) -> str:
    """Invert Mojang's ProGuard mappings into the TSRG SpecialSource wants.

    Mojang publishes `named -> obfuscated`; SpecialSource remaps *from* the
    obfuscated jar, so every line has to be flipped, and a method's descriptor
    has to be rewritten in terms of obfuscated class names -- which means the
    class table must be complete before any member line can be emitted. Hence
    two passes over the file rather than one.
    """
    lines = [line.rstrip() for line in text.splitlines()]

    classes: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#") or line[0].isspace():
            continue
        m = _PG_CLASS.match(line)
        if m:
            classes[m.group("named")] = m.group("obf")

    out: list[str] = []
    current_ok = False
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if not line[0].isspace():
            m = _PG_CLASS.match(line)
            current_ok = bool(m)
            if m:
                out.append(
                    f"{m.group('obf').replace('.', '/')} "
                    f"{m.group('named').replace('.', '/')}"
                )
            continue
        if not current_ok:
            continue

        m = _PG_METHOD.match(line)
        if m:
            args = [a for a in m.group("args").split(",") if a.strip()]
            desc = (
                "(" + "".join(_type_descriptor(a, classes) for a in args) + ")"
                + _type_descriptor(m.group("ret"), classes)
            )
            out.append(f"\t{m.group('obf')} {desc} {m.group('named')}")
            continue

        m = _PG_FIELD.match(line)
        if m:
            out.append(f"\t{m.group('obf')} {m.group('named')}")

    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# version-JSON rules, libraries, natives
# --------------------------------------------------------------------------

def _os_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "osx"
    return "linux"


def _os_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("i386", "i686", "x86"):
        return "x86"
    return machine


def _rule_matches(rule: dict) -> bool:
    """True when a version-JSON rule's conditions apply to this machine.

    Feature rules (`is_demo_user`, `has_custom_resolution`, quick-play) are
    always treated as false: those arguments exist for the real launcher's UI
    and a development workspace never wants them.
    """
    if rule.get("features"):
        return False
    os_block = rule.get("os")
    if not os_block:
        return True
    if "name" in os_block and os_block["name"] != _os_name():
        return False
    if "arch" in os_block and os_block["arch"] not in (_os_arch(), _os_arch().replace("_", "")):
        return False
    if "version" in os_block:
        try:
            if not re.search(os_block["version"], platform.release()):
                return False
        except re.error:
            pass
    return True


def _rules_allow(rules: Optional[Sequence[dict]]) -> bool:
    if not rules:
        return True
    allowed = False
    for rule in rules:
        if _rule_matches(rule):
            allowed = rule.get("action") == "allow"
    return allowed


def _natives_classifier(lib: dict) -> Optional[str]:
    """Pre-1.19 style: `natives: {windows: "natives-windows-${arch}"}`."""
    natives = lib.get("natives")
    if not natives:
        return None
    template = natives.get(_os_name())
    if not template:
        return None
    bits = "64" if _os_arch() in ("x86_64", "arm64") else "32"
    return template.replace("${arch}", bits)


def _library_artifacts(meta: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """-> (every artifact, this machine's artifacts, this machine's natives).

    The first two differ, and the difference is a compile error if you collapse
    them. A workspace has to *compile* code guarded by `os` rules that will
    never *run* here: `MacosUtil` imports `ca.weblite.objc`, whose jar the
    version JSON marks macOS-only, so filtering the classpath by rule at install
    time means javac cannot see it and the file fails on Windows. Everything
    goes into libs/; the rules only decide which natives get unpacked.

    Both native shapes are handled: the modern one where a native is just
    another library entry gated by an `os` rule, and the pre-1.19 one where a
    single entry carries a `natives` map into `downloads.classifiers`.
    """
    every: list[dict] = []
    mine: list[dict] = []
    natives: list[dict] = []
    for lib in meta.get("libraries", []):
        downloads = lib.get("downloads", {})
        artifact = downloads.get("artifact")
        allowed = _rules_allow(lib.get("rules"))
        if artifact and artifact.get("url"):
            every.append(artifact)
            if allowed:
                mine.append(artifact)
        if not allowed:
            continue
        classifier = _natives_classifier(lib)
        if classifier:
            native = (downloads.get("classifiers") or {}).get(classifier)
            if native and native.get("url"):
                natives.append(native)
    return every, mine, natives


# --------------------------------------------------------------------------
# jars
# --------------------------------------------------------------------------

def _jar_looks_unobfuscated(jar_path: str) -> bool:
    """Heuristic era-3 detector: are the class names already the real ones?

    An obfuscated client keeps a handful of entry points under `net/minecraft/`
    (`net/minecraft/client/main/Main` survives so the launcher can find it), so
    "does net/minecraft exist" is not the question. The question is what share
    of the *whole* jar lives in real packages -- obfuscated jars dump thousands
    of one-letter classes at the root, unobfuscated ones put essentially
    everything under net/minecraft or com/mojang.
    """
    named = total = 0
    try:
        with zipfile.ZipFile(jar_path) as zf:
            for name in zf.namelist():
                if not name.endswith(".class"):
                    continue
                total += 1
                if name.startswith(("net/minecraft/", "com/mojang/")):
                    named += 1
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProviderError(f"{os.path.basename(jar_path)} is not a readable jar: {exc}")
    if total == 0:
        raise ProviderError(f"{os.path.basename(jar_path)} contains no classes.")
    return named / total > 0.5


def _manifest_attrs(jar_path: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    try:
        with zipfile.ZipFile(jar_path) as zf:
            raw = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except (KeyError, OSError, zipfile.BadZipFile):
        return attrs
    for line in raw.splitlines():
        if ": " in line:
            key, _, val = line.partition(": ")
            attrs[key.strip()] = val.strip()
    return attrs


def _unbundle_server(jar_path: str, work_dir: str) -> tuple[str, list[str], Optional[str]]:
    """Since 1.18 `server.jar` is a bundler, not the server.

    It carries the real server jar and its dependencies under META-INF/, listed
    in versions.list / libraries.list. Decompiling the bundler itself gets you
    about forty classes of extraction logic, which is a confusing way to fail,
    so unwrap it first. Returns (server jar, library jars, main class) and
    passes non-bundler jars straight through.
    """
    try:
        zf = zipfile.ZipFile(jar_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProviderError(f"Could not open {os.path.basename(jar_path)}: {exc}")

    with zf:
        names = set(zf.namelist())
        if "META-INF/versions.list" not in names:
            return jar_path, [], None

        os.makedirs(work_dir, exist_ok=True)

        def entries(list_name: str, root: str) -> list[str]:
            if list_name not in names:
                return []
            out = []
            raw = zf.read(list_name).decode("utf-8", "replace")
            for line in raw.splitlines():
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                inner = f"{root}/{parts[2]}"
                if inner not in names:
                    continue
                dest = os.path.join(work_dir, os.path.basename(parts[2]))
                if not os.path.exists(dest):
                    with zf.open(inner) as src, open(dest, "wb") as out_fh:
                        shutil.copyfileobj(src, out_fh)
                out.append(dest)
            return out

        versions = entries("META-INF/versions.list", "META-INF/versions")
        libraries = entries("META-INF/libraries.list", "META-INF/libraries")

    if not versions:
        raise ProviderError(
            "The server jar looks like a bundler but contains no bundled server."
        )
    main_class = _manifest_attrs(jar_path).get("Launcher-Main-Class")
    return versions[0], libraries, main_class


# META-INF entries that make a jar *signed*. The manifest is handled separately
# because it has to survive -- only its per-entry digest sections go.
_SIGNATURE_FILE = re.compile(
    r"^META-INF/(?:[^/]+\.(?:SF|RSA|DSA|EC)|SIG-[^/]+)$", re.IGNORECASE
)


def _manifest_main_section(raw: bytes) -> bytes:
    """Drop everything after the manifest's first blank line.

    A signed jar's manifest is the main attributes, then one `Name:` +
    `SHA-*-Digest:` section per entry -- 4.6 MB of them for the Minecraft
    client. Those digests are what the JVM checks a class against, so they go
    with the signature files.
    """
    for sep in (b"\r\n\r\n", b"\n\n", b"\r\r"):
        index = raw.find(sep)
        if index != -1:
            return raw[:index] + sep[: len(sep) // 2]
    return raw


def unsign_jar(path: str) -> bool:
    """Strip a jar's signature in place. -> True if it was signed.

    Mojang signs the game jars, and we deliberately keep one on the classpath
    *after* the compiled sources so a quarantined file still resolves. That mix
    is exactly what the JVM refuses:

        java.lang.SecurityException: class "net.minecraft.util.ARGB"'s signer
        information does not match signer information of other classes in the
        same package

    Once a package has been defined by a class out of the signed jar, every
    later class in that package must carry the same signer -- and ours, freshly
    compiled from source, carry none. The package cannot be split.

    Removing the signature rather than the jar keeps the fallback working, and
    is the same step every Minecraft workspace toolchain has always done. It is
    not a security downgrade in any meaningful sense: the jar was already
    verified by SHA-1 against the version manifest when it was downloaded.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            signed = any(_SIGNATURE_FILE.match(n) for n in zf.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProviderError(f"Could not read {os.path.basename(path)}: {exc}")
    if not signed:
        return False

    tmp = path + ".unsigning"
    try:
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(
            tmp, "w", zipfile.ZIP_DEFLATED
        ) as out:
            for info in src.infolist():
                if _SIGNATURE_FILE.match(info.filename):
                    continue
                data = src.read(info.filename)
                if info.filename.upper() == "META-INF/MANIFEST.MF":
                    data = _manifest_main_section(data)
                # Reuse the entry so timestamps and per-entry compression
                # survive; only the size fields need recomputing.
                entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                entry.compress_type = info.compress_type
                entry.external_attr = info.external_attr
                out.writestr(entry, data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            _quiet_remove(tmp)
    return True


def _extract_resources(jar_path: str, dest: str) -> int:
    """Everything in the jar that is not a class, so the workspace can run.

    Assets, data packs, the version manifest, shaders. Without these the game
    starts and immediately dies looking for its own registries.
    """
    count = 0
    with zipfile.ZipFile(jar_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or name.endswith(".class"):
                continue
            if name.startswith("META-INF/") and name.upper().endswith(
                (".SF", ".RSA", ".DSA", "MANIFEST.MF")
            ):
                continue
            target = os.path.join(dest, *name.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


# A canonical record constructor whose entire body is `super();`.
#
# Vineflower's --hide-empty-super does not reach inside record constructors, so
# every record comes out as
#
#     public EntityDataAccessor {
#        super();
#     }
#
# and javac rejects it: "canonical constructor must not contain explicit
# constructor invocation". A compact canonical constructor with an empty body is
# indistinguishable from no declaration at all, so deleting the whole block is a
# provable no-op rather than a guess.
#
# The name is substituted per record found in the file, and the closing brace is
# matched at the same indent as the opening line, so this cannot run away into
# the rest of the class. `(?:\(\s*\))?` allows the explicit form javac emits for
# a record with no components (`public Fail() {`) while still refusing any
# constructor that actually takes parameters.
_RECORD_DECL = re.compile(r"\brecord\s+(\w+)")


def _record_super_pattern(name: str) -> "re.Pattern[str]":
    return re.compile(
        r"^([ \t]*)(?:(?:public|protected|private|static|final)[ \t]+)*"
        + re.escape(name)
        + r"[ \t]*(?:\([ \t]*\))?[ \t]*\{[ \t]*\n"
        r"[ \t]*super\(\);[ \t]*\n"
        r"\1\}[ \t]*\n",
        re.MULTILINE,
    )


def strip_record_super(text: str) -> tuple[str, int]:
    """-> (patched source, constructors removed)."""
    names = set(_RECORD_DECL.findall(text))
    removed = 0
    for name in names:
        text, count = _record_super_pattern(name).subn("", text)
        removed += count
    return text, removed


def _patch_sources(src_dir: str, cancel=None) -> tuple[int, int]:
    """Apply the record fix across the tree. -> (files changed, ctors removed)."""
    files = ctors = 0
    for root, _dirs, names in os.walk(src_dir):
        check_cancelled(cancel)
        for name in names:
            if not name.endswith(".java"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            patched, removed = strip_record_super(text)
            if removed:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(patched)
                files += 1
                ctors += removed
    return files, ctors


def _link_or_copy(src: str, dest: str) -> None:
    """Hardlink assets into the workspace, copying only when we must.

    The asset store is a gigabyte and is shared between every workspace; making
    each one a full copy turns three versions into three gigabytes for no
    reason. Hardlinks fail across drives and on some filesystems, hence the
    fallback.
    """
    if os.path.exists(dest):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.link(src, dest)
    except (OSError, AttributeError, NotImplementedError):
        shutil.copy2(src, dest)


# --------------------------------------------------------------------------
# provider
# --------------------------------------------------------------------------

class DecompiledProvider:
    """One dropdown entry per (Minecraft version, side)."""

    name = "Decompiled"
    content_dir = None
    compiles_from_source = False
    # The GUI does not read this, but it is the honest answer to "will this take
    # a while": Vineflower on a modern client is a few minutes, plus assets.
    decompiles_from_bytecode = True
    # Compile the result and quarantine what will not build, so the workspace
    # opens green instead of with two thousand errors. Costs several minutes;
    # set False for a decompile-only run.
    repair_workspace = True

    def __init__(self) -> None:
        self._vanilla = VanillaProvider()
        self._meta_cache: dict[str, dict] = {}
        self._tool_paths: dict[str, str] = {}
        self._yarn_cache: dict[str, Optional[str]] = {}

    # ---------------- index ----------------

    def list_versions(
        self, include_unstable: bool = False, progress: Optional[ProgressCB] = None
    ) -> list[Version]:
        base = self._vanilla.list_versions(include_unstable, progress=progress)
        out: list[Version] = []
        for version in base:
            out.append(
                Version(
                    id=version.id, kind=version.kind, released=version.released,
                    ref=version.ref, suffix="Client", variant=SIDE_CLIENT,
                )
            )
            # Mojang only started publishing a server jar with 1.2.5; offering a
            # Server row for Alpha would be a dropdown full of guaranteed errors.
            if (version.released or "") >= FIRST_SERVER_RELEASE:
                out.append(
                    Version(
                        id=version.id, kind=version.kind, released=version.released,
                        ref=version.ref, suffix="Server", variant=SIDE_SERVER,
                    )
                )
        return out

    # ---------------- resolve ----------------

    def _meta(self, version: Version) -> dict:
        if not version.ref:
            raise ProviderError(f"No metadata URL for {version.id}")
        if version.ref not in self._meta_cache:
            self._meta_cache[version.ref] = json.loads(fetch(version.ref))
        return self._meta_cache[version.ref]

    @staticmethod
    def _side(version: Version) -> str:
        return version.variant or SIDE_CLIENT

    def required_java(self, version: Version) -> Optional[int]:
        return self._meta(version).get("javaVersion", {}).get("majorVersion")

    def check_prerequisites(
        self, version: Version, dest_dir: Optional[str] = None
    ) -> list[str]:
        """Called by the GUI before the install starts.

        Everything here fails in an ugly place if left to run-time: no javac
        means the workspace compiles nothing, and the wrong major means the
        decompiled sources reference classes the JDK does not have.
        """
        problems: list[str] = []
        try:
            wanted = self.required_java(version)
        except ProviderError as exc:
            return [str(exc)]

        jdks = javafind.find_jdks()
        compilers = [j for j in jdks if j.has_javac]
        if not compilers:
            problems.append(
                "No JDK was found. A decompiled workspace has to be compiled, "
                "so a JRE is not enough -- install a JDK from adoptium.net."
            )
        elif wanted:
            # Newer is fine for running, but the workspace targets `wanted`, so
            # anything older cannot compile it at all.
            if not any(j.major >= wanted for j in compilers):
                have = ", ".join(sorted({str(j.major) for j in compilers}, key=int))
                problems.append(
                    f"Minecraft {version.id} needs Java {wanted} or newer to "
                    f"compile, but the only JDKs found are: {have}."
                )

        side = self._side(version)
        try:
            meta = self._meta(version)
        except ProviderError as exc:
            return problems + [str(exc)]
        if not (meta.get("downloads", {}).get(side, {}) or {}).get("url"):
            problems.append(
                f"Minecraft {version.id} has no official {side} download."
            )

        # Only meaningful once we know where it is going. The GUI currently
        # calls this with the version alone, so this stays dormant until it
        # passes the destination too -- better than measuring the wrong drive.
        if dest_dir:
            need_gb = 4 if side == SIDE_CLIENT else 2
            try:
                probe = dest_dir
                while probe and not os.path.isdir(probe):
                    parent = os.path.dirname(probe)
                    if parent == probe:
                        break
                    probe = parent
                if probe and shutil.disk_usage(probe).free < need_gb * (1 << 30):
                    problems.append(
                        f"Less than {need_gb} GB free on the target drive. A "
                        f"{side} workspace needs roughly that much once assets, "
                        "libraries, sources and class files are unpacked."
                    )
            except OSError:
                pass
        return problems

    # ---------------- mapping plan ----------------

    def _mapping_plan(self, version: Version, jar_path: str) -> tuple[str, Optional[dict]]:
        """-> (MAP_*, details). See the module docstring for the three eras."""
        side = self._side(version)
        meta = self._meta(version)

        if _jar_looks_unobfuscated(jar_path):
            return MAP_NONE, None

        official = meta.get("downloads", {}).get(f"{side}_mappings")
        if official and official.get("url"):
            return MAP_MOJANG, official

        maven = self._yarn_for(version.id)
        if maven:
            return MAP_YARN, {"maven": maven}

        raise ProviderError(
            f"Minecraft {version.id} is obfuscated, has no official mappings "
            "(those start at 1.14.4), and no community yarn mappings were "
            "found for it.\n\nIt can still be decompiled, but every name would "
            "be a single letter, which is not a modding environment."
        )

    def _yarn_for(self, mc_version: str) -> Optional[str]:
        """Newest yarn build for a version, as a `group:artifact:version` coord.

        Legacy Fabric covers 1.3 -> 1.13.2; upstream Fabric covers 1.14 and the
        few builds before official mappings existed. Both expose the same JSON,
        so one parser does for both.
        """
        if mc_version in self._yarn_cache:
            return self._yarn_cache[mc_version]

        quoted = urllib.parse.quote(mc_version, safe="")
        result: Optional[str] = None
        for meta_base in (LEGACY_FABRIC_META, FABRIC_META):
            try:
                raw = fetch(f"{meta_base}/versions/yarn/{quoted}", timeout=12, retries=1)
                builds = json.loads(raw)
            except (ProviderError, ValueError):
                continue
            if builds:
                # The meta APIs return newest first.
                result = builds[0].get("maven")
                break
        self._yarn_cache[mc_version] = result
        return result

    # ---------------- tools ----------------

    def _tool(self, spec: tuple, progress: Optional[ProgressCB], cancel) -> str:
        group, artifact, fallback, classifier, base = spec
        if artifact in self._tool_paths:
            return self._tool_paths[artifact]

        if progress:
            progress(STAGE_INDEX, 0, None, f"Resolving {artifact}...")
        ver = _maven_latest(base, group, artifact, fallback)
        rel = _maven_path(group, artifact, ver, classifier)
        dest = os.path.join(_cache_dir("tools"), os.path.basename(rel))
        _download(
            f"{base}/{rel}", dest,
            label=f"Downloading {artifact} {ver}", progress=progress, cancel=cancel,
        )
        self._tool_paths[artifact] = dest
        return dest

    # ---------------- java subprocess ----------------

    @staticmethod
    def _java_exe(wanted: Optional[int]) -> str:
        """A JDK new enough to run the tools and compile the sources."""
        jdks = javafind.find_jdks()
        compilers = [j for j in jdks if j.has_javac] or jdks
        if wanted:
            fits = sorted(
                (j for j in compilers if j.major >= wanted), key=lambda j: j.major
            )
            if fits:
                return fits[0].path
        if compilers:
            return max(compilers, key=lambda j: j.major).path
        found = shutil.which("java")
        if not found:
            raise ProviderError("No Java installation was found on this machine.")
        return found

    @staticmethod
    def _run_java(
        argv: list[str],
        *,
        cancel: Optional[threading.Event],
        on_line=None,
        what: str = "tool",
        cwd: Optional[str] = None,
    ) -> None:
        """Run a tool jar, streaming output so the GUI is not silent for minutes.

        Cancellation has to kill the child: Vineflower on a client jar will
        happily run for another three minutes after the user has given up, and
        a daemon thread cannot interrupt it.
        """
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1, cwd=cwd, **_NO_WINDOW,
            )
        except OSError as exc:
            raise ProviderError(f"Could not start {what}: {exc}") from None

        tail: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    tail.append(line)
                    del tail[:-40]
                    if on_line:
                        on_line(line)
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait(timeout=10)
                    raise Cancelled("Cancelled.")
        finally:
            if proc.stdout:
                proc.stdout.close()
        code = proc.wait()
        if code != 0:
            detail = "\n".join(tail[-12:]) or "(no output)"
            raise ProviderError(f"{what} failed (exit {code}):\n\n{detail}")

    @staticmethod
    def _run_capture(argv: list[str], cancel) -> tuple[int, str]:
        """Like `_run_java`, but hands back the output instead of raising.

        The repair loop needs javac's diagnostics on a *failed* run, which is
        exactly the case `_run_java` turns into an exception.
        """
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", **_NO_WINDOW,
            )
        except OSError as exc:
            raise ProviderError(f"Could not start javac: {exc}") from None
        chunks: list[str] = []
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            chunks.append(line)
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.wait(timeout=10)
                raise Cancelled("Cancelled.")
        proc.stdout.close()
        return proc.wait(), "".join(chunks)

    @staticmethod
    def _write_source_list(src_dir: str, path: str) -> int:
        """javac argfile listing every source, quoted and slash-separated.

        Both halves matter and they pull against each other: unquoted paths
        split on the space in "My Workspace", and inside quotes backslash is an
        escape, so C:\\Users\\Adam arrives as C:UsersAdam with the separators
        eaten. Forward slashes satisfy both at once, and Windows javac takes
        them happily.
        """
        count = 0
        with open(path, "w", encoding="utf-8") as fh:
            for root, _dirs, names in os.walk(src_dir):
                for name in sorted(names):
                    if name.endswith(".java"):
                        full = os.path.join(root, name).replace("\\", "/")
                        fh.write(f'"{full}"\n')
                        count += 1
        return count

    @staticmethod
    def _maybe_argfile(argv: list[str], work_dir: str) -> list[str]:
        """Collapse a huge command line into a java @argfile on Windows."""
        if sum(len(a) + 1 for a in argv) < _ARGV_LIMIT:
            return argv
        path = os.path.join(work_dir, "vineflower-args.txt")
        with open(path, "w", encoding="utf-8") as fh:
            for arg in argv[1:]:
                fh.write('"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"\n')
        return [argv[0], "@" + path]


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

        side = self._side(version)
        notes: list[str] = []

        report(STAGE_RESOLVE, 0, None, f"Resolving Minecraft {version.id} ({side})...")
        meta = self._meta(version)
        java_major = meta.get("javaVersion", {}).get("majorVersion")
        java_exe = self._java_exe(java_major)

        dl = meta.get("downloads", {}).get(side)
        if not dl or not dl.get("url"):
            raise ProviderError(
                f"Minecraft {version.id} has no official {side} download."
            )

        dest_dir = os.path.abspath(dest_dir)
        os.makedirs(dest_dir, exist_ok=True)
        src_dir = os.path.join(dest_dir, "src", "main", "java")
        res_dir = os.path.join(dest_dir, "src", "main", "resources")
        libs_dir = os.path.join(dest_dir, "libs")
        natives_dir = os.path.join(dest_dir, "natives")
        assets_dir = os.path.join(dest_dir, "assets")
        run_dir = os.path.join(dest_dir, "run")
        maps_dir = os.path.join(dest_dir, "mappings")
        work_dir = os.path.join(dest_dir, ".decompiled")
        for d in (src_dir, res_dir, libs_dir, run_dir, maps_dir, work_dir):
            os.makedirs(d, exist_ok=True)

        # ---- 1. the jar ----
        raw_jar = os.path.join(
            _cache_dir("versions", version.id), f"{side}.jar"
        )
        _download(
            dl["url"], raw_jar, sha1=dl.get("sha1"), size=dl.get("size"),
            label=f"Downloading {side}.jar", progress=progress, cancel=cancel,
        )
        check_cancelled(cancel)

        bundled_libs: list[str] = []
        server_main: Optional[str] = None
        if side == SIDE_SERVER:
            report(STAGE_RESOLVE, 0, None, "Checking for a bundled server...")
            raw_jar, bundled_libs, server_main = _unbundle_server(
                raw_jar, os.path.join(work_dir, "bundle")
            )
            if bundled_libs:
                notes.append(
                    f"{version.id} ships a bundler jar; the real server and its "
                    f"{len(bundled_libs)} libraries were unpacked from it."
                )

        # ---- 2. libraries (needed before decompiling: they feed its classpath) ----
        report(STAGE_DOWNLOAD, 0, None, "Downloading libraries...")
        lib_jars = self._fetch_libraries(
            meta, side, bundled_libs, libs_dir, natives_dir, progress, cancel
        )

        # ---- 3. mappings + remap ----
        plan, details = self._mapping_plan(version, raw_jar)
        remapped = self._remap(
            version, side, plan, details, raw_jar, libs_dir, maps_dir, work_dir,
            java_exe, lib_jars, progress, cancel, notes,
        )

        # ---- 4. decompile ----
        report(STAGE_DECOMPILE, 0, None, "Starting Vineflower...")
        self._decompile(
            remapped, src_dir, work_dir, java_exe, lib_jars, progress, cancel
        )
        check_cancelled(cancel)

        report(STAGE_FINALIZE, 0, None, "Extracting non-class resources...")
        extracted = _extract_resources(remapped, res_dir)

        # ---- 4b. mechanical source fixes ----
        report(STAGE_BUILD, 0, None, "Patching record constructors...")
        patched_files, patched_ctors = _patch_sources(src_dir, cancel)
        if patched_ctors:
            notes.append(
                f"Removed {patched_ctors:,} empty `super();` record constructors "
                f"across {patched_files:,} files -- Vineflower emits them and "
                "javac rejects them; deleting them changes nothing."
            )

        # ---- 5. assets (client only) ----
        if side == SIDE_CLIENT:
            self._fetch_assets(meta, assets_dir, run_dir, progress, cancel)

        # ---- 6. workspace ----
        report(STAGE_FINALIZE, 0, None, "Writing project files...")
        main_class = self._main_class(meta, side, remapped, server_main)
        game_args = self._game_arguments(meta, side, dest_dir)
        jvm_args = self._jvm_arguments(meta, side, dest_dir)

        project = os.path.basename(dest_dir.rstrip(os.sep)) or "minecraft"
        self._write_gradle(dest_dir, project, version, side, java_major, main_class,
                           game_args, jvm_args)
        self._write_eclipse(dest_dir, project, side, java_major, lib_jars, remapped,
                            main_class, game_args, jvm_args)
        self._write_scripts(dest_dir, side, java_major, main_class, game_args, jvm_args)
        self._write_readme(dest_dir, version, side, plan, main_class, java_major)

        # ---- 7. compile, and set aside whatever will not build ----
        if self.repair_workspace:
            report(STAGE_BUILD, 0, None, "Compiling the workspace...")
            rounds, quarantined, remaining = self._repair_workspace(
                dest_dir, java_exe, libs_dir, progress, cancel
            )
            if quarantined:
                moved = []
                qdir = os.path.join(work_dir, "quarantine")
                for root, _dirs, names in os.walk(qdir):
                    for name in names:
                        moved.append(
                            os.path.relpath(os.path.join(root, name), qdir)
                            .replace("\\", "/")
                        )
                self._write_quarantine_list(dest_dir, moved)
                notes.append(
                    f"{remaining:,} of {remaining + quarantined:,} sources compile. "
                    f"{quarantined} that the decompiler could not render as valid "
                    "Java were moved to .decompiled/quarantine/ (listed in "
                    "quarantine.txt); their original classes still load from libs/, "
                    f"so nothing is missing at runtime. Took {rounds} passes."
                )
            else:
                notes.append(
                    f"All {remaining:,} sources compile cleanly on the first pass."
                )

        if side == SIDE_SERVER:
            eula_path = os.path.join(run_dir, "eula.txt")
            if accept_eula:
                with open(eula_path, "w", encoding="utf-8") as fh:
                    fh.write(
                        "# Accepted via mc-server-manager on the user's behalf.\n"
                        "# https://aka.ms/MinecraftEULA\n"
                        "eula=true\n"
                    )
            elif not os.path.exists(eula_path):
                notes.append(
                    "run/eula.txt was not written. The server will exit on first "
                    "launch until you accept the Minecraft EULA."
                )

        notes.insert(0, {
            MAP_NONE: "This build ships unobfuscated -- no remapping was needed, "
                      "and the sources carry Mojang's own parameter names.",
            MAP_MOJANG: "Remapped with Mojang's official mappings. Local variable "
                        "names are not published, so those are decompiler guesses.",
            MAP_YARN: "Remapped with community yarn mappings (no official ones "
                      "exist this far back).",
        }[plan])
        notes.append(f"{extracted:,} resources and {len(lib_jars)} libraries in place.")
        notes.append(
            "Decompiler output is not guaranteed to compile as-is. Any file that "
            "will not build can simply be deleted -- the original class is still "
            "on the classpath in libs/, so the workspace keeps running."
        )
        script = "start-client" if side == SIDE_CLIENT else "start-server"
        notes.append(
            f"Import this folder into Eclipse and press Run, or use {script}.bat "
            f"/ ./{script}.sh. See WORKSPACE.md."
        )
        if java_major:
            notes.append(f"The workspace targets Java {java_major}.")

        # `bin` first so edited sources win, libs/* last so anything you had to
        # delete still resolves from the original jar -- the same ordering the
        # Gradle and Eclipse configs use.
        classpath = os.pathsep.join([
            os.path.join(dest_dir, "bin"),
            os.path.join(dest_dir, "src", "main", "resources"),
            os.path.join(libs_dir, "*"),
        ])
        return InstallResult(
            server_dir=dest_dir,
            jar_path=remapped,
            launch_argv=["{java}", "-Xms{min_ram}", "-Xmx{max_ram}", *jvm_args,
                         "-cp", classpath, main_class, *game_args],
            java_major=java_major,
            notes=notes,
        )

    # ---------------- install steps ----------------

    def _fetch_libraries(
        self, meta: dict, side: str, bundled: list[str], libs_dir: str,
        natives_dir: str, progress: Optional[ProgressCB], cancel,
    ) -> list[str]:
        """Dependency jars, plus natives unpacked where the JVM can find them."""
        def report(done, total, msg):
            if progress:
                progress(STAGE_DOWNLOAD, done, total, msg)

        jars: list[str] = []

        # A bundled server carries its own dependency set and does not use the
        # client's `libraries` list, which is full of LWJGL it will never load.
        for path in bundled:
            dest = os.path.join(libs_dir, os.path.basename(path))
            if not os.path.exists(dest):
                shutil.copy2(path, dest)
            jars.append(dest)

        if bundled:
            return jars

        every, mine, natives = _library_artifacts(meta)
        # `mine` is a subset of `every`, so remember which downloaded jars are
        # actually for this platform -- unpacking a .dylib on Windows because
        # the macOS jar is now on the compile classpath would be a regression.
        runtime_urls = {a["url"] for a in mine}
        runtime_jars: list[str] = []

        total = len(every) + len(natives) + len(COMPILE_EXTRAS)
        for index, artifact in enumerate(every, 1):
            check_cancelled(cancel)
            name = os.path.basename(artifact.get("path") or artifact["url"])
            dest = os.path.join(libs_dir, name)
            _download(
                artifact["url"], dest, sha1=artifact.get("sha1"),
                size=artifact.get("size"), cancel=cancel,
            )
            jars.append(dest)
            if artifact["url"] in runtime_urls:
                runtime_jars.append(dest)
            report(index, total, f"Downloading libraries... ({index}/{total})")

        jars += self._fetch_compile_extras(
            libs_dir, len(every), total, progress, cancel
        )

        if natives:
            os.makedirs(natives_dir, exist_ok=True)
        for index, artifact in enumerate(natives, len(every) + len(COMPILE_EXTRAS) + 1):
            check_cancelled(cancel)
            name = os.path.basename(artifact.get("path") or artifact["url"])
            bundle = os.path.join(_cache_dir("natives"), name)
            _download(
                artifact["url"], bundle, sha1=artifact.get("sha1"),
                size=artifact.get("size"), cancel=cancel,
            )
            self._extract_natives(bundle, natives_dir)
            report(index, total, f"Unpacking natives... ({index}/{total})")

        # Modern versions ship natives as ordinary library jars whose names say
        # so; the JVM still needs the .dll/.so/.dylib on disk, not in a jar.
        # Only the rule-allowed ones -- `runtime_jars`, not `jars`.
        if not natives:
            for jar in runtime_jars:
                if re.search(r"natives-(windows|linux|macos|osx)", os.path.basename(jar)):
                    os.makedirs(natives_dir, exist_ok=True)
                    self._extract_natives(jar, natives_dir)

        return jars

    def _fetch_compile_extras(
        self, libs_dir: str, done: int, total: int,
        progress: Optional[ProgressCB], cancel,
    ) -> list[str]:
        """Annotation jars the game compiles against but never ships.

        `@Contract`, `@Immutable` and `@CheckReturnValue` have CLASS retention,
        so they survive into the bytecode and Vineflower faithfully writes the
        imports back out -- but the jars that declare them are build-time only
        and appear nowhere in the version JSON. Without these, several hundred
        otherwise-perfect files fail on the import line alone.
        """
        out: list[str] = []
        for index, (group, artifact, fallback) in enumerate(COMPILE_EXTRAS, 1):
            check_cancelled(cancel)
            ver = _maven_latest(MAVEN_CENTRAL, group, artifact, fallback)
            rel = _maven_path(group, artifact, ver, None)
            dest = os.path.join(libs_dir, os.path.basename(rel))
            try:
                _download(f"{MAVEN_CENTRAL}/{rel}", dest, cancel=cancel)
            except ProviderError:
                # Resolution can land on a version whose jar is not published
                # (jsr305 has done this). The pin is the known-good answer.
                if ver == fallback:
                    raise
                rel = _maven_path(group, artifact, fallback, None)
                dest = os.path.join(libs_dir, os.path.basename(rel))
                _download(f"{MAVEN_CENTRAL}/{rel}", dest, cancel=cancel)
            out.append(dest)
            if progress:
                progress(
                    STAGE_DOWNLOAD, done + index, total,
                    f"Downloading libraries... ({done + index}/{total})",
                )
        return out

    @staticmethod
    def _extract_natives(bundle: str, natives_dir: str) -> None:
        try:
            with zipfile.ZipFile(bundle) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = os.path.basename(info.filename)
                    if not name.lower().endswith((".dll", ".so", ".dylib", ".jnilib")):
                        continue
                    target = os.path.join(natives_dir, name)
                    if os.path.exists(target):
                        continue
                    with zf.open(info) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
        except (OSError, zipfile.BadZipFile):
            pass  # a native bundle we cannot read is not worth failing the install

    def _remap(
        self, version: Version, side: str, plan: str, details: Optional[dict],
        raw_jar: str, libs_dir: str, maps_dir: str, work_dir: str, java_exe: str,
        lib_jars: list[str], progress: Optional[ProgressCB], cancel,
        notes: list[str],
    ) -> str:
        """-> path to the jar the decompiler should read."""
        def report(msg: str) -> None:
            if progress:
                progress(STAGE_BUILD, 0, None, msg)

        target = os.path.join(libs_dir, f"minecraft-{version.id}-{side}.jar")

        def finish(path: str) -> str:
            """Every path out of here goes through this.

            Mojang signs the game jars, and this one lives on the classpath
            after the compiled sources so quarantined files still resolve. A
            package cannot be half-signed, so the signature has to go or the
            game dies in <clinit> with a SecurityException. The remappers
            usually drop it as a side effect; `unsign_jar` makes that a
            guarantee rather than a hope, and is the only thing standing behind
            the MAP_NONE path, which is a straight copy.
            """
            report("Removing the jar signature...")
            if unsign_jar(path):
                notes.append(
                    "Stripped Mojang's jar signature. A signed jar and freshly "
                    "compiled classes cannot share a package -- the JVM rejects "
                    "the mismatch -- and both are on the classpath by design."
                )
            return path

        if plan == MAP_NONE:
            if not os.path.exists(target):
                shutil.copy2(raw_jar, target)
                return finish(target)
            return target

        if os.path.exists(target):
            return target

        if plan == MAP_MOJANG:
            assert details is not None
            report("Downloading official mappings...")
            proguard = os.path.join(maps_dir, f"{side}.txt")
            _download(
                details["url"], proguard, sha1=details.get("sha1"),
                size=details.get("size"), cancel=cancel,
            )
            report("Converting mappings to TSRG...")
            tsrg = os.path.join(work_dir, f"{side}.tsrg")
            with open(proguard, "r", encoding="utf-8") as fh:
                converted = proguard_to_tsrg(fh.read())
            with open(tsrg, "w", encoding="utf-8") as fh:
                fh.write(converted)

            special_source = self._tool(TOOL_SPECIALSOURCE, progress, cancel)
            report("Remapping with SpecialSource...")
            self._run_java(
                [
                    java_exe, "-Xmx2G", "-jar", special_source,
                    "--in-jar", raw_jar,
                    "--out-jar", target + ".part",
                    "--srg-in", tsrg,
                    # Deliberately no --kill-lvt. Mojang's obfuscated jars keep
                    # both the LocalVariableTable and the LocalVariableTypeTable,
                    # and the type table is the only place a local's *generic*
                    # type survives -- erase it and
                    #     Map.Entry<K, V> e = it.next();
                    # comes back out of the decompiler as
                    #     Object var0 = it.next();
                    # so every call made on it fails to compile. The same table
                    # carries the parameter names the decompiler matches against
                    # a record's components; without them it writes out an
                    # explicit canonical constructor it has no business writing.
                    # On 1.21.11 that was 1,664 of 6,622 files failing to
                    # compile; keeping the table takes it to 214.
                ],
                cancel=cancel, what="SpecialSource",
            )
            os.replace(target + ".part", target)
            return finish(target)

        # plan == MAP_YARN
        assert details is not None
        report("Downloading yarn mappings...")
        group, artifact, ver = details["maven"].split(":")
        base = LEGACY_FABRIC_MAVEN if "legacyfabric" in group else FABRIC_MAVEN
        rel = _maven_path(group, artifact, ver, "v2")
        yarn_jar = os.path.join(_cache_dir("yarn"), os.path.basename(rel))
        _download(f"{base}/{urllib.parse.quote(rel)}", yarn_jar, cancel=cancel)

        tiny = os.path.join(maps_dir, f"{artifact}-{ver}.tiny")
        try:
            with zipfile.ZipFile(yarn_jar) as zf, open(tiny, "wb") as out:
                shutil.copyfileobj(zf.open("mappings/mappings.tiny"), out)
        except (KeyError, OSError, zipfile.BadZipFile) as exc:
            raise ProviderError(f"Could not read yarn mappings: {exc}") from None

        remapper = self._tool(TOOL_TINY_REMAPPER, progress, cancel)
        report("Remapping with tiny-remapper...")
        self._run_java(
            [
                java_exe, "-Xmx2G", "-jar", remapper,
                raw_jar, target + ".part", tiny, "official", "named", *lib_jars,
            ],
            cancel=cancel, what="tiny-remapper",
        )
        os.replace(target + ".part", target)
        finish(target)
        notes.append(
            "Yarn's `official` namespace is built against the merged client+"
            "server jar of that era, so a few server-only names may stay "
            "obfuscated. Everything the client touches is covered."
        )
        return target

    def _decompile(
        self, jar: str, src_dir: str, work_dir: str, java_exe: str,
        lib_jars: list[str], progress: Optional[ProgressCB], cancel,
    ) -> None:
        vineflower = self._tool(TOOL_VINEFLOWER, progress, cancel)
        threads = max(2, min(8, (os.cpu_count() or 4)))

        seen = 0
        pattern = re.compile(r"Decompiling class|Processing class")

        def on_line(line: str) -> None:
            nonlocal seen
            if pattern.search(line):
                seen += 1
                if seen % 25 == 0 and progress:
                    progress(
                        STAGE_DECOMPILE, seen, None,
                        f"Decompiling... ({seen:,} classes)",
                    )

        argv = [
            java_exe, f"-Xmx{DECOMPILE_HEAP}", "-jar", vineflower,
            "--folder",
            f"--thread-count={threads}",
            # Fernflower-inherited switches Vineflower still honours. Generics
            # on and synthetics hidden is what makes the output look like source
            # rather than bytecode transcribed into Java.
            #
            # -hes and -hdc stay at their defaults (hide). Printing the empty
            # `super();` is not merely noise: inside a record's canonical
            # constructor an explicit constructor invocation is a compile error,
            # and Vineflower emits one for every record it cannot fold away --
            # 449 files on the 1.21.11 client.
            "-dgs=1", "-asc=1", "-lit=1", "-log=WARN",
        ]
        argv += [f"-e={lib}" for lib in lib_jars]
        argv += [jar, src_dir]

        self._run_java(
            self._maybe_argfile(argv, work_dir),
            cancel=cancel, on_line=on_line, what="Vineflower",
        )
        if progress:
            progress(STAGE_DECOMPILE, seen, seen or None, "Decompilation finished.")

    # javac prefixes every diagnostic with `<path>:<line>: error:`. Warnings and
    # notes use the same shape with a different keyword, so the keyword is part
    # of the match -- quarantining a file for a deprecation warning would be a
    # spectacular own goal.
    _JAVAC_ERROR = re.compile(r"^(.+?\.java):\d+: error:", re.MULTILINE)

    def _repair_workspace(
        self, dest_dir: str, java_exe: str, libs_dir: str,
        progress: Optional[ProgressCB], cancel,
    ) -> tuple[int, int, int]:
        """Compile, quarantine what will not build, repeat until clean.

        Decompiling a whole game always leaves a tail of files where the type
        information genuinely did not survive -- generic casts that need a
        witness, an interface that stopped being functional, a static context
        that lost its type variable. There is no fixing those mechanically, and
        there is no need to: the original classes are still in libs/, so a file
        moved aside costs nothing at runtime and costs only that one file's
        editability at compile time.

        Removing a file breaks whatever referenced it, so this runs to a fixed
        point rather than once. It converges downwards in practice; the fraction
        cap is there because if it *isn't* converging, something went wrong
        upstream and quietly deleting the game is the worst available answer.

        -> (rounds, files quarantined, files remaining)
        """
        src_dir = os.path.join(dest_dir, "src", "main", "java")
        bin_dir = os.path.join(dest_dir, "bin")
        work_dir = os.path.join(dest_dir, ".decompiled")
        quarantine = os.path.join(work_dir, "quarantine")
        os.makedirs(bin_dir, exist_ok=True)
        os.makedirs(work_dir, exist_ok=True)

        javac = os.path.join(
            os.path.dirname(java_exe),
            "javac" + (".exe" if sys.platform == "win32" else ""),
        )
        if not os.path.exists(javac):
            raise ProviderError(
                f"No javac next to {java_exe}. A decompiled workspace has to be "
                "compiled, so a JRE is not enough -- install a JDK."
            )

        argfile = os.path.join(work_dir, "sources.txt")
        initial = self._write_source_list(src_dir, argfile)
        budget = max(50, int(initial * REPAIR_MAX_FRACTION))
        moved: list[str] = []

        for round_no in range(1, REPAIR_MAX_ROUNDS + 1):
            check_cancelled(cancel)
            remaining = self._write_source_list(src_dir, argfile)
            if progress:
                progress(
                    STAGE_BUILD, 0, None,
                    f"Compiling {remaining:,} sources "
                    f"(pass {round_no}, {len(moved)} set aside)...",
                )
            code, output = self._run_capture(
                [
                    javac, "-J-Xmx2G", "-nowarn", "-proc:none",
                    "-encoding", "UTF-8", "-d", bin_dir,
                    "-cp", os.path.join(libs_dir, "*"),
                    # Default is 100, which would hide most of the failing files
                    # and turn one pass into twenty.
                    "-Xmaxerrs", "100000",
                    "@" + argfile,
                ],
                cancel,
            )
            if code == 0:
                return round_no, len(moved), remaining

            failing = {
                os.path.normpath(p) for p in self._JAVAC_ERROR.findall(output)
            }
            failing = {p for p in failing if os.path.isfile(p)}
            if not failing:
                # javac failed for a reason that is not attributable to a source
                # file -- bad classpath, out of memory, a broken JDK. Guessing
                # would mean deleting sources to fix a problem elsewhere.
                tail = "\n".join(output.strip().splitlines()[-15:])
                raise ProviderError(
                    "The workspace failed to compile and javac did not blame any "
                    f"particular file, so nothing was moved:\n\n{tail}"
                )

            if len(moved) + len(failing) > budget:
                raise ProviderError(
                    f"Compilation is still failing after setting aside "
                    f"{len(moved)} files, and this pass would add "
                    f"{len(failing)} more -- past the {REPAIR_MAX_FRACTION:.0%} "
                    f"limit of {budget} for a {initial:,} file workspace.\n\n"
                    "That is a broken decompile rather than a tail of awkward "
                    "files. The sources are still in place; nothing further was "
                    "removed."
                )

            for path in sorted(failing):
                rel = os.path.relpath(path, src_dir)
                target = os.path.join(quarantine, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.move(path, target)
                moved.append(rel.replace("\\", "/"))

        raise ProviderError(
            f"Still not compiling after {REPAIR_MAX_ROUNDS} passes "
            f"({len(moved)} files set aside). Stopping rather than looping."
        )

    @staticmethod
    def _write_quarantine_list(dest_dir: str, moved: Sequence[str]) -> None:
        path = os.path.join(dest_dir, ".decompiled", "quarantine.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "# Files the decompiler could not produce compilable Java for.\n"
                "# They were moved to .decompiled/quarantine/ keeping their\n"
                "# package layout. The original classes are still in libs/, so\n"
                "# the game runs exactly as before -- these are simply not\n"
                "# editable. Move one back if you want to try fixing it by hand.\n"
            )
            for rel in sorted(moved):
                fh.write(rel + "\n")

    def _fetch_assets(
        self, meta: dict, assets_dir: str, run_dir: str,
        progress: Optional[ProgressCB], cancel,
    ) -> None:
        """The client will not start without its asset objects.

        Thousands of tiny files, so they go in parallel and into a shared store
        that later versions reuse -- the objects are content-addressed by hash,
        so two versions sharing a sound file share the download.
        """
        index_info = meta.get("assetIndex")
        if not index_info or not index_info.get("url"):
            return

        index_id = index_info.get("id", "legacy")
        indexes_dir = os.path.join(assets_dir, "indexes")
        os.makedirs(indexes_dir, exist_ok=True)
        index_path = os.path.join(indexes_dir, f"{index_id}.json")
        _download(
            index_info["url"], index_path, sha1=index_info.get("sha1"),
            size=index_info.get("size"), cancel=cancel,
        )
        with open(index_path, "r", encoding="utf-8") as fh:
            index = json.load(fh)

        objects = index.get("objects", {})
        store = _cache_dir("assets", "objects")
        total = len(objects)
        done = 0
        lock = threading.Lock()
        failures: list[str] = []

        def one(item) -> None:
            nonlocal done
            name, info = item
            check_cancelled(cancel)
            digest = info["hash"]
            sub = digest[:2]
            cached = os.path.join(store, sub, digest)
            try:
                _download(
                    f"{RESOURCES_BASE}/{sub}/{digest}", cached,
                    sha1=digest, size=info.get("size"), cancel=cancel,
                )
            except Cancelled:
                raise
            except ProviderError as exc:
                with lock:
                    failures.append(f"{name}: {exc}")
                return
            _link_or_copy(cached, os.path.join(assets_dir, "objects", sub, digest))
            # Pre-1.7 clients read loose files by name instead of by hash.
            if index.get("virtual"):
                _link_or_copy(cached, os.path.join(assets_dir, "virtual", index_id, *name.split("/")))
            if index.get("map_to_resources"):
                _link_or_copy(cached, os.path.join(run_dir, "resources", *name.split("/")))
            with lock:
                done += 1
                if progress and done % 40 == 0:
                    progress(
                        STAGE_DOWNLOAD, done, total,
                        f"Downloading assets... ({done:,}/{total:,})",
                    )

        if progress:
            progress(STAGE_DOWNLOAD, 0, total, f"Downloading {total:,} assets...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(one, objects.items()))

        if failures:
            raise ProviderError(
                f"{len(failures)} asset(s) could not be downloaded, so the "
                "client would fail at startup. First few:\n\n"
                + "\n".join(failures[:5])
            )

    # ---------------- launch arguments ----------------

    @staticmethod
    def _main_class(
        meta: dict, side: str, jar: str, server_main: Optional[str]
    ) -> str:
        if side == SIDE_CLIENT:
            return meta.get("mainClass") or "net.minecraft.client.main.Main"
        # The version JSON's mainClass is the *client's*; the server's lives in
        # its own manifest (or the bundler's Launcher-Main-Class).
        return (
            server_main
            or _manifest_attrs(jar).get("Main-Class")
            or "net.minecraft.server.Main"
        )

    @staticmethod
    def _arg_values(meta: dict, dest_dir: str) -> dict[str, str]:
        index_id = (meta.get("assetIndex") or {}).get("id", "legacy")
        # Deterministic so a world saved under this profile is still yours after
        # a reinstall.
        offline_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, "mc-server-manager.dev").hex
        assets_root = os.path.join(dest_dir, "assets")
        return {
            "auth_player_name": "Dev",
            "version_name": meta.get("id", "dev"),
            "game_directory": os.path.join(dest_dir, "run"),
            "assets_root": assets_root,
            "game_assets": os.path.join(assets_root, "virtual", index_id),
            "assets_index_name": index_id,
            "auth_uuid": offline_uuid,
            "auth_access_token": "0",
            "auth_session": "0",
            "auth_xuid": "0",
            "clientid": "0",
            "user_type": "legacy",
            "user_properties": "{}",
            "version_type": meta.get("type", "release"),
            "natives_directory": os.path.join(dest_dir, "natives"),
            "library_directory": os.path.join(dest_dir, "libs"),
            "launcher_name": "mc-server-manager",
            "launcher_version": "0.1",
            "classpath_separator": os.pathsep,
            "resolution_width": "1280",
            "resolution_height": "720",
        }

    @classmethod
    def _game_arguments(cls, meta: dict, side: str, dest_dir: str) -> list[str]:
        """The client needs a real argument list; the server needs `nogui`.

        Both the old flat `minecraftArguments` string and the modern rule-gated
        `arguments.game` array are handled, because "everything back to 1.3"
        means both shapes turn up.
        """
        if side == SIDE_SERVER:
            return ["nogui"]

        values = cls._arg_values(meta, dest_dir)

        def substitute(text: str) -> str:
            for key, val in values.items():
                text = text.replace("${" + key + "}", val)
            return text

        legacy = meta.get("minecraftArguments")
        if legacy:
            return [substitute(tok) for tok in legacy.split()]

        out: list[str] = []
        for entry in (meta.get("arguments") or {}).get("game", []):
            if isinstance(entry, str):
                out.append(substitute(entry))
                continue
            if not _rules_allow(entry.get("rules")):
                continue
            value = entry.get("value")
            for tok in ([value] if isinstance(value, str) else value or []):
                out.append(substitute(tok))
        return out

    @classmethod
    def _jvm_arguments(cls, meta: dict, side: str, dest_dir: str) -> list[str]:
        if side == SIDE_SERVER:
            return []
        values = cls._arg_values(meta, dest_dir)
        out: list[str] = []
        for entry in (meta.get("arguments") or {}).get("jvm", []):
            if isinstance(entry, str):
                tokens = [entry]
            elif _rules_allow(entry.get("rules")):
                value = entry.get("value")
                tokens = [value] if isinstance(value, str) else (value or [])
            else:
                continue
            for tok in tokens:
                # Gradle, Eclipse and the shell scripts each build their own
                # classpath, so Mojang's -cp/${classpath} pair must not survive.
                if tok in ("-cp", "-classpath") or "${classpath}" in tok:
                    continue
                for key, val in values.items():
                    tok = tok.replace("${" + key + "}", val)
                out.append(tok)

        if not any("java.library.path" in a for a in out):
            out.append(f"-Djava.library.path={os.path.join(dest_dir, 'natives')}")
        if sys.platform == "darwin" and "-XstartOnFirstThread" not in out:
            # LWJGL 3 on macOS deadlocks without it.
            out.append("-XstartOnFirstThread")
        return out

    # ---------------- project files ----------------

    @staticmethod
    def _gradle_list(values: Iterable[str]) -> str:
        return ", ".join("'" + v.replace("\\", "/").replace("'", "\\'") + "'" for v in values)

    def _write_gradle(
        self, dest_dir: str, project: str, version: Version, side: str,
        java_major: Optional[int], main_class: str,
        game_args: list[str], jvm_args: list[str],
    ) -> None:
        target = java_major or 17
        run_task = "runClient" if side == SIDE_CLIENT else "runServer"

        with open(os.path.join(dest_dir, "settings.gradle"), "w", encoding="utf-8") as fh:
            fh.write(f"rootProject.name = '{project}'\n")

        body = f"""// Generated by mc-server-manager for Minecraft {version.id} ({side}).
// Sources in src/main/java are yours to edit; `gradle {run_task}` compiles them
// and launches the game against the result.
plugins {{
    id 'java'
    id 'eclipse'
    id 'idea'
}}

java {{
    toolchain {{
        languageVersion = JavaLanguageVersion.of({target})
    }}
}}

repositories {{
    mavenCentral()
}}

dependencies {{
    // Every jar in libs/, including minecraft-{version.id}-{side}.jar itself.
    //
    // Keeping Minecraft on the classpath is deliberate. Gradle puts the compiled
    // sources ahead of it at runtime, so anything you edit wins -- but a class
    // you had to delete because the decompiler mangled it still resolves from
    // the jar instead of taking the whole workspace down with it.
    implementation fileTree(dir: 'libs', include: ['*.jar'])
}}

tasks.withType(JavaCompile).configureEach {{
    options.encoding = 'UTF-8'
    // Decompiled code is full of things javac is entitled to complain about.
    options.compilerArgs << '-nowarn'
    options.warnings = false
}}

tasks.register('{run_task}', JavaExec) {{
    group = 'minecraft'
    description = 'Compile the workspace and launch Minecraft {version.id} ({side}).'
    dependsOn tasks.named('classes')
    mainClass = '{main_class}'
    classpath = sourceSets.main.runtimeClasspath
    workingDir = file('run')
    doFirst {{ workingDir.mkdirs() }}
    jvmArgs = [{self._gradle_list(jvm_args)}]
    args = [{self._gradle_list(game_args)}]
    minHeapSize = '1G'
    maxHeapSize = '2G'
}}
"""
        with open(os.path.join(dest_dir, "build.gradle"), "w", encoding="utf-8") as fh:
            fh.write(body)

    def _write_eclipse(
        self, dest_dir: str, project: str, side: str, java_major: Optional[int],
        lib_jars: list[str], minecraft_jar: str, main_class: str,
        game_args: list[str], jvm_args: list[str],
    ) -> None:
        """Real .project/.classpath/.launch, so Eclipse needs no Gradle at all.

        `File > Import > Existing Projects` and the Run button works. That is the
        whole point of this core, and making it depend on a 130 MB Gradle
        download would undercut it.
        """
        target = java_major or 17
        jre = f"JavaSE-{target}" if target >= 9 else "JavaSE-1.8"

        with open(os.path.join(dest_dir, ".project"), "w", encoding="utf-8") as fh:
            fh.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<projectDescription>\n"
                f"\t<name>{_xml(project)}</name>\n"
                "\t<comment>Decompiled Minecraft workspace</comment>\n"
                "\t<projects/>\n"
                "\t<buildSpec>\n\t\t<buildCommand>\n"
                "\t\t\t<name>org.eclipse.jdt.core.javabuilder</name>\n"
                "\t\t\t<arguments/>\n\t\t</buildCommand>\n\t</buildSpec>\n"
                "\t<natures>\n\t\t<nature>org.eclipse.jdt.core.javanature</nature>\n"
                "\t</natures>\n</projectDescription>\n"
            )

        # Minecraft goes last so the source folders shadow it, mirroring the
        # Gradle side.
        ordered = [j for j in lib_jars if os.path.abspath(j) != os.path.abspath(minecraft_jar)]
        ordered.append(minecraft_jar)

        entries = [
            '\t<classpathentry kind="src" path="src/main/java"/>',
            '\t<classpathentry kind="src" path="src/main/resources"/>',
            '\t<classpathentry kind="con" path="org.eclipse.jdt.launching.JRE_CONTAINER/'
            f'org.eclipse.jdt.internal.debug.ui.launcher.StandardVMType/{jre}"/>',
        ]
        for jar in ordered:
            rel = os.path.relpath(jar, dest_dir).replace("\\", "/")
            entries.append(f'\t<classpathentry kind="lib" path="{_xml(rel)}"/>')
        entries.append('\t<classpathentry kind="output" path="bin"/>')

        with open(os.path.join(dest_dir, ".classpath"), "w", encoding="utf-8") as fh:
            fh.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n<classpath>\n'
                + "\n".join(entries) + "\n</classpath>\n"
            )

        # Eclipse variables keep the launch config valid if the project is
        # imported under a different absolute path.
        loc = "${workspace_loc:" + project + "}"
        vm = " ".join(
            _quote(a.replace(dest_dir, loc)) for a in
            [*jvm_args, "-Xms1G", "-Xmx2G"]
        )
        program = " ".join(_quote(a.replace(dest_dir, loc)) for a in game_args)

        launch_name = "Minecraft " + ("Client" if side == SIDE_CLIENT else "Server")
        with open(
            os.path.join(dest_dir, f"{launch_name}.launch"), "w", encoding="utf-8"
        ) as fh:
            fh.write(
                '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
                '<launchConfiguration type="org.eclipse.jdt.launching.localJavaApplication">\n'
                '<listAttribute key="org.eclipse.debug.core.MAPPED_RESOURCE_PATHS">\n'
                f'<listEntry value="/{_xml(project)}"/>\n</listAttribute>\n'
                '<listAttribute key="org.eclipse.debug.core.MAPPED_RESOURCE_TYPES">\n'
                '<listEntry value="4"/>\n</listAttribute>\n'
                f'<stringAttribute key="org.eclipse.jdt.launching.MAIN_TYPE" value="{_xml(main_class)}"/>\n'
                f'<stringAttribute key="org.eclipse.jdt.launching.PROGRAM_ARGUMENTS" value="{_xml(program)}"/>\n'
                f'<stringAttribute key="org.eclipse.jdt.launching.PROJECT_ATTR" value="{_xml(project)}"/>\n'
                f'<stringAttribute key="org.eclipse.jdt.launching.VM_ARGUMENTS" value="{_xml(vm)}"/>\n'
                f'<stringAttribute key="org.eclipse.jdt.launching.WORKING_DIRECTORY" value="{_xml(loc)}/run"/>\n'
                "</launchConfiguration>\n"
            )

    @staticmethod
    def _write_scripts(
        dest_dir: str, side: str, java_major: Optional[int], main_class: str,
        game_args: list[str], jvm_args: list[str],
    ) -> None:
        """Run without an IDE at all -- compile src/, then launch.

        Useful for checking that a change works before opening Eclipse, and for
        proving the workspace is self-contained.
        """
        hint_bat = f"REM Requires Java {java_major}\n" if java_major else ""
        hint_sh = f"# Requires Java {java_major}\n" if java_major else ""
        vm = " ".join(_quote(a) for a in jvm_args)
        program = " ".join(_quote(a) for a in game_args)
        name = "client" if side == SIDE_CLIENT else "server"

        # Two rules fight each other in a javac argfile, and getting one right
        # while missing the other is how this broke the first time:
        #
        #   * paths must be quoted, or a workspace under "My Documents" splits
        #     into two arguments;
        #   * inside those quotes backslash is an *escape*, so a plain
        #     "C:\\Users\\Adam\\Desktop\\Test\\..." arrives at javac as
        #     "C:UsersAdamDesktopTest..." -- every separator silently eaten.
        #
        # Writing the separators as forward slashes satisfies both: Windows
        # javac accepts them, and there is nothing left for the quotes to eat.
        # Delayed expansion is what lets the loop rewrite each path in place.
        bat = f"""@echo off
{hint_bat}setlocal enabledelayedexpansion
cd /d "%~dp0"
if not exist bin mkdir bin
if not exist run mkdir run
if not exist .decompiled mkdir .decompiled
echo Compiling sources...
if exist .decompiled\\sources.txt del .decompiled\\sources.txt
for /r "src\\main\\java" %%f in (*.java) do (
    set "sf=%%f"
    >>.decompiled\\sources.txt echo "!sf:\\=/!"
)
javac -J-Xmx2G -nowarn -proc:none -encoding UTF-8 -d bin -cp "libs\\*" @.decompiled\\sources.txt || goto :error
cd run
java -Xms1G -Xmx2G {vm} -cp "..\\bin;..\\src\\main\\resources;..\\libs\\*" {main_class} {program}
goto :eof
:error
echo.
echo Compilation failed. Delete or fix the offending sources and try again --
echo the original classes are still in libs\\, so deleting is safe.
pause
"""
        with open(
            os.path.join(dest_dir, f"start-{name}.bat"), "w",
            encoding="utf-8", newline="\r\n",
        ) as fh:
            fh.write(bat)

        sh = f"""#!/bin/sh
{hint_sh}set -e
cd "$(dirname "$0")"
mkdir -p bin run .decompiled
echo "Compiling sources..."
find src/main/java -name '*.java' | sed 's|\\\\|/|g; s/.*/"&"/' > .decompiled/sources.txt
javac -J-Xmx2G -nowarn -proc:none -encoding UTF-8 -d bin -cp "libs/*" @.decompiled/sources.txt
cd run
exec java -Xms1G -Xmx2G {vm} -cp "../bin:../src/main/resources:../libs/*" {main_class} {program}
"""
        path = os.path.join(dest_dir, f"start-{name}.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(sh)
        try:
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass

    @staticmethod
    def _write_readme(
        dest_dir: str, version: Version, side: str, plan: str,
        main_class: str, java_major: Optional[int],
    ) -> None:
        how = {
            MAP_NONE: "none needed -- this build ships unobfuscated",
            MAP_MOJANG: "Mojang official mappings (ProGuard -> TSRG -> SpecialSource)",
            MAP_YARN: "community yarn mappings (tiny-remapper)",
        }[plan]
        run = "runClient" if side == SIDE_CLIENT else "runServer"
        script = "start-client" if side == SIDE_CLIENT else "start-server"

        with open(os.path.join(dest_dir, "WORKSPACE.md"), "w", encoding="utf-8") as fh:
            fh.write(f"""# Minecraft {version.id} ({side}) -- decompiled workspace

Mappings: {how}
Main class: `{main_class}`
Java: {java_major or "unspecified"}

## Open it

**Eclipse** -- File > Import > General > Existing Projects into Workspace, point
at this folder. The `.launch` file appears under Run Configurations; hit Run.
No Gradle required.

**IntelliJ / VS Code** -- open the folder as a Gradle project and run the
`{run}` task. (`gradle wrapper` once if you want a `gradlew` here.)

**Neither** -- `{script}.bat` / `./{script}.sh` compiles and launches directly.

## Layout

    src/main/java/       edit these
    src/main/resources/  non-class contents of the jar
    libs/                dependencies + the remapped Minecraft jar
    natives/             LWJGL natives
    assets/              asset objects and index
    run/                 game directory (worlds, options.txt, logs)
    mappings/            the mapping file used, for reference

## The quarantine

The install already compiled this workspace and moved anything that would not
build into `.decompiled/quarantine/`, keeping its package layout.
`.decompiled/quarantine.txt` lists what went. Those are files where the type
information genuinely did not survive decompilation -- a generic cast that needs
a witness, an interface that stopped being functional, a static context that
lost its type variable.

**Nothing is missing at runtime.** `libs/minecraft-{version.id}-{side}.jar` sits
after your sources on the classpath, so the original class loads instead. You
just cannot edit that one file without fixing it first. Move it back out of
quarantine if you want to try.

The same rule applies to anything you break later: if a file will not build,
deleting it is always safe and always safer than fighting it.

## What is *not* here

No mod loader, no mixins, no access wideners. This is the raw game, editable in
place -- the pre-Forge way of modding. If you want a loader instead, use the
Fabric / Forge / NeoForge cores.
""")


def _xml(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _cli() -> None:
    provider = DecompiledProvider()
    versions = provider.list_versions(include_unstable=False)
    print(f"{len(versions)} entries (client + server per release)")
    for v in versions[:10]:
        print(" ", v, "->", v.variant)


if __name__ == "__main__":
    _cli()
