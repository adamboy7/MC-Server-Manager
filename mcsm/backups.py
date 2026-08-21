"""Finding, reading, writing and restoring backups.

Six mutually incompatible backup systems turn up in practice and they
disagree about almost everything -- see the module's own section comments.
"""

import os
import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .world import (
    EXPORT_SKIP_FILES,
    ExportCancelled,
    _iter_files,
    get_nether_dir,
    get_the_end_dir,
    get_world_dir,
    has_satellite_dimension_folders,
)


def get_backups_dir(info: "ServerInfo") -> Path:
    return info.path / "backups"


def get_world_backups_dir(info: "ServerInfo") -> Path:
    """The folder that actually holds this world's dated backup archives --
    prefer the world-specific subfolder if present, falling back to the
    top-level backups folder otherwise.

    That preference turns out to be right for a reason this predates: both
    AromaBackup generations write into backups/{level_name}/. Browse
    Backups... opens whatever this returns."""
    backups_dir = get_backups_dir(info)
    world_backups_dir = backups_dir / info.level_name
    return world_backups_dir if world_backups_dir.is_dir() else backups_dir


# ---------------------------------------------------------------------------
# Backups
#
# A server's backups are whatever its tooling made them, and six mutually
# incompatible systems turn up in practice. They disagree about the output
# folder, the filename, whether the world sits at the archive root or one
# level down, and even about which character separates path components.
#
#   AromaBackup 3.x   backups/{level}/Backup--{world}--{Y-m-d--H-M}.zip
#                     + a .backupinfo sidecar; world at the archive root
#   AromaBackup 0.x   backups/{level}/{Y}/{M}/{D}/Backup-{world}-{Y}-{M}-{D}--{H}-{M}.zip
#                     + a shared backupstore.txt; world nested under {level}/
#   AutoBackup        backups/{folder}_{Y-m-d}_{H-M-S}.zip, ONE PER WORLD
#                     FOLDER, world at the root, BACKSLASH separators
#   SimpleBackups     simplebackups/{folder}_{Y-m-d}_{H-M-S}.zip, world
#                     nested under {folder}/
#   x-backup          a content-addressed blob store + SQLite index
#   ServerBackup      an uncompressed directory tree
#
# Two consequences shape everything below. First, a split (Bukkit) world is
# backed up as several archives sharing one timestamp, so the unit the user
# picks from is a *set*, not a file. Second, the archive's own layout has to
# be probed rather than inferred from its name -- two providers share a
# filename grammar and disagree about the contents.
# ---------------------------------------------------------------------------

# Aroma 3.x. This was the app's only pattern historically, and it is correct
# for that provider -- it just never matched the other nine archives.
AROMA3_ARCHIVE_RE = re.compile(
    r"^Backup--(?P<name>.+)--(?P<stamp>\d{4}-\d{2}-\d{2}--\d{2}-\d{2})"
    r"\.(?P<ext>zip|tar|tar\.gz)$"
)
AROMA3_DATE_FORMAT = "%Y-%m-%d--%H-%M"
BACKUP_DATE_FORMAT = AROMA3_DATE_FORMAT  # kept: other modules may import it

# AutoBackup and SimpleBackups. Anchored on the stamp because the world name
# is variable-length and "world" is a prefix of "world_nether" -- matching
# "^{level}_(.*)" on world_nether_2026-08-19_17-00-39.zip yields the useless
# "nether_2026-08-19_17-00-39".
DATED_ARCHIVE_RE = re.compile(
    r"^(?P<name>.+)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})\.zip$"
)

# Aroma 0.x: single hyphens, unpadded month/day, and level names that contain
# hyphens and spaces. The negative lookarounds matter: without them this also
# matches every Aroma 3.x name, capturing "-world-" from
# "Backup--world--2026-08-20--10-23.zip", because the greedy .* absorbs 3.x's
# doubled hyphens and \d{1,2} accepts zero-padded fields. 3.x is also tried
# first, so this is belt and braces.
AROMA0_ARCHIVE_RE = re.compile(
    r"^Backup-(?P<name>(?!-).*[^-])-(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})"
    r"--(?P<h>\d{1,2})-(?P<mi>\d{1,2})\.zip$"
)

# Aroma's own scratch directory, and the manifest it puts inside archives.
AROMA_MANIFEST_NAME = "incremental.aromabackup"

BACKUP_PROVIDER_NAMES = {
    "mcsm-safety": "Safety backup (ours)",
    "aroma3": "AromaBackup",
    "aroma0": "AromaBackup (legacy)",
    "autobackup": "AutoBackup",
    "simplebackups": "SimpleBackups",
    "xbackup": "x-backup",
    "serverbackup": "ServerBackup",
    "unknown": "Unrecognised",
}

# Providers whose archives we can read well enough to restore from.
RESTORABLE_PROVIDERS = {"mcsm-safety", "aroma3", "aroma0", "autobackup",
                        "simplebackups", "unknown"}


@dataclass
class ParsedBackupName:
    world: str
    dt: Optional[datetime]
    stamp: str        # the grouping key -- the raw stamp text, not the datetime
    provider: str


def parse_backup_filename(fname: str, level_name: str = "") -> Optional[datetime]:
    """Date encoded in a backup archive's filename, or None.

    Kept as a thin wrapper over parse_backup_archive_name for callers that
    only want the timestamp. level_name is accepted and ignored -- every
    pattern anchors on the stamp rather than on the world name, which is what
    lets "world_nether_..." parse correctly on a server whose level is
    "world"."""
    parsed = parse_backup_archive_name(fname)
    return parsed.dt if parsed else None


