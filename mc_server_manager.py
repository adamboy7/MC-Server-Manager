#!/usr/bin/env python3
"""
Minecraft Server Manager
=========================
A desktop GUI for browsing a folder full of Minecraft servers: shows the
world preview (if any), detected server type (Bukkit/Plugins/Forge/Vanilla),
detected Minecraft version, and the player roster (with face textures and
operator highlighting).

Requirements:
    pip install Pillow requests

Usage:
    python mc_server_manager.py

Notes
-----
* Version detection reads Data.Version.Name out of level.dat using a small
  hand-rolled NBT reader (no nbtlib dependency needed). That reader is lossy
  by design -- it collapses NBT's integer widths onto Python int and returns
  lists and the three array tags alike as plain lists -- so it must never be
  used as the basis for writing a file back out. The one setting that lives
  in level.dat (Allow Cheats, i.e. Data.allowCommands) is therefore applied
  by locating that tag's byte offset and patching it in place; see
  set_nbt_byte.
* Platform detection names the actual server software -- Vanilla, CraftBukkit,
  Spigot, Paper, Purpur, Fabric, Quilt, Forge, NeoForge, SpongeVanilla,
  SpongeForge or SpongeNeo -- from two independent probes: what is installed
  in the folder (jar manifests and loader directories) and what last ran on
  the world (level.dat). Both are kept: when they disagree, the world was
  moved between platforms and that is worth telling the user. See the
  "Platform detection" section for the full signal list, and the trap list at
  the end of it for the tempting shortcuts that are wrong.
* Player roster is built from the world's playerdata filenames (the UUIDs
  Minecraft uses on disk), cross-referenced against usercache.json /
  ops.json / whitelist.json for display names and operator status. Those
  files moved from {world}/playerdata/ to {world}/players/data/ in 26.x, so
  the folder is resolved rather than assumed -- see "World layout".
* "Export World" writes the world out in the vanilla single-folder layout.
  Bukkit splits the three dimensions across {world}, {world}_nether and
  {world}_the_end; vanilla/singleplayer nests them as {world}, {world}/DIM-1
  and {world}/DIM1. Exporting a Bukkit server folds the satellite folders'
  DIM payloads back in; a single-folder world is copied as-is, so the menu
  entry means the same thing on every server type.
* Geyser/Floodgate (Bedrock cross-play) UUIDs are not real Mojang UUIDs, so
  looking them up against the skin API will fail. Rather than let that
  surface as a parse error, we detect them ahead of time by UUID version:
  legitimate Mojang UUIDs are version 3 (offline-mode, name-derived) or
  version 4 (online-mode, random). Anything else is flagged as "Bedrock?"
  and given a placeholder face instead of hitting the network for it.
"""

import gzip
import io
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid as uuid_lib
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk, ImageDraw
except ImportError:
    raise SystemExit("Missing dependency: Pillow. Install with: pip install Pillow")

try:
    import requests
except ImportError:
    raise SystemExit("Missing dependency: requests. Install with: pip install requests")


# ---------------------------------------------------------------------------
# Minimal big-endian NBT reader -- just enough to pull the version string
# and level name out of level.dat. Handles gzip transparently.
# ---------------------------------------------------------------------------

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12


class NBTReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, n):
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        if len(b) < n:
            raise ValueError("Unexpected end of NBT data")
        return b

    def read_byte(self):
        return struct.unpack(">b", self.read(1))[0]

    def read_ubyte(self):
        return struct.unpack(">B", self.read(1))[0]

    def read_short(self):
        return struct.unpack(">h", self.read(2))[0]

    def read_int(self):
        return struct.unpack(">i", self.read(4))[0]

    def read_long(self):
        return struct.unpack(">q", self.read(8))[0]

    def read_float(self):
        return struct.unpack(">f", self.read(4))[0]

    def read_double(self):
        return struct.unpack(">d", self.read(8))[0]

    def read_string(self):
        length = struct.unpack(">H", self.read(2))[0]
        return self.read(length).decode("utf-8", errors="replace")

    def read_tag_payload(self, tag_type):
        if tag_type == TAG_BYTE:
            return self.read_byte()
        if tag_type == TAG_SHORT:
            return self.read_short()
        if tag_type == TAG_INT:
            return self.read_int()
        if tag_type == TAG_LONG:
            return self.read_long()
        if tag_type == TAG_FLOAT:
            return self.read_float()
        if tag_type == TAG_DOUBLE:
            return self.read_double()
        if tag_type == TAG_BYTE_ARRAY:
            n = self.read_int()
            return list(struct.unpack(f">{n}b", self.read(n))) if n else []
        if tag_type == TAG_STRING:
            return self.read_string()
        if tag_type == TAG_LIST:
            item_type = self.read_ubyte()
            n = self.read_int()
            return [self.read_tag_payload(item_type) for _ in range(n)]
        if tag_type == TAG_COMPOUND:
            compound = {}
            while True:
                t = self.read_ubyte()
                if t == TAG_END:
                    break
                name = self.read_string()
                compound[name] = self.read_tag_payload(t)
            return compound
        if tag_type == TAG_INT_ARRAY:
            n = self.read_int()
            return list(struct.unpack(f">{n}i", self.read(4 * n))) if n else []
        if tag_type == TAG_LONG_ARRAY:
            n = self.read_int()
            return list(struct.unpack(f">{n}q", self.read(8 * n))) if n else []
        raise ValueError(f"Unknown NBT tag type: {tag_type}")

    def skip_tag_payload(self, tag_type):
        """Advance past a tag's payload without building Python objects for
        it. Used by the offset finder, which only needs to navigate the
        structure -- decoding every string and array along the way would be
        wasted work, and for fixed-width types we can jump the whole payload
        in one step."""
        fixed = {
            TAG_BYTE: 1, TAG_SHORT: 2, TAG_INT: 4, TAG_LONG: 8,
            TAG_FLOAT: 4, TAG_DOUBLE: 8,
        }
        if tag_type in fixed:
            self.read(fixed[tag_type])
            return
        if tag_type == TAG_BYTE_ARRAY:
            self.read(self.read_int())
            return
        if tag_type == TAG_STRING:
            self.read(struct.unpack(">H", self.read(2))[0])
            return
        if tag_type == TAG_INT_ARRAY:
            self.read(4 * self.read_int())
            return
        if tag_type == TAG_LONG_ARRAY:
            self.read(8 * self.read_int())
            return
        if tag_type == TAG_LIST:
            item_type = self.read_ubyte()
            n = self.read_int()
            if item_type in fixed:
                # Homogeneous fixed-width payloads have no per-item framing,
                # so the whole list is one jump.
                self.read(fixed[item_type] * n)
                return
            for _ in range(n):
                self.skip_tag_payload(item_type)
            return
        if tag_type == TAG_COMPOUND:
            while True:
                t = self.read_ubyte()
                if t == TAG_END:
                    return
                self.read(struct.unpack(">H", self.read(2))[0])  # member name
                self.skip_tag_payload(t)
        raise ValueError(f"Unknown NBT tag type: {tag_type}")

    def read_compound_pruned(self, skip_paths: frozenset, prefix: tuple = ()):
        """read_tag_payload(TAG_COMPOUND), except that any member whose path
        from the root appears in skip_paths is stepped over with
        skip_tag_payload instead of being built into Python objects.

        Only compounds that actually contain a skip target are walked this
        way; everything else falls through to the normal reader, so the
        pruning check costs nothing on the vast majority of tags."""
        compound = {}
        while True:
            t = self.read_ubyte()
            if t == TAG_END:
                return compound
            name = self.read_string()
            here = prefix + (name,)
            if here in skip_paths:
                self.skip_tag_payload(t)
                continue
            if t == TAG_COMPOUND and any(p[:len(here)] == here for p in skip_paths):
                compound[name] = self.read_compound_pruned(skip_paths, here)
            else:
                compound[name] = self.read_tag_payload(t)

    def read_root(self, skip_paths=()):
        t = self.read_ubyte()
        if t != TAG_COMPOUND:
            raise ValueError("Root NBT tag is not a compound")
        _root_name = self.read_string()
        if not skip_paths:
            return self.read_tag_payload(TAG_COMPOUND)
        return self.read_compound_pruned(frozenset(skip_paths))


LEVEL_DAT_BULK_SUBTREES = frozenset({
    ("fml", "Registries"),          # modern FML (Forge 1.13+, NeoForge)
    ("FML", "ItemData"),            # legacy FML (<=1.12)
    ("FML", "BlockAliases"),
    ("FML", "ItemAliases"),
    ("FML", "BlockSubstitutions"),
    ("FML", "ItemSubstitutions"),
    ("FML", "BlockedItemIds"),
})


def load_nbt_file(path: Path, skip_paths=()) -> dict:
    """Load a (possibly gzipped) big-endian NBT file, return the root compound
    dict. skip_paths is an iterable of tuples naming subtrees to step over
    rather than decode -- see LEVEL_DAT_BULK_SUBTREES."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return NBTReader(raw).read_root(skip_paths)


def load_level_dat(path: Path) -> dict:
    """level.dat's whole root compound, with the mod-loader registries pruned.

    Note this is the *root*, not root["Data"]: the loader fingerprints live in
    siblings of Data ("fml"/"FML" on Forge-family servers, "SpongeData" on
    older SpongeForge), and they are the most reliable platform evidence a
    world carries."""
    return load_nbt_file(path, LEVEL_DAT_BULK_SUBTREES)


def nbt_path(root, *keys, default=None):
    """Walk a chain of compound keys, returning default the moment anything
    is missing or isn't a dict. Saves every caller writing the same nested
    isinstance/get dance against files whose shape varies by version."""
    node = root
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


# ---------------------------------------------------------------------------
# Targeted NBT editing
#
# read_tag_payload above is deliberately lossy -- it collapses NBT's several
# integer widths onto Python int and returns TAG_List, TAG_Byte_Array,
# TAG_Int_Array and TAG_Long_Array all as plain lists. That's fine for pulling
# out a version string, but it means the dict it produces cannot be serialised
# back to NBT without corrupting the file: a level.dat's Player.UUID (an int
# array) and DragonFight.Gateways (a list of ints) would come back with the
# wrong tag types, and an empty list would lose its element type entirely.
#
# So editing here never rebuilds the file. We parse only far enough to learn
# the byte offset of the one value we want, then patch that byte in place and
# leave every other byte exactly as it was.
# ---------------------------------------------------------------------------


def find_tag_offset(raw: bytes, path: tuple):
    """Locate a named tag inside decompressed NBT data by its compound path,
    e.g. ("Data", "allowCommands").

    Returns (payload_offset, tag_type), where payload_offset is the index of
    the tag's first payload byte -- for a TAG_Byte that's the value itself.
    Returns None if any path component is missing. Raises ValueError on
    malformed data (propagated from the reader)."""
    r = NBTReader(raw)
    if r.read_ubyte() != TAG_COMPOUND:
        raise ValueError("Root NBT tag is not a compound")
    r.read_string()  # root name -- after this we're at the root members

    for depth, target in enumerate(path):
        last = depth == len(path) - 1
        while True:
            tag_type = r.read_ubyte()
            if tag_type == TAG_END:
                return None  # ran out of members in this compound
            name = r.read_string()
            if name != target:
                r.skip_tag_payload(tag_type)
                continue
            if last:
                return r.pos, tag_type
            if tag_type != TAG_COMPOUND:
                return None  # path continues but this tag can't be descended
            break  # r.pos is now the first member of the child compound
    return None


def find_compound_offset(raw: bytes, path: tuple):
    """Byte offset of the first member entry inside the compound at `path`,
    or None if that path isn't a compound. This is where a new tag can be
    spliced in -- inserting at the front avoids having to find the matching
    TAG_End, and NBT compounds are unordered so position carries no meaning."""
    found = find_tag_offset(raw, path)
    if found is None:
        return None
    offset, tag_type = found
    return offset if tag_type == TAG_COMPOUND else None


def encode_byte_tag(name: str, value: int) -> bytes:
    """A complete TAG_Byte entry -- type, name, payload -- as it appears
    inside a compound."""
    encoded = name.encode("utf-8")
    return bytes([TAG_BYTE]) + struct.pack(">H", len(encoded)) + encoded + struct.pack(">b", value)


def read_nbt_byte(path: Path, tag_path: tuple) -> Optional[int]:
    """Current value of a TAG_Byte in an NBT file, or None if it isn't there
    (or isn't a byte). Reads the file directly rather than going through
    load_nbt_file so callers get a straight answer about presence."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    found = find_tag_offset(raw, tag_path)
    if found is None:
        return None
    offset, tag_type = found
    if tag_type != TAG_BYTE:
        return None
    return struct.unpack(">b", raw[offset:offset + 1])[0]


def set_nbt_byte(path: Path, tag_path: tuple, value: int, backup_suffix: str = ".mcsm-bak") -> str:
    """Set a TAG_Byte in a gzipped NBT file, patching bytes in place.

    Returns "unchanged", "patched" (the tag existed and its byte was flipped)
    or "inserted" (the tag was absent and a new entry was spliced into its
    parent compound). Raises ValueError if the parent compound doesn't exist
    or the target name is taken by a non-byte tag, OSError on IO failure.

    The original is copied to <name><backup_suffix> first, and the new file is
    written to a temp file in the same directory then os.replace()d over the
    original, so an interrupted write can never leave a truncated level.dat."""
    original = path.read_bytes()
    gzipped = original[:2] == b"\x1f\x8b"
    raw = gzip.decompress(original) if gzipped else original

    found = find_tag_offset(raw, tag_path)
    if found is not None:
        offset, tag_type = found
        if tag_type != TAG_BYTE:
            raise ValueError(
                f"{'.'.join(tag_path)} is NBT tag type {tag_type}, expected a byte"
            )
        if raw[offset] == value:
            return "unchanged"
        patched = raw[:offset] + struct.pack(">b", value) + raw[offset + 1:]
        outcome = "patched"
    else:
        parent = find_compound_offset(raw, tuple(tag_path[:-1]))
        if parent is None:
            raise ValueError(f"No compound at {'.'.join(tag_path[:-1]) or '<root>'}")
        patched = raw[:parent] + encode_byte_tag(tag_path[-1], value) + raw[parent:]
        outcome = "inserted"

    payload = gzip.compress(patched) if gzipped else patched

    shutil.copy2(path, path.with_name(path.name + backup_suffix))
    tmp = path.with_name(path.name + ".mcsm-tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return outcome


ALLOW_COMMANDS_PATH = ("Data", "allowCommands")


def get_level_dat_path(info: "ServerInfo") -> Path:
    return get_world_dir(info) / "level.dat"


def read_allow_commands(info: "ServerInfo") -> Optional[bool]:
    """Whether cheats are enabled for this world, or None if level.dat is
    missing/unreadable or carries no allowCommands tag. Worlds created by a
    dedicated server normally have the tag set to 0; worlds imported from
    singleplayer or made by much older versions sometimes lack it."""
    level_dat = get_level_dat_path(info)
    if not level_dat.is_file():
        return None
    try:
        value = read_nbt_byte(level_dat, ALLOW_COMMANDS_PATH)
    except Exception:
        # A damaged level.dat can fail out of gzip, zlib or struct in several
        # different ways depending on how it's damaged; the same broad catch
        # detect_server uses when reading the version applies here. Callers
        # only need to know the value is unavailable.
        return None
    return None if value is None else bool(value)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PlayerInfo:
    uuid: str
    name: str
    is_op: bool
    is_bedrock: bool
    is_banned: bool = False
    ban_reason: Optional[str] = None
    is_whitelisted: bool = False


@dataclass
class BackupEntry:
    path: Path
    dt: Optional[datetime]  # None if the filename didn't match the expected pattern


@dataclass
class Platform:
    """What a server folder is running, and how sure we are.

    Filled in by detect_platform() from two independent probes -- see the
    "Platform detection" section for what each one reads and why both are
    kept rather than collapsed into a single answer."""
    loader: Optional[str] = None          # "Paper", "NeoForge", "SpongeNeo", ...
    family: str = "unknown"               # see LOADER_FAMILIES
    mc_version: Optional[str] = None
    loader_version: Optional[str] = None
    # DataVersion is absent entirely before 1.9, so this is display/ordering
    # information only -- never branch layout decisions on it (see
    # world_paths() for why the layout is probed per-key instead).
    data_version: Optional[int] = None
    confidence: str = "unknown"           # certain | likely | conflict | unknown
    install_loader: Optional[str] = None
    world_loader: Optional[str] = None
    evidence: list = field(default_factory=list)
    mods_dir: Optional[Path] = None
    plugin_dirs: list = field(default_factory=list)
    # Mods the loader actually loaded, {mod_id: version}, straight out of
    # level.dat. Includes jar-in-jar dependencies that have no file in mods/,
    # so len() of this and the jar count legitimately differ.
    loaded_mods: dict = field(default_factory=dict)

    @property
    def display_version(self) -> str:
        if self.mc_version and self.loader_version and self.family != "bukkit":
            return f"{self.mc_version} ({self.loader} {self.loader_version})"
        return self.mc_version or "Unknown"

    @property
    def tags(self) -> list:
        """Short labels for the server list's Type column."""
        if self.loader is None:
            return ["Unrecognised"]
        out = [self.loader]
        if self.confidence == "conflict" and self.world_loader:
            out.append(f"was {self.world_loader}")
        elif self.confidence == "likely":
            out.append("?")
        return out


@dataclass
class ServerInfo:
    path: Path
    name: str
    platform: Platform
    version: str
    last_log_date: str
    level_name: str
    icon_path: Optional[Path]
    players: list
    motd: str = ""
    seed: Optional[str] = None
    difficulty: str = "Unknown"
    # Mod count (Forge servers only) is computed eagerly in detect_server
    # since a non-recursive directory listing is cheap. Folder sizes are
    # expensive (recursive) so they're computed lazily on first selection
    # instead -- see App._compute_sizes. Both are cached here so re-selecting
    # an already-scanned server in the tree reuses the cached value instead
    # of recomputing; they're only refreshed by a manual Rescan, which
    # replaces this ServerInfo instance entirely.
    mod_count: Optional[int] = None
    # Jars parked as *.jar.disabled in the same folder -- still "installed"
    # from the user's point of view, but not loaded.
    disabled_mod_count: int = 0
    world_size_bytes: Optional[int] = None
    total_size_bytes: Optional[int] = None
    backup_size_bytes: Optional[int] = None
    sizes_computed: bool = False

    @property
    def tags(self) -> list:
        return self.platform.tags


# ---------------------------------------------------------------------------
# Detection / parsing logic
# ---------------------------------------------------------------------------

def _unescape_properties_value(v: str) -> str:
    """Reverse Java Properties-style backslash escaping (e.g. "\\:" -> ":",
    "\\n" -> newline). Mainly matters for motd, which often contains a
    colon (server list ping separates name/motd on ':') that server.properties
    escapes on write."""
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), v)


