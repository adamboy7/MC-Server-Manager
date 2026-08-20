# Minecraft Server Setup (PoC)

Vanilla, CraftBukkit, Spigot, Paper, Fabric, Forge, NeoForge, SpongeVanilla,
SpongeForge, SpongeNeo and Decompiled. Pick a version, pick a folder, get a
runnable server -- or, with Decompiled, an editable source workspace.

    python gui.py

Provider list is standalone too:

    python -m mcserver.providers.vanilla
    python -m mcserver.providers.spigot
    python -m mcserver.providers.spigot --craftbukkit
    python -m mcserver.providers.paper
    python -m mcserver.providers.fabric
    python -m mcserver.providers.forge
    python -m mcserver.providers.neoforge
    python -m mcserver.providers.sponge
    python -m mcserver.providers.decompiled

## Layout

    gui.py                       Tkinter frontend, provider-agnostic
    mcserver/providers/base.py   Provider protocol, Version, InstallResult, fetch()
    mcserver/providers/vanilla.py  Mojang piston-meta implementation
    mcserver/providers/spigot.py   BuildTools compile: Spigot / CraftBukkit
    mcserver/providers/paper.py    papermc.io Fill v3 API implementation
    mcserver/providers/fabric.py   meta.fabricmc.net v2 implementation
    mcserver/providers/forge.py    Forge maven + installer implementation
    mcserver/providers/neoforge.py NeoForge, subclassing Forge's installer machinery
    mcserver/providers/sponge.py   SpongeVanilla / SpongeForge / SpongeNeo
    mcserver/providers/decompiled.py  Editable decompiled client/server workspace
    mcserver/providers/javafind.py JDK discovery (shared)
    mcserver/providers/__init__.py PROVIDERS registry

## Adding a loader

Write `mcserver/providers/<name>.py` exposing `list_versions()` and `install()`
per the `Provider` protocol, then add one line to `PROVIDERS`. The GUI picks it
up with no changes.

## Version channels

Two questions used to share one checkbox, and they aren't the same question:

- is this *Minecraft* version a snapshot or pre-release?
- did the *loader's* maintainers promote a recommended build for it?

Answering the second with the first is what made Forge painful. A Minecraft
release can carry builds for weeks before one is promoted, and those versions
were hidden until you ticked "include snapshots" -- which then also poured every
Minecraft dev snapshot into the list, so reaching an early mod loader for a
plain release meant wading through builds you didn't want.

