#!/usr/bin/env python3
"""Check that a directory is a valid country pack.

    python validate_pack.py ../../packs/ETH ../../packs/XKM

Exits non-zero on the first pack that fails, so this works as a CI gate and as a
pre-commit check after regenerating a pack.

Why this exists
---------------
A pack is a contract between three things that never run together: a generator
(fetch_country_pack.py or generate_synthetic_pack.py) writes it, MDS's
load_geo_pack.py seeds a database from it, and the Evidence map surface draws it.
A pack that is subtly wrong does not fail loudly — it seeds a hierarchy with
orphans, or draws a choropleth with holes in it, and the damage surfaces days
later as figures that do not add up.

The checks below are the ones that have actually caught bugs:

  - children escaping their parent. The first synthetic boundary generator inset
    each child at *render* time rather than at assignment, so every single child
    at every level poked outside its parent. Drill-down looked fine until you
    zoomed in.
  - geometry and hierarchy disagreeing. A unit with no polygon vanishes from the
    map while still counting in the totals, so the map and the KPI above it tell
    different stories.
  - non-hierarchical P-codes. The whole reason for using P-codes as identifiers
    is that a child's code contains its parent's; code that relies on that
    breaks silently when it stops being true.
  - unit_counts drifting from reality after a hand edit.

Checks are grouped as errors (the pack is unusable) and warnings (the pack works
but something is off).
"""

from __future__ import annotations

import argparse
import json
import datetime
import os
import re
import sys

from _geometry import (
    outer_rings,
    point_in_polygon,
    polygon_area,
    ring_is_simple,
    rings,
)

# A child's area may fall this far short of a clean partition of its parent
# before it counts as a gap. Simplification moves vertices, so an exact match is
# not achievable for a real pack; COD-AB levels are independently simplified and
# routinely differ by a few percent.
AREA_TOLERANCE = 0.06

# Fraction of a child's sampled interior points that must land inside the parent.
# Not 100%: a simplified child and a simplified parent disagree along their
# shared boundary, so points near an edge legitimately fall outside.
NESTING_THRESHOLD = 0.90

REQUIRED_MANIFEST = ["country", "source", "license", "levels", "unit_counts"]