def read_server_properties(server_path: Path) -> dict:
    props = {}
    fp = server_path / "server.properties"
    if fp.exists():
        try:
            for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                props[k.strip()] = _unescape_properties_value(v.strip())
        except OSError:
            pass
    return props


def _escape_properties_value(v: str) -> str:
    """Inverse of _unescape_properties_value -- escape backslashes and colons
    (the server-list-ping name/motd separator) plus newline/tab/carriage
    return so a value round-trips through server.properties as a single
    line."""
    v = v.replace("\\", "\\\\")
    v = v.replace(":", "\\:")
    v = v.replace("\n", "\\n")
    v = v.replace("\t", "\\t")
    v = v.replace("\r", "\\r")
    return v


def update_server_properties(server_path: Path, updates: dict) -> None:
    """Rewrite server.properties with the given key -> value updates applied
    in place, preserving existing line order/comments and appending any keys
    that weren't already present. Values are escaped the same way vanilla
    writes them."""
    fp = server_path / "server.properties"
    lines = []
    if fp.exists():
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()

    remaining = dict(updates)
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in remaining:
                new_lines.append(f"{k}={_escape_properties_value(remaining.pop(k))}")
                continue
        new_lines.append(line)
    for k, v in remaining.items():
        new_lines.append(f"{k}={_escape_properties_value(v)}")

    fp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


BOOL_PROPERTY_DEFAULTS = {
    "allow-flight": False,
    "enable-command-block": False,
    "pvp": True,
    "white-list": False,
}


def parse_bool_property(props: dict, key: str) -> bool:
    raw = props.get(key)
    if raw is None:
        return BOOL_PROPERTY_DEFAULTS.get(key, False)
    return raw.strip().lower() == "true"


DIFFICULTY_LEGACY_NAMES = {"0": "Peaceful", "1": "Easy", "2": "Normal", "3": "Hard"}
DIFFICULTY_NAMES = {"peaceful", "easy", "normal", "hard"}


def parse_difficulty(props: dict) -> str:
    """Difficulty as specified in server.properties. Pre-1.8 servers stored
    this as a legacy numeric id (0=peaceful, 1=easy, 2=normal, 3=hard)
    instead of a name; that's silently converted to the modern name here."""
    raw = props.get("difficulty", "").strip().lower()
    if raw in DIFFICULTY_LEGACY_NAMES:
        return DIFFICULTY_LEGACY_NAMES[raw]
    if raw in DIFFICULTY_NAMES:
        return raw.capitalize()
    return "Unknown"


UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_valid_uuid_format(u: str) -> bool:
    """Strict check for the canonical 8-4-4-4-12 dashed hex form Mojang/Geyser
    playerdata filenames use. Rejects anything uuid_lib.UUID() would otherwise
    accept loosely (no dashes, braces, urn: prefix, trailing junk like
    ' - Copy', stray whitespace, etc.) so that stray/duplicate files dropped
    into playerdata/ (e.g. "<uuid> - Copy.dat" from a manual backup) don't get
    parsed as players."""
    return bool(UUID_RE.match(u))


def is_probably_bedrock_uuid(u: str) -> bool:
    """Geyser/Floodgate UUIDs aren't real Mojang UUIDs. Legit Mojang UUIDs are
    version 3 (offline-mode, name-derived) or version 4 (online-mode). Anything
    else is almost certainly a synthesized cross-play UUID."""
    try:
        parsed = uuid_lib.UUID(u)
    except ValueError:
        return True
    return parsed.version not in (3, 4)


def load_name_map(server_path: Path):
    """Build uuid(lower, dashed) -> display name, the set of operator uuids,
    a uuid -> ban reason map for banned players, and the set of whitelisted
    uuids."""
    names = {}
    ops = set()
    banned = {}
    whitelisted = set()
    for fname, kind in (
        ("usercache.json", None),
        ("ops.json", "ops"),
        ("whitelist.json", "whitelist"),
        ("banned-players.json", "banned"),
    ):
        fp = server_path / fname
        if not fp.exists():
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            u = entry.get("uuid")
            n = entry.get("name")
            if u and n:
                names[u.lower()] = n
            if kind == "ops" and u:
                ops.add(u.lower())
            if kind == "banned" and u:
                banned[u.lower()] = entry.get("reason") or None
            if kind == "whitelist" and u:
                whitelisted.add(u.lower())
    return names, ops, banned, whitelisted


def get_world_dir(info: "ServerInfo") -> Path:
    return info.path / info.level_name


def uses_players_folder(world_dir: Path) -> bool:
    """True for the 26.x layout, where per-player files live under
    world/players/ instead of directly in the world folder."""
    return (world_dir / "players").is_dir()


def get_playerdata_dir(info: "ServerInfo") -> Path:
    """Where this world's <uuid>.dat files live. Also the destination for
    restores, so it resolves to the right folder even when that folder does
    not exist yet."""
    world_dir = get_world_dir(info)
    if uses_players_folder(world_dir):
        return world_dir / "players" / "data"
    return world_dir / "playerdata"


def get_player_advancements_dir(info: "ServerInfo") -> Path:
    world_dir = get_world_dir(info)
    if uses_players_folder(world_dir):
        return world_dir / "players" / "advancements"
    return world_dir / "advancements"


def get_player_stats_dir(info: "ServerInfo") -> Path:
    world_dir = get_world_dir(info)
    if uses_players_folder(world_dir):
        return world_dir / "players" / "stats"
    return world_dir / "stats"


def world_data_dirs(info: "ServerInfo") -> list:
    """Every folder that might hold the world-level .dat files that used to
    live inside level.dat, most authoritative first.

    Three shapes, and a server can present more than one at once:

      world/data/minecraft/     26.x, namespaced
      world/data/               pre-26, and 26.x for anything unnamespaced
      world/dimensions/minecraft/overworld/data/minecraft/
                                Paper and its forks

    That last one is the surprise. Paper keeps all three dimensions in a
    single folder but still treats each as a separate Bukkit world, so it
    writes a complete per-dimension data set and leaves the world-level
    folder without the pieces vanilla puts there -- world_gen_settings.dat
    among them. Vanilla, CraftBukkit and Spigot all write it at the world
    level, so both locations have to be searched, per file rather than per
    folder: on Paper the world-level folder exists, it just doesn't have the
    file in it."""
    world_dir = get_world_dir(info)
    data_dir = world_dir / "data"
    overworld = world_dir / "dimensions" / "minecraft" / "overworld" / "data"
    return [data_dir / "minecraft", data_dir, overworld / "minecraft", overworld]


def get_world_data_dir(info: "ServerInfo") -> Path:
    """The primary world data folder -- first of world_data_dirs() that
    exists, falling back to the conventional location so callers building a
    path to write always get something sensible."""
    for candidate in world_data_dirs(info):
        if candidate.is_dir():
            return candidate
    return get_world_dir(info) / "data"


def read_world_data_nbt(info: "ServerInfo", filename: str) -> Optional[dict]:
    """Payload of one of the split-out world data files, e.g.
    "world_gen_settings.dat". Each wraps its contents as
    {"data": {...}, "DataVersion": n}; the inner compound is returned.
    None if the file is absent everywhere, or unreadable."""
    for folder in world_data_dirs(info):
        path = folder / filename
        if not path.is_file():
            continue
        try:
            root = load_nbt_file(path)
        except Exception:
            continue
        data = root.get("data")
        if isinstance(data, dict):
            return data
    return None


def read_world_seed(info: "ServerInfo") -> Optional[str]:
    """The world seed, wherever this version keeps it. Returned as a string
    because it's only ever displayed and copied -- a 64-bit seed is not
    reliably round-trippable through anything narrower."""
    gen = read_world_data_nbt(info, "world_gen_settings.dat")   # 26.x
    if gen is not None and "seed" in gen:
        return str(gen["seed"])
    level_dat = get_level_dat_path(info)
    if not level_dat.is_file():
        return None
    try:
        data = load_level_dat(level_dat).get("Data") or {}
    except Exception:
        return None
    nested = nbt_path(data, "WorldGenSettings", "seed")          # 1.16 - 1.21.10
    if nested is not None:
        return str(nested)
    if "RandomSeed" in data:                                     # pre-1.16
        return str(data["RandomSeed"])
    return None


def get_playerdata_dir_for_zip_match(name: str) -> bool:
    """Whether a path inside a backup zip is a playerdata file, under either
    layout: <world>/playerdata/<uuid>.dat or
    <world>/players/data/<uuid>.dat."""
    parent = Path(name).parent
    if parent.name == "playerdata":
        return True
    return parent.name == "data" and parent.parent.name == "players"


def get_banned_players_path(info: "ServerInfo") -> Path:
    return info.path / "banned-players.json"


def get_whitelist_path(info: "ServerInfo") -> Path:
    return info.path / "whitelist.json"


def get_ops_path(info: "ServerInfo") -> Path:
    return info.path / "ops.json"


# What each ops.json permission level actually grants, for the Op
# confirmation dialog -- "level 4" means nothing to most users on its own.
OP_LEVEL_DESCRIPTIONS = {
    0: "no extra permissions",
    1: "bypass spawn protection",
    2: "use most build/cheat commands (e.g. /give, /tp, command blocks)",
    3: "use player-management commands (/ban, /kick, /whitelist, /op)",
    4: "full server command access, including /stop",
}


def default_op_level(info: "ServerInfo") -> int:
    """Permission level newly-opped players get, read from server.properties'
    op-permission-level; falls back to 4 (vanilla default) when the key is
    absent or unparseable."""
    props = read_server_properties(info.path)
    try:
        return int(props.get("op-permission-level", "4"))
    except (TypeError, ValueError):
        return 4


def server_looks_live(info: "ServerInfo") -> Optional[str]:
    """Best-effort check for whether the server appears to be running right
    now. Returns a human-readable reason if so, else None -- callers decide
    whether that's worth a warning or a hard block.

    session.lock is held open by the server process for as long as it's up
    and is rewritten on every world save, so a recently-touched lock file
    plus a level.dat that can't be opened for reading is as close to "it's
    running" as we can tell from outside the process."""
    world_dir = get_world_dir(info)
    lock_path = world_dir / "session.lock"
    if not lock_path.exists():
        return None
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        age = None
    if age is not None and age > 300:
        return None
    try:
        with open(world_dir / "level.dat", "rb"):
            pass
    except OSError:
        return "level.dat can't currently be opened for reading -- the server is likely running."
    if age is not None:
        return "session.lock was updated recently -- the server may be running."
    return None


def get_backups_dir(info: "ServerInfo") -> Path:
    return info.path / "backups"


def get_world_backups_dir(info: "ServerInfo") -> Path:
    """The folder that actually holds this world's dated backup archives --
    prefer the world-specific subfolder if present, falling back to the
    top-level backups folder otherwise (same layout Browse Backups... has
    always assumed)."""
    backups_dir = get_backups_dir(info)
    world_backups_dir = backups_dir / info.level_name
    return world_backups_dir if world_backups_dir.is_dir() else backups_dir


# ---------------------------------------------------------------------------
# World export -- flattening the Bukkit split-world layout back to vanilla
#
# Vanilla/singleplayer keeps every dimension inside one save folder:
#     world/level.dat, world/region/, world/DIM-1/region/, world/DIM1/region/
# Bukkit (and Spigot/Paper) splits them into three sibling server folders:
#     world/region/, world_nether/DIM-1/region/, world_the_end/DIM1/region/
# each with its own level.dat. Exporting is therefore a copy of the overworld
# folder plus a relocation of the other two dimensions' DIM folders into it.
# ---------------------------------------------------------------------------

# Bukkit writes a per-world uid.dat that pins the world's identity on that
# server; session.lock is just a runtime mutex. Neither belongs in an export --
# leaving them in makes the copy look "in use" to a client that opens it.
EXPORT_SKIP_FILES = {"session.lock", "uid.dat"}


def get_nether_dir(info: "ServerInfo") -> Path:
    return info.path / f"{info.level_name}_nether"


def get_the_end_dir(info: "ServerInfo") -> Path:
    return info.path / f"{info.level_name}_the_end"