def parse_backup_archive_name(fname: str) -> Optional[ParsedBackupName]:
    """Split a backup archive's filename into world, timestamp and provider.

    Patterns are tried most-specific first; see each regex above for why the
    ordering and the guards are load-bearing. Returns None for anything that
    doesn't look like a dated archive (a hand-renamed file, say) -- callers
    fall back to the file's mtime."""
    m = AROMA3_ARCHIVE_RE.match(fname)
    if m:
        try:
            dt = datetime.strptime(m.group("stamp"), AROMA3_DATE_FORMAT)
        except ValueError:
            dt = None
        return ParsedBackupName(m.group("name"), dt, m.group("stamp"), "aroma3")

    m = AROMA0_ARCHIVE_RE.match(fname)
    if m:
        try:
            dt = datetime(int(m.group("y")), int(m.group("mo")), int(m.group("d")),
                          int(m.group("h")), int(m.group("mi")))
        except ValueError:
            dt = None
        stamp = "{}-{:02d}-{:02d}--{:02d}-{:02d}".format(
            m.group("y"), int(m.group("mo")), int(m.group("d")),
            int(m.group("h")), int(m.group("mi")))
        return ParsedBackupName(m.group("name"), dt, stamp, "aroma0")

    m = DATED_ARCHIVE_RE.match(fname)
    if m:
        stamp = f"{m.group('date')}_{m.group('time')}"
        try:
            dt = datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            dt = None
        # AutoBackup and SimpleBackups are indistinguishable by name alone;
        # the caller resolves it from the folder the file was found in.
        return ParsedBackupName(m.group("name"), dt, stamp, "autobackup")

    return None


def read_aroma_backupinfo(path: Path) -> dict:
    """Parse an AromaBackup .backupinfo sidecar.

    It is a Java Properties file: comment lines start with '#', and the
    payload is key=value -- though Properties also accepts key:value, and an
    older Aroma wrote it that way, so both are handled. Returns {} when the
    file is missing or unreadable; it is free metadata, never a requirement."""
    out = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.replace("\r", "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        for sep in ("=", ":"):
            if sep in line:
                k, v = line.split(sep, 1)
                out[k.strip()] = v.strip()
                break
    return out


def parse_backupstore_txt(path: Path) -> dict:
    """AromaBackup 0.x's shared index: one "{level}={Y}={M}={D}={H}={M}" line
    per world, unpadded. Split from the *right* -- the level name is free text
    and can itself contain '='. Returns {level_name: datetime}."""
    out = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.replace("\r", "").splitlines():
        parts = line.strip().split("=")
        if len(parts) < 6:
            continue
        level = "=".join(parts[:-5])
        try:
            y, mo, d, h, mi = (int(x) for x in parts[-5:])
            out[level] = datetime(y, mo, d, h, mi)
        except ValueError:
            continue
    return out


def normalise_archive_name(name: str) -> str:
    """Path separators inside a zip, made uniform.

    AutoBackup writes every entry with backslashes -- out of spec (APPNOTE
    says forward slash) but it is what Java's File.separator produces on
    Windows. Left alone, zipfile treats "players\\data\\<uuid>.dat" as one
    flat filename with no directories in it, so Path(name).parent is "." on
    Linux/macOS and "players\\data" on Windows: behaviour that silently
    changes by platform.

    Note "./" prefixes are stripped as a *prefix*, in a loop -- str.lstrip
    takes a set of characters, so lstrip("./") would turn "../escape.txt"
    into "escape.txt" and quietly disarm the traversal check downstream."""
    norm = name.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def safe_archive_relpath(name: str) -> Optional[str]:
    """A normalised archive entry name that is safe to join onto a
    destination directory, or None if it isn't.

    An archive is untrusted input and a restore writes with our privileges,
    so absolute paths, Windows drive letters and any '..' segment are
    rejected rather than sanitised -- there is no legitimate backup that
    contains one, and a "cleaned up" traversal is still a traversal."""
    norm = normalise_archive_name(name)
    if not norm or norm.endswith("/"):
        return None
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        return None
    parts = [p for p in norm.split("/") if p != "."]
    if not parts or any(p in ("..", "") for p in parts):
        return None
    return "/".join(parts)


def archive_kind(path: Path) -> Optional[str]:
    """Which container format this file is, by extension.

    AromaBackup's S:compressionType accepts zip, tar, tar.gz and folder --
    "folder" isn't an archive at all and is handled elsewhere, but the other
    three all turn up as files in a backups directory."""
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "tar.gz"
    if name.endswith(".tar"):
        return "tar"
    return None


BACKUP_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".zip", ".tar")


def strip_archive_suffix(name: str) -> str:
    """An archive filename without its container extension. Longest suffix
    first, so ".tar.gz" isn't left as ".tar"."""
    lowered = name.lower()
    for suffix in BACKUP_ARCHIVE_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


