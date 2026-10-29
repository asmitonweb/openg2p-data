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
  Ethiopia packs describe a farmer the same way. Lists only the farmer
  registry needs are added from eth_oan_additions/ and never replace a base
  list. eth_oan_additions/values/ adds values to a base list, never replacing
  one: SELF, which carries the head_of_household role the contract requires.
  eth_oan_additions/attributes/ adds per-value `attributes`, and the
  `attribute_schema` describing them, to a base list, never replacing a value:
  LIVESTOCK_SPECIES says per species whether an animal carries an ear tag and
  whether it is recorded as a flock. Each file is {"attribute_schema": ...,
  "values": {value_id: attributes}}; the build stops if a named value is
  missing or the base list already has a different attribute_schema. Kebele moves out of address.json's sub_levels, because here it is a
  level.
- boundaries/ are the base pack's polygons for the units that are still the
  same unit, so every OAN registry and dashboard draws and looks up locations
  from Master Data rather than its own copy. A woreda keeps its polygon when
  its code is COD-AB's. A region or zone keeps it when, besides that, none of
  the base pack's woredas inside it has left it: a polygon is never larger than
  the unit now is. The new units (Sidama, South West, the Liyu zones, woredas
  split since 2021) and the kebeles have no polygon, so coverage is partial
  and the manifest says how partial (`boundary_coverage`).
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
# Code lists this pack adds to the base pack's, laid out as in a pack.
ADDITIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eth_oan_additions")


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


def carry_boundaries(base, values):
    """{level: features} from the base pack, for the units that are unchanged."""
    bdir = os.path.join(base, "boundaries")
    same = {v["level_value_id"]: v for v in values if v["pcode_source"] == BASE_SOURCE}
    level_of = {f"l{i}": m for i, m in enumerate(LEVELS)}
    layers = {m: read_json(os.path.join(bdir, f"{m}.geojson"))["features"]
              for m in LEVELS if os.path.exists(os.path.join(bdir, f"{m}.geojson"))}
    kept_woredas = {f["properties"]["pcode"] for f in layers.get("woreda", [])
                    if f["properties"]["pcode"] in same
                    and level_of[same[f["properties"]["pcode"]]["level_id"]] == "woreda"}
    base_woredas = {f["properties"]["pcode"] for f in layers.get("woreda", [])}
    out = {}
    for m, features in layers.items():
        kept = []
        for f in features:
            unit = same.get(f["properties"]["pcode"])
            if not unit or level_of[unit["level_id"]] != m:
                continue
            code = unit["level_value_id"]
            if m == "woreda" and code not in kept_woredas:
                continue
            if m in ("region", "zone") and any(w.startswith(code) and w not in kept_woredas for w in base_woredas):
                continue
            props = dict(f["properties"])
            props["area_name"] = unit["display_name"]
            props["parent_pcode"] = unit["parent_level_value_id"]
            kept.append({**f, "properties": props})
        out[m] = kept
    return out


def apply_attributes(out):
    """Add per-value attributes from eth_oan_additions/attributes/ to base lists."""
    attrs_dir = os.path.join(ADDITIONS, "attributes")
    for dirpath, _, files in os.walk(attrs_dir):
        for name in sorted(files):
            rel = os.path.relpath(os.path.join(dirpath, name), attrs_dir)
            dest = os.path.join(out, rel)
            if not os.path.exists(dest):
                raise SystemExit(f"{rel}: the base pack has no such list to add attributes to")
            doc = read_json(dest)
            overlay = read_json(os.path.join(dirpath, name))
            have = doc.get("attribute_schema")
            if have is not None and have != overlay["attribute_schema"]:
                raise SystemExit(f"{rel}: the base list already has a different attribute_schema")
            by_id = {v["value_id"]: v for v in doc["values"]}
            for value_id in overlay["values"]:
                if value_id not in by_id:
                    raise SystemExit(f"{rel}: the base list has no value {value_id}")
            doc["attribute_schema"] = overlay["attribute_schema"]
            for value_id, attributes in overlay["values"].items():
                by_id[value_id]["attributes"] = attributes
            missing = [v["value_id"] for v in doc["values"] if "attributes" not in v]
            if missing:
                raise SystemExit(f"{rel}: no attributes for {', '.join(missing)}")
            write_json(dest, doc)


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
    boundaries = carry_boundaries(args.base, values)
    os.makedirs(os.path.join(args.out, "boundaries"))
    for m, features in boundaries.items():
        # One line, as the base pack writes them: these are data, not reading.
        with open(os.path.join(args.out, "boundaries", f"{m}.geojson"), "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"type": "FeatureCollection", "features": features}, fh, ensure_ascii=False)
            fh.write("\n")
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
        # Units with a polygon, per level; the rest have none (see the docstring).
        "boundary_coverage": {m: len(boundaries.get(m, [])) for m in LEVELS},
    })

    for sub in ("codelists", "domains"):
        src = os.path.join(args.base, sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(args.out, sub))
    # Lists only the farmer registry needs are added on top. They never replace
    # a base list, so both Ethiopia packs keep describing a farmer the same way.
    for dirpath, _, files in os.walk(ADDITIONS):
        if os.path.relpath(dirpath, ADDITIONS).split(os.sep)[0] in ("values", "attributes"):
            continue
        for name in files:
            rel = os.path.relpath(os.path.join(dirpath, name), ADDITIONS)
            dest = os.path.join(args.out, rel)
            if os.path.exists(dest):
                raise SystemExit(f"{rel}: the base pack already has this list")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(os.path.join(dirpath, name), dest)
    # Values a base list lacks but the pack contract needs, such as the value
    # that carries a role. Added, never replacing one: a base value with the
    # same id stops the build, so the addition is dropped once ETH has it.
    values_dir = os.path.join(ADDITIONS, "values")
    for dirpath, _, files in os.walk(values_dir):
        for name in files:
            rel = os.path.relpath(os.path.join(dirpath, name), values_dir)
            dest = os.path.join(args.out, rel)
            if not os.path.exists(dest):
                raise SystemExit(f"{rel}: the base pack has no such list to add values to")
            doc = read_json(dest)
            have = {v["value_id"] for v in doc["values"]}
            for value in read_json(os.path.join(dirpath, name))["values"]:
                if value["value_id"] in have:
                    raise SystemExit(f"{rel}: the base pack already has {value['value_id']}; drop the addition")
                doc["values"].append(value)
            doc["values"].sort(key=lambda v: v.get("sort_order", 0))
            write_json(dest, doc)
    apply_attributes(args.out)
    address =read_json(os.path.join(args.base, "address.json"))
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