def has_satellite_dimension_folders(info: "ServerInfo") -> bool:
    """True when the nether and end live in sibling folders next to the
    overworld rather than inside it.

    This is a question about folder layout, not about server type, and the
    two stopped agreeing: CraftBukkit and Spigot still split, while Paper and
    Purpur -- every bit as much Bukkit servers -- keep all three dimensions
    inside the one world folder. Ask platform.family for the server type;
    ask this for what export has to do. It reads the disk each time because a
    folder could have been added or removed since the scan."""
    return (
        get_world_dir(info).is_dir()
        and get_nether_dir(info).is_dir()
        and get_the_end_dir(info).is_dir()
    )


# Vanilla's own name for each dimension inside a single world folder, in both
# layouts: (old DIM folder, 26.x dimensions/ path).
DIMENSION_FOLDER_NAMES = {
    "nether": ("DIM-1", Path("dimensions") / "minecraft" / "the_nether"),
    "the_end": ("DIM1", Path("dimensions") / "minecraft" / "the_end"),
}


def world_uses_dimensions_folder(world_dir: Path) -> bool:
    """True for the 26.x layout, where dimensions sit under
    dimensions/minecraft/<id>/ instead of DIM-1/ and DIM1/."""
    return (world_dir / "dimensions").is_dir()


def dimension_export_name(world_dir: Path, dim_key: str) -> Path:
    """Where a folded-in dimension has to land inside the exported world, in
    whichever layout the overworld folder is already using. Mixing the two
    would produce a world the game reads as empty."""
    old_name, new_path = DIMENSION_FOLDER_NAMES[dim_key]
    return new_path if world_uses_dimensions_folder(world_dir) else Path(old_name)


def resolve_dimension_source(dim_folder: Path, dim_key: str) -> Optional[Path]:
    """Locate the payload inside one of Bukkit's satellite world folders.

    Three shapes turn up, and all three are checked because a server can be
    upgraded in place and end up with either:

      {world}_nether/dimensions/minecraft/the_nether/region/   26.x
      {world}_nether/DIM-1/region/                             pre-26
      {world}_nether/region/                                   hand-assembled
                                                               from a
                                                               singleplayer
                                                               save, or left
                                                               that way by
                                                               some multiworld
                                                               plugins

    Returns None when none of them is present, so the caller can skip the
    folder rather than export an empty dimension."""
    old_name, new_path = DIMENSION_FOLDER_NAMES[dim_key]
    for candidate in (dim_folder / new_path, dim_folder / old_name):
        if candidate.is_dir():
            return candidate
    if (dim_folder / "region").is_dir():
        return dim_folder
    return None


class ExportCancelled(Exception):
    """Raised inside the worker thread when the user cancels an export."""


def _iter_files(src: Path):
    """Every regular file under src, as (absolute_path, relative_path) pairs.
    Symlinks are skipped for the same reason compute_folder_size skips them:
    a world folder should never contain one, and following it risks copying
    something enormous (or looping) from outside the world."""
    try:
        with os.scandir(src) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                for abs_path, rel_path in _iter_files(Path(entry.path)):
                    yield abs_path, Path(entry.name) / rel_path
            else:
                yield Path(entry.path), Path(entry.name)
        except OSError:
            continue


def plan_world_export(info: "ServerInfo") -> list:
    """Build the full list of (source_file, destination_relative_path) pairs
    for exporting this server's world, resolved up front so the progress
    dialog can show a real total and so nothing is written until the whole
    plan is known.

    For a split (Bukkit) world the overworld folder maps to the root of the
    export and the two satellite folders' DIM payloads are folded in under
    DIM-1/ and DIM1/. For a single-folder world it's a plain recursive copy.
    Either way EXPORT_SKIP_FILES are dropped."""
    world_dir = get_world_dir(info)
    plan = []

    for abs_path, rel_path in _iter_files(world_dir):
        if abs_path.name in EXPORT_SKIP_FILES:
            continue
        plan.append((abs_path, rel_path))

    if has_satellite_dimension_folders(info):
        satellites = (
            (get_nether_dir(info), "nether"),
            (get_the_end_dir(info), "the_end"),
        )
        for folder, dim_key in satellites:
            source = resolve_dimension_source(folder, dim_key)
            if source is None:
                continue
            # The destination name follows the overworld folder's layout, not
            # the satellite's: exporting a 26.x nether into DIM-1/ (or a
            # pre-26 one into dimensions/minecraft/) produces a world the
            # game silently reads as empty.
            dest_prefix = dimension_export_name(world_dir, dim_key)
            for abs_path, rel_path in _iter_files(source):
                if abs_path.name in EXPORT_SKIP_FILES:
                    continue
                # The satellite folder's own level.dat is a stub describing
                # that dimension as if it were a standalone world. Vanilla
                # reads only the overworld's level.dat, and a stray copy at
                # DIM-1/level.dat would be ignored at best and confusing at
                # worst -- drop it. Anything deeper (DIM-1/data/…) is kept.
                if rel_path.parent == Path(".") and abs_path.name == "level.dat":
                    continue
                plan.append((abs_path, dest_prefix / rel_path))

    return plan


def export_world(plan: list, dest: Path, progress_cb=None, cancel_event=None) -> int:
    """Execute an export plan into dest, which must not already exist -- it's
    created here so a cancel/failure can remove it wholesale without any risk
    of deleting pre-existing user files.

    progress_cb(files_done, bytes_done) is called after each file. Raises
    ExportCancelled if cancel_event is set between files; the caller is
    responsible for cleaning up dest in that case. Returns bytes copied."""
    dest.mkdir(parents=True)
    files_done = 0
    bytes_done = 0
    for src, rel in plan:
        if cancel_event is not None and cancel_event.is_set():
            raise ExportCancelled()
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        files_done += 1
        try:
            bytes_done += target.stat().st_size
        except OSError:
            pass
        if progress_cb is not None:
            progress_cb(files_done, bytes_done)
    return bytes_done


BACKUP_FILENAME_RE_TEMPLATE = r"^Backup--{level_name}--(\d{{4}}-\d{{2}}-\d{{2}}--\d{{2}}-\d{{2}})\.zip$"
BACKUP_DATE_FORMAT = "%Y-%m-%d--%H-%M"


def parse_backup_filename(fname: str, level_name: str) -> Optional[datetime]:
    """Parse the date out of a "Backup--{level_name}--{date}.zip" filename,
    e.g. "Backup--Madlands--2024-01-16--21-59.zip". Returns None if the
    filename doesn't match that pattern (e.g. a renamed/manual backup)."""
    pattern = BACKUP_FILENAME_RE_TEMPLATE.format(level_name=re.escape(level_name))
    m = re.match(pattern, fname)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), BACKUP_DATE_FORMAT)
    except ValueError:
        return None


def list_world_backups(info: "ServerInfo") -> list:
    """All dated backup zips for this world, newest first. Filenames that
    don't match the expected "Backup--{level_name}--{date}.zip" pattern are
    still included (falling back to the file's mtime) rather than silently
    dropped."""
    backups_dir = get_world_backups_dir(info)
    entries = []
    try:
        zips = sorted(backups_dir.glob("*.zip"))
    except OSError:
        return []
    for fp in zips:
        dt = parse_backup_filename(fp.name, info.level_name)
        if dt is None:
            try:
                dt = datetime.fromtimestamp(fp.stat().st_mtime)
            except OSError:
                dt = None
        entries.append(BackupEntry(path=fp, dt=dt))
    entries.sort(key=lambda e: e.dt or datetime.min, reverse=True)
    return entries


def find_uuid_entries_in_zip(zf: zipfile.ZipFile, uuid: str) -> list:
    """Entries inside a backup zip that belong to the given player's
    playerdata (prefix match, same convention as _export_playerdata --
    catches stray "<uuid> - Copy.dat"-style files too). Tolerant of the
    backup being rooted at the world folder or not -- only the immediate
    parent folder name is checked, not the full path."""
    matches = []
    for name in zf.namelist():
        if not get_playerdata_dir_for_zip_match(name):
            continue
        if Path(name).name.startswith(uuid):
            matches.append(name)
    return matches


def compute_folder_size(path: Path) -> int:
    """Recursively sum file sizes under path via os.scandir (faster than
    Path.rglob() on directories with huge file counts, e.g. a web-map tile
    cache). Best-effort: unreadable/race-deleted entries are skipped rather
    than raised. Symlinks are not followed (avoids double-counting/loops)."""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        total += compute_folder_size(Path(entry.path))
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def count_mods(mods_dir: Path) -> tuple:
    """(enabled, disabled) .jar counts directly inside a mods folder.

    Non-recursive: subfolders like mods/disabled, mods/plugins (Sponge) or a
    per-modpack sideload dir aren't mod slots themselves. Renaming a jar to
    *.jar.disabled is the usual way to park a mod without deleting it, so
    those are counted separately rather than ignored -- they're still
    installed, just not loaded.

    Note this counts *files*, which is not the same as the number of mods the
    loader reports: jar-in-jar dependencies are bundled inside other jars and
    have no file of their own. See Platform.loaded_mods for that figure."""
    enabled = disabled = 0
    try:
        for f in mods_dir.iterdir():
            if not f.is_file():
                continue
            name = f.name.lower()
            if name.endswith(".jar"):
                enabled += 1
            elif name.endswith(".jar.disabled"):
                disabled += 1
    except OSError:
        pass
    return enabled, disabled


def format_platform_line(info: "ServerInfo") -> str:
    """The detail pane's "Type:" line. Kept out of the widget code so the
    wording can be tested without a display."""
    plat = info.platform
    if plat.loader is None or plat.confidence == "unknown":
        return "Type: Unrecognised"
    text = f"Type: {plat.loader}"
    if plat.confidence == "conflict" and plat.world_loader:
        # The installed loader is what will run next, but this world was last
        # opened by something else. Worth saying out loud rather than quietly
        # picking a winner -- it's the one thing someone would want to know
        # before starting the server on it.
        text += f"  (world last ran on {plat.world_loader})"
    elif plat.confidence == "likely":
        text += "  (probable)"
    return text


def format_version_line(info: "ServerInfo") -> str:
    """The detail pane's "Version:" line. Bukkit-family loader versions are a
    whole build string ("Paper/26.1.2-74-e4e17fc (MC: 26.1.2)/...") which is
    too long to append, and the vanilla "loader" has no version of its own."""
    plat = info.platform
    text = f"Version: {info.version}"
    if plat.loader_version and plat.family not in ("bukkit", "vanilla"):
        text += f"   {plat.loader} {plat.loader_version}"
    return text


def format_mods_line(info: "ServerInfo") -> Optional[str]:
    """The detail pane's "Mods:" line, or None for servers that don't load
    mods at all (in which case the label is hidden rather than showing 0)."""
    plat = info.platform
    if plat.mods_dir is None:
        return None
    text = f"Mods: {info.mod_count if info.mod_count is not None else 0}"
    if info.disabled_mod_count:
        text += f" (+{info.disabled_mod_count} disabled)"
    if plat.loaded_mods:
        # Higher than the jar count whenever a mod bundles its dependencies
        # inside itself, which is normal. Both numbers are shown because they
        # answer different questions: what's in the folder, and what ran.
        text += f", {len(plat.loaded_mods)} loaded"
    return text


