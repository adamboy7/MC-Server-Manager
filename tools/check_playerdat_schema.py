#!/usr/bin/env python3
"""Check mcsm/playerdat_schema.py against real player files.

The generated table says what each field should look like at a given
DataVersion. This walks actual `<uuid>.dat` files, resolves every field through
the table, and reports one of four outcomes per field:

    ok        the tag is present and its NBT type matches the variant
    absent    the table expects it here and the file does not have it
    mismatch  the tag is present with a type the table did not predict
    n/a       the table says this field does not exist at that DataVersion

`absent` is not automatically a bug -- respawn tags are missing until a player
sleeps in a bed, and attributes only lists values that differ from default. It
is `mismatch` that means the table is wrong, and that is what the exit status
keys off.

The sample worlds are local scratch (Test/ is gitignored), so this is a
developer check rather than a committed test. Point it at whatever spread of
versions you have.

Usage
-----
    python tools/check_playerdat_schema.py Test/
    python tools/check_playerdat_schema.py Test/ --verbose
"""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcsm import playerdat_schema as schema           # noqa: E402
from mcsm.nbt import NBTReader, find_tag_offset       # noqa: E402


PLAYERDATA_GLOBS = ("*/playerdata/*.dat", "*/players/data/*.dat")


def load_raw(path: Path) -> bytes:
    raw = path.read_bytes()
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


def data_version(raw: bytes):
    """The file's DataVersion, or None for anything old enough to predate the
    tag. find_tag_offset walks rather than decodes, so this costs almost
    nothing even on a large file."""
    found = find_tag_offset(raw, ("DataVersion",))
    if found is None:
        return None
    offset, tag_type = found
    if tag_type != schema.TAG_INT:
        return None
    return int.from_bytes(raw[offset:offset + 4], "big", signed=True)


def probe(raw: bytes, variant) -> tuple[str, str]:
    """(outcome, detail) for one variant against one file."""
    try:
        found = find_tag_offset(raw, variant.path)
    except ValueError as exc:
        return "mismatch", f"unreadable: {exc}"
    if found is None:
        return "absent", ""

    _offset, tag_type = found
    if tag_type != variant.tag:
        return "mismatch", (
            f"expected {schema.TAG_NAMES.get(variant.tag, variant.tag)}, "
            f"found {schema.TAG_NAMES.get(tag_type, tag_type)}"
        )

    # For lists, the element type and length are part of the contract and are
    # exactly the part a naive reader gets wrong, so check them too.
    if variant.tag == schema.TAG_LIST and variant.item is not None:
        reader = NBTReader(raw)
        reader.pos = _offset
        item_type = reader.read_ubyte()
        length = reader.read_int()
        if item_type != variant.item:
            return "mismatch", (
                f"list of {schema.TAG_NAMES.get(item_type, item_type)}, "
                f"expected {schema.TAG_NAMES.get(variant.item, variant.item)}"
            )
        if variant.count is not None and length != variant.count:
            return "mismatch", f"{length} elements, expected {variant.count}"

    return "ok", ""


def check_file(path: Path, label: str, verbose: bool) -> dict:
    raw = load_raw(path)
    dv = data_version(raw)
    tally = {"ok": 0, "absent": 0, "mismatch": 0, "n/a": 0}
    rows = []

    for spec in schema.FIELDS:
        if spec.file != "playerdata":
            continue
        variant = spec.variant_for(dv)
        if variant is None:
            tally["n/a"] += 1
            rows.append(("n/a", spec.key, "no variant at this DataVersion"))
            continue
        outcome, detail = probe(raw, variant)
        tally[outcome] += 1
        rows.append((outcome, spec.key, detail or ".".join(variant.path)))

    # The respawn compound's members are only checkable when the compound is
    # there at all, which for most players it is not.
    respawn = schema.field_by_key("respawn")
    respawn_variant = respawn.variant_for(dv) if respawn else None
    if respawn_variant is not None and respawn_variant.tag == schema.TAG_COMPOUND:
        if find_tag_offset(raw, respawn_variant.path) is not None:
            for member in schema.RESPAWN_MEMBERS:
                full = respawn_variant.path + member.path
                outcome, detail = probe(raw, schema.Variant(
                    path=full, tag=member.tag,
                    item=member.item, count=member.count,
                ))
                # `forced` is an optional field with a default, so its absence
                # is expected rather than notable.
                if outcome == "absent" and member.path[-1] == "forced":
                    outcome = "ok"
                tally[outcome] = tally.get(outcome, 0) + 1
                rows.append((outcome, "respawn." + member.path[-1],
                             detail or ".".join(full)))

    print(f"\n{label}  ({path.name[:8]}..., "
          f"DataVersion {dv if dv is not None else 'absent'})")
    print(f"  {tally['ok']} ok, {tally['absent']} absent, "
          f"{tally['mismatch']} mismatch, {tally['n/a']} n/a")
    for outcome, key, detail in rows:
        if outcome == "mismatch":
            print(f"    MISMATCH  {key}: {detail}")
        elif verbose:
            print(f"    {outcome:<9} {key}: {detail}")
    return tally


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path,
                        help="folder holding sample server directories")
    parser.add_argument("--verbose", action="store_true",
                        help="list every field, not just mismatches")
    args = parser.parse_args(argv)

    files = []
    for server in sorted(p for p in args.root.iterdir() if p.is_dir()):
        for pattern in PLAYERDATA_GLOBS:
            files.extend((path, server.name) for path in sorted(server.glob(pattern)))

    if not files:
        print(f"no playerdata found under {args.root}", file=sys.stderr)
        return 2

    totals = {"ok": 0, "absent": 0, "mismatch": 0, "n/a": 0}
    for path, label in files:
        for key, value in check_file(path, label, args.verbose).items():
            totals[key] = totals.get(key, 0) + value

    print(f"\n{len(files)} files: {totals['ok']} ok, {totals['absent']} absent, "
          f"{totals['mismatch']} mismatch, {totals['n/a']} n/a")
    return 1 if totals["mismatch"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