# Closed vocabulary — see packs/ROLES.md. A role not listed here is a typo, and a
# typo must fail the build rather than silently match nothing.
#   single: exactly one value in that list must hold it
#   set:    one or more may
def load_roles():
    """The role vocabulary, read from packs/roles.json.

    Read rather than restated. It began as a markdown table beside a matching
    dict in this file — two copies of one contract, which is the drift this whole
    exercise exists to remove.

    Returns (single, many): role -> owning attribute. A role is only *required*
    when its owning list is present, since a pack need not define every list.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "packs", "roles.json")
    roles = json.load(open(path))["roles"]
    single = {r: m["attribute"] for r, m in roles.items() if m["cardinality"] == "one"}
    many = {r: m["attribute"] for r, m in roles.items() if m["cardinality"] == "many"}
    return single, many


SINGLE_ROLES, SET_ROLES = load_roles()
KNOWN_ROLES = set(SINGLE_ROLES) | set(SET_ROLES)


class Report:
    def __init__(self, name):
        self.name = name
        self.errors = []
        self.warnings = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def ok(self):
        return not self.errors


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


def sample_interior(geom, n=12):
    """A handful of points genuinely inside `geom`, for the nesting test.

    Vertices are the wrong thing to test: a child's vertices sit *on* the shared
    boundary, where inside/outside is ambiguous once either polygon has been
    simplified. So candidates are drawn inwards from the vertices instead — and
    then each is confirmed to be inside the child before it is used.

    That confirmation is the part that matters. Without it a doughnut-shaped
    unit fails its own test: Ethiopia's ET0420 (Shager City) encircles Addis
    Ababa, its vertex mean falls in the hole, and every point drawn towards that
    mean lands outside the unit — and therefore outside its parent too, which
    reads as a nesting violation when nothing is wrong. Only points inside the
    child can say anything about whether the child is inside its parent.
    """
    out = []
    for ring in outer_rings(geom):
        pts = ring[:-1] if ring and ring[0] == ring[-1] else ring
        if not pts:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        step = max(1, len(pts) // n)
        for cand in [(cx, cy)] + [(p[0] + 0.7 * (cx - p[0]), p[1] + 0.7 * (cy - p[1]))
                                  for p in pts[::step]]:
            if point_in_polygon(cand, geom):
                out.append(cand)
    return out


def check_codelists(pack_dir, r):
    """Code lists are optional; when present they must be loadable and coherent.

    The role checks matter most. Platform logic asks for a role rather than a
    literal, so a missing or duplicated role is a metric that silently reads
    wrong — which is how is_head came to be false for every household.
    """
    # Core lists, then every domain subtree (domains/<domain>/*.json, same
    # format). MDS loads both into one attribute table keyed by attribute_id, so
    # a domain list that reuses a core id would silently overwrite it.
    dirs = []
    if os.path.isdir(os.path.join(pack_dir, "codelists")):
        dirs.append("codelists")
    domains_dir = os.path.join(pack_dir, "domains")
    if os.path.isdir(domains_dir):
        dirs += [os.path.join("domains", dom) for dom in sorted(os.listdir(domains_dir))
                 if os.path.isdir(os.path.join(domains_dir, dom))]
    if not dirs:
        return
    seen_roles, docs, owner_of = {}, [], {}
    files = [(rel, fn) for rel in dirs
             for fn in sorted(os.listdir(os.path.join(pack_dir, rel))) if fn.endswith(".json")]
    for rel, fn in files:
        doc = read_json(os.path.join(pack_dir, rel, fn))
        docs.append(doc)
        attr = doc.get("attribute_id")
        if not attr:
            r.error(f"{rel}/{fn} has no attribute_id")
            continue
        if attr in owner_of:
            r.error(f"{attr} is declared twice: {owner_of[attr]} and {rel}/{fn}")
            continue
        owner_of[attr] = f"{rel}/{fn}"
        if fn != f"{attr.lower()}.json":
            r.warn(f"{rel}/{fn} declares {attr}; expected {attr.lower()}.json")
        values = doc.get("values") or []
        if not values:
            r.error(f"{attr} has no values")
            continue
        ids = [v.get("value_id") for v in values]
        if len(set(ids)) != len(ids):
            r.error(f"{attr} has duplicate value_id")
        if any(not i for i in ids):
            r.error(f"{attr} has a value with no value_id")
        for v in values:
            for role in v.get("roles", []):
                if role not in KNOWN_ROLES:
                    r.error(f"{attr}.{v.get('value_id')} claims unknown role "
                            f"'{role}' — add it to packs/roles.json first")
                seen_roles.setdefault(role, []).append(f"{attr}.{v.get('value_id')}")
        parents = {v.get("parent_value_id") for v in values if v.get("parent_value_id")}
        orphan = parents - set(ids)
        if orphan:
            r.error(f"{attr} has values whose parent is missing: {sorted(orphan)}")

    check_typed_attributes(docs, r)

    # Check every role whose list is present — NOT just the roles that turned up.
    # Iterating over what was found cannot see a role held by nobody, which is
    # the exact shape of the is_head bug: the list is there, the tag is missing,
    # and the metric silently reads zero.
    present = {d.get("attribute_id") for d in docs}
    for role, owner in sorted({**SINGLE_ROLES, **SET_ROLES}.items()):
        if owner not in present:
            continue
        holders = seen_roles.get(role, [])
        if not holders:
            r.error(f"{owner} is in this pack but no value carries the "
                    f"'{role}' role — anything derived from it reads empty")
        elif role in SINGLE_ROLES and len(holders) != 1:
            r.error(f"role '{role}' must be held by exactly one value, "
                    f"found {len(holders)}: {holders}")


_JSON_TYPES = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
               "array": (list,), "object": (dict,)}


def _list_refs(schema, path=""):
    """[(path, referenced list code)] for every x-list-ref, as Master Data reads them."""
    out = []
    if not isinstance(schema, dict):
        return out
    if isinstance(schema.get("x-list-ref"), str):
        out.append((path or "$", schema["x-list-ref"]))
    for name, sub in (schema.get("properties") or {}).items():
        out += _list_refs(sub, f"{path}.{name}" if path else name)
    if isinstance(schema.get("items"), dict):
        out += _list_refs(schema["items"], f"{path}[]")
    return out


def _attribute_errors(schema, value):
    """Errors of one value's attributes: JSON Schema when jsonschema is installed,
    else the subset the packs use (object, required, additionalProperties, types)."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        Draft202012Validator = None
    if Draft202012Validator is not None:
        return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
                for e in Draft202012Validator(schema).iter_errors(value)]
    errors = []
    if not isinstance(value, dict):
        return ["(root): not an object"]
    props = schema.get("properties") or {}
    errors += [f"{k}: required" for k in schema.get("required") or [] if k not in value]
    if schema.get("additionalProperties") is False:
        errors += [f"{k}: not in the schema" for k in value if k not in props]
    for k, v in value.items():
        want = (props.get(k) or {}).get("type")
        ok = _JSON_TYPES.get(want)
        if ok and (not isinstance(v, ok) or (want in ("integer", "number") and isinstance(v, bool))):
            errors.append(f"{k}: not {want}")
    return errors


