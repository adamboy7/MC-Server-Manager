"""Folia Minecraft server provider (papermc.io Fill v3 API).

Standalone: `python -m mcserver.providers.folia` prints the version list.

Folia is PaperMC's own fork of Paper, and it ships through the same Fill v3
API under a different project id -- so this module is the project identity and
nothing else. Everything from "list the versions" through "download, verify,
write the start scripts" is PaperProvider's, unchanged, the same way
neoforge.py inherits Forge's installer machinery and overrides only the index.

What is *not* shared is what the finished server means, and that is the whole
reason this file has a docstring longer than its code.

## Folia is not a drop-in Paper

Folia splits the world into regions and ticks them on separate threads. Two
consequences the install notes have to state plainly, because a user who picks
"Folia" out of a dropdown expecting a faster Paper will otherwise have a bad
afternoon:

**Plugins do not carry over.** Upstream is explicit: "only plugins that have
been explicitly marked by the author(s) to work with Folia will be loaded. By
placing 'folia-supported: true' into the plugin's plugin.yml, plugin authors
can mark their plugin as compatible with regionised multithreading." So a
plugins/ folder copied from a Paper server silently loads almost nothing.
`content_dir` is still "plugins" -- that is where they go -- but the note says
what will actually happen to them.

**It wants real hardware and real players.** Upstream again: "Ideally, at least
16 *cores* (not threads)", and "Server types that naturally spread players out,
like skyblock or SMP, will benefit the most from Folia. The server should have
a sizeable player count, too." On a 4-core box with six players Folia is a
downgrade, not an upgrade. Saying so at install time is cheaper than letting
someone find out.

## Channels

Folia's builds have historically sat on Fill's ALPHA/BETA channels rather than
STABLE, and PaperProvider already handles that correctly: with the checkbox
off, `_select_build(channel=None)` falls back to the newest build of any
channel instead of refusing, and the install note names the channel it landed
on. Choosing "Recommended" explicitly on a version with no STABLE build still
gets the pointed error naming the "Latest" entry -- which is the right
behaviour here rather than an annoyance, since on Folia that error is telling
you something true about how finished the build is.

The default start scripts are inherited as-is, `-Xms1G -Xmx2G`. That is
deliberately not special-cased: it is the wrong number for a 16-core Folia
deployment, but it is equally the wrong number for a busy Paper server, and
guessing a bigger one here would just be a different wrong number. The heap
note in the install output points at it.
"""

from __future__ import annotations

from .paper import PaperProvider


class FoliaProvider(PaperProvider):
    name = "Folia"
    content_dir = "plugins"
    compiles_from_source = False

    project = "folia"
    cache_file = "folia_versions.json"
    # download_key is inherited: Fill serves Folia under the same
    # "server:default" key as Paper.

    install_notes = (
        "Folia only loads plugins whose plugin.yml declares 'folia-supported: true'. "
        "Plugins copied from a Paper server will be skipped unless their author has "
        "added that flag -- check each one before relying on it.",
        "Folia is built for large, spread-out servers: upstream recommends at least "
        "16 CPU cores (not threads) and a sizeable player count. Below that it will "
        "usually perform worse than plain Paper.",
        "The generated start scripts use the default -Xms1G -Xmx2G. Raise it to suit "
        "the machine before running a real Folia server.",
    )


def _cli() -> None:
    p = FoliaProvider()
    versions = p.list_versions(include_unstable=False)
    print(f"{len(versions)} versions, latest = {p.latest_release()}")
    for v in versions[:15]:
        print(" ", v)
    print("\nwith pre-releases:")
    for v in p.list_versions(include_unstable=True)[:6]:
        print(" ", v)
    if versions:
        newest = versions[0]
        build = p._select_build(newest.id, channel=newest.channel)
        print(f"\n{newest.id} resolves to build "
              f"{build.get('id') or build.get('build')} "
              f"({build.get('channel', '?')})")


if __name__ == "__main__":
    _cli()
