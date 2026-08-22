"""The loader vocabulary shared across this package and Fetch/.

These identifiers are a contract, not an implementation detail: detection
returns them, and Fetch's PROVIDERS dict is keyed by the same strings, so a
detected server can be handed straight to the provider that installs it.
Adding a loader here without adding it to Fetch (or vice versa) is what the
cross-check in the test suite is there to catch.
"""


# ---------------------------------------------------------------------------
# The loader names
#
# One string per server platform, used as the identifier everywhere: what
# detect.probe_install() and probe_world() return, what Platform.loader holds,
# and what Fetch/mcserver/providers keys its PROVIDERS dict by.
#
# Fetch installs; this package inspects. Keeping one spelling between them is
# what makes "this folder is Paper 26.1.2" and "install the latest Paper"
# talk about the same thing. The two we detect but cannot install yet
# (Pufferfish, Leaf) are listed here deliberately -- detection running ahead
# of installation is fine, the reverse is a bug, and test_loaders_match_fetch()
# checks that direction.
# ---------------------------------------------------------------------------

LOADER_FAMILIES = {
    "Vanilla": "vanilla",
    "CraftBukkit": "bukkit", "Spigot": "bukkit", "Paper": "bukkit",
    "Purpur": "bukkit", "Folia": "bukkit", "Pufferfish": "bukkit", "Leaf": "bukkit",
    "Fabric": "fabric-like", "Quilt": "fabric-like",
    "Forge": "fml", "NeoForge": "fml",
    "SpongeVanilla": "sponge", "SpongeForge": "sponge", "SpongeNeo": "sponge",
}

# level.dat brand strings -> our loader names. Case-folded on lookup.
WORLD_BRAND_NAMES = {
    "vanilla": "Vanilla", "fabric": "Fabric", "quilt": "Quilt",
    "forge": "Forge", "neoforge": "NeoForge",
    "craftbukkit": "CraftBukkit", "spigot": "Spigot", "paper": "Paper",
    "purpur": "Purpur", "folia": "Folia", "pufferfish": "Pufferfish", "leaf": "Leaf",
}


#: Which family a loader belongs to. Family drives behaviour (where mods and
#: plugins live, whether dimensions are split); the loader name is for display
#: and for handing to Fetch.
FAMILIES = ("vanilla", "bukkit", "fabric-like", "fml", "sponge", "unknown")

#: Every loader this package can name, in rough lineage order.
KNOWN_LOADERS = tuple(LOADER_FAMILIES)


def family_of(loader):
    """The family a loader name belongs to, or "unknown" for anything we
    don't recognise -- including a brand read verbatim out of a world that
    was made by something we've never seen."""
    return LOADER_FAMILIES.get(loader or "", "unknown")