def _ref_values(schema, value):
    """{list code: codes} that the value's x-list-ref fields hold."""
    out = {}
    if not isinstance(schema, dict) or value is None:
        return out
    ref = schema.get("x-list-ref")
    if isinstance(ref, str):
        for v in value if isinstance(value, list) else [value]:
            if isinstance(v, str):
                out.setdefault(ref, set()).add(v)
    if isinstance(value, dict):
        for name, sub in (schema.get("properties") or {}).items():
            if name in value:
                for k, codes in _ref_values(sub, value[name]).items():
                    out.setdefault(k, set()).update(codes)
    if isinstance(schema.get("items"), dict) and isinstance(value, list):
        for item in value:
            for k, codes in _ref_values(schema["items"], item).items():
                out.setdefault(k, set()).update(codes)
    return out


def check_typed_attributes(docs, r):
    """A list's attribute_schema, and each value's attributes against it.

    Master Data checks these when a draft is submitted, not when the loader writes
    it: a pack value that breaks its list's schema, or names a code its x-list-ref
    list lacks, loads fine and then cannot be approved. So check them here:
    every x-list-ref names a list in this pack, and every referenced code is a
    value_code of that list.
    """
    by_code = {(d.get("attribute_code") or d.get("attribute_id")): d for d in docs if d.get("attribute_id")}
    for doc in docs:
        attr = doc.get("attribute_id")
        schema = doc.get("attribute_schema")
        values = doc.get("values") or []
        if schema is None:
            stray = [v.get("value_id") for v in values if v.get("attributes")]
            if stray:
                r.error(f"{attr} values carry attributes but the list has no attribute_schema: {stray[:5]}")
            continue
        if not isinstance(schema, dict) or schema.get("type") != "object":
            r.error(f"{attr}.attribute_schema must be a JSON Schema of type object")
            continue
        for path, ref in _list_refs(schema):
            if ref not in by_code:
                r.error(f"{attr}.attribute_schema {path}: x-list-ref names {ref}, not a list in this pack")
        for v in values:
            vid = v.get("value_id")
            for err in _attribute_errors(schema, v.get("attributes") or {}):
                r.error(f"{attr}.{vid} attributes {err}")
            for ref, used in _ref_values(schema, v.get("attributes") or {}).items():
                target = by_code.get(ref)
                if target is None:
                    continue
                unknown = used - {t.get("value_code") or t.get("value_id") for t in target.get("values") or []}
                if unknown:
                    r.error(f"{attr}.{vid} refers to {ref} code(s) the list lacks: {sorted(unknown)}")


