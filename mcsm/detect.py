"""Working out what server software a folder is running, from two independent
probes: what is installed, and what last ran on the world.
"""

import json
import re
import zipfile
from pathlib import Path
from typing import Optional

from .loaders import LOADER_FAMILIES, WORLD_BRAND_NAMES
from .model import Platform, PlayerInfo, ServerInfo
from .nbt import load_level_dat, nbt_path
from .players import is_probably_bedrock_uuid, is_valid_uuid_format, load_name_map
from .properties import _parse_properties_text, parse_difficulty, read_server_properties
from .util import find_last_log_date
from .world import get_playerdata_dir, read_world_seed


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

# Manifest Main-Class -> what kind of launcher this jar is. Note that Paper,
# Purpur, Spigot and CraftBukkit all write "org.bukkit.craftbukkit.Main" into
# META-INF/main-class, so that file cannot be used here -- only the manifest
# attribute plus META-INF/versions.list separates them.
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
