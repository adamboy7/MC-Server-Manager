"""Everything the Minecraft Server Manager knows about Minecraft servers,
with no GUI dependency anywhere in it.

Layered bottom-up, and the layering is enforced by there being no import
cycles -- each module only reaches downward:

    nbt  loaders  model  properties  util  textedit     (no internal imports)
    players  -> properties
    world    -> nbt
    backups  -> world
    mojang   -> util
    detect   -> loaders model nbt players properties util world

Only `mojang` needs a third-party package (requests). Everything else is
stdlib, so the domain logic can be imported and tested without tkinter, PIL
or a display.

Importing this package pulls in `mojang` and therefore `requests`; import the
submodules directly (`from mcsm.backups import ...`) if you want to avoid it.
"""

from . import backups, detect, loaders, model, nbt, players, properties, util, world
from .model import Platform, PlayerInfo, ServerInfo

__all__ = [
    "backups", "detect", "loaders", "model", "nbt", "players", "properties",
    "util", "world",
    "Platform", "PlayerInfo", "ServerInfo",
]