# sample field -> the code list it must draw from
SAMPLE_CODED = {
    "individuals": {
        "gender": "GENDER", "relationship_to_head": "RELATIONSHIP_TO_HEAD",
        "marital_status": "MARITAL_STATUS", "education_level": "EDUCATION_LEVEL",
        "employment_status": "EMPLOYMENT_STATUS", "disability_status": "DISABILITY_STATUS",
    },
    "households": {
        "headship_type": "HEADSHIP_TYPE", "dwelling_type": "DWELLING_TYPE",
        "tenure_status": "TENURE_STATUS", "water_source_type": "WATER_SOURCE_TYPE",
        "sanitation_type": "SANITATION_TYPE", "lighting_source": "LIGHTING_SOURCE",
        "cooking_fuel_type": "COOKING_FUEL_TYPE",
    },
}


def check_address(pack_dir, r):
    """The address contract: what is written BELOW the lowest level in the pack.

    Returns (declared ids, required ids), or (None, None) when the pack declares
    no address shape — which is allowed, and simply means its samples carry no
    address parts either.

    The rule this enforces is that the two halves never overlap. The P-code says
    which unit; address.json says what is written inside it. An admin name stored
    as address text is the same value in a second place, and it is the copy that
    goes stale when a woreda is renamed or resplit.
    """
    path = os.path.join(pack_dir, "address.json")
    if not os.path.exists(path):
        return None, None
    doc = read_json(path)

    subs = doc.get("sub_levels") or []
    ids = [s.get("id") for s in subs]
    declared = set(ids)
    if len(declared) != len(ids):
        r.error("address.json declares a duplicate sub_level id")
    for s in subs:
        if not s.get("id") or not s.get("label"):
            r.error(f"address sub_level {s!r} needs both an id and a label")
    required = {s["id"] for s in subs if s.get("required") and s.get("id")}

    # A line referring to a part nobody declares renders as a literal
    # "{street}" in whatever the registry stores — visible only once it is data.
    for line in doc.get("lines") or []:
        for token in re.findall(r"\{([^{}]*)\}", line):
            if token not in declared:
                r.error(f"address line {line!r} uses {{{token}}}, which is not "
                        f"a declared sub_level")

    # levels.json already names every administrative level. Re-declaring one as
    # an address part is exactly the duplication this file exists to prevent.
    levels = read_json(os.path.join(pack_dir, "levels.json"))
    admin = {lv.get("level_mnemonic") for lv in levels}
    for dup in sorted(declared & admin):
        r.error(f"address.json declares '{dup}' as a sub_level, but it is "
                f"already an administrative level — the P-code identifies it")

    return declared, required


# The year the packs' `age` values were written against. An age in a pack is a
# snapshot, not a live figure — every one of them drifts by a year each January —
# so the check below allows a year of slack rather than pretending otherwise.
REFERENCE_YEAR = 2026


