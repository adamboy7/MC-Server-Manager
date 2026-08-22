"""Server providers. Register new loaders here."""

from .base import InstallResult, Provider, ProviderError, Version  # noqa: F401
from .decompiled import DecompiledProvider
from .fabric import FabricProvider
from .folia import FoliaProvider
from .forge import ForgeProvider
from .neoforge import NeoForgeProvider
from .paper import PaperProvider
from .purpur import PurpurProvider
from .quilt import QuiltProvider
from .spigot import CraftBukkitProvider, SpigotProvider
from .sponge import (
    SpongeForgeProvider,
    SpongeNeoProvider,
    SpongeVanillaProvider,
)
from .vanilla import VanillaProvider

# GUI reads this. Adding a new loader = one import + one line.
PROVIDERS = {
    "Vanilla": VanillaProvider,
    # CraftBukkit -> Spigot -> Paper -> Purpur is the actual lineage, and since
    # this order is the dropdown order it also reads as increasing distance from
    # vanilla behaviour.
    "CraftBukkit": CraftBukkitProvider,
    "Spigot": SpigotProvider,
    "Paper": PaperProvider,
    "Purpur": PurpurProvider,
    # PaperMC's own fork, and the one entry here that is not a drop-in for the
    # line above it -- Folia only loads plugins that opted in. Kept adjacent to
    # Paper anyway, because that is where someone will look for it.
    "Folia": FoliaProvider,
    "Fabric": FabricProvider,
    # Quilt is a Fabric fork but installs like Forge -- it runs an installer,
    # so it needs a JVM present. See quilt.py's header.
    "Quilt": QuiltProvider,
    "Forge": ForgeProvider,
    "NeoForge": NeoForgeProvider,
    "SpongeVanilla": SpongeVanillaProvider,
    "SpongeForge": SpongeForgeProvider,
    "SpongeNeo": SpongeNeoProvider,
    # Not a server at all: produces an editable, runnable source workspace.
    # Listed last because it is the odd one out, not because it is least useful.
    "Decompiled": DecompiledProvider,
}


def get_provider(name: str):
    try:
        return PROVIDERS[name]()
    except KeyError:
        raise ProviderError(f"Unknown provider: {name}") from None
