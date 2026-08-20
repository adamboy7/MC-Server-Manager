"""Player identity: UUID shapes, and resolving them to names from the JSON
files a server keeps beside its world.
"""

import json
import re
import uuid as uuid_lib
from pathlib import Path

from .properties import read_server_properties


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