class BackupArchive:
    """A backup archive, read through one consistent lens.

    Handles zip and tar containers, separator normalisation, locating the
    world inside the archive, and the path-safety checks a restore needs. Use
    as a context manager."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.kind = archive_kind(self.path) or "zip"
        self.entries = []          # [(normalised name, member)] -- files only
        self._raw_by_norm = {}     # normalised name -> the name the lib wants
        if self.kind == "zip":
            self._zf = zipfile.ZipFile(self.path)
            self._tf = None
            members = [z for z in self._zf.infolist() if not z.is_dir()]
            raw_names = [z.filename for z in members]
        else:
            self._zf = None
            mode = "r:gz" if self.kind == "tar.gz" else "r:"
            self._tf = tarfile.open(self.path, mode)
            members = [m for m in self._tf.getmembers() if m.isfile()]
            raw_names = [m.name for m in members]
        for member, raw in zip(members, raw_names):
            norm = normalise_archive_name(raw)
            self.entries.append((norm, member))
            self._raw_by_norm[norm] = member
        self.world_root = self._find_world_root()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._zf is not None:
            self._zf.close()
        if self._tf is not None:
            self._tf.close()

    def _find_world_root(self) -> str:
        """Where the world starts inside the archive: "" when its contents sit
        at the root (AutoBackup, Aroma 3.x), or "world/" / "level with
        spaces/" when it is nested (SimpleBackups, Aroma 0.x).

        Found as the *shallowest* level.dat -- a nested archive also contains
        DIM-1/level.dat and the satellite stubs, which are deeper."""
        candidates = [n for n, _ in self.entries if n.rsplit("/", 1)[-1] == "level.dat"]
        if not candidates:
            return ""
        shallowest = min(candidates, key=lambda n: n.count("/"))
        return shallowest[: -len("level.dat")]

    def rel(self, name: str) -> str:
        """An entry's path relative to the world root."""
        norm = normalise_archive_name(name)
        if self.world_root and norm.startswith(self.world_root):
            return norm[len(self.world_root):]
        return norm

    def world_entries(self):
        """(path-within-the-world, ZipInfo) for everything under the world
        root, with the files no restore should ever write filtered out."""
        for norm, zinfo in self.entries:
            if self.world_root and not norm.startswith(self.world_root):
                continue
            rel = self.rel(norm)
            if not rel or rel.rsplit("/", 1)[-1] in EXPORT_SKIP_FILES:
                # session.lock is a runtime mutex and uid.dat pins a world's
                # identity on a particular Bukkit server. Both Aroma versions
                # archive session.lock and AutoBackup archives uid.dat, so
                # this filter has to run on the way *out* of an archive too,
                # not just when we build one.
                continue
            if safe_archive_relpath(rel) is None:
                continue
            yield rel, zinfo

    def _member(self, name: str):
        member = self._raw_by_norm.get(normalise_archive_name(name))
        if member is None:
            raise KeyError(name)
        return member

    def read(self, name: str) -> bytes:
        with self.open(name) as fh:
            return fh.read()

    def open(self, name: str):
        """A binary file object for one entry. Callers treat zip and tar
        alike; tarfile hands back None for a member it can't extract, which
        becomes a KeyError here so it surfaces the same way a missing zip
        entry would."""
        member = self._member(name)
        if self._zf is not None:
            return self._zf.open(member)
        fh = self._tf.extractfile(member)
        if fh is None:
            raise KeyError(name)
        return fh

    @property
    def has_level_dat(self) -> bool:
        return any(n.rsplit("/", 1)[-1] == "level.dat" for n, _ in self.entries)

    def incremental_reason(self) -> Optional[str]:
        """Why this archive can't be restored on its own, or None.

        A delta restored alone produces a world with holes and *looks* like a
        success, so this errs toward refusing. Neither sample in the test set
        is actually incremental, so both checks are built from the formats
        rather than from an observed delta."""
        if not self.has_level_dat:
            return "no level.dat -- this looks like a partial or incremental archive"
        manifest = next((n for n, _ in self.entries if n == AROMA_MANIFEST_NAME), None)
        if manifest is not None:
            # The manifest is a hash baseline written on FULL backups too, so
            # its presence proves nothing on its own -- compare what it lists
            # against what the archive actually carries.
            try:
                listed = 0
                for line in self.read(manifest).decode("utf-8", "replace").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        listed += 1
            except (OSError, KeyError, zipfile.BadZipFile, tarfile.TarError):
                return None
            present = len(self.entries) - 1   # the manifest doesn't list itself
            if listed > present:
                return (f"its manifest lists {listed} files but the archive holds "
                        f"{present} -- an incremental backup that needs its chain")
        return None

    def player_file_entries(self, uuid: str) -> list:
        """Normalised entry names for one player's files.

        Prefix match rather than "{uuid}.dat" so stray siblings a mod may have
        written ("<uuid> - Copy.dat", "<uuid>.dat_old") come along, matching
        what _export_playerdata does. Both world layouts are accepted -- an
        archive carries whatever the server was running when it was taken,
        which is not necessarily what it runs now."""
        matches = []
        for norm, _zinfo in self.entries:
            rel = self.rel(norm)
            parent, _, base = rel.rpartition("/")
            if parent not in ("playerdata", "players/data"):
                continue
            if base.startswith(uuid):
                matches.append(norm)
        return matches


def open_backup_archive(path: Path) -> Optional[BackupArchive]:
    """BackupArchive for `path`, or None if it isn't a readable zip. A
    truncated or half-downloaded archive is an ordinary thing to find in a
    backups folder and must not abort a scan."""
    try:
        return BackupArchive(path)
    except (OSError, zipfile.BadZipFile, tarfile.TarError, RuntimeError, EOFError):
        return None


def find_uuid_entries_in_zip(zf, uuid: str) -> list:
    """Backwards-compatible wrapper. Accepts either a BackupArchive or a raw
    zipfile.ZipFile so older call sites keep working, but routes both through
    the normalising reader -- passing a raw ZipFile used to silently return
    nothing for AutoBackup archives on Linux/macOS."""
    if isinstance(zf, BackupArchive):
        return zf.player_file_entries(uuid)
    matches = []
    for name in zf.namelist():
        norm = normalise_archive_name(name)
        parent, _, base = norm.rpartition("/")
        tail = parent.rsplit("/", 2)[-2:]
        is_playerdata = parent.endswith("playerdata") or tail == ["players", "data"]
        if is_playerdata and base.startswith(uuid):
            matches.append(name)
    return matches


# ---------------------------------------------------------------------------
# Which backup tool made these, and where does it keep them
# ---------------------------------------------------------------------------

def _read_forge_cfg(path: Path) -> dict:
    """Values out of a Forge-style .cfg (the "S:key=value" / "I:key=value"
    format both AromaBackup generations use). Type prefixes are dropped;
    everything comes back as a string."""
    out = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for m in re.finditer(r"^\s*(?:[BISD]:)?([\w.\-]+)\s*=\s*(.*?)\s*$", text, re.M):
        out[m.group(1)] = m.group(2)
    return out


