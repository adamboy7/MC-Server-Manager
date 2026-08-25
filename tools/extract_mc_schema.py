#!/usr/bin/env python3
"""Generate mcsm/playerdat_schema.py from a decompiled Minecraft workspace.

Why this exists
---------------
The tags the player editor touches -- Pos, Health, the respawn compound --
change name, type and location between versions, and getting any of that wrong
corrupts someone's save. Deriving the table from four sample worlds tells you
what those four worlds happen to contain; deriving it from the game's own
sources tells you what every version does.

Two registries in the decompiled tree carry the answer, and this script reads
both:

  1. **Current shape** -- since the ValueInput/ValueOutput migration, entity
     serialisation is declarative. `input.getFloatOr("Health", ...)` and
     `output.store("respawn", RespawnConfig.CODEC, ...)` name the tag and its
     type directly, so the accessor calls in Entity/LivingEntity/Player/
     ServerPlayer/FoodData *are* the schema for the version you point this at.

  2. **Version history** -- util/datafix/DataFixers.java registers every data
     fix against a schema version, and the fix classes themselves spell out the
     renames (`renameField("Attributes", "attributes")`). Pairing the two gives
     a (data_version, old_name, new_name) history for free.

What it cannot do
-----------------
Neither registry knows what the *editor* should allow. Bounds, which fields are
safe to write, what has to be reset alongside them, and why a field is disabled
are editorial decisions, and they live in the CURATED table below rather than
being invented from the source. The generated module marks which half each
entry came from.

No Mojang source is copied into the repo -- only derived facts (tag names,
types, version numbers), which is why the output is committable and the
workspace it reads is not.

Usage
-----
    python tools/extract_mc_schema.py --fetch          # get a workspace, then read it
    python tools/extract_mc_schema.py --workspace "/path/to/26.2 Decompiled"
    python tools/extract_mc_schema.py --fetch --check  # CI: no write

`--fetch` borrows Fetch's decompiler to build a server workspace for the latest
Minecraft release, cached between runs -- see tools/fetch_workspace.py, which is
the only place the two projects touch. `--workspace` needs nothing from Fetch.

Stdlib only, like the rest of mcsm.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
import textwrap
from pathlib import Path
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Locating sources
#
# A decompiler that cannot round-trip a file moves it to .decompiled/quarantine
# keeping its package layout -- DataFixers.java, 1600 lines of generics, is a
# regular casualty. It is still valid enough to regex, so both roots are
# searched.
# ---------------------------------------------------------------------------

SOURCE_ROOTS = (
    Path("src") / "main" / "java",
    Path(".decompiled") / "quarantine",
)

# The classes whose read/write methods define a player file. Order matters only
# for error messages.
PLAYER_CLASSES = (
    "net/minecraft/world/entity/Entity.java",
    "net/minecraft/world/entity/LivingEntity.java",
    "net/minecraft/world/entity/player/Player.java",
    "net/minecraft/server/level/ServerPlayer.java",
    "net/minecraft/world/food/FoodData.java",
)

LEVEL_CLASSES = (
    "net/minecraft/world/level/storage/PrimaryLevelData.java",
)

FIXER_REGISTRY = "net/minecraft/util/datafix/DataFixers.java"
FIXES_DIR = "net/minecraft/util/datafix/fixes"


class ExtractionError(RuntimeError):
    pass


def default_out_path() -> Path:
    """mcsm/playerdat_schema.py in *this script's* repo.

    Anchored on __file__ rather than the working directory: a CWD-relative
    default silently writes tools/mcsm/playerdat_schema.py when you run the
    script from inside tools/, which looks like a successful regeneration and
    leaves the real table untouched."""
    return Path(__file__).resolve().parent.parent / "mcsm" / "playerdat_schema.py"


def find_source(workspace: Path, relpath: str) -> Path:
    for root in SOURCE_ROOTS:
        candidate = workspace / root / relpath
        if candidate.is_file():
            return candidate
    raise ExtractionError(
        f"{relpath} not found under {' or '.join(str(r) for r in SOURCE_ROOTS)}"
        f" in {workspace}"
    )


def find_dir(workspace: Path, relpath: str) -> Path:
    for root in SOURCE_ROOTS:
        candidate = workspace / root / relpath
        if candidate.is_dir():
            return candidate
    raise ExtractionError(f"{relpath}/ not found in {workspace}")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Pass 1 -- the fix registry
#
# DataFixers.addFixers() is a flat chronological script:
#
#     Schema v109 = fixerUpper.addSchema(109, SAME);
#     fixerUpper.addFixer(new EntityHealthFix(v109, true));
#
# so the schema version in scope is just the most recent addSchema seen. Fixes
# are also registered via static factories (BlockRenameFix.create(...)) and
# occasionally across a line break, hence the tolerant pattern.
# ---------------------------------------------------------------------------

RE_ADD_SCHEMA = re.compile(r"addSchema\(\s*(\d+)")
RE_ADD_FIXER = re.compile(r"addFixer\(\s*(?:new\s+)?([A-Za-z0-9_]+)")


def extract_fix_history(workspace: Path) -> list[tuple[int, str]]:
    """[(data_version, fix_class_name)] in registration order."""
    text = read(find_source(workspace, FIXER_REGISTRY))
    history: list[tuple[int, str]] = []
    current: Optional[int] = None
    for line in text.splitlines():
        schema = RE_ADD_SCHEMA.search(line)
        if schema:
            current = int(schema.group(1))
        fixer = RE_ADD_FIXER.search(line)
        if fixer and current is not None:
            history.append((current, fixer.group(1)))
    if not history:
        raise ExtractionError(f"no fixers parsed out of {FIXER_REGISTRY}")
    return history


# ---------------------------------------------------------------------------
# Pass 2 -- renames declared by the fix classes
#
# renameField("Attributes", "attributes") and its fixing sibling are a stable
# enough idiom to read mechanically. Anything more elaborate (a fix that
# restructures rather than renames, like PlayerRespawnDataFix) is not parsed --
# those are curated below with a pointer to the class.
# ---------------------------------------------------------------------------

RE_RENAME = re.compile(
    r"rename(?:AndFix)?Field\(\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\""
)


def extract_renames(workspace: Path, history: list[tuple[int, str]]) -> list[tuple[int, str, str, str]]:
    """[(data_version, fix_class, old_name, new_name)] for every rename a fix
    class declares literally. Fixes never registered are skipped -- the tree
    carries a few that nothing calls."""
    registered = {}
    for dv, name in history:
        registered.setdefault(name, dv)

    out: list[tuple[int, str, str, str]] = []
    for java in sorted(find_dir(workspace, FIXES_DIR).glob("*.java")):
        name = java.stem
        if name not in registered:
            continue
        for old, new in RE_RENAME.findall(read(java)):
            out.append((registered[name], name, old, new))
    return out


# ---------------------------------------------------------------------------
# Pass 3 -- current-version field shapes
#
# Three accessor idioms cover everything the player classes do:
#
#     input.getFloatOr("Health", ...)        primitive read, with a default
#     output.putInt("XpLevel", ...)          primitive write
#     output.store("respawn", X.CODEC, ...)  codec-typed read/write
#
# The primitive forms carry their NBT type in the method name. The codec forms
# do not, so CODEC_TAGS resolves the ones the player file actually uses; an
# unrecognised codec is reported rather than guessed, so this fails loudly when
# a future version swaps a plain putFloat for a codec.
# ---------------------------------------------------------------------------

RE_PRIM_READ = re.compile(
    r"\.get(Boolean|Byte|Short|Int|Long|Float|Double|String)Or\(\s*\"([^\"]+)\""
)
RE_PRIM_WRITE = re.compile(
    r"\.put(Boolean|Byte|Short|Int|Long|Float|Double|String)\(\s*\"([^\"]+)\""
)
RE_CODEC = re.compile(
    r"\.(?:read|store|storeNullable)\(\s*\"([^\"]+)\"\s*,\s*([A-Za-z0-9_.]*CODEC)\b"
)

# Java accessor name -> (tag constant, element tag, fixed length)
PRIMITIVE_TAGS = {
    "Boolean": ("TAG_BYTE", None, None),
    "Byte": ("TAG_BYTE", None, None),
    "Short": ("TAG_SHORT", None, None),
    "Int": ("TAG_INT", None, None),
    "Long": ("TAG_LONG", None, None),
    "Float": ("TAG_FLOAT", None, None),
    "Double": ("TAG_DOUBLE", None, None),
    "String": ("TAG_STRING", None, None),
}

# Codec -> resolved NBT shape. Each entry was read off the codec's own
# definition in the workspace; the comment records where.
CODEC_TAGS = {
    # Codec.DOUBLE.listOf() + Util.fixedSize(_, 3)     -- world/phys/Vec3.java
    "Vec3.CODEC": ("TAG_LIST", "TAG_DOUBLE", 3),
    # Codec.FLOAT.listOf() + Util.fixedSize(_, 2)      -- world/phys/Vec2.java
    "Vec2.CODEC": ("TAG_LIST", "TAG_FLOAT", 2),
    # Codec.INT_STREAM + Util.fixedSize(_, 3)          -- core/BlockPos.java
    "BlockPos.CODEC": ("TAG_INT_ARRAY", None, 3),
    # Codec.INT_STREAM, four ints                      -- core/UUIDUtil.java
    "UUIDUtil.CODEC": ("TAG_INT_ARRAY", None, 4),
    # Codec.INT.xmap(GameType::byId)                   -- world/level/GameType.java
    "GameType.LEGACY_ID_CODEC": ("TAG_INT", None, None),
    # RecordCodecBuilder over dimension/pos/yaw/pitch(/forced)
    "ServerPlayer.RespawnConfig.CODEC": ("TAG_COMPOUND", None, None),
    "RespawnConfig.CODEC": ("TAG_COMPOUND", None, None),
    "LevelData.RespawnData.CODEC": ("TAG_COMPOUND", None, None),
    "RespawnData.CODEC": ("TAG_COMPOUND", None, None),
    # CODEC.listOf() over id/base/modifiers -- ai/attributes/AttributeInstance.java
    "AttributeInstance.Packed.LIST_CODEC": ("TAG_LIST", "TAG_COMPOUND", None),
    "CompoundTag.CODEC": ("TAG_COMPOUND", None, None),
}

# Codecs that appear in the player classes but describe subtrees the editor
# never reads. Listed so an unresolved-codec report stays meaningful instead of
# drowning in noise.
CODEC_IGNORED = {
    "Abilities.Packed.CODEC",
    "Brain.Packed.CODEC",
    "ComponentSerialization.CODEC",
    "CustomData.CODEC",
    "EntityEquipment.CODEC",
    "EntityType.CODEC",
    "GlobalPos.CODEC",          # entered_nether_pos etc., not the respawn one
    "ItemStack.CODEC",
    "Level.RESOURCE_KEY_CODEC",
    "LevelSettings.DifficultySettings.CODEC",
    "MobEffectInstance.CODEC",
    "Parrot.Variant.LEGACY_CODEC",
    "ServerRecipeBook.Packed.CODEC",
    "TAG_LIST_CODEC",
    "WardenSpawnTracker.CODEC",
    "Waypoint.Icon.CODEC",
}


def extract_field_shapes(workspace: Path, classes: Iterable[str]):
    """{tag_name: (tag, item_tag, count, [source_files])} for the current
    version, plus the set of codecs that could not be resolved."""
    shapes: dict[str, tuple] = {}
    origins: dict[str, set] = {}
    unresolved: set[tuple[str, str]] = set()

    for relpath in classes:
        path = find_source(workspace, relpath)
        text = read(path)
        short = relpath.rsplit("/", 1)[-1]

        for pattern in (RE_PRIM_READ, RE_PRIM_WRITE):
            for kind, tag_name in pattern.findall(text):
                shapes.setdefault(tag_name, PRIMITIVE_TAGS[kind])
                origins.setdefault(tag_name, set()).add(short)

        for tag_name, codec in RE_CODEC.findall(text):
            # Codecs are referenced both fully qualified and via the enclosing
            # class alone (ServerPlayer.RespawnConfig.CODEC vs
            # RespawnConfig.CODEC), so try the trailing two segments as well.
            resolved = CODEC_TAGS.get(codec) or CODEC_TAGS.get(
                ".".join(codec.split(".")[-2:])
            )
            if resolved is None:
                if codec not in CODEC_IGNORED:
                    unresolved.add((short, codec))
                continue
            shapes.setdefault(tag_name, resolved)
            origins.setdefault(tag_name, set()).add(short)

    return shapes, origins, unresolved


# ---------------------------------------------------------------------------
# The curated layer
#
# Everything above is a fact about the game. Everything below is a decision
# about the editor, and none of it can be derived from the source: what may be
# written, within what range, and what has to be reset alongside it.
#
# `variants` is newest-first. Each is (path, min_dv, max_dv, shape_override).
# min_dv None means "since forever", max_dv None means "still current". A
# shape_override of None means "take the shape extracted from the sources",
# which only works for the current variant -- historical ones must state their
# own, since a 26.2 workspace cannot describe a 1.8 file.
# ---------------------------------------------------------------------------

INF = None  # readability at the ends of version ranges

CURATED = [
    dict(
        key="pos", label="Position", group="position", editable=True,
        variants=[(("Pos",), INF, INF, None)],
        finite=True,
        bounds=[("x", -3.0000512e7, 3.0000512e7),
                ("y", -2.0e7, 2.0e7),
                ("z", -3.0000512e7, 3.0000512e7)],
        resets=("motion", "fall_distance"),
        note="Entity.load clamps each axis to these bounds and throws "
             "IllegalStateException on a non-finite value, which surfaces as a "
             "ReportedException and takes the login with it.",
    ),
    dict(
        key="motion", label="Motion", group="position", editable=False,
        variants=[(("Motion",), INF, INF, None)],
        finite=True, bounds=None, resets=(),
        note="Reset to zero whenever position changes. Entity.load already "
             "discards any axis whose magnitude exceeds 10.0, so this is "
             "belt-and-braces rather than the only guard.",
    ),
    dict(
        key="rotation", label="Rotation", group="position", editable=False,
        variants=[(("Rotation",), INF, INF, None)],
        finite=True, bounds=None, resets=(),
        note="Yaw then pitch. Non-finite values throw at load exactly as Pos "
             "does, so the repair path has to check them even though the "
             "editor does not offer them.",
    ),
    dict(
        key="fall_distance", label="Fall distance", group="position",
        editable=False,
        variants=[(("fall_distance",), 4303, INF, None),
                  (("FallDistance",), INF, 4302, ("TAG_FLOAT", None, None))],
        finite=True, bounds=None, resets=(),
        note="Renamed and widened to a double by "
             "EntityFallDistanceFloatToDoubleFix. Reset to zero alongside "
             "position, or the player takes the fall damage they were owed "
             "before being moved.",
    ),
    dict(
        key="dimension", label="Dimension", group="position", editable=False,
        variants=[(("Dimension",), 2537, INF, None),
                  (("Dimension",), INF, 2536, ("TAG_INT", None, None))],
        finite=False, bounds=None, resets=(),
        note="Display only, but Pos is meaningless without it. Became a "
             "namespaced string at LegacyDimensionIdFix; before that it was "
             "-1/0/1.",
    ),
    dict(
        key="health", label="Health", group="vitals", editable=True,
        variants=[(("Health",), 109, INF, None),
                  (("HealF",), INF, 108, ("TAG_FLOAT", None, None)),
                  (("Health",), INF, 108, ("TAG_SHORT", None, None))],
        finite=True, bounds=[("health", 0.0, None)], resets=(),
        note="setHealth clamps to [0, maxHealth] via Mth.clamp, which is "
             "`value < min ? min : Math.min(value, max)` -- NaN fails both "
             "comparisons and passes straight through, so a NaN here survives "
             "into the running game. A missing tag reads as max health, not "
             "zero. Below DataVersion 109 a file carries both a truncated "
             "short Health and a float HealF; the float is listed first "
             "deliberately, because EntityHealthFix prefers HealF when both "
             "are present and so should any reader.",
    ),
    dict(
        key="max_health", label="Max health", group="vitals", editable=False,
        variants=[(("attributes",), 3945, INF, None),
                  (("Attributes",), INF, 3944, ("TAG_LIST", "TAG_COMPOUND", None))],
        finite=False, bounds=None, resets=(),
        note="A list of only the attributes that differ from their defaults, "
             "so the normal case is no max_health entry at all, meaning 20. "
             "AttributeModifierIdFix renamed the list and its entry keys "
             "(Name/Base -> id/base); AttributeIdPrefixFix dropped the "
             "'generic.' infix. Read to bound the Health field; editing it is "
             "out of scope for v1.",
    ),
    dict(
        key="food_level", label="Food", group="vitals", editable=True,
        variants=[(("foodLevel",), INF, INF, None)],
        finite=False, bounds=[("food", 0, 20)], resets=(),
        note="FoodData clamps to [0, 20] on every change.",
    ),
    dict(
        key="gamemode", label="Game mode", group="mode", editable=True,
        variants=[(("playerGameType",), INF, INF, None)],
        finite=False, bounds=[("mode", 0, 3)], resets=(),
        note="0 survival, 1 creative, 2 adventure, 3 spectator. Written "
             "through GameType.LEGACY_ID_CODEC, which is a plain int. A "
             "server with force-gamemode set overrides this at login.",
    ),
    dict(
        key="previous_gamemode", label="Previous game mode", group="mode",
        editable=False,
        variants=[(("previousPlayerGameType",), INF, INF, None)],
        finite=False, bounds=None, resets=(),
        note="Written nullable, so absent rather than -1 on a player who has "
             "never switched.",
    ),
    dict(
        key="xp_level", label="XP level", group="xp", editable=True,
        variants=[(("XpLevel",), INF, INF, None)],
        finite=False, bounds=[("level", 0, None)], resets=(),
        note="",
    ),
    dict(
        key="xp_progress", label="XP progress", group="xp", editable=True,
        variants=[(("XpP",), INF, INF, None)],
        finite=True, bounds=[("progress", 0.0, 1.0)], resets=(),
        note="Fraction of the way to the next level.",
    ),
    dict(
        key="xp_total", label="XP total", group="xp", editable=True,
        variants=[(("XpTotal",), INF, INF, None)],
        finite=False, bounds=[("total", 0, None)], resets=(),
        note="Lifetime total. Independent of XpLevel/XpP -- editing one "
             "without the others leaves them inconsistent, which the game "
             "tolerates but the scoreboard notices.",
    ),
    dict(
        key="xp_seed", label="Enchantment seed", group="xp", editable=False,
        variants=[(("XpSeed",), INF, INF, None)],
        finite=False, bounds=None, resets=(),
        note="Never write. Determines the enchanting table offers; changing "
             "it rerolls them.",
    ),

    # --- repair sources (read-only; see Todo.md 5.7) -----------------------
    dict(
        key="respawn", label="Player respawn point", group="repair",
        editable=False,
        variants=[(("respawn",), 4548, INF, None),
                  (("SpawnX",), INF, 4547, ("TAG_INT", None, None))],
        finite=False, bounds=None, resets=(),
        note="PlayerRespawnDataFix replaced the flat SpawnX/Y/Z + "
             "SpawnDimension/SpawnForced/SpawnAngle tags with a compound of "
             "{dimension: string, pos: int[3], yaw: float, pitch: float, "
             "forced: byte}, sharing LevelData.RespawnData with level.dat. "
             "yaw and pitch are range-checked codecs, so a NaN in either makes "
             "the whole compound fail to parse and ServerPlayer falls back to "
             "null -- the player silently loses their bed spawn. Absent "
             "entirely until a bed or anchor is used, which is the common case.",
    ),
    dict(
        key="world_spawn", label="World spawn", group="repair", editable=False,
        file="level.dat",
        variants=[(("Data", "spawn"), 4548, INF, ("TAG_COMPOUND", None, None)),
                  (("Data", "SpawnX"), INF, 4547, ("TAG_INT", None, None))],
        finite=False, bounds=None, resets=(),
        note="WorldSpawnDataFix, same schema version and same RespawnData "
             "codec as the player side. It now carries an explicit dimension, "
             "so the fallback no longer has to assume the overworld.",
    ),
]

# Sub-fields of the two respawn compounds, which share one codec.
RESPAWN_MEMBERS = [
    ("dimension", "TAG_STRING", None, None, "namespaced dimension id"),
    ("pos", "TAG_INT_ARRAY", None, 3, "block position"),
    ("yaw", "TAG_FLOAT", None, None, "Codec.floatRange(-180, 180)"),
    ("pitch", "TAG_FLOAT", None, None, "Codec.floatRange(-90, 90)"),
    ("forced", "TAG_BYTE", None, None, "player respawn only; optional, default 0"),
]

# The flat tags those compounds replaced, for files below DataVersion 4548.
# Player side and level.dat side used the same names, the latter under Data.
LEGACY_SPAWN_MEMBERS = [
    ("SpawnX", "TAG_INT", None, None, "both sides"),
    ("SpawnY", "TAG_INT", None, None, "both sides"),
    ("SpawnZ", "TAG_INT", None, None, "both sides"),
    ("SpawnAngle", "TAG_FLOAT", None, None, "both sides; the fix renamed it to yaw"),
    ("SpawnDimension", "TAG_STRING", None, None, "player side only, 1.16+"),
    ("SpawnForced", "TAG_BYTE", None, None, "player side only"),
]

# The fixes that reshape rather than rename the fields tracked above. Curated,
# because deciding which of 366 registered fixes matter to this editor is a
# judgement the source cannot make -- but each name is checked against the
# registry at render time, so a fix that disappears in a future version breaks
# the build rather than going quietly stale.
NOTABLE_FIXES = {
    "EntityHealthFix": "short Health + float HealF -> float Health",
    "PlayerUUIDFix": "UUIDLeast/UUIDMost longs -> UUID int array",
    "LegacyDimensionIdFix": "Dimension int -> namespaced string",
    "AttributeModifierIdFix": "Attributes -> attributes, Name/Base -> id/base",
    "AttributeIdPrefixFix": "dropped the 'generic.' infix from attribute ids",
    "EntityFallDistanceFloatToDoubleFix": "FallDistance float -> fall_distance double",
    "PlayerRespawnDataFix": "flat SpawnX/Y/Z -> respawn compound",
    "WorldSpawnDataFix": "flat Data.SpawnX/Y/Z -> Data.spawn compound",
}

# Load-time behaviour worth stating once rather than repeating per field.
LOAD_CONSTANTS = [
    ("POS_CLAMP_XZ", "3.0000512e7", "Entity.load clamps X and Z to +/- this"),
    ("POS_CLAMP_Y", "2.0e7", "Entity.load clamps Y to +/- this"),
    ("MOTION_DISCARD_ABOVE", "10.0", "Entity.load zeroes any Motion axis above this magnitude"),
    ("DEFAULT_MAX_HEALTH", "20.0", "when the attributes list carries no max_health entry"),
    ("DEFAULT_FOOD_LEVEL", "20", "FoodData's initial value"),
]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

HEADER = '''"""Where each editable player value lives, in every version that stores it.

