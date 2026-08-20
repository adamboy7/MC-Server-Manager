"""Server providers. Register new loaders here."""

from .base import InstallResult, Provider, ProviderError, Version  # noqa: F401
from .decompiled import DecompiledProvider
from .fabric import FabricProvider
from .forge import ForgeProvider
from .neoforge import NeoForgeProvider
from .paper import PaperProvider
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
    # CraftBukkit -> Spigot -> Paper is the actual lineage, and since this
    # order is the dropdown order it also reads as increasing distance from
    # vanilla behaviour.
    "CraftBukkit": CraftBukkitProvider,
    "Spigot": SpigotProvider,
    "Paper": PaperProvider,
    "Fabric": FabricProvider,
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
