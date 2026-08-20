"""Minimal big-endian NBT reading, plus the targeted byte patching used to
edit a single tag in place without rewriting the file.
"""

import gzip
import os
import shutil
import struct
from pathlib import Path
from typing import Optional


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
