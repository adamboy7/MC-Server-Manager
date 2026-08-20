"""Mojang's session server, for names that aren't in any local file. The only
module here that touches the network.
"""

import json
import threading
from typing import Optional

import requests

from .util import CACHE_DIR


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