def _read_simple_yaml_scalar(path: Path, key: str) -> Optional[str]:
    """One top-level scalar out of a small YAML config, without taking a
    dependency on a YAML parser for the two keys we actually want."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(rf"^{re.escape(key)}\s*:\s*(.+?)\s*(?:#.*)?$", text, re.M)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


@dataclass
class BackupProvider:
    key: str
    name: str
    version: Optional[str] = None
    config_path: Optional[Path] = None
    search_dirs: list = field(default_factory=list)
    retention: Optional[str] = None
    evidence: list = field(default_factory=list)

    @property
    def restorable(self) -> bool:
        return self.key in RESTORABLE_PROVIDERS


def detect_backup_providers(info: "ServerInfo") -> list:
    """Backup tooling installed on this server, with wherever each one is
    configured to write.

    Two sources, for the same reason platform detection uses two: a tool can
    be installed with no backups yet, and archives outlive the tool that made
    them. Probe A reads the jars and configs; Probe C reads the world's own
    mod list, which names the mod and its version and survives the jar being
    deleted. Bukkit plugins are not mods and never appear there, so C
    supplements A rather than replacing it."""
    root = info.path
    mods = info.platform.loaded_mods or {}
    found = []

    def add(key, **kw):
        found.append(BackupProvider(key=key, name=BACKUP_PROVIDER_NAMES[key], **kw))

    # --- AromaBackup. The two generations share a jar name and differ only by
    # config filename case, which Windows does not preserve -- so tell them
    # apart by the config's *contents* (3.x grew a [backup_location] section)
    # and cross-check against the mod version from the world.
    aroma_cfg = None
    for candidate in ("aromabackup.cfg", "AromaBackup.cfg"):
        p = root / "config" / "aroma1997" / candidate
        if p.is_file():
            aroma_cfg = p
            break
    aroma_version = mods.get("aromabackup")
    if aroma_cfg is not None or aroma_version:
        cfg = _read_forge_cfg(aroma_cfg) if aroma_cfg else {}
        is_new = "filename" in cfg or "fullBackupsToKeep" in cfg
        if not cfg and aroma_version:
            is_new = not aroma_version.startswith("0.")
        location = cfg.get("location", "./backups").strip()
        base = (root / location).resolve() if location else get_backups_dir(info)
        if is_new:
            add("aroma3", version=aroma_version, config_path=aroma_cfg,
                search_dirs=[base, base / info.level_name],
                retention=(f"keeps {cfg['fullBackupsToKeep']} full backups"
                           if cfg.get("fullBackupsToKeep") else None),
                evidence=[str(aroma_cfg)] if aroma_cfg else ["world mod list"])
        else:
            add("aroma0", version=aroma_version, config_path=aroma_cfg,
                search_dirs=[base, base / info.level_name],
                retention=(f"keeps {cfg['keep']} backups" if cfg.get("keep") else None),
                evidence=[str(aroma_cfg)] if aroma_cfg else ["world mod list"])

    # --- AutoBackup (Bukkit plugin)
    ab_cfg = root / "plugins" / "AutoBackup" / "config.yml"
    ab_jar = next(iter(sorted((root / "plugins").glob("AutoBackup*.jar"))), None) \
        if (root / "plugins").is_dir() else None
    if ab_cfg.is_file() or ab_jar is not None:
        rel = _read_simple_yaml_scalar(ab_cfg, "backup-path") if ab_cfg.is_file() else None
        keep = _read_simple_yaml_scalar(ab_cfg, "max-backups") if ab_cfg.is_file() else None
        base = (root / (rel or "backups")).resolve()
        add("autobackup", config_path=ab_cfg if ab_cfg.is_file() else None,
            search_dirs=[base],
            retention=f"keeps {keep} archives" if keep else None,
            evidence=[str(x) for x in (ab_jar, ab_cfg) if x])

    # --- SimpleBackups (Forge/NeoForge mod)
    sb_cfg = root / "config" / "simplebackups-common.toml"
    sb_version = mods.get("simplebackups")
    if sb_cfg.is_file() or sb_version:
        cfg = {}
        if sb_cfg.is_file():
            try:
                text = sb_cfg.read_text(encoding="utf-8", errors="replace")
                cfg = dict(re.findall(r'^\s*(\w+)\s*=\s*"?([^"\n]*)"?\s*$', text, re.M))
            except OSError:
                cfg = {}
        base = (root / cfg.get("outputPath", "simplebackups")).resolve()
        dirs = [base, base / info.level_name]
        add("simplebackups", version=sb_version,
            config_path=sb_cfg if sb_cfg.is_file() else None,
            search_dirs=dirs,
            retention=f"keeps {cfg['backupsToKeep']}" if cfg.get("backupsToKeep") else None,
            evidence=([str(sb_cfg)] if sb_cfg.is_file() else []) +
                     (["onlyModified=true -- archives may be incremental"]
                      if cfg.get("onlyModified") == "true" else []))

    # --- x-backup (Fabric): a blob store, listed but never restored from here
    xb_dir = root / "xb.backups"
    if xb_dir.is_dir() or mods.get("x_backup") or mods.get("xbackup"):
        add("xbackup", version=mods.get("x_backup") or mods.get("xbackup"),
            search_dirs=[xb_dir], evidence=[str(xb_dir)] if xb_dir.is_dir() else [])

    # --- ServerBackup (Fabric): plain directory trees
    sbk_dir = root / "server_backups" / "manual_backups"
    if sbk_dir.is_dir() or (root / "backup_history.json").is_file():
        add("serverbackup", search_dirs=[sbk_dir],
            evidence=[str(sbk_dir)] if sbk_dir.is_dir() else ["backup_history.json"])

    return found


def backup_search_dirs(info: "ServerInfo") -> list:
    """Every folder that might hold this world's archives, most specific
    first, de-duplicated. Configured provider paths win over the conventional
    ones; the conventional ones stay so that archives left behind by a tool
    that has since been uninstalled still list."""
    dirs = []
    seen = set()

    def add(d: Path):
        try:
            key = d.resolve()
        except OSError:
            key = d
        if key in seen or not d.is_dir():
            return
        seen.add(key)
        dirs.append(d)

    for provider in detect_backup_providers(info):
        for d in provider.search_dirs:
            add(d)
    backups = get_backups_dir(info)
    # Ours. Listed first so a safety copy is always visible in the rollback
    # dialog -- a safety backup nobody can find is not a safety backup.
    add(backups / "mcsm-safety")
    add(backups / info.level_name)
    add(backups)
    simple = info.path / "simplebackups"
    add(simple / info.level_name)
    add(simple)
    return dirs


def _iter_backup_archive_files(folder: Path):
    """Archive files directly in `folder`, plus AromaBackup 0.x's
    {Y}/{M}/{D}/ nesting -- which is why this can't just glob one level.
    Bounded to that depth so a backups folder that happens to contain an
    unpacked world isn't walked in full."""
    def is_archive(p: Path) -> bool:
        return p.is_file() and archive_kind(p) is not None

    try:
        for entry in sorted(folder.iterdir()):
            if is_archive(entry):
                yield entry
            elif entry.is_dir() and re.fullmatch(r"\d{1,4}", entry.name):
                for sub in sorted(entry.rglob("*")):
                    if is_archive(sub) and len(sub.relative_to(entry).parts) <= 3:
                        yield sub
    except OSError:
        return