GENERATED by tools/extract_mc_schema.py -- do not edit by hand. Regenerate with

    python tools/extract_mc_schema.py --workspace "<decompiled workspace>"

Source of truth: Minecraft {version}, {when}.

Two halves, and the difference matters when you are deciding whether to trust
an entry. Tag names, types and version boundaries are *extracted* -- read off
the game's own ValueInput/ValueOutput accessors and its data-fix registry, so
they are as right as the sources are. Bounds, editability and the reset lists
are *curated* -- they encode what this editor chooses to allow, and no amount
of reading Mojang's code will confirm them.

This module is pure data with no internal imports, so it sits in the bottom
layer alongside nbt and util.
"""

from dataclasses import dataclass, field
from typing import Optional


# NBT tag constants, mirrored from mcsm.nbt so this module stays importable on
# its own. Kept in sync by test_playerdat_schema.
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

TAG_NAMES = {{
    TAG_BYTE: "TAG_Byte", TAG_SHORT: "TAG_Short", TAG_INT: "TAG_Int",
    TAG_LONG: "TAG_Long", TAG_FLOAT: "TAG_Float", TAG_DOUBLE: "TAG_Double",
    TAG_BYTE_ARRAY: "TAG_Byte_Array", TAG_STRING: "TAG_String",
    TAG_LIST: "TAG_List", TAG_COMPOUND: "TAG_Compound",
    TAG_INT_ARRAY: "TAG_Int_Array", TAG_LONG_ARRAY: "TAG_Long_Array",
}}
'''

BODY = '''

@dataclass(frozen=True)
class Variant:
    """One historical shape of a field.

    `min_dv`/`max_dv` are inclusive DataVersion bounds; None at either end
    means unbounded. A file with no DataVersion tag at all predates 1.9 and is
    matched against the oldest variant."""

    path: tuple
    tag: int
    item: Optional[int] = None
    count: Optional[int] = None
    min_dv: Optional[int] = None
    max_dv: Optional[int] = None

    def covers(self, data_version: Optional[int]) -> bool:
        dv = 0 if data_version is None else data_version
        if self.min_dv is not None and dv < self.min_dv:
            return False
        if self.max_dv is not None and dv > self.max_dv:
            return False
        return True

    def describe(self) -> str:
        name = TAG_NAMES.get(self.tag, str(self.tag))
        if self.item is not None:
            name += f"<{TAG_NAMES.get(self.item, self.item)}>"
        if self.count is not None:
            name += f" x{self.count}"
        return name


@dataclass(frozen=True)
class Bound:
    """An inclusive range on one component of a field. None at either end means
    unbounded in that direction."""

    name: str
    low: Optional[float] = None
    high: Optional[float] = None

    def check(self, value) -> Optional[str]:
        """None if value is acceptable, else a message naming the problem."""
        if self.low is not None and value < self.low:
            return f"{self.name} must be at least {self.low}"
        if self.high is not None and value > self.high:
            return f"{self.name} must be at most {self.high}"
        return None


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    group: str
    editable: bool
    variants: tuple
    finite: bool = False
    bounds: tuple = ()
    resets: tuple = ()
    file: str = "playerdata"
    note: str = ""

    def variant_for(self, data_version: Optional[int]) -> Optional[Variant]:
        """The shape this field takes in a file of the given DataVersion, or
        None if the field did not exist then. Variants are ordered newest
        first, so the first match wins."""
        for variant in self.variants:
            if variant.covers(data_version):
                return variant
        return None


def field_by_key(key: str) -> Optional[FieldSpec]:
    return _BY_KEY.get(key)


def fields_in_group(group: str) -> list:
    return [f for f in FIELDS if f.group == group]


def editable_fields() -> list:
    return [f for f in FIELDS if f.editable]


def resets_for(key: str) -> list:
    """The FieldSpecs that must be written back to their defaults whenever
    `key` is changed. Position is the case that matters: moving a player
    without zeroing motion and fall distance lands them mid-fall at the
    destination, still owed the damage."""
    spec = _BY_KEY.get(key)
    if spec is None:
        return []
    return [_BY_KEY[k] for k in spec.resets if k in _BY_KEY]
'''


def render_variants(spec: dict, shapes: dict) -> list[str]:
    lines = []
    for path, min_dv, max_dv, override in spec["variants"]:
        if override is not None:
            tag, item, count = override
        else:
            found = shapes.get(path[-1])
            if found is None:
                raise ExtractionError(
                    f"field {spec['key']}: tag {path[-1]!r} is not written by "
                    f"any parsed class, and no explicit shape was given"
                )
            tag, item, count = found
        args = [f"path={path!r}", f"tag={tag}"]
        if item is not None:
            args.append(f"item={item}")
        if count is not None:
            args.append(f"count={count}")
        if min_dv is not None:
            args.append(f"min_dv={min_dv}")
        if max_dv is not None:
            args.append(f"max_dv={max_dv}")
        lines.append("            Variant(" + ", ".join(args) + "),")
    return lines


def wrap_note(note: str, indent: str) -> list[str]:
    if not note:
        return [f'{indent}note="",']
    body = textwrap.wrap(note, width=72 - len(indent))
    out = [f"{indent}note="]
    for i, chunk in enumerate(body):
        escaped = chunk.replace('\\', '\\\\').replace('"', '\\"')
        sep = " " if i < len(body) - 1 else ""
        out.append(f'{indent}    "{escaped}{sep}"')
    out[-1] += ","
    return out


def render(workspace: Path, version: str, shapes: dict, origins: dict,
           history: list, renames: list) -> str:
    out = [HEADER.format(
        version=version,
        when=datetime.date.today().isoformat(),
    ), BODY, "\n"]

    out.append("# " + "-" * 73)
    out.append("# Load-time behaviour, read off the game's own load path.")
    out.append("# " + "-" * 73 + "\n")
    for name, value, comment in LOAD_CONSTANTS:
        out.append(f"{name} = {value}  # {comment}")
    out.append("")

    out.append("\n# " + "-" * 73)
    out.append("# Members of the shared respawn compound (LevelData.RespawnData,")
    out.append("# plus `forced` on the player side via ServerPlayer.RespawnConfig).")
    out.append("# " + "-" * 73 + "\n")
    for const_name, members in (("RESPAWN_MEMBERS", RESPAWN_MEMBERS),
                                ("LEGACY_SPAWN_MEMBERS", LEGACY_SPAWN_MEMBERS)):
        out.append(f"{const_name} = (")
        for name, tag, item, count, comment in members:
            args = [f"path={(name,)!r}", f"tag={tag}"]
            if item is not None:
                args.append(f"item={item}")
            if count is not None:
                args.append(f"count={count}")
            out.append(f"    Variant({', '.join(args)}),  # {comment}")
        out.append(")\n")

    out.append("\n# " + "-" * 73)
    out.append("# The fields themselves.")
    out.append("# " + "-" * 73 + "\n")
    out.append("FIELDS = (")
    for spec in CURATED:
        # Provenance: which class's read/write methods this tag was found in.
        # A future reader chasing a changed shape starts there.
        current_tag = spec["variants"][0][0][-1]
        where = sorted(origins.get(current_tag, ()))
        if where:
            out.append(f"    # {current_tag}: {', '.join(where)}")
        out.append("    FieldSpec(")
        out.append(f'        key="{spec["key"]}",')
        out.append(f'        label="{spec["label"]}",')
        out.append(f'        group="{spec["group"]}",')
        out.append(f'        editable={spec["editable"]},')
        if spec.get("file", "playerdata") != "playerdata":
            out.append(f'        file="{spec["file"]}",')
        out.append("        variants=(")
        out.extend(render_variants(spec, shapes))
        out.append("        ),")
        out.append(f'        finite={spec["finite"]},')
        if spec.get("bounds"):
            out.append("        bounds=(")
            for name, low, high in spec["bounds"]:
                out.append(f'            Bound("{name}", {low!r}, {high!r}),')
            out.append("        ),")
        if spec.get("resets"):
            out.append(f'        resets={spec["resets"]!r},')
        out.extend(wrap_note(spec["note"], "        "))
        out.append("    ),")
    out.append(")\n")
    out.append("_BY_KEY = {f.key: f for f in FIELDS}\n")

    out.append("\n# " + "-" * 73)
    out.append("# Rename history, extracted from the data-fix registry. Every entry is a")
    out.append("# tag that changed name at a known DataVersion -- the reason a reader")
    out.append("# has to match more than one spelling. Filtered to the fields above.")
    out.append("# " + "-" * 73 + "\n")
    out.append("# (data_version, fix_class, old_name, new_name)")
    out.append("RENAMES = (")
    seen = set()
    for dv, fix, old, new in sorted(renames):
        # Scoped to the fixes that touch the fields above. Without this the
        # list fills with renames from unrelated subtrees that happen to share
        # a tag name -- "Name" alone appears in half a dozen of them.
        if fix not in NOTABLE_FIXES or (old, new) in seen:
            continue
        seen.add((old, new))
        out.append(f'    ({dv}, "{fix}", "{old}", "{new}"),')
    out.append(")\n")

    out.append("\n# The fixes that reshape (rather than rename) those fields.")
    out.append("# (data_version, fix_class, what it did)")
    out.append("RESHAPES = (")
    by_fix = {}
    for dv, name in history:
        by_fix.setdefault(name, dv)
    missing = sorted(set(NOTABLE_FIXES) - set(by_fix))
    if missing:
        raise ExtractionError(
            "these fixes are named in NOTABLE_FIXES but are not registered in "
            "this version's DataFixers: " + ", ".join(missing) + ". Either the "
            "fix was renamed or the field moved again -- check before dropping "
            "it from the table."
        )
    for name, what in sorted(NOTABLE_FIXES.items(), key=lambda kv: by_fix[kv[0]]):
        out.append(f'    ({by_fix[name]}, "{name}", "{what}"),')
    out.append(")\n")

    return "\n".join(out)


# ---------------------------------------------------------------------------


def detect_version(workspace: Path) -> str:
    """Best effort at naming the version this workspace holds, for the header.
    The folder name is what the install script wrote, so it is the most
    reliable label available without parsing SharedConstants."""
    for jar in (workspace / "libs").glob("minecraft-*.jar"):
        stem = jar.stem
        parts = stem.split("-")
        if len(parts) >= 2:
            return parts[1]
    return workspace.name


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate mcsm/playerdat_schema.py from decompiled sources."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--workspace", type=Path,
                        help="root of a decompiled Minecraft workspace you already have")
    source.add_argument("--fetch", action="store_true",
                        help="build one with Fetch's decompiler (latest release, "
                             "server side), cached between runs")
    parser.add_argument("--fetch-dir", type=Path, default=None,
                        help="where --fetch caches workspaces")
    parser.add_argument("--refetch", action="store_true",
                        help="with --fetch, re-decompile even if the cache has it")
    parser.add_argument("--out", type=Path, default=default_out_path(),
                        help="default: mcsm/playerdat_schema.py beside this "
                             "script's repo, regardless of where you run it from")
    parser.add_argument("--check", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args(argv)

    if args.fetch:
        # Imported here, not at module scope: --workspace must keep working on a
        # checkout with no Fetch/ beside it. The path insert makes this work when
        # the script is imported rather than run directly, where sys.path[0]
        # would not already be tools/.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from fetch_workspace import FetchUnavailable, ensure_workspace
        try:
            args.workspace = ensure_workspace(args.fetch_dir, force=args.refetch)
        except FetchUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            print("       (or pass --workspace with a tree you already have)",
                  file=sys.stderr)
            return 2
    elif not args.workspace.is_dir():
        print(f"error: no such workspace: {args.workspace}", file=sys.stderr)
        return 2

    try:
        history = extract_fix_history(args.workspace)
        renames = extract_renames(args.workspace, history)
        shapes, origins, unresolved = extract_field_shapes(
            args.workspace, PLAYER_CLASSES + LEVEL_CLASSES
        )
        rendered = render(
            args.workspace, detect_version(args.workspace),
            shapes, origins, history, renames,
        )
    except ExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"  fixes registered:   {len(history)}")
    print(f"  renames declared:   {len(renames)}")
    print(f"  tags resolved:      {len(shapes)}")
    if unresolved:
        print(f"  codecs unresolved:  {len(unresolved)}")
        for where, codec in sorted(unresolved):
            print(f"      {where}: {codec}")
        print("  (add to CODEC_TAGS if the editor needs the field, or to "
              "CODEC_IGNORED if it does not)")

    if args.check:
        existing = args.out.read_text(encoding="utf-8") if args.out.is_file() else ""
        if existing == rendered:
            print(f"  {args.out}: up to date")
            return 0
        print(f"  {args.out}: would change")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"  wrote {args.out} ({len(rendered.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
