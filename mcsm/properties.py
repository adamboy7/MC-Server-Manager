"""Reading and writing server.properties, including the Java Properties
escaping rules the file uses.
"""

import re
from pathlib import Path


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


def _parse_properties_text(text: str) -> dict:
    return dict(re.findall(r"^([\w.\-]+)=(.*)$", text.replace("\r", ""), re.M))