def world_folder_allowlist(info: "ServerInfo") -> dict:
    """The only folder names a restore may ever write into, keyed by a
    case-folded lookup name.

    This exists because an archive's *filename* is not a fact about this
    server. "plugins_2026-08-21_10-00-00.zip" parses as cleanly as
    "world_nether_2026-08-21_10-00-00.zip", and without this check a restore
    would replace the plugins folder wholesale."""
    names = [info.level_name,
             f"{info.level_name}_nether",
             f"{info.level_name}_the_end"]
    return {n.casefold(): n for n in names}


def resolve_member_target(world_name: str, info: "ServerInfo",
                          provider: str) -> Optional[str]:
    """Which folder an archive named `world_name` may be restored into, or
    None if it may not be restored at all.

    Three cases:
      * it names one of this world's folders  -> that folder;
      * it didn't parse as a dated archive at all (a hand-renamed file) ->
        the overworld, which is the only sensible target for a loose backup
        and matches what this app did before restores existed;
      * it names some *other* world (a multiworld server's second world, or
        a stray zip that merely matches the naming grammar) -> None.
    """
    allow = world_folder_allowlist(info)
    target = allow.get(world_name.casefold())
    if target is not None:
        return target
    if provider == "unknown":
        return info.level_name
    return None


def has_any_backup_folder(info: "ServerInfo") -> bool:
    """Whether Browse Backups... has anywhere to go.

    Separate from backup_folder_for_browsing because this runs on every
    right-click and only needs a yes/no: it stops at the first archive it
    sees, where working out *which* folder holds the newest one has to stat
    every archive. On a server with a few years of daily Aroma backups that
    is the difference between a handful of stats and a couple of thousand."""
    for folder in backup_search_dirs(info):
        for _fp in _iter_backup_archive_files(folder):
            return True
    return bool(backup_search_dirs(info))


def backup_folder_for_browsing(info: "ServerInfo") -> Optional[Path]:
    """The folder "Browse Backups..." should open, or None if this server has
    nowhere to look.

    Not simply `backups/`: SimpleBackups writes to `simplebackups/`, Aroma to
    `backups/{level}/` (and 0.x a further `{Y}/{M}/{D}` down), and AutoBackup's
    path is configurable. So this returns the directory actually holding the
    most recent archive -- which is what someone means by "my backups" --
    falling back to the first search directory that exists when there are no
    archives yet."""
    newest_time = None
    newest_dir = None
    for folder in backup_search_dirs(info):
        for fp in _iter_backup_archive_files(folder):
            try:
                mtime = fp.stat().st_mtime
            except OSError:
                continue
            if newest_time is None or mtime > newest_time:
                newest_time, newest_dir = mtime, fp.parent
    if newest_dir is not None:
        return newest_dir
    dirs = backup_search_dirs(info)
    return dirs[0] if dirs else None


def backup_storage_dirs(info: "ServerInfo") -> list:
    """Non-overlapping folders holding this server's backups, for measuring
    disk use.

    backup_search_dirs deliberately lists nested paths (`backups/{level}/`
    *and* `backups/`) because either may hold archives. Summing both would
    count the same bytes twice, so this drops any directory that lives inside
    another one in the list."""
    dirs = []
    for folder in backup_search_dirs(info):
        try:
            resolved = folder.resolve()
        except OSError:
            resolved = folder
        if any(resolved == kept or kept in resolved.parents for kept in dirs):
            continue
        dirs = [d for d in dirs if resolved not in d.parents]
        dirs.append(resolved)
    return dirs


@dataclass
class BackupMember:
    path: Path
    world_folder: str
    size_bytes: int = 0
    target_folder: Optional[str] = None


@dataclass
class BackupSet:
    """One backup, which on a split world is several archives sharing a
    timestamp."""
    stamp: str
    dt: Optional[datetime]
    provider: str
    members: dict = field(default_factory=dict)   # world folder -> BackupMember

    @property
    def provider_name(self) -> str:
        return BACKUP_PROVIDER_NAMES.get(self.provider, self.provider)

    @property
    def restorable(self) -> bool:
        return self.provider in RESTORABLE_PROVIDERS

    @property
    def total_bytes(self) -> int:
        return sum(m.size_bytes for m in self.members.values())

    @property
    def paths(self) -> list:
        return [m.path for m in self.members.values()]

    def display_date(self) -> str:
        if self.dt:
            return self.dt.strftime("%Y-%m-%d %H:%M")
        return self.stamp or (self.paths[0].name if self.paths else "?")

    def missing_folders(self, info: "ServerInfo") -> list:
        """World folders this server has now that the set has no archive for.
        Restoring an incomplete set leaves the world inconsistent."""
        expected = [info.level_name]
        if has_satellite_dimension_folders(info):
            expected += [f"{info.level_name}_nether", f"{info.level_name}_the_end"]
        return [name for name in expected if name not in self.members]