def check_samples(pack_dir, r, geo_ids):
    """Sample people must be placeable and their coded values must be real.

    A sample record is the one thing in the pack a human actually reads, so an
    incoherent one is visible in a way a bad boundary is not — today's shared
    fixture has American names in Swahili-named villages. These checks stop the
    quieter version: a value that no longer exists in its list, or a household
    sitting on a P-code the pack does not contain.
    """
    d = os.path.join(pack_dir, "samples")
    if not os.path.isdir(d):
        return
    lists = {}
    cl = os.path.join(pack_dir, "codelists")
    if os.path.isdir(cl):
        for fn in os.listdir(cl):
            if fn.endswith(".json"):
                doc = read_json(os.path.join(cl, fn))
                lists[doc.get("attribute_id")] = {v.get("value_id") for v in doc.get("values", [])}

    data = {}
    for kind in ("individuals", "households"):
        path = os.path.join(d, f"{kind}.json")
        if not os.path.exists(path):
            r.error(f"samples/ exists but {kind}.json is missing")
            return
        data[kind] = read_json(path)

    for kind, fields in SAMPLE_CODED.items():
        for rec in data[kind]:
            for field, attr in fields.items():
                val = rec.get(field)
                if val is None or attr not in lists:
                    continue
                if val not in lists[attr]:
                    r.error(f"sample {kind[:-1]} {rec.get(kind[:-1] + '_id')} has "
                            f"{field}={val!r}, which is not a value of {attr}")
            # A birth date that disagrees with its own birth_year, or with age,
            # is the kind of incoherence nobody spots by reading: both fields
            # look plausible alone. Registries seed from ONE of them depending on
            # their vintage, so a pack that disagrees with itself produces two
            # different people from the same record.
            if kind == "individuals":
                bd, by = rec.get("birth_date"), rec.get("birth_year")
                ident = rec.get("individual_id")
                if bd:
                    try:
                        y, m, dd = (int(x) for x in str(bd).split("-"))
                        datetime.date(y, m, dd)
                    except (ValueError, TypeError):
                        r.error(f"sample individual {ident} has birth_date={bd!r}, "
                                f"which is not a valid ISO date")
                    else:
                        if by is not None and y != by:
                            r.error(f"sample individual {ident} has birth_date={bd} "
                                    f"but birth_year={by}")
                        age = rec.get("age")
                        if age is not None:
                            implied = REFERENCE_YEAR - y
                            if abs(implied - age) > 1:
                                r.error(f"sample individual {ident} has age={age} "
                                        f"but birth_date={bd} implies about "
                                        f"{implied}")

            g = rec.get("geo_pcode")
            if g and g not in geo_ids:
                r.error(f"sample {kind[:-1]} {rec.get(kind[:-1] + '_id')} sits on "
                        f"{g}, which is not a unit in this pack")

    declared, required = check_address(pack_dir, r)
    for kind in ("individuals", "households"):
        for rec in data[kind]:
            rid = rec.get(kind[:-1] + "_id")
            # A flat address string is the P-code's ancestry copied out as text.
            # It was in this pack once ("Tahtay Adiyabo, North Western, Tigray")
            # and it is the copy that rots, so it stays out.
            if isinstance(rec.get("address"), str):
                r.error(f"sample {kind[:-1]} {rid} carries a flat 'address' "
                        f"string — put what is below the lowest level in "
                        f"address_parts, and let geo_pcode carry the rest")
            parts = rec.get("address_parts")
            if not parts:
                if required:
                    r.error(f"sample {kind[:-1]} {rid} has no address_parts, but "
                            f"address.json requires {sorted(required)}")
                continue
            if declared is None:
                r.error(f"sample {kind[:-1]} {rid} has address_parts but the "
                        f"pack declares no address.json to interpret them")
                continue
            for k in sorted(set(parts) - declared):
                r.error(f"sample {kind[:-1]} {rid} has address part {k!r}, "
                        f"which address.json does not declare")
            for k in sorted(required - set(parts)):
                r.error(f"sample {kind[:-1]} {rid} is missing required address "
                        f"part {k!r}")

    hh_ids = {h.get("household_id") for h in data["households"]}
    ind_by_id = {i.get("individual_id"): i for i in data["individuals"]}
    for i in data["individuals"]:
        if i.get("household_id") not in hh_ids:
            r.error(f"individual {i.get('individual_id')} belongs to unknown "
                    f"household {i.get('household_id')}")
    for h in data["households"]:
        head = ind_by_id.get(h.get("head_individual_id"))
        if not head:
            r.error(f"household {h.get('household_id')} names an unknown head")
            continue
        if head.get("household_id") != h.get("household_id"):
            r.error(f"household {h.get('household_id')} is headed by someone in "
                    f"another household")
        if head.get("relationship_to_head") != "SELF":
            r.error(f"the head of {h.get('household_id')} is recorded as "
                    f"{head.get('relationship_to_head')}, not SELF")
        # headship_type must agree with the head's gender, or every gender
        # breakdown of headship disagrees with the underlying people
        want = {"MALE": "MALE_HEADED", "FEMALE": "FEMALE_HEADED"}.get(head.get("gender"))
        if want and h.get("headship_type") not in (want, "CHILD_HEADED",
                                                   "ELDERLY_HEADED", "DISABLED_HEADED"):
            r.error(f"household {h.get('household_id')} is {h.get('headship_type')} "
                    f"but its head is {head.get('gender')}")


