"""Shared contract every server provider implements.

The point of this module is that the GUI never imports a concrete provider's
internals -- it only ever touches these three types. Adding Paper/Fabric/Forge
later means writing a new module that returns the same shapes.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Sequence

USER_AGENT = "mc-server-manager/0.1 (proof-of-concept; +https://adamboy7.win)"

# Stage names passed to ProgressCB. Kept loose on purpose -- providers that need
# extra stages (BuildTools compiling, Forge installer running) can invent their
# own without touching the GUI.
STAGE_INDEX = "index"
STAGE_RESOLVE = "resolve"
STAGE_DOWNLOAD = "download"
STAGE_VERIFY = "verify"
STAGE_FINALIZE = "finalize"
STAGE_BUILD = "build"      # long, opaque work (BuildTools, Forge installer)

# (stage, done, total_or_None, human_message)
# total is None when the length is unknown -> GUI shows an indeterminate bar.
ProgressCB = Callable[[str, int, Optional[int], str], None]


class ProviderError(RuntimeError):
    """Anything that went wrong that the user should see in a dialog."""


class Cancelled(ProviderError):
    """Raised when the user aborts a long-running install."""


def check_cancelled(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("Cancelled.")


# Which build of the *loader* an entry installs, independent of how stable the
# *Minecraft* version is. `kind` below answers "is this Minecraft release a
# snapshot"; `channel` answers "did the loader's maintainers promote this
# build". Conflating the two is what used to force the snapshots checkbox on
# just to install a plain Minecraft release Forge hadn't promoted a build for.
CHANNEL_RECOMMENDED = "recommended"
CHANNEL_LATEST = "latest"
# channel=None means "the provider decides": prefer recommended, fall back to
# latest.

LABEL_RECOMMENDED = "Recommended"
LABEL_LATEST = "Latest"
# Shown on a collapsed entry that had to fall back, so the list stays honest
# without making you enable pre-releases to see the version at all.
LABEL_FALLBACK = "(latest)"


@dataclass(frozen=True)
class Version:
    """One selectable entry in the version dropdown."""

    id: str
    kind: str = "release"          # release | snapshot | old_beta | old_alpha | ...
    released: Optional[str] = None  # ISO8601, for sorting
    # Opaque provider-specific handle (e.g. Mojang's per-version JSON URL,
    # a Paper build number, a Forge maven coordinate). The GUI never reads this.
    ref: Optional[str] = None
    # Which loader build this entry means. See the constants above.
    channel: Optional[str] = None
    # The exact build, when the provider could resolve it while listing.
    # Providers whose build list costs a request per Minecraft version leave
    # this None and resolve from `channel` at install time.
    build: Optional[str] = None
    # Display only. Kept separate from `channel` so listing policy (what the
    # dropdown says) never leaks into resolution policy (what gets installed).
    suffix: str = ""
    # A second axis a provider may need *within* one Minecraft version, where
    # `channel` is already spoken for. The Decompiled core carries
    # "client" / "server" here so one dropdown entry means one distribution
    # rather than one loader build -- conflating the two would have made
    # "1.21.4 Client Recommended" impossible to express. The GUI never reads it.
    variant: Optional[str] = None

    @property
    def is_stable(self) -> bool:
        return self.kind == "release"

    def __str__(self) -> str:  # what shows up in the combobox
        label = self.id if self.is_stable else f"{self.id}  ({self.kind})"
        return f"{label}  {self.suffix}" if self.suffix else label


def channel_entries(
    id: str,
    include_unstable: bool,
    *,
    kind: str = "release",
    released: Optional[str] = None,
    ref: Optional[str] = None,
    recommended: Optional[str] = None,
    latest: Optional[str] = None,
    resolved: bool = True,
) -> list[Version]:
    """Dropdown entries for one Minecraft version. Shared by every loader.

    `recommended` / `latest` are build identifiers, or None when that build
    does not exist. `resolved=False` means the provider could not find out
    without a network round-trip per Minecraft version (Fabric, Paper, Sponge);
    the channel is then carried on the entry and resolved during install.

    Pre-releases off:
      * snapshot / pre-release *Minecraft* versions are hidden
      * exactly one entry per version -- the recommended build where there is
        one, otherwise the latest build, marked `(latest)`

    Pre-releases on:
      * unstable Minecraft versions appear
      * "{version}  Recommended" and "{version}  Latest" as separate entries,
        collapsed to one when both would install the same build
    """
    def make(channel: Optional[str], build: Optional[str], suffix: str) -> Version:
        return Version(
            id=id, kind=kind, released=released, ref=ref,
            channel=channel, build=build, suffix=suffix,
        )

    if not include_unstable and kind != "release":
        return []

    if not resolved:
        # Unknown until install time, so collapsing is impossible here and the
        # Recommended entry is offered on faith; providers raise a pointed
        # error at resolve time if it turns out not to exist.
        if not include_unstable:
            return [make(None, None, "")]
        return [
            make(CHANNEL_RECOMMENDED, None, LABEL_RECOMMENDED),
            make(CHANNEL_LATEST, None, LABEL_LATEST),
        ]

    if not include_unstable:
        if recommended:
            return [make(CHANNEL_RECOMMENDED, recommended, "")]
        if latest:
            return [make(CHANNEL_LATEST, latest, LABEL_FALLBACK)]
        return []

    out: list[Version] = []
    if recommended:
        out.append(make(CHANNEL_RECOMMENDED, recommended, LABEL_RECOMMENDED))
    if latest and latest != recommended:
        out.append(make(CHANNEL_LATEST, latest, LABEL_LATEST))
    return out


@dataclass
class InstallResult:
    """What a completed install produced."""

    server_dir: str
    jar_path: Optional[str] = None
    # argv template for launching. Placeholders: {java}, {jar}, {min_ram}, {max_ram}
    # A list, not a string, so Forge's @argfile launch fits without shell quoting.
    launch_argv: Sequence[str] = field(default_factory=list)
    java_major: Optional[int] = None   # required Java, when the provider knows it
    notes: Sequence[str] = field(default_factory=list)


class Provider(Protocol):
    """Implemented by vanilla.py, and later paper.py / fabric.py / forge.py."""

    name: str
    content_dir: Optional[str]  # "plugins", "mods", or None for vanilla

    def list_versions(self, include_unstable: bool = False) -> list[Version]:
        """Newest first. May hit the network; cache internally."""
        ...

    def install(
        self,
        version: Version,
        dest_dir: str,
        progress: Optional[ProgressCB] = None,
        accept_eula: bool = False,
        cancel: Optional[threading.Event] = None,
    ) -> InstallResult:
        ...


def fetch(url: str, timeout: float = 20.0, retries: int = 2) -> bytes:
    """GET with a real User-Agent and naive backoff.

    Mojang doesn't currently require a UA, but Paper's v3 API does and several
    Maven mirrors 403 the Python default, so everything goes through here.
    """
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
    raise ProviderError(f"Could not fetch {url}: {last}")