def format_size(num_bytes: int) -> str:
    """Human-readable byte size: 512 -> '512 B', 1536 -> '1.5 KB'."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def find_last_log_date(server_path: Path) -> str:
    """Date (YYYY-MM-DD) the most recently modified file in {server}/logs
    was written, or a placeholder if no logs folder/files are found."""
    logs_dir = server_path / "logs"
    if not logs_dir.is_dir():
        return "No logs"
    try:
        files = [f for f in logs_dir.iterdir() if f.is_file()]
    except OSError:
        return "No logs"
    if not files:
        return "No logs"
    latest = max(files, key=lambda f: f.stat().st_mtime)
    return datetime.fromtimestamp(latest.stat().st_mtime).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Platform detection
#
# A server folder tells two separate stories and they answer different
# questions, so both are read and neither is allowed to silently overwrite
# the other:
#
#   probe_install()  what is installed right now. Works on a server that has
#                    never been started, and survives the world being deleted.
#   probe_world()    what last actually ran, from level.dat. Works when the
#                    jar has been renamed, moved or deleted, and carries the
#                    exact loader build.
#
# Agreement is the confidence signal. Disagreement is real information --
# it means the world was moved between platforms -- and is surfaced rather
# than resolved away.
#
# Every rule below was checked against a folder holding one server of each
# supported type spanning 1.7.10 to 26.2. The traps that cost the most are
# collected in the "do not use these alone" list at the end of this block.
# ---------------------------------------------------------------------------

LOADER_FAMILIES = {
    "Vanilla": "vanilla",
    "CraftBukkit": "bukkit", "Spigot": "bukkit", "Paper": "bukkit",
    "Purpur": "bukkit", "Folia": "bukkit", "Pufferfish": "bukkit", "Leaf": "bukkit",
    "Fabric": "fabric-like", "Quilt": "fabric-like",
    "Forge": "fml", "NeoForge": "fml",
    "SpongeVanilla": "sponge", "SpongeForge": "sponge", "SpongeNeo": "sponge",
}

# Manifest Main-Class -> what kind of launcher this jar is. Note that Paper,
# Purpur, Spigot and CraftBukkit all write "org.bukkit.craftbukkit.Main" into
# META-INF/main-class, so that file cannot be used here -- only the manifest
# attribute plus META-INF/versions.list separates them.
JAR_MAIN_CLASS_KINDS = {
    "net.minecraft.bundler.Main": "vanilla-bundler",
    "net.minecraft.server.MinecraftServer": "vanilla-legacy",
    "io.papermc.paperclip.Main": "paperclip",
    "org.bukkit.craftbukkit.bootstrap.Main": "bukkit-bootstrap",
    "net.fabricmc.installer.ServerLauncher": "fabric-launcher",
    "org.quiltmc.loader.impl.launch.server.QuiltServerLauncher": "quilt-launcher",
    "org.spongepowered.vanilla.installer.InstallerMain": "spongevanilla",
}

# Legacy Forge (<=1.12) ships no Main-Class at all -- it is launched through
# LaunchWrapper and identifies itself with this tweaker instead. The folder
# also contains an untouched minecraft_server.<version>.jar whose Main-Class
# *does* look like vanilla, so this check has to be able to override.
LEGACY_FML_TWEAKER = "cpw.mods.fml.common.launcher.FMLTweaker"

# level.dat brand strings -> our loader names. Case-folded on lookup.
WORLD_BRAND_NAMES = {
    "vanilla": "Vanilla", "fabric": "Fabric", "quilt": "Quilt",
    "forge": "Forge", "neoforge": "NeoForge",
    "craftbukkit": "CraftBukkit", "spigot": "Spigot", "paper": "Paper",
    "purpur": "Purpur", "folia": "Folia", "pufferfish": "Pufferfish", "leaf": "Leaf",
}

# Mod ids that mean a Sponge implementation is layered over an FML loader,
# most specific first. Matched case-insensitively.
SPONGE_MOD_IDS = (("spongeneo", "SpongeNeo"), ("spongeforge", "SpongeForge"))

MAX_PROBE_JAR_BYTES = 512 * 1024 * 1024  # skip anything absurd rather than map it


def _first_dir(parent: Path, pattern: str) -> Optional[Path]:
    """Lowest-sorting directory matching a glob, or None. Used for the
    single-entry version folders the loaders drop into libraries/."""
    try:
        return next((p for p in sorted(parent.glob(pattern)) if p.is_dir()), None)
    except OSError:
        return None


def _first_file(parent: Path, pattern: str) -> Optional[Path]:
    try:
        return next((p for p in sorted(parent.glob(pattern)) if p.is_file()), None)
    except OSError:
        return None


def _parse_properties_text(text: str) -> dict:
    return dict(re.findall(r"^([\w.\-]+)=(.*)$", text.replace("\r", ""), re.M))


def read_jar_markers(jar: Path) -> Optional[dict]:
    """Everything probe_install needs from one candidate server jar.

    Returns None when the file isn't a readable zip -- a half-downloaded or
    truncated jar is a completely ordinary thing to find in a server folder
    and must not abort the scan."""
    try:
        if jar.stat().st_size > MAX_PROBE_JAR_BYTES:
            return None
    except OSError:
        return None
    out = {"name": jar.name}
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
            if "META-INF/MANIFEST.MF" in names:
                manifest = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
                attrs = dict(re.findall(r"^([A-Za-z][A-Za-z-]*):[ \t]*(.+?)[ \t]*$",
                                        manifest.replace("\r", ""), re.M))
                out["main_class"] = attrs.get("Main-Class", "")
                out["tweak_class"] = attrs.get("TweakClass", "")
                out["spec_title"] = attrs.get("Specification-Title", "")
            out["kind"] = JAR_MAIN_CLASS_KINDS.get(out.get("main_class", ""), "")
            if "META-INF/versions.list" in names:
                lines = z.read("META-INF/versions.list").decode("utf-8", "replace").split("\n")
                first = next((l for l in lines if l.strip()), "")
                # "<sha> <id> <path>" (tab separated) on modern bundlers,
                # "<sha> *<file>" on the CraftBukkit/Spigot ones.
                out["bundled_jar"] = first.replace("\t", " ").split()[-1].lstrip("*") if first else ""
            for probe in ("version.json", "install.properties", "fmlversion.properties"):
                if probe in names:
                    blob = z.read(probe).decode("utf-8", "replace")
                    out[probe] = json.loads(blob) if probe.endswith(".json") \
                        else _parse_properties_text(blob)
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, json.JSONDecodeError):
        return None if "kind" not in out else out
    return out


def _brand_from_bundled_jar(bundled: str, fallback: str) -> str:
    """Loader name from a versions.list entry such as "26.2/purpur-26.2.jar"
    or "spigot-26.2-R0.1-SNAPSHOT.jar". Deliberately reads the filename
    prefix rather than testing against a fixed list, so a paperclip fork we
    have never seen still lands on its own name instead of being reported as
    Paper."""
    stem = bundled.split("/")[-1]
    prefix = stem.split("-")[0].strip()
    if not prefix:
        return fallback
    return WORLD_BRAND_NAMES.get(prefix.lower(), prefix[:1].upper() + prefix[1:])


def probe_install(server_path: Path) -> Platform:
    """What is installed in this folder, from the filesystem and jar
    manifests alone. Never touches the world."""
    p = Platform()
    ev = p.evidence
    mods = server_path / "mods"

    # 1. Sponge overlays first: SpongeNeo *is* a NeoForge install plus one mod
    #    jar, so checking the base loader first would shadow it.
    sponge_neo = _first_file(mods, "spongeneo-*.jar")
    sponge_forge = _first_file(mods, "spongeforge-*.jar")
    sponge_vanilla = _first_file(server_path, "spongevanilla-*.jar")

    # 2. Modern FML bases. These directories are also what start.bat actually
    #    invokes (via win_args.txt), and their names carry the versions.
    neo_dir = _first_dir(server_path, "libraries/net/neoforged/neoforge/*")
    forge_dir = _first_dir(server_path, "libraries/net/minecraftforge/forge/*")
    if neo_dir is not None:
        p.loader_version = neo_dir.name
        ev.append(f"libraries/net/neoforged/neoforge/{neo_dir.name}")
    if forge_dir is not None:
        ev.append(f"libraries/net/minecraftforge/forge/{forge_dir.name}")
        if "-" in forge_dir.name:
            p.mc_version, p.loader_version = forge_dir.name.split("-", 1)

    if sponge_neo is not None:
        p.loader = "SpongeNeo"
        ev.append(f"mods/{sponge_neo.name}")
    elif sponge_forge is not None:
        p.loader = "SpongeForge"
        ev.append(f"mods/{sponge_forge.name}")
    elif sponge_vanilla is not None:
        p.loader = "SpongeVanilla"
        ev.append(sponge_vanilla.name)
    elif neo_dir is not None:
        p.loader = "NeoForge"
    elif forge_dir is not None:
        p.loader = "Forge"

    # 3. Quilt before Fabric: Quilt reuses Fabric's plumbing, shipping
    #    libraries/net/fabricmc/sponge-mixin/ and running fabric-api out of
    #    mods/. Only the fabric-loader artifact is Fabric-specific.
    if p.loader is None:
        quilt_loader = _first_dir(server_path, "libraries/org/quiltmc/quilt-loader/*")
        fabric_loader = _first_dir(server_path, "libraries/net/fabricmc/fabric-loader/*")
        if quilt_loader is not None or (server_path / "quilt-server-launch.jar").is_file():
            p.loader = "Quilt"
            if quilt_loader is not None:
                p.loader_version = quilt_loader.name
                ev.append(f"libraries/org/quiltmc/quilt-loader/{quilt_loader.name}")
            else:
                ev.append("quilt-server-launch.jar")
        elif (fabric_loader is not None or (server_path / ".fabric").is_dir()
                or (server_path / "fabric-server-launch.jar").is_file()):
            p.loader = "Fabric"
            if fabric_loader is not None:
                p.loader_version = fabric_loader.name
                ev.append(f"libraries/net/fabricmc/fabric-loader/{fabric_loader.name}")
            else:
                ev.append(".fabric/ or fabric-server-launch.jar")

    # 4. Root jars. Six platforms ship a file literally called server.jar, so
    #    the filename is worthless and the manifest is everything.
    try:
        root_jars = sorted(j for j in server_path.glob("*.jar") if j.is_file())
    except OSError:
        root_jars = []
    for jar in root_jars:
        info = read_jar_markers(jar)
        if info is None:
            continue
        kind = info.get("kind", "")
        bundled = info.get("bundled_jar", "")
        if kind == "fabric-launcher":
            p.loader = p.loader or "Fabric"
            props = info.get("install.properties", {})
            p.mc_version = p.mc_version or props.get("game-version")
            p.loader_version = p.loader_version or props.get("fabric-loader-version")
            ev.append(f"{jar.name}: Fabric installer launcher")
        elif kind == "quilt-launcher":
            p.loader = p.loader or "Quilt"
            ev.append(f"{jar.name}: Quilt server launcher")
        elif kind == "spongevanilla":
            p.loader = "SpongeVanilla"
            ev.append(f"{jar.name}: {info.get('spec_title', 'SpongeVanilla')}")
        elif kind == "paperclip":
            p.loader = p.loader or _brand_from_bundled_jar(bundled, "Paper")
            ev.append(f"{jar.name}: paperclip -> {bundled or '?'}")
        elif kind == "bukkit-bootstrap":
            p.loader = p.loader or _brand_from_bundled_jar(bundled, "CraftBukkit")
            ev.append(f"{jar.name}: bukkit bootstrap -> {bundled or '?'}")
        elif kind in ("vanilla-bundler", "vanilla-legacy"):
            if p.loader is None:
                p.loader = "Vanilla"
            ev.append(f"{jar.name}: {kind}")
            if kind == "vanilla-legacy" and not p.mc_version:
                m = re.match(r"minecraft_server[._](.+)\.jar$", jar.name)
                if m:
                    p.mc_version = m.group(1)

        # Legacy Forge has no Main-Class, only the tweaker -- and it sits in
        # the same folder as an untouched vanilla server jar, so this must be
        # able to override a "Vanilla" verdict reached a few lines above.
        if info.get("tweak_class") == LEGACY_FML_TWEAKER:
            p.loader = "Forge"
            fml_props = info.get("fmlversion.properties", {})
            p.mc_version = fml_props.get("fmlbuild.mcversion") or p.mc_version
            m = re.match(r"forge-.+?-(\d+\.\d+[\w.]*)-.*universal\.jar$", jar.name)
            if m:
                p.loader_version = p.loader_version or m.group(1)
            ev.append(f"{jar.name}: TweakClass {LEGACY_FML_TWEAKER}")

        # version.json is only a Minecraft version on a genuine vanilla
        # bundle. Legacy Forge's universal jar carries a launcher profile
        # whose id is "1.7.10-Forge10.13.4.1614-1.7.10"; taking that as the
        # game version puts the whole string in the version column.
        version_json = info.get("version.json")
        if version_json and not p.mc_version and kind in ("vanilla-bundler", "paperclip"):
            p.mc_version = version_json.get("id")

    # 5. Config fallback for a folder whose jar has been deleted. Strictly
    #    most-specific-first: every fork also ships its ancestors' files, so
    #    Purpur has purpur.yml *and* paper-global.yml *and* spigot.yml *and*
    #    bukkit.yml.
    if p.loader is None:
        for marker, name in (
            ("purpur.yml", "Purpur"),
            ("config/paper-global.yml", "Paper"),
            ("spigot.yml", "Spigot"),
            ("bukkit.yml", "CraftBukkit"),
            ("config/fml.toml", "Forge"),
            ("config/forge.cfg", "Forge"),
        ):
            if (server_path / marker).is_file():
                p.loader = name
                ev.append(marker)
                break

    if (server_path / "config" / "sponge").is_dir():
        # Corroboration only -- it outlives an uninstall, so it never gets to
        # decide on its own.
        ev.append("config/sponge/")

    p.family = LOADER_FAMILIES.get(p.loader or "", "unknown")
    p.install_loader = p.loader
    return p


def probe_world(level_dat: Path) -> Platform:
    """What last ran on this world, from level.dat."""
    p = Platform()
    ev = p.evidence
    if not level_dat.is_file():
        ev.append("no level.dat")
        return p
    try:
        root = load_level_dat(level_dat)
    except Exception as e:
        # A damaged level.dat can fail out of gzip, zlib or struct in a
        # dozen ways; callers only need to know there's no answer here.
        ev.append(f"level.dat unreadable ({e})")
        return p

    data = root.get("Data") or {}
    version = data.get("Version") or {}
    p.mc_version = version.get("Name") if isinstance(version, dict) else None
    dv = data.get("DataVersion")
    p.data_version = dv if isinstance(dv, int) else None

    # 1. The mod list, which is a sibling of Data rather than inside it.
    #    Modern FML writes root["fml"]["LoadingModList"]; FML up to 1.12
    #    wrote root["FML"]["ModList"] with capitalised ids ("Forge", "FML")
    #    and no "minecraft" entry at all -- hence the case fold.
    mod_entries = (nbt_path(root, "fml", "LoadingModList", default=[])
                   or nbt_path(root, "FML", "ModList", default=[]) or [])
    for entry in mod_entries:
        if isinstance(entry, dict) and entry.get("ModId"):
            p.loaded_mods[str(entry["ModId"]).lower()] = entry.get("ModVersion")
    if p.loaded_mods:
        ev.append(f"level.dat mod list ({len(p.loaded_mods)} entries)")
        for mod_id, name in SPONGE_MOD_IDS:
            if mod_id in p.loaded_mods:
                p.loader, p.loader_version = name, p.loaded_mods[mod_id]
                break
        else:
            for mod_id, name in (("neoforge", "NeoForge"), ("forge", "Forge")):
                if mod_id in p.loaded_mods:
                    p.loader, p.loader_version = name, p.loaded_mods[mod_id]
                    break
    if "Forge" in root:
        ev.append("root Forge compound (legacy)")
    if "SpongeData" in root:
        ev.append("root SpongeData compound")

    # 2. Bukkit stamps its exact build into the world.
    bukkit_version = data.get("Bukkit.Version")
    if isinstance(bukkit_version, str) and bukkit_version and p.loader is None:
        brand = bukkit_version.split("/")[0]
        p.loader = WORLD_BRAND_NAMES.get(brand.lower(), brand)
        p.loader_version = bukkit_version
        ev.append(f"Bukkit.Version = {bukkit_version}")

    # 3. Enabled datapacks. This is the only thing in level.dat that
    #    identifies SpongeVanilla, which otherwise looks exactly like an
    #    unmodded server (see the ServerBrands note below).
    enabled = [str(x) for x in (nbt_path(data, "DataPacks", "Enabled", default=[]) or [])]
    if enabled:
        ev.append(f"DataPacks.Enabled = {enabled}")
    if p.loader is None:
        if "plugin-spongevanilla" in enabled:
            p.loader = "SpongeVanilla"
        elif any(x.startswith("plugin-sponge") for x in enabled):
            # Some Sponge implementation, but the datapack names don't say
            # which. Record the family and let the install probe name it.
            p.family = "sponge"
            ev.append("Sponge datapacks, implementation not named")
        elif "fabric" in enabled:
            p.loader = "Fabric"
        elif "quilt" in enabled:
            p.loader = "Quilt"
        elif "paper" in enabled:
            p.loader = "Paper"
        elif "mod_data" in enabled or any(x.startswith("mod:") for x in enabled):
            # 26.x FML writes "mod_data" whether it's Forge or NeoForge, so
            # this narrows to the family and nothing more.
            p.loader = None
            p.family = "fml"
            ev.append("FML-family datapacks, loader not distinguishable from datapacks alone")
        elif "file/bukkit" in enabled:
            p.loader = "CraftBukkit"

    # 4. Brands. Weakest signal of the four, for three separate reasons:
    #    it accumulates across every server that ever opened the world (so a
    #    Spigot world later run on Paper lists both); 26.x Sponge writes an
    #    empty list; and it doesn't exist at all before 1.13.
    brands = [str(b) for b in (data.get("ServerBrands") or []) if str(b)]
    if brands:
        ev.append(f"ServerBrands = {brands}")
    if p.loader is None and brands:
        p.loader = WORLD_BRAND_NAMES.get(brands[-1].lower(), brands[-1])

    # 5. Residue, for worlds too old or too sparse for anything above.
    if p.loader is None:
        if "forgeLifecycle" in data:
            p.family = "fml"
            ev.append("Data.forgeLifecycle")
        elif p.family == "unknown" and data:
            p.loader = "Vanilla"

    if p.loader:
        p.family = LOADER_FAMILIES.get(p.loader, p.family)
    p.world_loader = p.loader
    return p


def detect_platform(server_path: Path, level_name: str) -> Platform:
    """Run both probes and reconcile them."""
    install = probe_install(server_path)
    world = probe_world(server_path / level_name / "level.dat")

    p = install
    p.world_loader = world.world_loader
    p.data_version = world.data_version
    p.loaded_mods = world.loaded_mods
    p.evidence = [f"install: {e}" for e in install.evidence] + \
                 [f"world: {e}" for e in world.evidence]

    if install.install_loader and world.world_loader:
        p.confidence = "certain" if install.install_loader == world.world_loader else "conflict"
    elif install.install_loader or world.world_loader:
        p.confidence = "likely"
    else:
        p.confidence = "unknown"

    # The install answer wins a conflict because it describes what will run
    # next; the world answer is kept on world_loader and surfaced in the UI,
    # since "this world last ran on something else" is exactly the warning
    # someone wants before starting the server.
    if p.loader is None:
        p.loader = world.world_loader
        p.family = world.family if world.family != "unknown" else p.family
    if not p.mc_version:
        p.mc_version = world.mc_version
    if not p.loader_version:
        p.loader_version = world.loader_version
    if p.family == "unknown" and p.loader:
        p.family = LOADER_FAMILIES.get(p.loader, "unknown")

    mods_dir = server_path / "mods"
    # SpongeVanilla is deliberately excluded: it has a mods/ folder, but only
    # as the container for mods/plugins -- it loads no mods, so reporting a
    # mod count for it would be answering a question it doesn't have.
    if p.family in ("fml", "fabric-like") or p.loader in ("SpongeForge", "SpongeNeo"):
        p.mods_dir = mods_dir if mods_dir.is_dir() else None
    if p.family == "bukkit":
        plugins = server_path / "plugins"
        p.plugin_dirs = [plugins] if plugins.is_dir() else []
    elif p.family == "sponge":
        p.plugin_dirs = sponge_plugin_dirs(server_path)

    return p


def sponge_plugin_dirs(server_path: Path) -> list:
    """Where a Sponge server keeps its plugins.

    Sponge ships an empty top-level plugins/ folder and puts the real one at
    mods/plugins/ -- but that path is configurable, and config/sponge/
    launch.properties records where it actually is, so read that when it
    exists. The top-level folder is included only if somebody has actually
    put something in it."""
    configured = None
    launch_props = server_path / "config" / "sponge" / "launch.properties"
    if launch_props.is_file():
        try:
            props = _parse_properties_text(
                launch_props.read_text(encoding="utf-8", errors="replace"))
            raw = props.get("additional-plugins-directory", "")
            if raw:
                rel = (raw.replace("${MODS_DIR}", "mods")
                          .replace("${BASE_DIR}", ".").strip())
                candidate = (server_path / rel).resolve()
                if candidate.is_dir():
                    configured = candidate
        except (OSError, ValueError):
            configured = None
    dirs = []
    seen = set()

    def add(d: Optional[Path]):
        # Compare resolved paths: the configured directory comes back
        # absolute from launch.properties while the default is built by
        # joining, and the two are usually the same folder.
        if d is None or not d.is_dir():
            return
        try:
            key = d.resolve()
        except OSError:
            key = d
        if key in seen:
            return
        seen.add(key)
        dirs.append(d)

    add(configured)
    add(server_path / "mods" / "plugins")
    top = server_path / "plugins"
    try:
        # Sponge ships an empty top-level plugins/ folder that is not where
        # its plugins go; only surface it if somebody has actually used it.
        if top.is_dir() and any(top.iterdir()):
            add(top)
    except OSError:
        pass
    return dirs


# Signals that look decisive and are not. Every one of these is wrong for at
# least one real server type, so none of them appears above on its own:
#
#   mods/ exists                -> Forge      (Fabric, Quilt and Sponge use it;
#                                              Forge and NeoForge often leave
#                                              it empty)
#   plugins/ exists             -> Bukkit     (SpongeVanilla and SpongeForge
#                                              ship an empty one)
#   world + _nether + _the_end  -> Bukkit     (Paper and Purpur stopped
#                                              splitting; CraftBukkit and
#                                              Spigot still do)
#   the world folder is "world" -> always     (server.properties level-name
#                                              wins, and a stale world/ next
#                                              to the real one parses fine)
#   libraries/net/fabricmc/     -> Fabric     (Quilt ships sponge-mixin there)
#   mods/fabric-api-*.jar       -> Fabric     (Quilt runs Fabric API)
#   file/bukkit datapack        -> Bukkit     (Purpur 26.2 doesn't have it)
#   mod_data datapack           -> NeoForge   (plain Forge writes it too)
#   WasModded                   -> modded     (0 on 26.x Sponge, absent <1.13)
#   ServerBrands[0]             -> platform   (accumulates; empty on 26.x
#                                              Sponge; absent before 1.13)
#   root jar filename           -> platform   (six platforms use server.jar)
#   META-INF/main-class         -> platform   (Paper writes CraftBukkit's)
#   a jar's version.json id     -> mc version (legacy Forge's is a profile id)
#   a server jar has Main-Class -> always     (legacy Forge has only TweakClass)
#   config/fml.toml             -> Forge      (NeoForge and SpongeNeo too)
#   Data.version                -> mc version (it's the anvil format id, 19133
#                                              on every version we support)
#   DataVersion is present      -> always     (absent before 1.9)
#   mods/*.jar count            -> mods loaded (jar-in-jar deps have no file)


def detect_server(server_path: Path) -> ServerInfo:
    props = read_server_properties(server_path)
    level_name = props.get("level-name", "world")
    difficulty = parse_difficulty(props)

    world_dir = server_path / level_name

    platform = detect_platform(server_path, level_name)

    mod_count = disabled_mod_count = None
    if platform.mods_dir is not None:
        mod_count, disabled_mod_count = count_mods(platform.mods_dir)

    last_log_date = find_last_log_date(server_path)

    # Version comes from the platform probes, which read the game version out
    # of level.dat when the world has been generated and out of the installed
    # jar when it hasn't. Note there is deliberately no fall back to
    # Data.version: that tag is the anvil *format* id (19133 on every version
    # from 1.7.10 to 26.2), not a game version, and displaying it produced a
    # confident-looking "DataVersion 19133" for anything predating 1.9.
    level_dat = world_dir / "level.dat"
    if platform.mc_version:
        version = platform.mc_version
    elif not level_dat.exists():
        version = "No level.dat"
    elif any("unreadable" in e for e in platform.evidence):
        version = "Unreadable"
    else:
        version = "Unknown"

    icon_path = None
    for candidate in (server_path / "server-icon.png", world_dir / "icon.png"):
        if candidate.exists():
            icon_path = candidate
            break

    info = ServerInfo(
        path=server_path,
        name=server_path.name,
        platform=platform,
        version=version,
        last_log_date=last_log_date,
        level_name=level_name,
        icon_path=icon_path,
        players=[],
        motd=props.get("motd", ""),
        seed=None,
        difficulty=difficulty,
        mod_count=mod_count,
        disabled_mod_count=disabled_mod_count or 0,
    )

    # Both of these need the resolved world folder, so they run against the
    # ServerInfo rather than re-deriving the paths here. They also need to
    # cope with either world layout -- see the "World layout" section.
    info.seed = read_world_seed(info)

    names, ops, banned, whitelisted = load_name_map(server_path)

    players = []
    playerdata_dir = get_playerdata_dir(info)
    if playerdata_dir.is_dir():
        for dat in sorted(playerdata_dir.glob("*.dat")):
            u = dat.stem
            if not is_valid_uuid_format(u):
                # Not a real playerdata file -- e.g. a manually duplicated
                # "<uuid> - Copy.dat" backup left in the folder. Skip it here;
                # it's still picked up by _export_playerdata's uuid-prefix
                # glob when exporting the real player's files as a zip.
                continue
            key = u.lower()
            players.append(PlayerInfo(
                uuid=u,
                name=names.get(key, u),
                is_op=key in ops,
                is_bedrock=is_probably_bedrock_uuid(u),
                is_banned=key in banned,
                ban_reason=banned.get(key),
                is_whitelisted=key in whitelisted,
            ))

    info.players = players
    return info


def looks_like_server_folder(path: Path) -> bool:
    if (path / "server.properties").exists() or (path / "eula.txt").exists():
        return True
    # A root *.jar is not a reliable marker on its own: NeoForge and SpongeNeo
    # install no jar at the top level at all, launching out of libraries/
    # instead. Check for the loader directories those leave behind as well.
    for marker in ("libraries/net/neoforged/neoforge", "libraries/net/minecraftforge/forge",
                   "libraries/net/fabricmc/fabric-loader", "libraries/org/quiltmc/quilt-loader"):
        if (path / marker).is_dir():
            return True
    try:
        return any(path.glob("*.jar"))
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Face texture fetching (threaded, disk-cached)
# ---------------------------------------------------------------------------

CACHE_DIR = Path.home() / ".mc_server_manager_cache"
CACHE_DIR.mkdir(exist_ok=True)

FACE_URL_TEMPLATES = [
    "https://crafatar.com/avatars/{uuid}?size={size}&overlay",
    "https://mc-heads.net/avatar/{uuid}/{size}",
]

PREVIEW_SIZE = 128  # world icon preview box, in pixels

# Used for anything destructive or irreversible -- the Ban menu entry, and the
# Allow Cheats toggle, which unlike the rest of the Settings dialog writes into
# the world's level.dat rather than a plain-text config file.
DANGER_COLOR = "#CC0000"


# ---------------------------------------------------------------------------
# Username resolution (threaded, disk-cached)
# ---------------------------------------------------------------------------
# usercache.json only holds names for accounts the server has recently seen
# a login from (it's an LRU-style cache Mojang/Vanilla trims), and
# ops.json/whitelist.json/banned-players.json only cover the subset of
# players that were ever added to those lists. A playerdata/*.dat UUID can
# easily fall outside all four files (e.g. a player who joined once a long
# time ago) even though it's a perfectly valid Mojang UUID -- in that case
# we fall back to asking Mojang's session server directly, same as
# mcuuid.net does.

NAME_CACHE_FILE = CACHE_DIR / "name_cache.json"
_name_cache_lock = threading.Lock()

NAME_API_TEMPLATES = [
    "https://sessionserver.mojang.com/session/minecraft/profile/{uuid}",
    "https://api.mojang.com/user/profile/{uuid}",
]


def _load_name_cache() -> dict:
    if NAME_CACHE_FILE.exists():
        try:
            return json.loads(NAME_CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_name_cache(cache: dict) -> None:
    try:
        NAME_CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def fetch_username_from_api(u: str) -> Optional[str]:
    """Resolve a Mojang UUID to its current username via the session server,
    with a small disk cache so repeated lookups (across rescans/restarts)
    don't keep hitting the network for players that never change name."""
    key = u.lower()
    with _name_cache_lock:
        cache = _load_name_cache()
    if key in cache:
        return cache[key] or None

    trimmed = key.replace("-", "")
    resolved = None
    for template in NAME_API_TEMPLATES:
        url = template.format(uuid=trimmed)
        try:
            resp = requests.get(url, timeout=6)
            if resp.status_code == 204 or resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
            name = data.get("name") if isinstance(data, dict) else None
            if name:
                resolved = name
                break
        except Exception:
            continue

    with _name_cache_lock:
        cache = _load_name_cache()
        cache[key] = resolved or ""
        _save_name_cache(cache)
    return resolved