def check_sample_locations(pack_dir, r, geoms):
    """A sample's coordinates must fall inside the unit its P-code names.

    Two ways of saying where someone lives, so they can disagree — and a map
    built from the coordinates then contradicts every table built from the
    P-code, with neither one visibly wrong on its own.
    """
    d = os.path.join(pack_dir, "samples")
    if not os.path.isdir(d):
        return
    for kind in ("individuals", "households"):
        path = os.path.join(d, f"{kind}.json")
        if not os.path.exists(path):
            continue
        for rec in read_json(path):
            lat, lon = rec.get("latitude"), rec.get("longitude")
            pcode = rec.get("geo_pcode")
            rid = rec.get(kind[:-1] + "_id")
            if lat is None and lon is None:
                continue
            if lat is None or lon is None:
                r.error(f"sample {kind[:-1]} {rid} has only one of "
                        f"latitude/longitude")
                continue
            geom = geoms.get(pcode)
            if not geom:
                continue
            if not point_in_polygon((lon, lat), geom):
                r.error(f"sample {kind[:-1]} {rid} is at ({lat}, {lon}), which "
                        f"is outside {pcode} — its coordinates and its P-code "
                        f"disagree about where it is")


def validate(pack_dir):
    r = Report(os.path.basename(pack_dir.rstrip("/")))

    for f in ("levels.json", "values.json", "manifest.json"):
        if not os.path.exists(os.path.join(pack_dir, f)):
            r.error(f"missing {f}")
    if not r.ok():
        return r

    levels = read_json(os.path.join(pack_dir, "levels.json"))
    values = read_json(os.path.join(pack_dir, "values.json"))
    manifest = read_json(os.path.join(pack_dir, "manifest.json"))

    # -- manifest ----------------------------------------------------------
    for k in REQUIRED_MANIFEST:
        if k not in manifest:
            r.error(f"manifest is missing '{k}'")
    # load_geo_pack.py stamps every seeded row with this, so a pack without one
    # produces rows whose provenance cannot be told apart from a later reseed.
    if not any(k in manifest for k in ("version", "upstream_last_modified", "fetched_on")):
        r.error("manifest carries no version, upstream_last_modified or fetched_on")
    if manifest.get("synthetic") and "attribution" in str(manifest.get("license", "")).lower():
        r.warn("pack is marked synthetic but its license mentions attribution")

    # -- levels: one root, single chain, no cycles --------------------------
    by_id = {lv["level_id"]: lv for lv in levels}
    if len(by_id) != len(levels):
        r.error("duplicate level_id in levels.json")
    roots = [lv for lv in levels if not lv["parent_level_id"]]
    if len(roots) != 1:
        r.error(f"expected exactly 1 root level, found {len(roots)}")
    for lv in levels:
        p = lv["parent_level_id"]
        if p and p not in by_id:
            r.error(f"level {lv['level_id']} references unknown parent {p}")

    order, seen = [], set()
    cur = roots[0]["level_id"] if roots else None
    while cur:
        if cur in seen:
            r.error("cycle in the level hierarchy")
            break
        seen.add(cur)
        order.append(cur)
        kids = [lv["level_id"] for lv in levels if lv["parent_level_id"] == cur]
        if len(kids) > 1:
            r.error(f"level {cur} has {len(kids)} child levels — the hierarchy "
                    f"must be a single chain, not a tree")
            break
        cur = kids[0] if kids else None
    if len(order) != len(levels):
        r.error(f"{len(levels) - len(order)} level(s) are not reachable from the root")

    depth_of = {lid: d for d, lid in enumerate(order)}
    mnemonic = {lv["level_id"]: lv["level_mnemonic"] for lv in levels}
    if manifest.get("levels") and manifest["levels"] != [mnemonic[l] for l in order]:
        r.error(f"manifest levels {manifest.get('levels')} do not match levels.json "
                f"{[mnemonic[l] for l in order]}")

    # -- values: identity, parentage, P-code hierarchy ----------------------
    vals = {}
    for v in values:
        vid = v.get("level_value_id")
        if vid in vals:
            r.error(f"duplicate level_value_id {vid}")
        vals[vid] = v
        if v.get("level_id") not in by_id:
            r.error(f"unit {vid} is at unknown level {v.get('level_id')}")
        if v.get("pcode") and v["pcode"] != vid:
            r.error(f"unit {vid} has pcode {v['pcode']} — the pack contract is "
                    f"that the P-code IS the level_value_id")

    n_roots = 0
    for v in values:
        vid, parent = v.get("level_value_id"), v.get("parent_level_value_id")
        d = depth_of.get(v.get("level_id"))
        if parent is None:
            n_roots += 1
            if d not in (0, None):
                r.error(f"unit {vid} at depth {d} has no parent")
            continue
        if parent not in vals:
            r.error(f"unit {vid} references unknown parent {parent}")
            continue
        pd = depth_of.get(vals[parent].get("level_id"))
        if d is not None and pd is not None and pd != d - 1:
            r.error(f"unit {vid} (depth {d}) has a parent at depth {pd}")
        if not str(vid).startswith(str(parent)):
            r.error(f"P-code {vid} does not extend its parent {parent}")
    if n_roots != 1:
        r.error(f"expected exactly 1 root unit, found {n_roots}")

    counts = {}
    for v in values:
        counts[mnemonic.get(v.get("level_id"), "?")] = \
            counts.get(mnemonic.get(v.get("level_id"), "?"), 0) + 1
    if manifest.get("unit_counts") and manifest["unit_counts"] != counts:
        r.error(f"manifest unit_counts {manifest.get('unit_counts')} "
                f"do not match values.json {counts}")

    check_codelists(pack_dir, r)
    check_samples(pack_dir, r, set(vals))

    # -- boundaries --------------------------------------------------------
    bdir = os.path.join(pack_dir, "boundaries")
    if not os.path.isdir(bdir):
        r.warn("no boundaries/ directory — the pack seeds a hierarchy but draws no map")
        return r

    # A pack that declares boundary_coverage has polygons for only some units
    # (ETH-OAN: none for its kebeles and its new units). The declared counts
    # must then be exact, and a unit without a polygon is not an error.
    coverage = manifest.get("boundary_coverage")
    geoms = {}
    for lid in order:
        m = mnemonic[lid]
        path = os.path.join(bdir, f"{m}.geojson")
        if not os.path.exists(path):
            if coverage is not None and not coverage.get(m):
                continue
            r.error(f"no boundaries/{m}.geojson for level {m}")
            continue
        fc = read_json(path)
        level_units = {v["level_value_id"] for v in values if v.get("level_id") == lid}
        seen_pcodes = set()
        for f in fc.get("features", []):
            pr = f.get("properties") or {}
            pcode = pr.get("pcode")
            geom = f.get("geometry") or {}
            if not pcode:
                r.error(f"{m}.geojson has a feature with no pcode")
                continue
            if pcode in seen_pcodes:
                r.error(f"{m}.geojson has two features for {pcode}")
            seen_pcodes.add(pcode)
            if pcode not in vals:
                r.error(f"{m}.geojson draws {pcode}, which is not in values.json")
                continue
            # The map surface reads `area_name`, never `name` — a property
            # called `name` collides with the read-only Function.name on
            # Evidence's callable input proxy and kills the map outright.
            if "name" in pr:
                r.error(f"{m}.geojson feature {pcode} carries a 'name' property; "
                        f"use 'area_name'")
            if not pr.get("area_name"):
                r.warn(f"{m}.geojson feature {pcode} has no area_name")
            if geom.get("type") not in ("Polygon", "MultiPolygon"):
                r.error(f"{pcode} has geometry type {geom.get('type')}")
                continue
            for ring in rings(geom):
                if len(ring) < 4:
                    r.error(f"{pcode} has a ring with {len(ring)} points")
                elif ring[0] != ring[-1]:
                    r.error(f"{pcode} has an unclosed ring")
            for ring in outer_rings(geom):
                if not ring_is_simple(ring):
                    r.error(f"{pcode} has a self-intersecting outer ring")
            geoms[pcode] = geom

        missing = level_units - seen_pcodes
        if coverage is not None:
            if coverage.get(m) != len(seen_pcodes):
                r.error(f"manifest boundary_coverage says {coverage.get(m)} {m} polygon(s), "
                        f"{m}.geojson has {len(seen_pcodes)}")
        elif missing:
            r.error(f"{len(missing)} {m} unit(s) have no geometry, e.g. "
                    f"{sorted(missing)[:3]}")

    check_sample_locations(pack_dir, r, geoms)

    # -- nesting and coverage ---------------------------------------------
    # The invariant drill-down depends on: a child lies inside its parent, and a
    # parent's children account for its area.
    escaped, checked = [], 0
    for v in values:
        parent = v.get("parent_level_value_id")
        vid = v["level_value_id"]
        if not parent or vid not in geoms or parent not in geoms:
            continue
        pts = sample_interior(geoms[vid])
        if not pts:
            continue
        checked += 1
        inside = sum(1 for p in pts if point_in_polygon(p, geoms[parent]))
        if inside < NESTING_THRESHOLD * len(pts):
            escaped.append((vid, parent, inside, len(pts)))
    if escaped:
        pct = 100 * len(escaped) / checked
        detail = ", ".join(f"{c} outside {p} ({i}/{n} inside)"
                           for c, p, i, n in escaped[:4])
        msg = f"{len(escaped)}/{checked} units ({pct:.1f}%) are not inside their parent: {detail}"
        # A handful of strays is simplification noise on a real pack; a large
        # share means the generator's nesting is broken.
        (r.error if pct > 2.0 else r.warn)(msg)

    kids_area = {}
    for v in values:
        parent = v.get("parent_level_value_id")
        if parent and v["level_value_id"] in geoms:
            kids_area[parent] = kids_area.get(parent, 0.0) + polygon_area(geoms[v["level_value_id"]])
    bad = []
    for parent, area in kids_area.items():
        if parent not in geoms:
            continue
        pa = polygon_area(geoms[parent])
        if pa <= 0:
            r.error(f"{parent} has zero area")
            continue
        ratio = area / pa
        if abs(ratio - 1.0) > AREA_TOLERANCE:
            bad.append((parent, ratio))
    if bad:
        worst = sorted(bad, key=lambda t: abs(t[1] - 1))[-3:]
        detail = ", ".join(f"{p} children cover {100 * rt:.0f}%" for p, rt in worst)
        pct = 100 * len(bad) / max(1, len(kids_area))
        msg = (f"{len(bad)}/{len(kids_area)} parents are not cleanly partitioned "
               f"by their children ({pct:.0f}%): {detail}")
        (r.error if pct > 25.0 else r.warn)(msg)

    return r


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("packs", nargs="+", help="pack directories to validate")
    p.add_argument("--strict", action="store_true",
                   help="treat warnings as failures")
    args = p.parse_args()

    failed = False
    for pack in args.packs:
        r = validate(pack)
        for w in r.warnings:
            print(f"[{r.name}] WARN  {w}")
        for e in r.errors:
            print(f"[{r.name}] ERROR {e}")
        bad = r.errors or (args.strict and r.warnings)
        print(f"[{r.name}] {'FAIL' if bad else 'ok'}"
              f"  ({len(r.errors)} errors, {len(r.warnings)} warnings)")
        failed = failed or bool(bad)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
