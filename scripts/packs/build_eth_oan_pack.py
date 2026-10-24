#!/usr/bin/env python3
"""Build the ETH-OAN pack: Ethiopia's current hierarchy down to kebele.

    python build_eth_oan_pack.py --source geo_level_values.json \\
        --base ../../packs/ETH --out ../../packs/ETH-OAN

Why a second Ethiopia pack
--------------------------
`ETH` is OCHA COD-AB: four levels (country -> region -> zone -> woreda), with
boundaries. A farmer registry in Ethiopia records the kebele as well, and it
uses the hierarchy as it stands after the 2021-2023 reorganisation: Sidama and
South West regions, the new Liyu zones, and woredas split since COD-AB was cut.
That catalog, 14 regions, 125 zones, 1,379 woredas and 19,535 kebeles, was
maintained by the OpenAgriNet farmer registry. This script turns it into a pack.
`ETH` stays untouched, and an environment chooses one with `geoSeed.countryPack`.

What is taken from where
------------------------
- Hierarchy and names come from the source catalog.
- Code lists, domains and address.json come from the base pack, so the two
  Ethiopia packs describe a farmer the same way. Kebele moves out of
  address.json's sub_levels, because here it is a level.
- No boundaries/. No polygons exist for the kebeles or the new units. A pack
  without boundaries is valid and draws no map. Map surfaces keep using `ETH`,
  whose region, zone and woreda P-codes this pack reuses wherever the unit is
  the same.
- samples/ are the base pack's people, moved down one level: the kebele
  written in address_parts becomes the unit, geo_pcode = <woreda><kebele>.
  Master Data loads them into g2p_sample_individuals, which both the
  registry's sample data and the mock identity system seed from, so they must
  sit on units this pack has. A sample whose kebele is missing stops the build.

Identifiers
-----------
The pack contract requires the id to be the P-code, and each child's code to
extend its parent's code by two digits. The source ids were prefixed
("woreda-ET010101") and do not always nest, because woredas were re-parented
without their codes, or their kebeles' codes, changing. So:

- a unit whose bare code nests under its parent keeps that code;
- a woreda that does not nest is re-minted under its zone, with the lowest
  two-digit suffix that no unit in this pack or in the base pack uses. A new
  code can then never collide with a COD-AB unit, which would draw another
  woreda's polygon on a map;
- every kebele is re-minted as <woreda code><two-digit sequence>, ordered by
  source id. The largest woreda has 79 kebeles, so two digits are enough.

Duplicate names
---------------
Master Data rejects two units with the same mnemonic under one parent, and the
source has 155 such groups. Where exactly one unit in a group has children,
the others are leftovers of a re-parented unit (a woreda listed once under its
old code with no kebeles, and again under its new code with them). Those are
merged into the unit with children. Every other collision is kept as distinct
units with the same display_name; the second and later get the pack id
appended to their mnemonic, so only the internal key changes.

migration/id_map.csv maps every source id, merged ones included, to its pack id
for records that already store the old ids. Output is deterministic: re-running
on the same inputs gives a byte-identical pack.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
from collections import defaultdict

LEVELS = ["country", "region", "zone", "woreda", "kebele"]
SOURCE_LEVEL = {"level-region": "region", "level-zone": "zone",
                "level-woreda": "woreda", "level-kebele": "kebele"}
COUNTRY = {"pcode": "ET", "name": "Ethiopia"}
BASE_SOURCE = "OCHA COD-AB"
OWN_SOURCE = "OpenAgriNet"


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def bare(source_id):
    return source_id.split("-", 1)[1]


def merge_leftovers(rows):
    """Drop childless duplicates of a unit that has children: {dropped id: kept id}."""
    kids = defaultdict(int)
    for r in rows:
        kids[r["parent_level_value_id"]] += 1
    groups = defaultdict(list)
    for r in rows:
        groups[(r["level_id"], r["parent_level_value_id"], r["level_value_mnemonic"])].append(r["level_value_id"])
    merged = {}
    for members in groups.values():
        alive = [m for m in members if kids[m]]
        if len(members) > 1 and len(alive) == 1:
            merged |= {m: alive[0] for m in members if m != alive[0]}
    return merged


def build_ids(rows, base_codes):
    """Return {source_id: pack_id}, walking the hierarchy top-down."""
    by_level = defaultdict(list)
    for r in rows:
        by_level[SOURCE_LEVEL[r["level_id"]]].append(r)
    ids: dict[str, str] = {}
    taken = set(base_codes)
    for lvl in LEVELS[1:]:
        for r in by_level[lvl]:
            if lvl != "kebele":
                taken.add(bare(r["level_value_id"]))

    for lvl in LEVELS[1:]:
        children = defaultdict(list)
        for r in sorted(by_level[lvl], key=lambda r: r["level_value_id"]):
            parent = ids[r["parent_level_value_id"]] if r["parent_level_value_id"] else COUNTRY["pcode"]
            children[parent].append(r)
        for parent, rs in sorted(children.items()):
            seq = 0
            for r in rs:
                code = bare(r["level_value_id"])
                if lvl != "kebele" and code.startswith(parent) and len(code) == len(parent) + 2:
                    ids[r["level_value_id"]] = code
                    continue
                while True:  # kebeles always land here; a non-nesting unit too
                    seq += 1
                    if seq > 99:
                        raise SystemExit(f"{parent}: more than 99 {lvl} children")
                    cand = f"{parent}{seq:02d}"
                    if lvl == "kebele" or cand not in taken:
                        break
                taken.add(cand)
                ids[r["level_value_id"]] = cand
    return ids


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", required=True, help="source catalog (geo_level_values.json)")
    ap.add_argument("--base", required=True, help="base Ethiopia pack (packs/ETH)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", default="2026-10-24")
    args = ap.parse_args()

    with open(args.source, "rb") as fh:
        raw = fh.read()
    source_rows = json.loads(raw)
    merged = merge_leftovers(source_rows)
    rows = [r for r in source_rows if r["level_value_id"] not in merged]
    base_values = read_json(os.path.join(args.base, "values.json"))
    base_by_code = {v["level_value_id"]: v for v in base_values}
    base_depth = {lv["level_id"]: i for i, lv in enumerate(read_json(os.path.join(args.base, "levels.json")))}

    ids = build_ids(rows, base_by_code)
    ids |= {old: ids[kept] for old, kept in merged.items()}
    level_ids = {m: f"l{i}" for i, m in enumerate(LEVELS)}

    values = [{
        "level_value_id": COUNTRY["pcode"], "level_id": "l0",
        "level_value_mnemonic": COUNTRY["name"], "parent_level_value_id": None,
        "pcode": COUNTRY["pcode"], "pcode_source": BASE_SOURCE, "display_name": COUNTRY["name"],
    }]
    for r in rows:
        lvl = SOURCE_LEVEL[r["level_id"]]
        pid = ids[r["level_value_id"]]
        same_unit = pid == bare(r["level_value_id"]) and pid in base_by_code \
            and base_depth.get(base_by_code[pid]["level_id"]) == LEVELS.index(lvl)
        values.append({
            "level_value_id": pid,
            "level_id": level_ids[lvl],
            "level_value_mnemonic": r["level_value_mnemonic"],
            "parent_level_value_id": ids[r["parent_level_value_id"]] if r["parent_level_value_id"] else COUNTRY["pcode"],
            "pcode": pid,
            "pcode_source": BASE_SOURCE if same_unit else OWN_SOURCE,
            "display_name": r["display_name"],
        })
    values.sort(key=lambda v: (int(v["level_id"][1:]), v["level_value_id"]))
    seen = set()
    for v in values:
        key = (v["level_id"], v["parent_level_value_id"], v["level_value_mnemonic"])
        if key in seen:
            v["level_value_mnemonic"] = f"{v['level_value_mnemonic']} ({v['level_value_id']})"
        seen.add(key)

    readme = os.path.join(args.out, "README.md")  # hand-written, survives a rebuild
    readme_text = open(readme, encoding="utf-8").read() if os.path.exists(readme) else None
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    if readme_text is not None:
        os.makedirs(args.out)
        with open(readme, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(readme_text)
    write_json(os.path.join(args.out, "levels.json"), [
        {"level_id": level_ids[m], "level_mnemonic": m,
         "parent_level_id": level_ids[LEVELS[i - 1]] if i else None}
        for i, m in enumerate(LEVELS)])
    write_json(os.path.join(args.out, "values.json"), values)

    counts = {m: sum(1 for v in values if v["level_id"] == level_ids[m]) for m in LEVELS}
    base_manifest = read_json(os.path.join(args.base, "manifest.json"))
    write_json(os.path.join(args.out, "manifest.json"), {
        "country": "ETH",
        "synthetic": False,
        "source": f"{OWN_SOURCE} farmer registry location catalog; codes reused from {BASE_SOURCE} where the unit is unchanged",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "base_pack": "ETH",
        "base_pack_version": base_manifest.get("version"),
        "license": base_manifest["license"],
        "license_note": base_manifest.get("license_note"),
        "version": args.version,
        "identifier": "P-code used as level_value_id",
        "levels": LEVELS,
        "unit_counts": counts,
    })

    for sub in ("codelists", "domains"):
        src = os.path.join(args.base, sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(args.out, sub))
    address = read_json(os.path.join(args.base, "address.json"))
    address["sub_levels"] = [s for s in address["sub_levels"] if s["id"] != "kebele"]
    address["lines"] = ["House {house_number}"]
    write_json(os.path.join(args.out, "address.json"), address)

    units = {v["level_value_id"] for v in values}
    for kind in ("individuals", "households"):
        recs = read_json(os.path.join(args.base, "samples", f"{kind}.json"))
        for rec in recs:
            parts = dict(rec.get("address_parts") or {})
            kebele = rec["geo_pcode"] + parts.pop("kebele", "")
            if kebele not in units:
                raise SystemExit(f"sample {rec.get(kind[:-1] + '_id')}: no unit {kebele} in this pack")
            rec["geo_pcode"] = kebele
            rec["address_parts"] = parts
        write_json(os.path.join(args.out, "samples", f"{kind}.json"), recs)

    os.makedirs(os.path.join(args.out, "migration"))
    with open(os.path.join(args.out, "migration", "id_map.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["source_id", "pack_id", "level", "change"])
        for r in sorted(source_rows, key=lambda r: r["level_value_id"]):
            sid = r["level_value_id"]
            change = "merged" if sid in merged else "reminted" if ids[sid] != bare(sid) else "none"
            w.writerow([sid, ids[sid], SOURCE_LEVEL[r["level_id"]], change])

    remint = defaultdict(int)
    for r in rows:
        if ids[r["level_value_id"]] != bare(r["level_value_id"]):
            remint[SOURCE_LEVEL[r["level_id"]]] += 1
    print(f"ETH-OAN: {counts}; re-minted {dict(remint)}; merged {len(merged)} leftover duplicates")


if __name__ == "__main__":
    main()
