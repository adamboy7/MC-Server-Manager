"""The data the rest of the package passes around: what a server is, what
platform it runs, and who plays on it.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


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