def fetch_face_image(u: str, size: int = 32) -> Optional[Image.Image]:
    cache_file = CACHE_DIR / f"{u}.png"
    if cache_file.exists():
        try:
            return Image.open(cache_file).convert("RGBA").resize((size, size), Image.NEAREST)
        except Exception:
            cache_file.unlink(missing_ok=True)

    for template in FACE_URL_TEMPLATES:
        url = template.format(uuid=u, size=size)
        try:
            resp = requests.get(url, timeout=6)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
            img.save(cache_file)
            return img.resize((size, size), Image.NEAREST)
        except Exception:
            continue
    return None


def placeholder_face(size: int = 32) -> Image.Image:
    img = Image.new("RGBA", (size, size), (90, 90, 90, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, size - 3, size - 3], outline=(200, 200, 200, 255))
    draw.line([2, 2, size - 3, size - 3], fill=(140, 140, 140, 255))
    draw.line([2, size - 3, size - 3, 2], fill=(140, 140, 140, 255))
    return img


class Tooltip:
    """Small hover tooltip attached to a single Tk widget."""

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _show(self, event=None):
        if self.tip_window or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#FFFFE0",
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
            wraplength=300,
        ).pack(ipadx=4, ipady=2)

    def _hide(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Minecraft Server Manager")
        self.geometry("1050x680")
        self.minsize(820, 520)

        self.root_folder: Optional[Path] = None
        self.servers: list = []
        self.tree_index: dict = {}
        self.image_refs: list = []  # keep PhotoImage refs alive, Tk drops GC'd images
        self.task_queue: "queue.Queue" = queue.Queue()
        self.current_server: Optional[ServerInfo] = None
        self._pending_world_size = ""
        self._pending_total_size = ""

        self._build_ui()
        self.after(100, self._poll_queue)

    # -- UI construction ---------------------------------------------------

    def _build_ui(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=8, pady=6)
        ttk.Button(toolbar, text="Select Servers Folder...", command=self.choose_folder).pack(side="left")
        self.folder_label = ttk.Label(toolbar, text="No folder selected")
        self.folder_label.pack(side="left", padx=10)
        ttk.Button(toolbar, text="Rescan", command=self.rescan).pack(side="right")

        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        # Left: server list
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        columns = ("tags", "date", "difficulty")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings")
        self.tree.heading("#0", text="Server")
        self.tree.heading("tags", text="Type")
        self.tree.heading("date", text="Date")
        self.tree.heading("difficulty", text="Difficulty")
        self.tree.column("#0", width=220)
        self.tree.column("tags", width=120)
        self.tree.column("date", width=100)
        self.tree.column("difficulty", width=80)
        self.tree.pack(fill="both", expand=True, side="left")
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select_server)
        self.tree.bind("<Button-3>", self._show_server_menu)

        # Right: details
        right = ttk.Frame(paned)
        paned.add(right, weight=2)

        details = ttk.Frame(right)
        details.pack(fill="x", pady=(0, 8))

        self.name_label = ttk.Label(details, text="Select a server", font=("Segoe UI", 14, "bold"))
        self.name_label.pack(anchor="w")

        self.motd_label = ttk.Label(
            details, text="", foreground="#555555", font=("Segoe UI", 9, "italic"),
            wraplength=360, justify="left",
        )
        # Not packed here -- only shown when the selected server has a motd set;
        # on_select_server() toggles it with .pack()/.pack_forget().

        self.preview_frame = preview_frame = tk.Frame(
            details, width=PREVIEW_SIZE, height=PREVIEW_SIZE,
            bg="#222222", relief="groove", bd=1,
        )
        preview_frame.pack_propagate(False)
        preview_frame.pack(anchor="w", pady=(6, 6))
        self.preview_label = tk.Label(preview_frame, bg="#222222",
                                       text="No\nPreview", fg="#888888", justify="center")
        self.preview_label.pack(fill="both", expand=True)
        preview_frame.bind("<Button-3>", self._show_preview_menu)
        self.preview_label.bind("<Button-3>", self._show_preview_menu)

        self.tags_label = ttk.Label(details, text="")
        self.tags_label.pack(anchor="w")
        self.version_label = ttk.Label(details, text="")
        self.version_label.pack(anchor="w")
        self.mods_label = ttk.Label(details, text="")
        # Not packed here -- only shown for servers that have a mods folder
        # (platform.mods_dir); on_select_server() toggles it with
        # .pack()/.pack_forget().
        self.path_label = ttk.Label(details, text="", foreground="#666666")
        self.path_label.pack(anchor="w")

        self.world_size_label = ttk.Label(details, text="", foreground="#666666")
        self.world_size_label.pack(anchor="w")

        self.backup_size_label = ttk.Label(details, text="", foreground="#666666")
        # Not packed here -- only shown when the selected server has a backups
        # folder; on_select_server() toggles it with .pack()/.pack_forget().

        players_header = ttk.Frame(right)
        players_header.pack(fill="x")
        ttk.Label(players_header, text="Players", font=("Segoe UI", 11, "bold")).pack(side="left")
        self.player_count_label = ttk.Label(players_header, text="")
        self.player_count_label.pack(side="left", padx=8)

        player_container = ttk.Frame(right)
        player_container.pack(fill="both", expand=True, pady=(4, 0))

        self.player_canvas = tk.Canvas(player_container, highlightthickness=0)
        player_vsb = ttk.Scrollbar(player_container, orient="vertical", command=self.player_canvas.yview)
        self.player_frame = ttk.Frame(self.player_canvas)
        self.player_frame.bind(
            "<Configure>",
            lambda e: self.player_canvas.configure(scrollregion=self.player_canvas.bbox("all")),
        )
        self.player_canvas.create_window((0, 0), window=self.player_frame, anchor="nw")
        self.player_canvas.configure(yscrollcommand=player_vsb.set)
        self.player_canvas.pack(side="left", fill="both", expand=True)
        player_vsb.pack(side="right", fill="y")
        self._bind_mousewheel(self.player_canvas)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x")

        # Styling for controls that write somewhere riskier than a config
        # file. Themes vary in how much of a widget's colour they'll cede to
        # a style (the Windows "vista" theme in particular draws its own
        # checkbox indicator), so anything using this pairs it with a plain
        # red ttk.Label, whose foreground every theme honours.
        style = ttk.Style(self)
        style.configure("Danger.TCheckbutton", foreground=DANGER_COLOR)
        style.map(
            "Danger.TCheckbutton",
            foreground=[("active", DANGER_COLOR), ("selected", DANGER_COLOR),
                        ("disabled", "#AA8888")],
        )

    def _bind_mousewheel(self, widget):
        """Scroll `widget` (a Canvas) on the mouse wheel, but only while the
        pointer is actually over it -- not globally. MouseWheel events go
        straight to whatever child widget (a player row's label, say) is
        under the cursor rather than bubbling up to the canvas, so a plain
        widget.bind() won't see them; the traditional workaround is
        bind_all(). But bind_all() is application-wide, so once bound it
        also hijacks wheel scrolling in *other* windows -- e.g. it would
        keep scrolling the player list while the Roll Back dialog is
        focused and being scrolled. To avoid that, only bind_all() while
        hovering `widget` or one of its descendants (toggled via Enter/
        Leave), and undo it the moment the pointer leaves. Call
        _hook_scroll_children() after adding new descendants (e.g. player
        rows) so they're covered too.
        """
        self._scroll_widget = widget

        def _is_scrollable():
            bbox = widget.bbox("all")
            return bool(bbox) and (bbox[3] - bbox[1]) > widget.winfo_height()

        def _scroll(units):
            if _is_scrollable():
                widget.yview_scroll(units, "units")

        def _bind_wheel(_event=None):
            widget.bind_all("<MouseWheel>", lambda e: _scroll(-1 if e.delta > 0 else 1))  # Windows/macOS
            widget.bind_all("<Button-4>", lambda e: _scroll(-1))  # Linux
            widget.bind_all("<Button-5>", lambda e: _scroll(1))

        def _unbind_wheel(_event=None):
            widget.unbind_all("<MouseWheel>")
            widget.unbind_all("<Button-4>")
            widget.unbind_all("<Button-5>")

        self._scroll_bind_wheel = _bind_wheel
        self._scroll_unbind_wheel = _unbind_wheel
        self._hook_scroll_children(widget)

    def _hook_scroll_children(self, widget):
        """Recursively wire up Enter/Leave hover tracking (see
        _bind_mousewheel) on `widget` and all of its current descendants.
        Call this again after adding new child widgets -- e.g. after
        rebuilding the player list -- so the wheel keeps working while
        hovering the new rows."""
        widget.bind("<Enter>", self._scroll_bind_wheel, add="+")
        widget.bind("<Leave>", self._scroll_unbind_wheel, add="+")
        for child in widget.winfo_children():
            self._hook_scroll_children(child)

    def _center_over_main_window(self, dialog: tk.Toplevel):
        """Position a Toplevel centered over the main window instead of
        wherever the OS/WM decides to place it (on Windows this is often a
        fixed cascade position with no relation to the main window or the
        mouse). Requires the dialog's widgets to already be packed so its
        requested size is known."""
        dialog.update_idletasks()
        w = dialog.winfo_width()
        h = dialog.winfo_height()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # -- Folder scanning -----------------------------------------------------

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Select folder containing Minecraft servers")
        if not folder:
            return
        self.root_folder = Path(folder)
        self.folder_label.config(text=str(self.root_folder))
        self.rescan()

    def rescan(self):
        if not self.root_folder:
            return
        self.tree.delete(*self.tree.get_children())
        self.status_var.set("Scanning...")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        servers = []
        try:
            subdirs = [p for p in sorted(self.root_folder.iterdir()) if p.is_dir()]
        except OSError as e:
            self.task_queue.put(("error", f"Could not read folder: {e}"))
            self.task_queue.put(("scan_done", []))
            return

        for sub in subdirs:
            if not looks_like_server_folder(sub):
                continue
            try:
                info = detect_server(sub)
                servers.append(info)
            except Exception as e:
                self.task_queue.put(("error", f"Failed to scan {sub.name}: {e}"))
        self.task_queue.put(("scan_done", servers))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.task_queue.get_nowait()
                if kind == "scan_done":
                    self.servers = payload
                    self._populate_tree()
                    self.status_var.set(f"Found {len(self.servers)} server(s)")
                elif kind == "error":
                    self.status_var.set(payload)
                elif kind == "face_ready":
                    # PhotoImage MUST be constructed on the main thread -- Tk is not
                    # thread-safe, so the background worker only fetched a PIL image.
                    pil_img, target_label = payload
                    if target_label.winfo_exists():
                        tkimg = ImageTk.PhotoImage(pil_img)
                        self.image_refs.append(tkimg)
                        target_label.configure(image=tkimg)
                elif kind == "name_ready":
                    target_label, resolved_name, suffix_bits = payload
                    if target_label.winfo_exists():
                        name_text = resolved_name + ("  [" + ", ".join(suffix_bits) + "]" if suffix_bits else "")
                        target_label.configure(text=name_text)
                elif kind == "preview_ready":
                    pil_img = payload
                    tkimg = ImageTk.PhotoImage(pil_img)
                    self.image_refs.append(tkimg)
                    self.preview_label.configure(image=tkimg, text="")
                elif kind == "world_size_ready":
                    info, size_bytes = payload
                    if info is self.current_server:
                        self._pending_world_size = format_size(size_bytes) if size_bytes is not None else "n/a"
                        self._update_size_label()
                elif kind == "total_size_ready":
                    info, size_bytes = payload
                    if info is self.current_server:
                        self._pending_total_size = format_size(size_bytes)
                        self._update_size_label()
                elif kind == "backup_size_ready":
                    info, size_bytes = payload
                    if info is self.current_server:
                        self.backup_size_label.config(text=f"Backup size: {format_size(size_bytes)}")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.tree_index = {}
        for s in self.servers:
            iid = self.tree.insert("", "end", text=s.name, values=(", ".join(s.tags), s.last_log_date, s.difficulty))
            self.tree_index[iid] = s

    # -- Server list context menu --------------------------------------------

    def _show_server_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        info = self.tree_index.get(iid)
        if not info:
            return
        has_backups = get_backups_dir(info).is_dir()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Open Folder...", command=lambda: self._open_server_folder(info))
        menu.add_command(
            label="Browse Backups...",
            command=lambda: self._open_backups_folder(info),
            state="normal" if has_backups else "disabled",
        )
        menu.add_command(
            label="Copy Seed",
            command=lambda: self._copy_seed(info),
            state="normal" if info.seed is not None else "disabled",
        )
        menu.add_command(
            label="Export World...",
            command=lambda: self._export_world(info),
            state="normal" if get_world_dir(info).is_dir() else "disabled",
        )
        menu.add_separator()
        menu.add_command(label="Set MOTD...", command=lambda: self._set_motd(info))
        menu.add_command(label="Settings...", command=lambda: self._open_settings(info))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_server_folder(self, info: ServerInfo):
        if not info.path.exists():
            messagebox.showwarning("Folder Not Found", f"{info.path} no longer exists.")
            return
        try:
            subprocess.run(["explorer", str(info.path)])
        except OSError as e:
            messagebox.showerror("Error", f"Could not open Explorer: {e}")

    def _copy_seed(self, info: ServerInfo):
        self.clipboard_clear()
        self.clipboard_append(info.seed)
        self.update()
        self.status_var.set(f"Copied seed for {info.name}")

    def _open_backups_folder(self, info: ServerInfo):
        backups_dir = get_backups_dir(info)
        if not backups_dir.is_dir():
            messagebox.showwarning("Folder Not Found", f"{backups_dir} no longer exists.")
            return
        target_dir = get_world_backups_dir(info)
        try:
            subprocess.run(["explorer", str(target_dir)])
        except OSError as e:
            messagebox.showerror("Error", f"Could not open Explorer: {e}")

    # -- World export ---------------------------------------------------------

    def _export_world(self, info: ServerInfo):
        """Copy this server's world out to a standalone folder in the vanilla
        single-folder layout, ready to drop into .minecraft/saves or a vanilla
        server. For a Bukkit-style split world the three dimension folders are
        merged back into one; for a single-folder world it's a plain copy, so
        the menu entry behaves the same way everywhere."""
        world_dir = get_world_dir(info)
        if not world_dir.is_dir():
            messagebox.showwarning("World Not Found", f"{world_dir} no longer exists.")
            return

        split = has_satellite_dimension_folders(info)
        parent = filedialog.askdirectory(
            title=f"Export {info.level_name} to folder...",
            mustexist=True,
        )
        if not parent:
            return
        parent_path = Path(parent)

        # The exported folder is named after the level, since that's the name
        # the world will show up under in the singleplayer save list.
        safe_level = re.sub(r'[\\/:*?"<>|]', "_", info.level_name) or "world"
        dest = parent_path / safe_level
        if dest.exists():
            alt = self._next_available_name(parent_path, safe_level)
            if not messagebox.askyesno(
                "Folder Exists",
                f"'{safe_level}' already exists in that folder.\n\n"
                f"Export as '{alt.name}' instead?",
            ):
                return
            dest = alt

        # Guard against writing the export inside one of the folders being
        # read. The plan is built before any file is written so this wouldn't
        # actually recurse forever, but it would leave a copy of the world
        # nested inside the live world -- never what anyone meant. Exporting
        # elsewhere under the server folder is fine and stays allowed.
        try:
            dest_resolved = dest.resolve()
            sources = [world_dir]
            if split:
                sources += [get_nether_dir(info), get_the_end_dir(info)]
            for source in sources:
                source_resolved = source.resolve()
                if dest_resolved == source_resolved or source_resolved in dest_resolved.parents:
                    messagebox.showerror(
                        "Invalid Destination",
                        f"Choose a destination outside {source.name} -- exporting a "
                        "world into itself would nest the copy inside the original.",
                    )
                    return
        except OSError:
            pass

        self._run_export(info, dest, split)

    @staticmethod
    def _next_available_name(parent: Path, base: str) -> Path:
        """First unused '{base} (n)' inside parent. Bounded rather than a bare
        while True so a pathological folder can't spin forever; after the cap
        we fall back to a timestamp, which is effectively guaranteed free."""
        for n in range(2, 1000):
            candidate = parent / f"{base} ({n})"
            if not candidate.exists():
                return candidate
        return parent / f"{base} {datetime.now().strftime('%Y-%m-%d--%H-%M-%S')}"

    def _run_export(self, info: ServerInfo, dest: Path, split: bool):
        """Modal progress dialog driving the export on a worker thread. The
        worker owns everything slow (walking the world to build the plan, then
        the copy itself) and reports through a shared state dict that the UI
        polls -- pushing one queue message per file would swamp the event loop
        on a world with tens of thousands of region/chunk files."""
        dialog = tk.Toplevel(self)
        dialog.title("Export World")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        summary = (
            f"Merging {info.level_name}, _nether and _the_end into one world folder"
            if split else
            f"Copying {info.level_name}"
        )
        ttk.Label(dialog, text=summary, font=("Segoe UI", 10, "bold")).pack(
            padx=14, pady=(14, 2), anchor="w"
        )
        ttk.Label(dialog, text=f"To: {dest}", foreground="#666666", wraplength=420).pack(
            padx=14, pady=(0, 10), anchor="w"
        )

        bar = ttk.Progressbar(dialog, mode="indeterminate", length=420)
        bar.pack(padx=14, pady=(0, 6))
        bar.start(15)

        detail_var = tk.StringVar(value="Scanning world...")
        ttk.Label(dialog, textvariable=detail_var, foreground="#555555").pack(
            padx=14, pady=(0, 10), anchor="w"
        )

        cancel_event = threading.Event()
        result_queue: "queue.Queue" = queue.Queue()
        # Written by the worker, read by the poller. Plain dict assignment is
        # atomic enough under the GIL for a progress readout; nothing here
        # needs a consistent multi-field snapshot.
        state = {"phase": "scan", "files": 0, "bytes": 0, "total": 0}

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(padx=14, pady=(0, 14), anchor="e")
        cancel_btn = ttk.Button(btn_frame, text="Cancel")
        cancel_btn.pack(side="right")

        def request_cancel():
            cancel_event.set()
            cancel_btn.configure(state="disabled")
            detail_var.set("Cancelling...")

        cancel_btn.configure(command=request_cancel)
        dialog.protocol("WM_DELETE_WINDOW", request_cancel)
        dialog.bind("<Escape>", lambda e: request_cancel())

        def worker():
            try:
                plan = plan_world_export(info)
                if cancel_event.is_set():
                    raise ExportCancelled()
                if not plan:
                    result_queue.put(("empty", None))
                    return
                state["total"] = len(plan)
                state["phase"] = "copy"

                def progress_cb(files_done, bytes_done):
                    state["files"] = files_done
                    state["bytes"] = bytes_done

                total_bytes = export_world(plan, dest, progress_cb, cancel_event)
                result_queue.put(("done", (len(plan), total_bytes)))
            except ExportCancelled:
                self._remove_partial_export(dest)
                result_queue.put(("cancelled", None))
            except OSError as e:
                self._remove_partial_export(dest)
                result_queue.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            if not dialog.winfo_exists():
                return
            try:
                status, payload = result_queue.get_nowait()
            except queue.Empty:
                if state["phase"] == "copy" and state["total"]:
                    if str(bar.cget("mode")) != "determinate":
                        bar.stop()
                        bar.configure(mode="determinate", maximum=state["total"])
                    bar["value"] = state["files"]
                    detail_var.set(
                        f"Copying {state['files']} of {state['total']} files "
                        f"({format_size(state['bytes'])})"
                    )
                dialog.after(100, poll)
                return

            bar.stop()
            dialog.grab_release()
            dialog.destroy()
            self._finish_export(info, dest, status, payload)

        dialog.after(100, poll)
        self._center_over_main_window(dialog)

    @staticmethod
    def _remove_partial_export(dest: Path):
        """Delete a half-written export. Safe because export_world creates dest
        itself and refuses to run if it already exists, so whatever is in there
        was written by this export and nothing else."""
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except OSError:
            pass

    def _finish_export(self, info: ServerInfo, dest: Path, status: str, payload):
        if status == "done":
            file_count, total_bytes = payload
            self.status_var.set(
                f"Exported {info.level_name} ({file_count} files, "
                f"{format_size(total_bytes)}) to {dest}"
            )
            if messagebox.askyesno(
                "Export Complete",
                f"Exported {info.level_name} to:\n{dest}\n\n"
                f"{file_count} files, {format_size(total_bytes)}.\n\n"
                "Open the folder now?",
            ):
                try:
                    subprocess.run(["explorer", str(dest)])
                except OSError as e:
                    messagebox.showerror("Error", f"Could not open Explorer: {e}")
        elif status == "cancelled":
            self.status_var.set(f"Export of {info.level_name} cancelled")
        elif status == "empty":
            messagebox.showwarning(
                "Nothing to Export", f"No world files found under {get_world_dir(info)}."
            )
        else:
            messagebox.showerror("Export Failed", f"Could not export the world: {payload}")
            self.status_var.set(f"Export of {info.level_name} failed")

    def _set_motd(self, info: ServerInfo):
        """Modal dialog to set/overwrite this server's motd, pre-filled with
        the current value. Writes straight to server.properties."""
        dialog = tk.Toplevel(self)
        dialog.title("Set MOTD")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        ttk.Label(dialog, text=f"MOTD for {info.name}:").pack(padx=12, pady=(12, 4), anchor="w")
        motd_var = tk.StringVar(value=info.motd)
        entry = ttk.Entry(dialog, textvariable=motd_var, width=50)
        entry.pack(padx=12, pady=(0, 12), fill="x")
        entry.focus_set()
        entry.select_range(0, "end")

        def on_ok(event=None):
            new_motd = motd_var.get()
            try:
                update_server_properties(info.path, {"motd": new_motd})
            except OSError as e:
                messagebox.showerror("Error", f"Could not write server.properties: {e}")
                return
            info.motd = new_motd
            dialog.destroy()
            if self.current_server is info:
                self.on_select_server(None)
            self.status_var.set(f"Updated MOTD for {info.name}")

        def on_cancel(event=None):
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(padx=12, pady=(0, 12), anchor="e")
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btn_frame, text="Save", command=on_ok).pack(side="right")

        dialog.bind("<Return>", on_ok)
        dialog.bind("<Escape>", on_cancel)
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        self._center_over_main_window(dialog)

    def _open_settings(self, info: ServerInfo):
        """Modal dialog with quick checkboxes for the handful of boolean
        server.properties toggles that get flipped often enough to warrant a
        shortcut. Re-reads properties fresh each time so it reflects any
        manual edits made outside this app.

        "Allow Cheats" is the odd one out and is styled red to say so: it
        lives in the world's level.dat, not server.properties, so saving it
        rewrites a binary world file rather than a line of text."""
        props = read_server_properties(info.path)
        allow_cheats_initial = read_allow_commands(info)
        level_dat = get_level_dat_path(info)
        level_dat_present = level_dat.is_file()

        dialog = tk.Toplevel(self)
        dialog.title(f"Settings - {info.name}")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        ttk.Label(
            dialog, text=f"Settings for {info.name}", font=("Segoe UI", 11, "bold")
        ).pack(padx=12, pady=(12, 8), anchor="w")

        toggles = (
            ("allow-flight", "Allow Flight"),
            ("enable-command-block", "Enable Command Block"),
            ("pvp", "PvP"),
            ("white-list", "Whitelist"),
        )
        vars_by_key = {}
        for key, label in toggles:
            var = tk.BooleanVar(value=parse_bool_property(props, key))
            vars_by_key[key] = var
            ttk.Checkbutton(dialog, text=label, variable=var).pack(padx=12, pady=2, anchor="w")

        # A world with no allowCommands tag reads as None; the tag gets
        # inserted on save, so the checkbox still works -- it just starts
        # from the same "off" state Minecraft assumes when the tag is absent.
        cheats_var = tk.BooleanVar(value=bool(allow_cheats_initial))
        cheats_box = ttk.Checkbutton(
            dialog, text="Allow Cheats", variable=cheats_var, style="Danger.TCheckbutton"
        )
        cheats_box.pack(padx=12, pady=2, anchor="w")

        # The red styling is the at-a-glance signal that this one isn't a
        # server.properties toggle; the tooltip carries the detail.
        tip = "Edits level.dat, stop the server first"
        if not level_dat_present:
            tip = f"level.dat not found in {info.level_name} -- cannot edit"
            cheats_box.configure(state="disabled")
        elif allow_cheats_initial is None:
            tip += " (this world has no allowCommands tag yet -- one will be added)"
        Tooltip(cheats_box, tip)

        def on_save(event=None):
            cheats_changed = (
                level_dat_present and cheats_var.get() != bool(allow_cheats_initial)
            )
            if cheats_changed and not self._confirm_cheats_change(
                info, level_dat, cheats_var.get(), allow_cheats_initial
            ):
                return

            updates = {key: ("true" if var.get() else "false") for key, var in vars_by_key.items()}
            try:
                update_server_properties(info.path, updates)
            except OSError as e:
                messagebox.showerror("Error", f"Could not write server.properties: {e}")
                return

            # server.properties is already saved at this point, so a level.dat
            # failure below reports itself but doesn't roll anything back --
            # the two files are independent and the properties write succeeded
            # on its own terms.
            cheats_note = ""
            if cheats_changed:
                try:
                    outcome = set_nbt_byte(
                        level_dat, ALLOW_COMMANDS_PATH, 1 if cheats_var.get() else 0
                    )
                except Exception as e:
                    # Broad on purpose: a corrupt or unexpected level.dat can
                    # surface as OSError, ValueError, EOFError, gzip.BadGzipFile,
                    # zlib.error or struct.error. In a GUI a dialog beats a
                    # traceback to a console nobody is watching.
                    messagebox.showerror(
                        "Error",
                        f"Saved server.properties, but could not write level.dat: {e}",
                    )
                    dialog.destroy()
                    return
                state = "enabled" if cheats_var.get() else "disabled"
                cheats_note = f", cheats {state}"
                if outcome == "inserted":
                    cheats_note += " (allowCommands tag added)"

            dialog.destroy()
            self.status_var.set(f"Updated settings for {info.name}{cheats_note}")

        def on_cancel(event=None):
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(padx=12, pady=(8, 12), anchor="e")
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btn_frame, text="Save", command=on_save).pack(side="right")

        dialog.bind("<Escape>", on_cancel)
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        self._center_over_main_window(dialog)

    def _confirm_cheats_change(
        self, info: ServerInfo, level_dat: Path, enabling: bool, previous: Optional[bool]
    ) -> bool:
        """Confirm a level.dat write before it happens. Returns True to
        proceed. Called before any file is touched, so declining leaves both
        server.properties and level.dat untouched."""
        action = "Enable" if enabling else "Disable"
        lines = [
            f"{action} cheats for {info.level_name}?",
            "",
            "This writes to the world's level.dat, not server.properties:",
            f"  {level_dat}",
            "",
        ]
        if previous is None:
            lines.append(
                "This world has no allowCommands tag yet, so one will be added to "
                "the Data compound."
            )
            lines.append("")
        lines.append(
            f"The current file will be copied to {level_dat.name}.mcsm-bak first."
        )
        lines.append("")
        lines.append(
            "Stop the server before saving -- a running server holds the world in "
            "memory and will overwrite level.dat on its next save, discarding this "
            "change."
        )
        return messagebox.askyesno(f"{action} Cheats", "\n".join(lines))

    # -- Server selection / details ------------------------------------------

    def on_select_server(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        info = self.tree_index.get(sel[0])
        if not info:
            return
        self.current_server = info

        self.name_label.config(text=info.name)
        if info.motd:
            self.motd_label.config(text=info.motd)
            self.motd_label.pack(anchor="w", pady=(2, 4), before=self.preview_frame)
        else:
            self.motd_label.pack_forget()
        self.tags_label.config(text=format_platform_line(info))
        self.version_label.config(text=format_version_line(info))

        mods_text = format_mods_line(info)
        if mods_text is None:
            self.mods_label.pack_forget()
        else:
            self.mods_label.config(text=mods_text)
            self.mods_label.pack(anchor="w", after=self.version_label)
        self.path_label.config(text=str(info.path))

        world_dir = get_world_dir(info)
        backups_dir = get_backups_dir(info)
        has_backups = backups_dir.is_dir()  # cheap stat, fine synchronously

        if info.sizes_computed:
            # Already scanned this ServerInfo instance -- reuse the cached
            # sizes instead of re-walking the folders. A manual Rescan
            # replaces the instance entirely, which is what invalidates this.
            self._pending_world_size = (
                format_size(info.world_size_bytes) if info.world_size_bytes is not None else "n/a"
            )
            self._pending_total_size = format_size(info.total_size_bytes)
            self._update_size_label()
            if has_backups:
                self.backup_size_label.config(text=f"Backup size: {format_size(info.backup_size_bytes)}")
                self.backup_size_label.pack(anchor="w")
            else:
                self.backup_size_label.pack_forget()
        else:
            self._pending_world_size = "Calculating..." if world_dir.is_dir() else "n/a"
            self._pending_total_size = "Calculating..."
            self._update_size_label()

            if has_backups:
                self.backup_size_label.config(text="Backup size: Calculating...")
                self.backup_size_label.pack(anchor="w")
            else:
                self.backup_size_label.pack_forget()

            # Total is always computed (the server folder always exists here);
            # world may not (e.g. a "No level.dat" server).
            threading.Thread(
                target=self._compute_sizes,
                args=(info, world_dir if world_dir.is_dir() else None, backups_dir if has_backups else None),
                daemon=True,
            ).start()

        self.preview_label.configure(image="", text="No\nPreview", bg="#222222")
        if info.icon_path:
            threading.Thread(target=self._load_preview, args=(info.icon_path,), daemon=True).start()

        for w in self.player_frame.winfo_children():
            w.destroy()

        op_count = sum(1 for p in info.players if p.is_op)
        self.player_count_label.config(
            text=f"({len(info.players)} known, {op_count} op{'s' if op_count != 1 else ''})"
        )

        if not info.players:
            ttk.Label(self.player_frame, text="No playerdata found").pack(anchor="w", padx=4, pady=4)
            self._hook_scroll_children(self.player_frame)
            return

        # Ops first, then alphabetical, for quick scanning.
        for p in sorted(info.players, key=lambda pl: (not pl.is_op, pl.name.lower())):
            self._add_player_row(p)
        self._hook_scroll_children(self.player_frame)

    def _load_preview(self, path: Path):
        try:
            img = Image.open(path).convert("RGBA")
            w, h = img.size
            scale = PREVIEW_SIZE / max(w, h)
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
            self.task_queue.put(("preview_ready", img))
        except Exception:
            pass

    def _update_size_label(self):
        self.world_size_label.config(text=f"World: {self._pending_world_size} | Total: {self._pending_total_size}")

    def _compute_sizes(self, info: ServerInfo, world_dir: Optional[Path], backups_dir: Optional[Path]):
        """Background thread: sums folder sizes and posts results back through
        the task queue, tagged with `info` so a stale result (user already
        selected a different server) can be dropped in _poll_queue. Posted as
        separate messages -- world, then total (the whole server folder, which
        can be much bigger and slower, e.g. a plugins/web-map tile cache) --
        then backups, so each label fills in as soon as its own scan is done
        rather than waiting on the slowest one. Results are also cached on
        `info` itself so re-selecting this server later reuses them instead
        of re-walking the folders -- only a manual Rescan (which replaces the
        ServerInfo instance) triggers a fresh walk."""
        if world_dir is not None:
            info.world_size_bytes = compute_folder_size(world_dir)
            self.task_queue.put(("world_size_ready", (info, info.world_size_bytes)))
        else:
            info.world_size_bytes = None
        info.total_size_bytes = compute_folder_size(info.path)
        self.task_queue.put(("total_size_ready", (info, info.total_size_bytes)))
        if backups_dir is not None:
            info.backup_size_bytes = compute_folder_size(backups_dir)
            self.task_queue.put(("backup_size_ready", (info, info.backup_size_bytes)))
        else:
            info.backup_size_bytes = None
        info.sizes_computed = True

    # -- World image preview context menu ------------------------------------

    def _show_preview_menu(self, event):
        info = self.current_server
        if not info:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label="Save Image...",
            command=lambda: self._save_preview_image(info),
            state="normal" if info.icon_path else "disabled",
        )
        menu.add_command(label="Replace Image...", command=lambda: self._replace_preview_image(info))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _save_preview_image(self, info: ServerInfo):
        if not info.icon_path or not info.icon_path.exists():
            messagebox.showwarning("No Image", "This server has no icon image to save.")
            return
        dest = filedialog.asksaveasfilename(
            title="Save Image",
            initialfile=f"{info.name}_icon.png",
            defaultextension=".png",
            filetypes=[("PNG files", "*.png"), ("All files", "*.*")],
        )
        if not dest:
            return
        shutil.copy2(info.icon_path, dest)
        self.status_var.set(f"Saved image to {dest}")

    def _replace_preview_image(self, info: ServerInfo):
        src = filedialog.askopenfilename(
            title="Replace Image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.gif *.bmp"), ("All files", "*.*")],
        )
        if not src:
            return
        # No existing icon anywhere for this server -- create the standard
        # vanilla dedicated-server icon at the server root.
        target = info.icon_path or (info.path / "server-icon.png")
        try:
            img = Image.open(src).convert("RGBA")
            img = img.resize((64, 64), Image.LANCZOS)  # required size for a working server icon
            img.save(target, format="PNG")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save image: {e}")
            return
        info.icon_path = target
        self.status_var.set(f"Replaced image for {info.name}")
        self.on_select_server(None)

    def _add_player_row(self, p: PlayerInfo):
        row = ttk.Frame(self.player_frame)
        row.pack(fill="x", padx=4, pady=2, anchor="w")

        face_label = tk.Label(row, width=32, height=32, bg="#333333")
        face_label.pack(side="left", padx=(0, 8))

        suffix_bits = []
        if p.is_banned:
            suffix_bits.append("BANNED")
        if p.is_op:
            suffix_bits.append("OP")
        if p.is_bedrock:
            suffix_bits.append("Bedrock?")
        name_text = p.name + ("  [" + ", ".join(suffix_bits) + "]" if suffix_bits else "")

        # Banned red takes priority over operator amber.
        fg = "#CC0000" if p.is_banned else ("#CC7A00" if p.is_op else "#000000")
        name_label = tk.Label(
            row,
            text=name_text,
            anchor="w",
            fg=fg,
            font=("Segoe UI", 10, "bold" if (p.is_op or p.is_banned) else "normal"),
        )
        name_label.pack(side="left")

        uuid_label = tk.Label(row, text=p.uuid, fg="#999999", font=("Consolas", 8))
        uuid_label.pack(side="left", padx=8)

        tooltip_text = (p.ban_reason or "No reason recorded.") if p.is_banned else None
        for widget in (row, face_label, name_label, uuid_label):
            widget.bind("<Button-3>", lambda e, pl=p: self._show_player_menu(e, pl))
            if tooltip_text:
                Tooltip(widget, tooltip_text)

        threading.Thread(target=self._load_face, args=(p, face_label), daemon=True).start()

        # Name fell back to the raw UUID -- not present in usercache.json /
        # ops.json / whitelist.json / banned-players.json. Try resolving it
        # against Mojang's API in the background rather than leaving the
        # UUID displayed.
        if p.name == p.uuid and not p.is_bedrock:
            threading.Thread(target=self._load_name, args=(p, name_label, suffix_bits), daemon=True).start()

    def _load_face(self, p: PlayerInfo, label: tk.Label):
        if p.is_bedrock:
            img = placeholder_face(32)
        else:
            img = fetch_face_image(p.uuid, 32) or placeholder_face(32)
        self.task_queue.put(("face_ready", (img, label)))

    def _load_name(self, p: PlayerInfo, label: tk.Label, suffix_bits: list):
        name = fetch_username_from_api(p.uuid)
        if name:
            p.name = name
            self.task_queue.put(("name_ready", (label, name, suffix_bits)))

    # -- Player context menu ------------------------------------------------

    def _show_player_menu(self, event, p: PlayerInfo):
        info = self.current_server
        if not info:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="View File", command=lambda: self._view_playerdata_file(info, p))
        menu.add_command(label="Copy UUID", command=lambda: self._copy_uuid(p))
        menu.add_command(label="Export Playerdata...", command=lambda: self._export_playerdata(info, p))
        has_backups = bool(list_world_backups(info))
        menu.add_command(
            label="Roll Back...",
            command=lambda: self._open_rollback_dialog(info, p),
            state="normal" if has_backups else "disabled",
        )
        menu.add_separator()
        if p.is_whitelisted:
            menu.add_command(label="Remove from Whitelist", command=lambda: self._remove_from_whitelist(info, p))
        else:
            menu.add_command(label="Add to Whitelist", command=lambda: self._add_to_whitelist(info, p))
        menu.add_separator()
        if p.is_op:
            menu.add_command(label="Remove OP...", command=lambda: self._deop_player(info, p))
        else:
            menu.add_command(label="Make OP...", command=lambda: self._op_player(info, p))
        menu.add_separator()
        if p.is_banned:
            menu.add_command(label="Pardon", command=lambda: self._pardon_player(info, p))
        else:
            menu.add_command(label="Ban...", foreground="#CC0000", command=lambda: self._ban_player(info, p))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _view_playerdata_file(self, info: ServerInfo, p: PlayerInfo):
        dat_file = get_playerdata_dir(info) / f"{p.uuid}.dat"
        if not dat_file.exists():
            messagebox.showwarning("File Not Found", f"{dat_file.name} no longer exists.")
            return
        try:
            # explorer.exe parses its command line itself rather than via
            # normal argv, and expects the path immediately after the comma,
            # quoted on its own (`/select,"C:\path with spaces\file"`). Passed
            # as a list, subprocess's list2cmdline would instead quote the
            # whole "/select,<path>" token together whenever the path has a
            # space, which explorer fails to parse -- it silently falls back
            # to its default window instead of erroring. Passing a single
            # string sidesteps list2cmdline and gives explorer exactly the
            # command line it expects.
            subprocess.run(f'explorer /select,"{dat_file}"')
        except OSError as e:
            messagebox.showerror("Error", f"Could not open Explorer: {e}")

    def _copy_uuid(self, p: PlayerInfo):
        self.clipboard_clear()
        self.clipboard_append(p.uuid)
        self.update()
        self.status_var.set(f"Copied UUID for {p.name}")

    def _export_playerdata(self, info: ServerInfo, p: PlayerInfo):
        # Prefix match (not "{uuid}.*") so stray/duplicate files that share
        # the player's UUID but aren't exact "<uuid>.<ext>" -- e.g. a manual
        # backup copy named "<uuid> - Copy.dat" -- are still swept into the
        # export even though they're filtered out of the player list itself.
        matches = sorted(get_playerdata_dir(info).glob(f"{p.uuid}*"))
        if not matches:
            messagebox.showwarning("Nothing to Export", f"No playerdata files found for {p.name}.")
            return
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", p.name)

        if len(matches) > 1:
            choice = messagebox.askyesnocancel(
                "Multiple Files Found",
                f"Found {len(matches)} files for {p.name} (backups/mod-created files "
                "alongside the main playerdata file).\n\n"
                "Export ALL of them as a zip? Choose \"No\" to export just the main "
                f"{p.uuid}.dat file instead.",
            )
            if choice is None:
                return
            if not choice:
                main_file = get_playerdata_dir(info) / f"{p.uuid}.dat"
                matches = [main_file] if main_file in matches else matches[:1]

        if len(matches) == 1:
            src = matches[0]
            dest = filedialog.asksaveasfilename(
                title="Export Playerdata",
                initialfile=f"{safe_name}_{p.uuid}{src.suffix}",
                defaultextension=src.suffix,
                filetypes=[("All files", "*.*")],
            )
            if not dest:
                return
            shutil.copy2(src, dest)
        else:
            dest = filedialog.asksaveasfilename(
                title="Export Playerdata (zip)",
                initialfile=f"{safe_name}_{p.uuid}_playerdata.zip",
                defaultextension=".zip",
                filetypes=[("Zip files", "*.zip")],
            )
            if not dest:
                return
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in matches:
                    zf.write(f, arcname=f.name)
        self.status_var.set(f"Exported playerdata for {p.name} to {dest}")

    def _open_rollback_dialog(self, info: ServerInfo, p: PlayerInfo):
        """Modal dialog listing this world's dated backups, greyed out for
        any backup that doesn't contain the selected player. Presence is
        checked in a background thread (each backup zip's central directory
        has to be opened and scanned) and streamed back into the list as
        results come in, so the dialog isn't blocked on however many backups
        there are."""
        backups = list_world_backups(info)

        dialog = tk.Toplevel(self)
        dialog.title(f"Roll Back - {p.name}")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        ttk.Label(
            dialog, text=f"Backups for {p.name} ({p.uuid}):", font=("Segoe UI", 10, "bold")
        ).pack(padx=12, pady=(12, 6), anchor="w")

        tree = ttk.Treeview(dialog, columns=("status",), show="tree headings", height=10)
        tree.heading("#0", text="Date")
        tree.heading("status", text="Status")
        tree.column("#0", width=180)
        tree.column("status", width=160)
        tree.tag_configure("unavailable", foreground="#999999")
        tree.pack(padx=12, fill="both", expand=True)

        matches_by_iid = {}
        path_by_iid = {}
        for entry in backups:
            display_date = entry.dt.strftime("%Y-%m-%d %H:%M") if entry.dt else entry.path.name
            iid = tree.insert("", "end", text=display_date, values=("Checking...",))
            path_by_iid[iid] = entry

        result_queue: "queue.Queue" = queue.Queue()

        def scan_worker():
            for iid, entry in path_by_iid.items():
                try:
                    with zipfile.ZipFile(entry.path) as zf:
                        matches = find_uuid_entries_in_zip(zf, p.uuid)
                except (OSError, zipfile.BadZipFile):
                    matches = []
                result_queue.put((iid, matches))

        threading.Thread(target=scan_worker, daemon=True).start()

        def poll_results():
            if not dialog.winfo_exists():
                return
            try:
                while True:
                    iid, matches = result_queue.get_nowait()
                    if matches:
                        matches_by_iid[iid] = matches
                        tree.item(iid, values=("Available",), tags=("available",))
                    else:
                        tree.item(iid, values=("Player not found",), tags=("unavailable",))
            except queue.Empty:
                pass
            dialog.after(100, poll_results)

        dialog.after(100, poll_results)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(padx=12, pady=12, anchor="e")
        cancel_btn = ttk.Button(btn_frame, text="Cancel", command=dialog.destroy)
        cancel_btn.pack(side="right", padx=(6, 0))
        rollback_btn = ttk.Button(btn_frame, text="Roll Back", state="disabled")
        rollback_btn.pack(side="right")

        def do_rollback():
            sel = tree.selection()
            if not sel:
                return
            iid = sel[0]
            matches = matches_by_iid.get(iid)
            if not matches:
                return
            entry = path_by_iid[iid]
            display_date = entry.dt.strftime("%Y-%m-%d %H:%M") if entry.dt else entry.path.name
            if self._perform_rollback(info, p, entry.path, matches, display_date):
                dialog.destroy()

        rollback_btn.configure(command=do_rollback)

        def on_select(_event=None):
            sel = tree.selection()
            if sel and "available" in tree.item(sel[0], "tags"):
                rollback_btn.configure(state="normal")
            else:
                rollback_btn.configure(state="disabled")

        tree.bind("<<TreeviewSelect>>", on_select)
        tree.bind("<Double-1>", lambda e: do_rollback())

        dialog.bind("<Escape>", lambda e: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self._center_over_main_window(dialog)

    def _perform_rollback(
        self, info: ServerInfo, p: PlayerInfo, backup_path: Path, matches: list, display_date: str
    ) -> bool:
        """Restore the given player's playerdata file(s) from inside a
        backup zip, overwriting the live copies. Returns True on success (so
        the caller can close the Roll Back dialog), False if the user
        cancelled or the restore failed."""
        if len(matches) > 1:
            choice = messagebox.askyesnocancel(
                "Multiple Files Found",
                f"Found {len(matches)} files for {p.name} in this backup "
                "(backups/mod-created files alongside the main playerdata file).\n\n"
                "Restore ALL of them? Choose \"No\" to restore just the main "
                f"{p.uuid}.dat file instead.",
            )
            if choice is None:
                return False
            if not choice:
                main = next((m for m in matches if Path(m).name == f"{p.uuid}.dat"), None)
                matches = [main] if main else matches[:1]

        file_list = "\n".join(sorted(Path(m).name for m in matches))
        if not messagebox.askyesno(
            "Roll Back Playerdata",
            f"Restore playerdata for {p.name} from the backup dated {display_date}?\n\n"
            f"This will overwrite the following live file(s):\n{file_list}\n\n"
            "This cannot be undone.",
        ):
            return False

        try:
            with zipfile.ZipFile(backup_path) as zf:
                for name in matches:
                    data = zf.read(name)
                    (get_playerdata_dir(info) / Path(name).name).write_bytes(data)
        except (OSError, zipfile.BadZipFile) as e:
            messagebox.showerror("Error", f"Could not restore playerdata: {e}")
            return False

        self.status_var.set(f"Restored playerdata for {p.name} from backup dated {display_date}")
        return True

    def _prompt_ban_reason(self, p: PlayerInfo) -> Optional[str]:
        """Modal dialog asking for a ban reason, pre-filled with the default.
        Returns the entered reason, or None if the user cancelled."""
        dialog = tk.Toplevel(self)
        dialog.title("Ban Player")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text=f"Ban {p.name} ({p.uuid})?\n\n"
            "This edits banned-players.json directly. If the server is currently "
            "running, it may overwrite this file -- restart the server for the ban "
            "to take effect reliably.",
            wraplength=360,
            justify="left",
        ).pack(padx=12, pady=(12, 8), anchor="w")

        ttk.Label(dialog, text="Reason:").pack(padx=12, anchor="w")
        reason_var = tk.StringVar(value="Banned by an operator.")
        reason_entry = ttk.Entry(dialog, textvariable=reason_var, width=50)
        reason_entry.pack(padx=12, pady=(0, 12), fill="x")
        reason_entry.focus_set()
        reason_entry.select_range(0, "end")

        result = {"reason": None}

        def on_ok(event=None):
            result["reason"] = reason_var.get().strip() or "Banned by an operator."
            dialog.destroy()

        def on_cancel(event=None):
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(padx=12, pady=(0, 12), anchor="e")
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btn_frame, text="Ban", command=on_ok).pack(side="right")

        dialog.bind("<Return>", on_ok)
        dialog.bind("<Escape>", on_cancel)
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)

        self._center_over_main_window(dialog)
        dialog.wait_window()
        return result["reason"]

    def _ban_player(self, info: ServerInfo, p: PlayerInfo):
        reason = self._prompt_ban_reason(p)
        if reason is None:
            return
        banned_path = get_banned_players_path(info)
        entries = []
        if banned_path.exists():
            try:
                loaded = json.loads(banned_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        entries.append({
            "uuid": p.uuid,
            "name": p.name,
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000"),
            "source": "Server",
            "expires": "forever",
            "reason": reason,
        })
        try:
            banned_path.write_text(json.dumps(entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write banned-players.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Banned {p.name}")

    def _pardon_player(self, info: ServerInfo, p: PlayerInfo):
        reason_text = p.ban_reason or "No reason recorded."
        if not messagebox.askyesno(
            "Pardon Player",
            f"Pardon {p.name} ({p.uuid})?\n\n"
            f"Ban reason: {reason_text}",
        ):
            return
        banned_path = get_banned_players_path(info)
        entries = []
        if banned_path.exists():
            try:
                loaded = json.loads(banned_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        new_entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        if len(new_entries) == len(entries):
            return
        try:
            banned_path.write_text(json.dumps(new_entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write banned-players.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Pardoned {p.name}")

    def _add_to_whitelist(self, info: ServerInfo, p: PlayerInfo):
        whitelist_path = get_whitelist_path(info)
        entries = []
        if whitelist_path.exists():
            try:
                loaded = json.loads(whitelist_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        entries.append({"uuid": p.uuid, "name": p.name})
        try:
            whitelist_path.write_text(json.dumps(entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write whitelist.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Added {p.name} to whitelist")

    def _remove_from_whitelist(self, info: ServerInfo, p: PlayerInfo):
        whitelist_path = get_whitelist_path(info)
        entries = []
        if whitelist_path.exists():
            try:
                loaded = json.loads(whitelist_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        new_entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        if len(new_entries) == len(entries):
            return
        try:
            whitelist_path.write_text(json.dumps(new_entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write whitelist.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Removed {p.name} from whitelist")

    def _op_player(self, info: ServerInfo, p: PlayerInfo):
        level = default_op_level(info)
        lines = [
            f"Give operator status to {p.name} ({p.uuid})?",
            "",
            f"Permission level {level} — {OP_LEVEL_DESCRIPTIONS.get(level, 'custom permissions')}.",
        ]
        if p.name == p.uuid:
            lines.append("")
            lines.append("This player's name hasn't been resolved -- the raw UUID will be written as the name.")
        if p.is_bedrock:
            lines.append("")
            lines.append("This is a Bedrock/Geyser player -- the UUID is a Floodgate identifier, not a Mojang account.")
        live_reason = server_looks_live(info)
        if live_reason:
            lines.append("")
            lines.append(f"Warning: {live_reason} ops.json may be overwritten by the running server.")
        if not messagebox.askyesno("Op Player", "\n".join(lines)):
            return
        ops_path = get_ops_path(info)
        entries = []
        if ops_path.exists():
            try:
                loaded = json.loads(ops_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        existing = next(
            (e for e in entries if isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower()),
            None,
        )
        entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        entries.append({
            "uuid": p.uuid,
            "name": p.name,
            "level": (existing or {}).get("level", level),
            "bypassesPlayerLimit": (existing or {}).get("bypassesPlayerLimit", False),
        })
        try:
            ops_path.write_text(json.dumps(entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write ops.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Opped {p.name}")

    def _deop_player(self, info: ServerInfo, p: PlayerInfo):
        lines = [f"Remove operator status from {p.name} ({p.uuid})?"]
        live_reason = server_looks_live(info)
        if live_reason:
            lines.append("")
            lines.append(f"Warning: {live_reason} ops.json may be overwritten by the running server.")
        if not messagebox.askyesno("Deop Player", "\n".join(lines)):
            return
        ops_path = get_ops_path(info)
        entries = []
        if ops_path.exists():
            try:
                loaded = json.loads(ops_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        new_entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        if len(new_entries) == len(entries):
            return
        try:
            ops_path.write_text(json.dumps(new_entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write ops.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Deopped {p.name}")

    def _reload_current_server_player_state(self):
        info = self.current_server
        if not info:
            return
        _, ops, banned, whitelisted = load_name_map(info.path)
        for pl in info.players:
            key = pl.uuid.lower()
            pl.is_op = key in ops
            pl.is_banned = key in banned
            pl.ban_reason = banned.get(key)
            pl.is_whitelisted = key in whitelisted
        self.on_select_server(None)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