def list_world_backups(info: "ServerInfo") -> list:
    """Every backup this server has, newest first, grouped into sets.

    A split (Bukkit) world is archived as one file per world folder sharing a
    single timestamp, so the returned unit is a BackupSet rather than a file.
    Grouping is on the raw stamp text, not the parsed datetime: the members
    are written seconds apart -- one confirmed sample is named 17-00-39 while
    its newest entry is stamped 17:00:38 -- and the stamp is the only thing
    they agree on."""
    provider_by_dir = {}
    for provider in detect_backup_providers(info):
        for d in provider.search_dirs:
            try:
                provider_by_dir.setdefault(d.resolve(), provider.key)
            except OSError:
                pass

    sets = {}
    for folder in backup_search_dirs(info):
        try:
            folder_provider = provider_by_dir.get(folder.resolve())
        except OSError:
            folder_provider = None
        for fp in _iter_backup_archive_files(folder):
            parsed = parse_backup_archive_name(fp.name)
            if parsed is None:
                # Hand-renamed, or a shape we don't know. Still listed, keyed
                # on its own name so it forms a set of one.
                try:
                    dt = datetime.fromtimestamp(fp.stat().st_mtime)
                except OSError:
                    dt = None
                parsed = ParsedBackupName(fp.stem, dt, fp.name, "unknown")
            provider = parsed.provider
            if folder.name == "mcsm-safety":
                provider = "mcsm-safety"
            elif provider == "autobackup" and folder_provider in ("simplebackups",):
                # The two share a filename grammar; the folder disambiguates.
                provider = folder_provider
            elif parsed.provider == "unknown" and folder_provider:
                provider = folder_provider
            target = resolve_member_target(parsed.world, info, parsed.provider)
            if target is None:
                continue
            key = (provider, parsed.stamp)
            bset = sets.get(key)
            if bset is None:
                bset = sets[key] = BackupSet(stamp=parsed.stamp, dt=parsed.dt,
                                             provider=provider)
            try:
                size = fp.stat().st_size
            except OSError:
                size = 0
            bset.members.setdefault(
                target, BackupMember(path=fp, world_folder=parsed.world,
                                     size_bytes=size, target_folder=target))
    out = sorted(sets.values(), key=lambda s: s.dt or datetime.min, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Writing a backup
#
# Decision: mimic whatever convention the server's own backup tool uses, so
# the archives we write sit alongside its own rather than in a parallel
# folder nobody looks in. The costs of that are real and are spelled out for
# the user in the confirm dialog:
#
#   * their retention applies to ours -- AutoBackup keeps 15, SimpleBackups
#     10, Aroma 3.x 30 full backups -- so ours are eventually pruned;
#   * we cannot tell ours from theirs afterwards, which is why nothing in
#     this app ever offers to prune or bulk-delete backups;
#   * on Aroma we inherit its sidecar contract (see below).
# ---------------------------------------------------------------------------

@dataclass
class BackupConvention:
    provider: str
    directory: Path
    nested: bool           # world contents under "{folder}/" inside the archive
    date_nested: bool      # archives filed under {Y}/{M}/{D}/
    sidecar: Optional[str] # "backupinfo" | "backupstore" | None
    kind: str = "zip"      # container format: zip | tar | tar.gz

    @property
    def suffix(self) -> str:
        return {"tar": ".tar", "tar.gz": ".tar.gz"}.get(self.kind, ".zip")

    def archive_name(self, world_folder: str, dt: datetime) -> str:
        if self.provider == "aroma3":
            return (f"Backup--{world_folder}--{dt.strftime(AROMA3_DATE_FORMAT)}"
                    f"{self.suffix}")
        if self.provider == "aroma0":
            # 0.x predates the compressionType option and is always a zip.
            return (f"Backup-{world_folder}-{dt.year}-{dt.month}-{dt.day}"
                    f"--{dt.hour:02d}-{dt.minute:02d}.zip")
        return f"{world_folder}_{dt.strftime('%Y-%m-%d_%H-%M-%S')}.zip"

    def target_dir(self, level_name: str, dt: datetime) -> Path:
        d = self.directory
        if self.date_nested:
            d = d / str(dt.year) / str(dt.month) / str(dt.day)
        return d


def resolve_backup_convention(info: "ServerInfo",
                              purpose: str = "manual") -> BackupConvention:
    """How to write a backup on this server, following whatever is already
    installed. Falls back to the AutoBackup shape in backups/, which is the
    most common and matches what get_world_backups_dir already reads.

    purpose="safety" is the deliberate exception to the mimicry rule. A
    pre-rollback safety copy written under the local convention counts
    toward the installed tool's retention, and on a full folder that can
    evict the very archive the user is restoring *from*. It is the one
    backup whose entire purpose is to survive, so it goes somewhere we
    control and nothing else prunes."""
    if purpose == "safety":
        return BackupConvention("mcsm-safety",
                                get_backups_dir(info) / "mcsm-safety",
                                False, False, None)
    providers = {p.key: p for p in detect_backup_providers(info)}
    for key in ("aroma3", "aroma0", "simplebackups", "autobackup"):
        p = providers.get(key)
        if p is None:
            continue
        base = p.search_dirs[0] if p.search_dirs else get_backups_dir(info)
        if key == "aroma3":
            # Aroma can be configured to emit tar or tar.gz instead of zip.
            # Mimicry means matching that, or ours is the odd file out in a
            # folder the mod is pruning by pattern.
            kind = "zip"
            if p.config_path is not None:
                ctype = _read_forge_cfg(p.config_path).get("compressionType", "zip")
                if ctype in ("tar", "tar.gz"):
                    kind = ctype
            return BackupConvention("aroma3", base / info.level_name, False, False,
                                    "backupinfo", kind)
        if key == "aroma0":
            return BackupConvention("aroma0", base / info.level_name, True, True,
                                    "backupstore")
        if key == "simplebackups":
            return BackupConvention("simplebackups", base, True, False, None)
        return BackupConvention("autobackup", base, False, False, None)
    return BackupConvention("autobackup", get_backups_dir(info), False, False, None)


def _archive_world_folder(src: Path, dest_part: Path, arc_prefix: str,
                          kind: str = "zip", progress_cb=None,
                          cancel_event=None) -> tuple:
    """Archive one world folder to dest_part. Returns (files written, skipped).

    Always writes forward slashes, even when mimicking AutoBackup: its
    backslashes are out of spec, nothing reads ours by path except us, and
    the retention we are blending in with is filename-based."""
    count = 0
    skipped = []
    if kind == "zip":
        container = zipfile.ZipFile(dest_part, "w", zipfile.ZIP_DEFLATED)
        add = lambda src_file, arcname: container.write(src_file, arcname)
    else:
        container = tarfile.open(dest_part, "w:gz" if kind == "tar.gz" else "w")
        add = lambda src_file, arcname: container.add(src_file, arcname,
                                                      recursive=False)
    with container:
        for abs_path, rel_path in _iter_files(src):
            if cancel_event is not None and cancel_event.is_set():
                raise ExportCancelled()
            if abs_path.name in EXPORT_SKIP_FILES:
                continue
            arcname = arc_prefix + rel_path.as_posix()
            try:
                add(abs_path, arcname)
            except (OSError, ValueError):
                skipped.append(rel_path.as_posix())
                continue
            count += 1
            if progress_cb is not None:
                progress_cb(count, abs_path.name)
    return count, skipped


def create_world_backup(info: "ServerInfo", dt: Optional[datetime] = None,
                        progress_cb=None, cancel_event=None,
                        purpose: str = "manual",
                        skipped_out: Optional[list] = None) -> list:
    """Back this server's world up, following the local convention. Returns
    the archive paths written, newest-set-first order irrelevant.

    On a split world this writes one archive per world folder, all from a
    single datetime so they group as one set, and all-or-nothing: every
    archive is staged as a .part and only renamed once the last one closes.
    A half-written set must never look like a backup."""
    dt = dt or datetime.now()
    convention = resolve_backup_convention(info, purpose)
    target = convention.target_dir(info.level_name, dt)

    folders = [(info.level_name, get_world_dir(info))]
    if has_satellite_dimension_folders(info):
        folders.append((f"{info.level_name}_nether", get_nether_dir(info)))
        folders.append((f"{info.level_name}_the_end", get_the_end_dir(info)))
    folders = [(name, path) for name, path in folders if path.is_dir()]
    if not folders:
        raise OSError(f"No world folder found at {get_world_dir(info)}")

    # Two backups in the same second would collide. The stamp has seconds
    # resolution (Aroma's has minutes), so nudge the clock forward rather
    # than inventing a suffix -- a suffix would break the filename grammar
    # and orphan the set from its siblings.
    step = 60 if convention.provider in ("aroma3", "aroma0") else 1
    for _ in range(120):
        target = convention.target_dir(info.level_name, dt)
        names = [convention.archive_name(n, dt) for n, _ in folders]
        if not any((target / n).exists() for n in names):
            break
        dt = dt + timedelta(seconds=step)
    else:
        raise OSError(
            f"could not find a free backup name near {dt:%Y-%m-%d %H:%M:%S} "
            f"in {target} -- 120 consecutive slots are already taken")
    target.mkdir(parents=True, exist_ok=True)

    parts, finals, skipped = [], [], []
    try:
        for world_folder, src in folders:
            name = convention.archive_name(world_folder, dt)
            final = target / name
            part = target / (name + ".part")
            prefix = f"{world_folder}/" if convention.nested else ""
            parts.append(part)
            finals.append(final)
            _n, part_skipped = _archive_world_folder(
                src, part, prefix, convention.kind, progress_cb, cancel_event)
            skipped.extend(f"{world_folder}/{s}" for s in part_skipped)
    except BaseException:
        for part in parts:
            try:
                part.unlink()
            except OSError:
                pass
        raise

    for part, final in zip(parts, finals):
        os.replace(part, final)

    _write_backup_sidecar(info, convention, target, finals, dt)
    if skipped_out is not None:
        skipped_out.extend(skipped)
    return finals


def _write_backup_sidecar(info: "ServerInfo", convention: BackupConvention,
                          target: Path, archives: list, dt: datetime) -> None:
    """The metadata file the local convention expects beside an archive.

    Mimicry reverses the "never write a sidecar" default on Aroma servers:
    Aroma's own file says "Do not move, edit or delete this file. If you do,
    backups may not be automatically restorable", and AromaBackupRecovery
    reads it. An archive of ours dropped into an Aroma folder without one is
    exactly the half-a-backup that warning describes. Everywhere else this
    writes nothing.

    Deliberately never writes incremental.aromabackup -- ours are always full
    backups, and a manifest we generated would become the baseline Aroma
    diffs its next incremental against: a chain rooted in a file we wrote and
    do not maintain."""
    try:
        if convention.sidecar == "backupinfo":
            body = (
                "#=============================================\r\n"
                "#This is an important file for your backups.\r\n"
                "#Do not move, edit or delete this file.\r\n"
                "#If you do, backups may not be automatically restorable.\r\n"
                "#=============================================\r\n"
                f"#{dt.strftime('%a %b %d %H:%M:%S %Z %Y').replace('  ', ' ')}\r\n"
                f"date={int(dt.timestamp() * 1000)}\r\n"
                f"world={info.level_name}\r\n"
            )
            for archive in archives:
                # Not Path.with_suffix: it replaces only the final component,
                # so "Backup--world--….tar.gz" would become "….tar.backupinfo".
                sidecar_path = archive.with_name(
                    strip_archive_suffix(archive.name) + ".backupinfo")
                sidecar_path.write_bytes(body.encode("utf-8"))
        elif convention.sidecar == "backupstore":
            # Aroma 0.x keeps one shared index at the world's backup root
            # (above the {Y}/{M}/{D} nesting), one line per world. Append
            # rather than overwrite -- other worlds have lines in here.
            store = convention.directory / "backupstore.txt"
            line = (f"{info.level_name}={dt.year}={dt.month}={dt.day}"
                    f"={dt.hour}={dt.minute}\r\n")
            existing = ""
            if store.is_file():
                existing = store.read_text(encoding="utf-8", errors="replace")
                kept = [l for l in existing.replace("\r", "").splitlines()
                        if l.strip() and not l.startswith(f"{info.level_name}=")]
                existing = "".join(l + "\r\n" for l in kept)
            store.write_text(existing + line, encoding="utf-8", newline="")
    except OSError:
        # The archives are already in place and are the actual backup; a
        # sidecar we couldn't write is worth neither failing nor rolling back.
        pass


# ---------------------------------------------------------------------------
# Restoring
# ---------------------------------------------------------------------------

class RestoreRevertError(Exception):
    """A restore failed *and* the original could not be moved back. Carries
    the paths the untouched world is sitting under, because that is the one
    thing the user has to be told."""


RESTORE_SCOPES = ("all", "world", "players")

# Folders inside a world that hold per-player state, in both layouts. An
# archive carries whichever the server was running when it was taken, which
# is not necessarily what it runs now, so both are always matched.
PLAYER_SUBPATHS = ("playerdata/", "players/", "stats/", "advancements/")


def _is_player_path(rel: str) -> bool:
    return any(rel == p.rstrip("/") or rel.startswith(p) for p in PLAYER_SUBPATHS)


def restore_backup_set(info: "ServerInfo", bset: "BackupSet", scope: str = "all",
                       progress_cb=None, cancel_event=None) -> dict:
    """Restore a whole backup set over the live world.

    Every member is unpacked to a sibling temp folder first, then applied --
    never straight over a live world. For scope "all" each world folder is
    swapped out wholesale and the displaced copy is kept as
    {folder}.old-{stamp} until every member has landed, so a failure part way
    through a three-dimension restore can be undone. Partial scopes copy over
    the top instead (overwrite-only; full replacement is what "all" is for).

    Returns {"restored": [folder names], "files": n}."""
    if scope not in RESTORE_SCOPES:
        raise ValueError(f"unknown restore scope: {scope}")

    allow = world_folder_allowlist(info)
    targets = []
    seen_dest = {}
    for key, member in sorted(bset.members.items()):
        folder = member.target_folder or key
        if folder.casefold() not in allow:
            raise ValueError(
                f"refusing to restore into {folder!r}: it is not one of "
                f"{info.level_name}'s world folders")
        folder = allow[folder.casefold()]
        dest = info.path / folder
        try:
            resolved = dest.resolve()
        except OSError:
            resolved = dest
        if resolved in seen_dest:
            raise ValueError(
                f"this backup has two archives for {folder!r} "
                f"({seen_dest[resolved].name} and {member.path.name}) -- "
                "restoring it could destroy the folder it set aside")
        seen_dest[resolved] = member.path
        targets.append((folder, member, dest))
    if not targets:
        raise ValueError("this backup has no archives that belong to this world")

    stamp = bset.stamp.replace(":", "-").replace(" ", "_")
    temp_dirs, swapped = [], []
    files_done = 0
    try:
        # Phase 1: unpack everything. Nothing live is touched yet, so a bad
        # archive discovered here costs nothing.
        staged = []
        for world_folder, member, dest in targets:
            archive = open_backup_archive(member.path)
            if archive is None:
                raise OSError(f"{member.path.name} is not a readable archive")
            with archive:
                problem = archive.incremental_reason()
                if problem is not None:
                    raise ValueError(
                        f"{member.path.name} cannot be restored: {problem}")
                tmp = dest.parent / f"{dest.name}.restore-tmp"
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir(parents=True)
                temp_dirs.append(tmp)
                for rel, zinfo in archive.world_entries():
                    if cancel_event is not None and cancel_event.is_set():
                        raise ExportCancelled()
                    if scope == "world" and _is_player_path(rel):
                        continue
                    if scope == "players" and not _is_player_path(rel):
                        continue
                    safe = safe_archive_relpath(rel)
                    if safe is None:
                        continue
                    out_path = tmp / safe
                    # Belt and braces over safe_archive_relpath: confirm the
                    # resolved destination really is inside the temp folder
                    # before opening it for writing.
                    try:
                        out_path.resolve().relative_to(tmp.resolve())
                    except ValueError:
                        continue
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(rel if not archive.world_root
                                      else archive.world_root + rel) as src, \
                            open(out_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    files_done += 1
                    if progress_cb is not None:
                        progress_cb(files_done, rel)
                staged.append((world_folder, tmp, dest))

        # Phase 2: apply. For "all" this is a directory swap per member; the
        # displaced copies are only deleted once every member has landed.
        for world_folder, tmp, dest in staged:
            if scope == "all":
                old = None
                if dest.exists():
                    old = dest.parent / f"{dest.name}.old-{stamp}"
                    if old.exists():
                        shutil.rmtree(old, ignore_errors=True)
                    os.replace(dest, old)
                swapped.append((dest, old))
                os.replace(tmp, dest)
            else:
                for src_file in tmp.rglob("*"):
                    if src_file.is_dir():
                        continue
                    rel = src_file.relative_to(tmp)
                    out = dest / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, out)
                shutil.rmtree(tmp, ignore_errors=True)
        temp_dirs = [t for t in temp_dirs if t.exists()]
    except BaseException as exc:
        stranded = []
        for dest, old in reversed(swapped):
            if old is None:
                continue
            try:
                if dest.exists():
                    shutil.rmtree(dest)
                os.replace(old, dest)
            except OSError:
                stranded.append(old)
        for tmp in temp_dirs:
            shutil.rmtree(tmp, ignore_errors=True)
        if stranded:
            raise RestoreRevertError(
                f"{exc}\n\nThe rollback failed and the original could not be "
                "put back automatically. Nothing has been deleted -- your "
                "world is intact under:\n\n"
                + "\n".join(f"    {p}" for p in stranded)
                + "\n\nRename it back before starting the server, or the "
                  "server will generate a new empty world.") from exc
        raise

    for _dest, old in swapped:
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    for tmp in temp_dirs:
        shutil.rmtree(tmp, ignore_errors=True)

    return {"restored": [t[0] for t in targets], "files": files_done}