`Version` now carries a `channel` (`CHANNEL_RECOMMENDED` / `CHANNEL_LATEST` /
`None` = provider's choice) alongside `kind`, and `base.channel_entries()` turns
one Minecraft version into dropdown entries for every provider:

    checkbox off   1.21.11              recommended build
                   1.21.10  (latest)    no promotion yet -- installs the newest
                                        build, and says so, instead of vanishing
                   (snapshots and pre-releases hidden)

    checkbox on    1.21.11  Recommended
                   1.21.11  Latest      only when it differs from recommended
                   25w41a  (snapshot)  Latest

`Version.build` carries the exact build when the provider could resolve it while
listing. Forge and NeoForge can -- their whole build index is one document -- so
they know which entries to emit and collapse the pair when both point at the
same build. Fabric, Paper and Sponge need a request per Minecraft version, so
they pass `resolved=False`, offer both entries, and resolve at install time;
choosing `Recommended` where none exists gets a pointed error naming the
`Latest` entry rather than a silent substitution. Either way the install notes
name which of the two you ended up with.

Vanilla, Spigot and CraftBukkit have no second axis and are untouched -- the
checkbox means there exactly what it always did. SpigotMC promotes nothing, so
a revision either builds or it doesn't.

The rules are unit-tested against canned indexes in `test_channels.py`
(`python test_channels.py`), which needs no network.

## Paper notes

Paper publishes prebuilt jars (unlike Spigot), so this is a plain
download-and-verify against papermc.io's current "Fill" API
(`fill.papermc.io/v3`, requires a real User-Agent -- the old
`api.papermc.io/v2` this used to target is deprecated). `/versions/{id}/builds`
returns every build for a Minecraft version as a bare array, each tagged
with a channel (`STABLE`/`RECOMMENDED` = promoted, `BETA`/`ALPHA` = newer
but unvetted) and a `server:default` download entry. That channel is what the
Recommended/Latest split resolves against (see "Version channels"): by default
the newest stable-channel build is installed, and a version that only has
BETA/ALPHA builds installs the newest of those rather than refusing, which is
what it used to do.

Fill's exact field names for the download's checksum/filename weren't fully
pinned down while writing this (couldn't get a live response through this
session's network), so `_download_info()` tries a couple of plausible
shapes and degrades gracefully -- if no checksum is found the jar still
installs, just without SHA-256 verification, and a note says so.
**Worth a smoke test** (`python -m mcserver.providers.paper`) on a machine
that can actually reach papermc.io before relying on this.

Paper doesn't reliably expose its own required Java version, so
`required_java()` looks up the same Minecraft version in Mojang's manifest
via `VanillaProvider` and reuses that. This is best-effort and silently
skipped if it fails for any reason.

## Spigot / CraftBukkit notes

These compile rather than download -- SpigotMC cannot legally redistribute
built jars, so BuildTools builds from source on your machine. Requires Git on
PATH, ~2 GB scratch, and a JDK inside the version's supported range. All three
are checked before the build starts.

The work dir (`%LOCALAPPDATA%/mc-server-manager/buildtools`) is reused between
builds so the git clones persist; a second build is far faster than the first.
Build logs land there as `build-<target>-<version>.log`.

Java range comes from `javaVersions` in each version's JSON, as class-file
majors (52 = Java 8). Pre-2021 entries omit it and are treated as Java 8.

Both providers live in `spigot.py` because Spigot *is* CraftBukkit plus a patch
set: same version index, same Java range, same prerequisites, same build. Only
the `--compile` target and the resulting jar name differ, so `build_target` and
`artifact_glob` are class attributes and `CraftBukkitProvider` is a dozen lines.
Since 1.14 BuildTools no longer emits the CraftBukkit jar as a byproduct of a
Spigot build, so `--compile craftbukkit` really is a separate build rather than
a different file out of the same output. Everything written into the shared work
dir is keyed by target as well as revision -- otherwise building one would wipe
the other's staging directory and overwrite the log its error message cites.

Prefer Spigot unless you specifically want unpatched behaviour: Spigot is a
strict superset, and its patches change observable mechanics (mob spawning,
item merging, and others). CraftBukkit earns its slot for two cases -- retesting
a plugin that only misbehaves under Spigot, and wanting vanilla mechanics with
a plugin API, which nothing else here offers. The install notes say as much.

## Fabric notes

Fabric is the odd one out. It neither ships a runnable server jar (vanilla,
Paper) nor compiles anything (Spigot) nor runs a heavy installer (Forge). It
publishes a small *launcher* jar that boots the vanilla server with Fabric's
classloader patched in, and pulls everything else from Maven at first start.

Four endpoints, all under `meta.fabricmc.net/v2`:

- `/versions/game` -- every Minecraft version Fabric knows about, newest first,
  with a `stable` flag that is the release/snapshot distinction.
- `/versions/intermediary` -- every version Fabric actually has mappings for.
  This is the real supported set; `/game` is a superset by a couple dozen
  entries, and `/versions/loader/{game}` returns `[]` for the difference, so the
  dropdown is the intersection of the two. Intermediary's own `stable` field is
  `true` for everything and carries no information.
- `/versions/loader/{game}` -- loader builds for that version, newest first.
  `launcherMeta.min_java_version` is the only machine-readable Java hint any
  provider in this project gets for free.
- `/versions/installer` -- installer builds.

The download is a URL shape rather than JSON:
`/versions/loader/{game}/{loader}/{installer}/server/jar`.

The dropdown lists Minecraft versions, not loader builds -- same as Forge,
including the Recommended/Latest split. "Recommended" is the newest loader
Fabric flagged `stable`; a version whose loaders are all betas now installs the
newest beta instead of refusing. The loader list costs a request per Minecraft
version, so the split resolves at install time rather than while listing.
`builds_for()` returns the full list if a future UI wants a second dropdown.
Installer version ignores the split and just prefers stable; it only affects how
the launcher jar is built, which is far less consequential than the loader.

### The vanilla jar

Left alone, the launcher fetches the vanilla server jar itself on first start.
This module downloads it up front through `VanillaProvider` and writes
`fabric-server-launcher.properties` with `serverJar=server.jar` pointing at it.
The jar therefore arrives SHA-1 verified against Mojang's manifest like every
other jar here, and the finished directory doesn't depend on a network
round-trip going right at first launch on someone else's machine. A version
Mojang has no server download for fails during resolve, before anything is
written.

The launcher still fetches Fabric's own libraries (loader, ASM, mixin -- a few
MB) into `.fabric/` on first start, so first launch is not fully offline. There
is no way around that short of reimplementing the installer. A note says so.

Fabric publishes no checksum at the `/server/jar` endpoint, so the launcher jar
is size-checked only and a note says that too. (`launcherMeta.libraries` does
carry hashes, but those are for artifacts the launcher fetches itself.)

`required_java()` takes the max of `min_java_version` and Mojang's
`javaVersion` for the same Minecraft version. Fabric's number is about the
loader, not the game -- it still reads 8 on a 1.21 entry Mojang requires 21 for.

Endpoint shapes were confirmed live and the resolve/install logic was exercised
against a local fixture server (loader selection, the beta-only refusal, the
empty-loader case, checksum mismatch leaving nothing behind). **A real install
against live meta.fabricmc.net has not been run** -- worth one
`python -m mcserver.providers.fabric` plus a single install before relying on
it.

## NeoForge notes

NeoForge is a fork of Forge by most of Forge's former maintainers, and its
installer is a fork too -- same `--installServer` flag, same processors, same
post-install layout. So `NeoForgeProvider` subclasses `ForgeProvider` and
overrides only the index layer; everything from "download the installer"
onwards is inherited.

To make that possible the Forge-specific strings in that shared machinery were
lifted into class attributes (`cache_subdir`, `maven_base`, `maven_group_path`,
`installer_name_fmt`, `legacy_jar_globs`, and `name` for user-facing messages).
`_detect_layout` and `_write_start_scripts` became instance methods. Nothing
about Forge's behaviour changed -- there's an equivalence test for that.

The index is one flat list and there is no promotions file:

    GET https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge
    -> {"isSnapshot": false, "versions": ["20.2.3-beta", ..., "26.2.0.59-beta"]}

oldest first. NeoForge has no "recommended" concept at all, so the only
stability signal is the string itself: a `-beta` suffix means unstable. That
maps onto the channel split cleanly: newest non-beta plays the recommended
build, newest of any plays latest, and a Minecraft version with only betas
lists as `(latest)` instead of being hidden. The release/snapshot state of the
Minecraft version itself comes from Mojang's manifest, which this module
already fetches to map versions in the first place.

### Version numbers

Versions don't name their Minecraft version outright. The scheme is "Minecraft
version with the leading `1.` stripped, plus a build number":

    MC 1.20.2  -> 20.2.<build>
    MC 1.21    -> 21.0.<build>      (a zero patch component is inserted)
    MC 1.21.11 -> 21.11.<build>
    MC 26.1.2  -> 26.1.2.<build>    (new-style MC ids keep all three parts)
    MC 26.2    -> 26.2.0.<build>

Reversing it is ambiguous in two directions at once: `26.2.0.59` could be MC
`26.2.0` or `26.2`, and `21.0.167` could be `1.21.0` or `1.21`. Rather than
guess a rule that breaks the next time Mojang renumbers, candidates are
generated most-likely-first and the first one that actually exists in Mojang's
manifest wins. Mojang's id set is fetched once per provider instance, not once
per version -- there are ~1500. Versions that resolve to nothing (the
`0.25w14craftmine.*` April Fools builds) are dropped rather than shown as bogus
entries. Offline, the first candidate is used so the dropdown still populates.

Layout is always the argfile form -- NeoForge starts at 1.20.2, well past the
1.17 cutoff where runnable jars disappeared -- so `legacy_jar_globs` is empty.
NeoForge also writes its own `run.sh`/`run.bat`/`user_jvm_args.txt`; those are
left alone and our `start.bat`/`start.sh` are written alongside them.

**Not smoke-tested against a live install.** The version->Minecraft mapping and
the beta-only refusal are unit-tested against fixtures, but this session's
network could not reach maven.neoforged.net, so no real `--installServer` run
happened. Worth one `python -m mcserver.providers.neoforge` plus a single
install.

## Sponge notes

Three providers in one module, because SpongeVanilla, SpongeForge and SpongeNeo
are one API with three artifact ids rather than three APIs. Everything except
the last step of `install()` is shared in `_SpongeBase`.

Everything hangs off `dl-api.spongepowered.org/v2/groups/org.spongepowered`:

- `/artifacts` -- `{"artifactIds": ["spongevanilla", "spongeforge",
  "spongeneo"]}`.
- `/artifacts/{id}` -- project metadata whose `tags` object lists every value
  each tag has ever taken: `api`, `minecraft`, and `forge` or `neoforge`.
- `/artifacts/{id}/versions?tags=minecraft:1.21.1&recommended=true` -- paginated
  builds, each with a `recommended` flag and a `tagValues` map naming the exact
  Forge/NeoForge build it needs.
- `/artifacts/{id}/versions/{build}` -- an `assets` array of about a dozen
  entries. The only one that matters is `classifier == "universal"`,
  `extension == "jar"`: the server for SpongeVanilla, the mod jar for the
  other two.

### Picking a build

`recommended` is rare -- Minecraft 1.21.1 has 285 SpongeVanilla builds and not
one is flagged; they're all release candidates. So the rule is Paper's and
Forge's: prefer a recommended build, fall back to the newest RC, and name which
one you got in the install notes. Requiring the checkbox for that fallback -- as
this used to -- made nearly every Sponge version un-installable without also
pouring every Minecraft snapshot into the dropdown. The API returns builds
newest-first, so selection
is "first entry of the filtered page" rather than a sort -- Sponge's build
strings (`1.21.1-12.0.4-RC2684`) aren't reliably sortable as text.

### The version dropdown

Built from the artifact's `minecraft` tag values. Neither the release/snapshot
distinction nor the ordering is parsed out of the version string -- `1.21.11`,
`25w41a` and `26.1.2` share no grammar and no sortable shape. Each id is looked
up in Mojang's manifest through `VanillaProvider`, which already has both the
type and the release date. Ids Mojang doesn't know are kept but treated as
snapshots and sorted last, so a newly-targeted version never vanishes from the
list just because the lookup lagged.

### What each one installs

**SpongeVanilla** is self-contained: the universal jar *is* the server, and it
downloads Minecraft and its libraries itself on first start. Unlike Fabric,
nothing is pre-fetched here -- Sponge's installer does its own remapping and
wants to fetch its own copy.

**SpongeForge** and **SpongeNeo** are mod jars needing a base server underneath
at *exactly* the build named in the Sponge build's `forge`/`neoforge` tag --
Sponge patches those internals, so the build number has to match. Both delegate
the entire base install to `ForgeProvider.install_build()` /
`NeoForgeProvider.install_build()` with the build pinned, then drop the
universal jar into `mods/`. Launch shape, start scripts and Java requirement all
come back from the base provider unchanged; this module only appends notes.
`check_prerequisites()` delegates too, so a missing JDK is reported before a
`--installServer` run starts rather than several minutes in. The base build is
resolved before anything is written, so a Sponge release pinned to a build that
has vanished from maven fails cleanly instead of leaving a half-built directory.

That pinning is what `ForgeProvider.find_build()` and `install_build()` were
added for -- `install()` previously chose the promoted build itself and there
was no way to ask for a specific one. `install()` is now a thin wrapper that
resolves the build then delegates.

### Folders

`content_dir` is `plugins` for all three. That trips people up on the Forge
variants, where the Sponge jar itself goes in `mods/` but Sponge *plugins* go in
`plugins/` -- so both directories get created and a note says which is which.

**Not smoke-tested against a live install.** All three flows are unit-tested
against a fixture server (build selection, the RC-only refusal, asset picking,
base-build pinning, the mod jar landing in `mods/`, notes merging, and a failed
base resolve writing nothing), but no real download from
dl-api.spongepowered.org happened. Worth one `python -m mcserver.providers.sponge`
plus a single install of each.

## Forge notes

Forge sits between the two: it doesn't compile, but it doesn't ship a runnable
server jar either. What gets downloaded is an *installer*, which is then run
locally with `--installServer`. The installer pulls the vanilla server jar plus
a few hundred megabytes of libraries and lays out the directory itself. Expect
2-10 minutes, mostly download-bound, rather than Spigot's 10-30.

Two endpoints, both needed:

- `files.minecraftforge.net/.../maven-metadata.json` -- every artifact version,
  keyed by Minecraft version.
- `files.minecraftforge.net/.../promotions_slim.json` -- `{"1.20.1-recommended":
  "47.4.10", "1.20.1-latest": "47.4.22", ...}`.

The promos file holds a *bare* Forge version, but the maven artifact for older
releases carries a trailing branch (`1.7.10-10.13.4.1614-1.7.10`), so
`resolve_build()` matches the promo against the metadata list instead of
formatting a URL directly. Formatting directly 404s on everything pre-1.8.

The dropdown lists Minecraft versions, not Forge builds. Forge is the provider
the channel split was written for (see "Version channels"): a Minecraft release
with no promotion is listed as `1.21.10  (latest)` and installs the newest
build, rather than being hidden behind the snapshots checkbox as it used to be.
Tick the checkbox and each version splits into `Recommended` and `Latest`
entries, collapsing to one where both resolve to the same artifact. The
release/snapshot state of the Minecraft version comes from Mojang's manifest
via `VanillaProvider`, since Forge's own index says nothing about it.
`builds_for()` returns the full list if a future UI wants a second dropdown.

Installers are immutable, so they're cached for a month in
`%LOCALAPPDATA%/mc-server-manager/forge/`. Maven serves a sibling `.sha1`; it is
verified when present, and a `.verified` sidecar records that fact so a later
cache hit doesn't wrongly report the jar as unchecked. Installer logs land in
the same folder as `install-<artifact>.log`.

### Launch shape

- **1.16.5 and older** -- a runnable `forge-*.jar` in the server root, launched
  with `-jar` like everything else.
- **1.17 and newer** -- no runnable jar. Forge writes per-platform argfiles to
  `libraries/net/minecraftforge/forge/<artifact>/{win,unix}_args.txt` and the
  server starts with `java @<that file> nogui`. This is why
  `InstallResult.launch_argv` is a list rather than a jar path.

`_detect_layout()` decides by looking at what is on disk rather than by version
number, since the exact release where this flipped is fuzzier than the changelog
suggests. `start.bat` and `start.sh` are both written pointing at their own
platform's argfile, so a directory built on Windows still starts on Linux.

Forge publishes no machine-readable Java requirement, so `required_java()`
reuses Mojang's `javaVersion` for the same Minecraft version via
`VanillaProvider`, falling back to Java 8 for anything pre-1.17. The installer
runs under the *oldest* JVM found that satisfies that -- Forge's processors have
historically broken on JVMs far newer than the target.

**Not smoke-tested against a live install** -- this session's network could not
reach maven.minecraftforge.net, so the index and promos shapes were confirmed
but a real `--installServer` run was not. Worth one
`python -m mcserver.providers.forge` plus a single install before relying on it.

## Decompiled notes

The only core that does not produce a server. It produces a source tree you can
edit, compile and launch -- the pre-loader way of modding, and still the fastest
way to answer "what does the game actually do here".

The dropdown carries two rows per Minecraft version, `Client` and `Server`,
distinguished by `Version.variant`. The client is the interesting one; the
server row exists because sometimes it isn't. Rows before 1.2.5 are client-only,
since that is when Mojang started publishing a server jar at all.

### Three eras of getting readable names

This is the whole design. Everything else follows from it.

1. **Pre-1.14.4** -- no official mappings exist. Legacy Fabric's yarn covers
   1.3 -> 1.13.2 and is applied with tiny-remapper. The only third-party
   dependency in the project.
2. **1.14.4 -> the last obfuscated build** -- Mojang publishes ProGuard mapping
   files in the version JSON (`downloads.client_mappings`). They run
   *named -> obfuscated*; SpecialSource wants TSRG the other way, so
   `proguard_to_tsrg()` inverts them. Method descriptors have to be rewritten in
   obfuscated class names, which is why it takes two passes over the file.
3. **Post-deobfuscation** -- Mojang removed obfuscation from Java Edition. The
   first snapshot after Mounts of Mayhem ships client and server with the
   original names, parameter names included, and drops the mapping entries from
   the version JSON entirely. Nothing to remap.

`_mapping_plan()` does not use a cutoff date, which would be wrong the first
time Mojang backfills something. It **looks at the jar**: if most of its classes
already live under `net/minecraft/` or `com/mojang/`, that's era 3. The
heuristic is a ratio rather than "does net/minecraft exist", because obfuscated
clients keep `net/minecraft/client/main/Main` so the launcher can find it.

### Decompiler

Vineflower, not Fernflower or CFR. It is a Fernflower fork whose Minecraft
output is close enough to real source to be worth the extra minute. Libraries
are passed with `-e=` so it can resolve types; on Windows that argument list
gets close to the 32k command-line cap, so past 30k it is written to a
`java @argfile` instead.

Tool jars (Vineflower, SpecialSource, tiny-remapper) are resolved from
`maven-metadata.xml` at install time with pinned versions as an offline
fallback. Pinning alone rots; resolving alone breaks offline.

### The server jar is not the server

Since 1.18 `server.jar` is a *bundler* -- it carries the real server jar and its
dependencies under `META-INF/`, listed in `versions.list` / `libraries.list`.
Decompiling it directly gets you forty classes of extraction logic, which is a
confusing way to fail, so `_unbundle_server()` unwraps it first and uses the
bundled dependency set instead of the client's LWJGL-heavy `libraries` list.

### What lands in the folder

    src/main/java/         decompiled, editable sources
    src/main/resources/    everything in the jar that wasn't a class
    libs/                  dependencies + the remapped Minecraft jar
    natives/               extracted LWJGL natives (client only)
    assets/                asset objects + index (client only)
    run/                   game directory for the launch configs
    mappings/              the mapping file used, kept for reference
    build.gradle, settings.gradle
    .project, .classpath, "Minecraft Client.launch"
    start-client.(bat|sh) or start-server.(bat|sh)
    WORKSPACE.md

Eclipse imports it with **no Gradle at all** -- `.project`, `.classpath` and a
`.launch` config are written directly. Requiring a 130 MB Gradle distribution to
open a workspace whose whole point is "just let me edit the game" would undercut
the exercise. `build.gradle` is there for IntelliJ/VS Code, and the start
scripts prove the folder is self-contained.

### Why Minecraft stays on the classpath

`libs/minecraft-<version>-<side>.jar` sits *after* the compiled sources in every
one of the three configs. Decompiler output for a whole game is never fully
javac-clean; when a handful of files won't build you delete them and the
original classes resolve from the jar. That escape hatch is the difference
between a source dump and a working environment, and it's what `WORKSPACE.md`
tells the user to do first.

### Making it actually compile

A raw Vineflower run on 26.2 produced ~1986 javac errors. They were three
distinct things, and only the third is real:

1. **Annotations Minecraft compiles against but does not ship.** `@Contract`,
   `@Immutable` and `@CheckReturnValue` have CLASS retention, so they survive
   into the bytecode and come back out as imports -- but their jars are
   build-time only and appear nowhere in the version JSON. `COMPILE_EXTRAS`
   pulls jsr305 and org.jetbrains:annotations from Maven Central.

   Related: `MacosUtil` imports `ca.weblite.objc`, whose jar the version JSON
   marks macOS-only. Filtering the classpath by `os` rule meant javac could not
   see it on Windows. `_library_artifacts()` now returns *every* artifact for
   the compile classpath and the rule-allowed subset separately -- the rules
   only decide which natives get unpacked, never what can be compiled.

2. **Vineflower's record constructors.** `--hide-empty-super` does not reach
   inside them, so every record comes out as

       public EntityDataAccessor {
          super();
       }

   which javac rejects outright. A compact canonical constructor with an empty
   body is indistinguishable from no declaration, so `strip_record_super()`
   deletes the block -- a provable no-op, not a guess. It matches only names
   declared with the `record` keyword in that file, only bodies that are exactly
   `super();`, and only closing braces at the opening line's indent, so it
   cannot touch a plain class (where `super()` is legal and load-bearing) or a
   record with a real compact constructor. The test suite proves both halves by
   handing the before and after to a real javac.

3. **Genuine information loss.** `StreamCodec.cast()` returning `this`,
   `EntityDataSerializer` no longer being a functional interface, `Registry.get`
   inference, statics that lost their type variable. Nothing fixes these
   mechanically.

### The jar signature

Mojang signs the game jars. Keeping one on the classpath after the compiled
sources -- which is the whole point of the fallback -- means a package gets
classes from both a signed jar and unsigned freshly-compiled output, and the JVM
refuses:

    java.lang.SecurityException: class "net.minecraft.util.ARGB"'s signer
    information does not match signer information of other classes in the same
    package

Once a package is defined by a class out of the signed jar, every later class in
it must carry the same signer, and ours carry none. A package cannot be split.

`unsign_jar()` strips `META-INF/*.{SF,RSA,DSA,EC}` and trims the manifest back
to its main section -- for 26.2 that manifest was 4.6 MB of per-entry SHA-384
digests, and the jar drops from 39 MB to 34 MB as a side effect. Every path out
of `_remap()` goes through it, including MAP_NONE, which is otherwise a straight
copy of Mojang's file. Removing the signature rather than the jar keeps the
fallback intact, and is what every Minecraft workspace toolchain has always
done; it is not a meaningful security downgrade, since the jar was already
verified by SHA-1 against the version manifest at download time.

The test suite proves this end to end with a real JDK: it builds a fixture jar,
signs it with a generated `keytool` key, confirms the JVM produces that exact
error, unsigns, and confirms the game runs and uses the recompiled class.

### The quarantine loop

For (3), `_repair_workspace()` compiles, parses javac's `<path>:<line>: error:`
prefixes, moves the blamed files to `.decompiled/quarantine/`, and repeats to a
fixed point. Removing a file breaks its dependents, hence the loop rather than a
single pass.

Two guards, both there because the failure modes are bad:

* Only lines with the `error:` keyword count. Quarantining a file over a
  deprecation warning would be a spectacular own goal, and this build log is
  full of `[removal]` warnings.
* If javac fails but blames no file -- bad classpath, out of memory, broken JDK
  -- nothing is moved and the error is surfaced. Deleting sources to fix a
  problem somewhere else is the worst available outcome.
* A `REPAIR_MAX_FRACTION` cap (5%) aborts the whole thing rather than eating the
  game if it stops converging.

Quarantined files cost nothing at runtime -- their original classes still load
from `libs/` -- they are simply not editable. `.decompiled/quarantine.txt` lists
them. Set `DecompiledProvider.repair_workspace = False` for a decompile-only
run.

### Assets

Content-addressed by hash, so the store lives in the shared cache and is
hardlinked into each workspace -- three versions of the client would otherwise
be three gigabytes of identical sound files. Copy is the fallback for
cross-drive and filesystems without links. Pre-1.7 index quirks (`virtual`,
`map_to_resources`) are materialised as loose files where those clients expect
them.

### Testing

`python test_decompiled.py` covers the parts that fail silently rather than
loudly: the ProGuard inversion (including constructors and array descriptors),
the version-JSON rule evaluator, native classifier selection, argument
templating for both the modern `arguments.game` array and the old flat
`minecraftArguments` string, the bundler unwrap, the obfuscation heuristic, and
the generated Eclipse XML. No network, no Java needed.

**Live-tested on 26.2 client (Windows, Java 25)** -- download, decompile,
resources, assets and project generation all worked. Two bugs came out of that
run and are fixed with regression tests:

* the javac argfile quoting bug below;
* the ~1986 compile errors, addressed above.

After those fixes the 26.2 client compiled with **zero errors and 58 warnings**
(all `[removal]` deprecations), and nothing needed quarantining. The next
failure was the jar signature, above.

**Not yet live-tested:** the server side, the Mojang-mappings path (1.14.4 to
the last obfuscated build) and the yarn path (pre-1.14.4). This session has no
route to piston-meta or the Legacy Fabric maven, so those formats are confirmed
from documentation only.

### The javac argfile trap

Two rules fight each other and getting one right while missing the other is how
the first Windows run broke:

* paths must be quoted, or a workspace under `My Documents` splits into two
  arguments;
* inside those quotes backslash is an **escape**, so a plain
  `"C:\Users\Adam\Desktop\Test\..."` reaches javac as
  `C:UsersAdamDesktopTest...` with every separator eaten. Every escape
  collapses to the bare character -- `\b` gives `b`, not a backspace.

Writing the separators as forward slashes satisfies both: Windows javac accepts
them and there is nothing left for the quotes to eat. Both the generated batch
file (via `!sf:\=/!` and delayed expansion) and `_write_source_list()` do this,
and the test suite models javac's argfile parser to assert that naive quoting
*does* destroy the path -- so nobody simplifies it back.
