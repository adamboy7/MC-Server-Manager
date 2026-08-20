"""Small helpers with no domain knowledge.
"""

import os
from datetime import datetime
from pathlib import Path


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
# Face texture fetching (threaded, disk-cached)
# ---------------------------------------------------------------------------

CACHE_DIR = Path.home() / ".mc_server_manager_cache"
