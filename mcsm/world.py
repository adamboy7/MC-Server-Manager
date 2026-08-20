"""Where things live inside a world folder, and moving a world around.

The layout changed in stages rather than in one cutover, so each of these
resolves its own path newest-first and falls back instead of consulting a
version number.
"""

import os
import shutil
import time
from pathlib import Path
from typing import Optional

from .nbt import load_level_dat, load_nbt_file, nbt_path, read_nbt_byte


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
