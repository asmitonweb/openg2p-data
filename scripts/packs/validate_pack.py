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


# farms.json: the farm facts of the sample people, for registries that hold
# farmers (land, crops, livestock, farm inputs, cooperative membership). Field
# -> code list, per record kind; a list the pack does not ship is not checked.
# Values are value_codes: a registry stores and validates the code, which for
# some lists differs from the value_id (LBCOND_GOOD has the code GOOD).
FARM_CODED = {
    "farmer": {"language_spoken": "LANGUAGE", "local_language": "LANGUAGE",
               "farming_type": "FARMING_TYPE", "income_sources": "SOURCE_OF_INCOME",
               "identity_document_type": "IDENTITY_DOCUMENT_TYPE",
               "phone_type": "PHONE_NUMBER_TYPE"},
    "lands": {"land_ownership_type": "LAND_OWNERSHIP_TYPE", "unit": "LAND_SIZE_UNIT",
              "soil_fertility": "SOIL_FERTILITY", "current_land_use": "CURRENT_LAND_USE",
              "farming_type": "FARMING_TYPE", "means_of_acquisition": "MEANS_OF_ACQUISITION"},
    "crops": {"commodity": "CROP_COMMODITY", "season": "CROP_SEASON",
              "end_use": "CROP_END_USE", "water_sources": "WATER_SOURCE"},
    "livestock": {"livestock_type": "LIVESTOCK_TYPE", "breed": "LIVESTOCK_BREED",
                  "livestock_system": "LIVESTOCK_SYSTEM",
                  "body_condition": "LIVESTOCK_BODY_CONDITION",
                  "production_type": "LIVESTOCK_PRODUCTION_TYPE",
                  "water_sources": "WATER_SOURCE"},
    "farm_inputs": {"water_sources": "WATER_SOURCE", "machinery_types": "FARM_MACHINERY",
                    "finance_access_types": "FINANCE_ACCESS_TYPE",
                    "irrigation_type": "IRRIGATION_METHOD"},
    "membership": {"farmer_cluster_role": "FARMER_CLUSTER_ROLE",
                   "primary_commodity": "CROP_COMMODITY"},
}


def check_farm_samples(pack_dir, r):
    """Farm facts must belong to a sample person and use the pack's own codes.

    Crops, livestock and farm inputs sit on one of the person's lands, named by
    land_ref, as a registry links them; a dangling land_ref has nowhere to go.
    """
    path = os.path.join(pack_dir, "samples", "farms.json")
    if not os.path.exists(path):
        return
    lists = {}
    for sub in ("codelists", os.path.join("domains", "agriculture")):
        dd = os.path.join(pack_dir, sub)
        if os.path.isdir(dd):
            for fn in os.listdir(dd):
                if fn.endswith(".json"):
                    doc = read_json(os.path.join(dd, fn))
                    lists[doc.get("attribute_id")] = {v.get("value_code") for v in doc.get("values", [])}
    people = {i.get("individual_id") for i in read_json(os.path.join(pack_dir, "samples", "individuals.json"))}
    seen = set()
    for farm in read_json(path):
        ident = farm.get("individual_id")
        if ident not in people:
            r.error(f"farms.json names unknown individual {ident}")
        if ident in seen:
            r.error(f"farms.json has more than one record for {ident}")
        seen.add(ident)
        land_refs = [land.get("land_ref") for land in farm.get("lands", [])]
        if len(land_refs) != len(set(land_refs)):
            r.error(f"farm of {ident} repeats a land_ref")
        for kind, fields in FARM_CODED.items():
            records = farm.get(kind) or []
            for rec in records if isinstance(records, list) else [records]:
                for field, attr in fields.items():
                    if attr not in lists:
                        continue
                    for val in rec.get(field) if isinstance(rec.get(field), list) else [rec.get(field)]:
                        if val is not None and val not in lists[attr]:
                            r.error(f"farm of {ident}: {kind}.{field}={val!r} is not a value of {attr}")
                if kind in ("crops", "livestock", "farm_inputs") and rec.get("land_ref") not in land_refs:
                    r.error(f"farm of {ident}: {kind} row on unknown land_ref {rec.get('land_ref')!r}")
                if kind == "lands" and not (isinstance(rec.get("land_size"), (int, float)) and rec["land_size"] > 0):
                    r.error(f"farm of {ident}: land {rec.get('land_ref')} needs a positive land_size")
    missing = people - seen
    if missing:
        r.warn(f"farms.json has no record for {len(missing)} sample people")


# farmer_ids.json, crop_sown.json and livestock.json: what the Crop Sown and the
# Livestock registries seed for the sample people. A loader copies these values
# into intake forms and invents none, so they are checked as a registry would
# check them: against the pack's lists, against the person's own farms.json, and
# against the rules the registries' domain services apply (a harvest follows a
# sowing, a sown area fits the plot, a birth belongs to a female animal).

# The day the livestock samples are written against: an event is never later.
SAMPLE_AS_OF = datetime.date(2026, 10, 29)
# Animals in the samples are registered by the start of the last quarter.
LAST_REGISTRATION = datetime.date(2026, 10, 1)
# Ethiopia, with a margin: a point outside it is a swapped latitude and longitude.
ETHIOPIA_BOUNDS = (3.0, 15.5, 32.5, 48.5)  # lat min, lat max, lon min, lon max

FARMER_ID = re.compile(r"^FR-[0-9]{12}$")
MOBILE = re.compile(r"^0[79][0-9]{8}$")
# letters, spaces and apostrophes: what the registries accept as a person's name
PERSON_NAME = re.compile(r"^[A-Za-z]+(?:[ '][A-Za-z]+)*$")
EAR_TAG = re.compile(r"^ET[0-9]{10}$")

# farms.json names a species by its LIVESTOCK_TYPE code; the registries use
# LIVESTOCK_SPECIES. This is the one place the two are joined.
LIVESTOCK_SPECIES_OF_TYPE = {
    "LSTK_CATTLE": "CATTLE", "LSTK_GOAT": "GOATS", "LSTK_SHEEP": "SHEEP", "LSTK_CHICKEN": "POULTRY",
    "LSTK_DONKEY": "EQUINES", "LSTK_HORSE": "EQUINES", "LSTK_MULE": "EQUINES", "LSTK_CAMEL": "CAMELS",
}

# Options the registries' forms fix themselves (static select widgets).
CROP_OPTIONS = {
    "cropping_system": {"MONO_CROPPING", "INTER_CROPPING"},
    "seed_class": {"LOCAL", "IMPROVED"}, "actual_seed_class": {"LOCAL", "IMPROVED"},
    "seed_source": {"GOVERNMENT / WOREDS AGRICULTURE", "AGRICULTURE COOPERATIVE / UNION",
                    "PRIVATE SEED ENTERPRISE", "FARMER-TO-FARMER EXCHANGE / SAVED"},
    "agro_ecological_zone": {"KOLLA", "WOINA_DEGA", "DEGA", "WURCH", "BEREHA"},
    "fpc_type": {"GREEN"},
    "crop_maturity_status": {"READY_FOR_HARVEST", "NOT_YET_READY_FOR_HARVEST"},
    "cluster_crop_maturity_status": {"READY_FOR_HARVEST", "NOT_YET_READY_FOR_HARVEST"},
    "pesticide_method": {"chemical_spray", "hand_picking", "early_harvesting"},
    "pesticide_type": {"insecticide", "fungicide", "herbicide", "rodenticide", "bactericide", "nematicide",
                       "acaricide", "molluscicide", "termiticide", "avicide", "piscicide", "algicide", "virucide"},
    "weed_control_method": {"chemical_spray", "hand_picking", "early_harvesting"},
    "weedicide_type": {"pre_emergent", "post_emergent", "systemic", "contact", "graminicide", "broadleaf",
                       "sedge", "aquatic", "foliar", "soil"},
    "disease_control_method": {"chemical_spray", "hand_picking", "early_harvesting"},
    "fungicide_type": {"fungicide", "bactericide"},
    "growth_stage": {"EMERGENCE", "VEGETATIVE", "FLOWERING", "MATURITY"},
    "severity_level": {"LOW", "MEDIUM", "HIGH"},
}
CROP_OPTIONS["actual_seed_source"] = CROP_OPTIONS["seed_source"]
CROP_BOOLEANS = {"is_plot_not_registered", "has_pest_disease", "cluster_has_pest_disease"}

# section -> field -> code list. A list the pack does not ship is not checked,
# but then the field must be left out (see CROP_LEFT_OUT).
CROP_CODED = {
    "planning": {"soil_fertility_type": "SOIL_FERTILITY", "season": "CROP_SEASON", "commodity": "CROP_COMMODITY",
                 "crop_category": "CROP_CATEGORY", "seed_variety": "SEED_VARIETY",
                 "planned_fertilizer_type": "FERTILIZER_TYPE", "water_source": "WATER_SOURCE"},
    "clusters": {"season": "CROP_SEASON", "primary_commodity": "CROP_COMMODITY",
                 "secondary_commodity": "CROP_COMMODITY", "water_source": "WATER_SOURCE"},
    "cultivation": {"season": "CROP_SEASON", "commodity": "CROP_COMMODITY", "crop_category": "CROP_CATEGORY",
                    "seed_variety": "SEED_VARIETY", "actual_fertilizer_type": "FERTILIZER_TYPE",
                    "water_source": "WATER_SOURCE"},
    "cultivation_clusters": {"season": "CROP_SEASON", "primary_commodity": "CROP_COMMODITY",
                             "secondary_commodity": "CROP_COMMODITY", "water_source": "WATER_SOURCE"},
    "sowing": {"cluster_season": "CROP_SEASON", "season": "CROP_SEASON", "commodity": "CROP_COMMODITY",
               "fertilizer_type": "FERTILIZER_TYPE"},
    "infestations": {"commodity": "CROP_COMMODITY", "infestation_type": "INFESTATION_TYPE"},
    "harvest": {"commodity": "CROP_COMMODITY"},
}
# Fields of the registries' forms whose list is not in the pack (at the time of
# writing): the samples leave them out rather than guess a code.
CROP_LEFT_OUT = {
    "plot_category": "PLOT_CATEGORY", "ownership_type": "OWNERSHIP_TYPE", "crop_variety": "CROP_VARIETY",
    "water_source_method": "WATER_SOURCE_METHOD", "water_source_frequency": "WATER_SOURCE_FREQUENCY",
    "land_prep_method": "LAND_PREP_METHOD", "cultivation_type": "MACHINERY", "pest_type": "PEST",
    "weed_type": "WEED", "disease_type": "CROP_DISEASE",
}
CROP_SECTIONS = ("planning", "clusters", "cultivation", "cultivation_clusters", "sowing", "infestations", "harvest")
CROP_DATES = {
    "planning": ("start_gc", "end_gc", "planned_date"), "clusters": ("start_gc", "end_gc", "establishing_date"),
    "cultivation": ("start_gc", "end_gc", "actual_cultivation_date"),
    "cultivation_clusters": ("start_gc", "end_gc", "establishing_date"), "sowing": ("sowing_date",),
    "infestations": ("observation_date",), "harvest": ("harvest_date", "cluster_harvest_date"),
}
# Free text a person reads: no underscores or hyphens (codes keep theirs).
CROP_FREE_TEXT = (
    "land_id", "cluster_id", "cluster_name", "infestation_id", "pest_name", "pesticide_name", "pesticide_frequency",
    "weed_name", "weedicide_name", "weedicide_frequency", "disease_name", "fungicide_name",
    "disease_frequency_of_application", "nutrient_name", "shock_event_name", "local_name", "scientific_name",
)
CROP_NAMES = ("da_name", "supervisor_name")
CROP_MOBILES = ("da_mobile_number", "supervisor_mobile_number")

LIVESTOCK_OPTIONS = {
    "animals": {"gender": {"MALE", "FEMALE", "MIXED"}, "health_status": {"HEALTHY", "SICK", "QUARANTINED", "DECEASED"},
                "vaccination_status": {"UP_TO_DATE", "OVERDUE", "NONE"}},
    "health_events": {"event_type": {"DISEASE", "INJURY", "TREATMENT", "RECOVERY"},
                      "location": {"HOME", "VETERINARY", "MARKET", "FIELD", "QUARANTINE_CENTER", "OTHER"}},
    "vaccinations": {},
    "vital_events": {"event_type": {"BIRTH", "MORTALITY", "DISEASE"},
                     "cause": {"DISEASE", "ACCIDENT", "PREDATION", "SLAUGHTER", "UNKNOWN"},
                     "location": {"HOME", "VETERINARY", "MARKET", "FIELD", "QUARANTINE_CENTER", "OTHER"},
                     "offspring_gender": {"MALE", "FEMALE", "MIXED"}},
    "breeding": {"event_type": {"NATURAL", "AI"}, "outcome": {"PENDING", "SUCCESSFUL", "FAILED"},
                 "location": {"HOME", "VETERINARY", "MARKET", "FIELD", "QUARANTINE_CENTER", "OTHER"}},
}
LIVESTOCK_CODED = {
    "animals": {"species": "LIVESTOCK_SPECIES", "breed": "LIVESTOCK_BREED"},
    "health_events": {"species": "LIVESTOCK_SPECIES", "disease_type": "LIVESTOCK_DISEASE"},
    "vaccinations": {"species": "LIVESTOCK_SPECIES", "vaccine_type": "VACCINE_TYPE"},
    "vital_events": {"species": "LIVESTOCK_SPECIES", "disease_type": "LIVESTOCK_DISEASE"},
    "breeding": {"species": "LIVESTOCK_SPECIES"},
}
LIVESTOCK_SECTIONS = ("animals", "health_events", "vaccinations", "vital_events", "breeding")
LIVESTOCK_DATES = {
    "animals": ("date_of_birth", "registration_date"), "health_events": ("date_onset", "date_resolution"),
    "vaccinations": ("vaccination_date", "next_due_date"), "vital_events": ("event_date", "date_onset", "date_resolution"),
    "breeding": ("breeding_date", "expected_calving_date", "pregnancy_confirmation_date"),
}
LIVESTOCK_FREE_TEXT = (
    "secondary_identifier", "batch_number", "treatment", "location_details", "notes", "sire_or_semen_id",
    "ai_technique", "semen_batch_number", "reporting_officer", "administered_by",
)
LIVESTOCK_NAMES = ("veterinarian_name", "ai_technician_name", "administered_by", "reporting_officer")
# Two breedings of the same kind within this many days are one cycle logged twice.
BREEDING_CYCLE_DAYS = 21


def _agriculture_lists(pack_dir):
    """{list id: {value_code: value}} of the pack's code lists and agriculture lists."""
    lists = {}
    for sub in ("codelists", os.path.join("domains", "agriculture")):
        dd = os.path.join(pack_dir, sub)
        if os.path.isdir(dd):
            for fn in os.listdir(dd):
                if fn.endswith(".json"):
                    doc = read_json(os.path.join(dd, fn))
                    lists[doc.get("attribute_id")] = {v.get("value_code"): v for v in doc.get("values", [])}
    return lists


def _iso(value):
    """A real YYYY-MM-DD date, or None."""
    if not isinstance(value, str) or not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def check_farmer_ids(pack_dir, r):
    """Every sample person has a Farmer ID, as the Farmer Registry would issue it.

    The Crop Sown and Livestock registries identify a farmer by Farmer ID, so a
    sample person without one cannot be seeded there. The ids follow the people's
    numbers (ETH-IND-0007 is FR-100000000007), so a reader can tell whose it is.
    """
    path = os.path.join(pack_dir, "samples", "farmer_ids.json")
    if not os.path.exists(path):
        return
    people = {i.get("individual_id") for i in read_json(os.path.join(pack_dir, "samples", "individuals.json"))}
    seen, farmer_ids = set(), set()
    for rec in read_json(path):
        ident, fid = rec.get("individual_id"), rec.get("farmer_id")
        if ident not in people:
            r.error(f"farmer_ids.json names unknown individual {ident}")
        if ident in seen:
            r.error(f"farmer_ids.json has more than one id for {ident}")
        seen.add(ident)
        if not isinstance(fid, str) or not FARMER_ID.match(fid):
            r.error(f"farmer_ids.json: {ident} has farmer_id={fid!r}, which is not FR- and twelve digits")
            continue
        if fid in farmer_ids:
            r.error(f"farmer_ids.json: farmer_id {fid} is used by more than one person")
        farmer_ids.add(fid)
        number = str(ident).rsplit("-", 1)[-1]
        if number.isdigit() and fid != f"FR-1000000000{int(number) % 100:02d}":
            r.error(f"farmer_ids.json: {ident} has {fid}, expected FR-1000000000{int(number) % 100:02d}")
    if people - seen:
        r.error(f"farmer_ids.json has no id for {sorted(people - seen)}")


def _check_free_text(r, label, row, fields):
    for field in fields:
        val = row.get(field)
        if isinstance(val, str) and ("_" in val or "-" in val):
            r.error(f"{label}: {field}={val!r} has an underscore or hyphen; free text reads like a person wrote it")


def _check_staff(r, label, row):
    for field in CROP_NAMES:
        if row.get(field) is not None and not (isinstance(row[field], str) and PERSON_NAME.match(row[field])):
            r.error(f"{label}: {field}={row[field]!r} is not a person's name (letters and spaces)")
    for field in CROP_MOBILES:
        if row.get(field) is not None and not (isinstance(row[field], str) and MOBILE.match(row[field])):
            r.error(f"{label}: {field}={row[field]!r} is not an Ethiopian mobile number (09XXXXXXXX)")


def _check_coded(r, label, row, fields, lists):
    for field, attr in fields.items():
        val = row.get(field)
        if val is None:
            continue
        if attr not in lists:
            r.error(f"{label}: {field} is set but the pack has no {attr} list; leave the field out")
        elif val not in lists[attr]:
            r.error(f"{label}: {field}={val!r} is not a value of {attr}")


def _check_location(r, label, loc):
    if not isinstance(loc, dict) or _number(loc.get("latitude")) is None or _number(loc.get("longitude")) is None:
        r.error(f"{label}: location needs a numeric latitude and longitude")
        return
    lat, lon = loc["latitude"], loc["longitude"]
    lat_min, lat_max, lon_min, lon_max = ETHIOPIA_BOUNDS
    if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        r.error(f"{label}: location ({lat}, {lon}) is not in Ethiopia")


def check_crop_sown_samples(pack_dir, r):
    """Crop Sown samples: the same people, lands, crops and season as farms.json.

    One record per farmer and crop year, so one production season per person:
    a person whose farms.json crops sit in several seasons is seeded with one of
    them. Every line then follows the stages a farmer reports in order (plan,
    cultivate, sow, observe, harvest), on a land the person owns, within the
    land's size.
    """
    path = os.path.join(pack_dir, "samples", "crop_sown.json")
    if not os.path.exists(path):
        return
    if not os.path.exists(os.path.join(pack_dir, "samples", "farms.json")):
        r.error("%s needs samples/farms.json" % os.path.basename(path))
        return
    samples = os.path.join(pack_dir, "samples")
    farms = {f.get("individual_id"): f for f in read_json(os.path.join(samples, "farms.json"))}
    lists = _agriculture_lists(pack_dir)
    seen = set()
    infestation_types, edge = set(), {"multi_land": 0, "inter": 0, "cluster_sowing": 0, "cluster_harvest": 0,
                                      "loss": 0, "planning_only": 0}
    for rec in read_json(path):
        ident = rec.get("individual_id")
        who = f"crop_sown.json {ident}"
        farm = farms.get(ident)
        if farm is None:
            r.error(f"{who}: no farms.json record for this person")
            continue
        if ident in seen:
            r.error(f"{who}: more than one record for this person")
        seen.add(ident)
        season = rec.get("production_season")
        if "CROP_SEASON" in lists and season not in lists["CROP_SEASON"]:
            r.error(f"{who}: production_season={season!r} is not a value of CROP_SEASON")
        lands = {land.get("land_ref"): land.get("land_size") for land in farm.get("lands", [])}
        farm_crop = {c.get("land_ref"): c for c in farm.get("crops", [])}
        if not any(c.get("season") == season for c in farm_crop.values()):
            r.error(f"{who}: none of the person's farms.json crops is in {season}")
        _check_location(r, who, rec.get("location"))
        for section in CROP_SECTIONS:
            if not isinstance(rec.get(section), list):
                r.error(f"{who}: {section} must be a list (empty when there are none)")
                rec[section] = []
        label_of, ref_of = {}, {}  # land_ref -> land_id and back
        rows = [(s, i, row) for s in CROP_SECTIONS for i, row in enumerate(rec[s])]
        for section, i, row in rows:
            label = f"{who} {section}[{i}]"
            ref, land_id = row.get("land_ref"), row.get("land_id")
            if ref not in lands:
                r.error(f"{label}: land_ref {ref!r} is not one of the person's lands in farms.json")
            if not isinstance(land_id, str) or not land_id.strip():
                r.error(f"{label}: land_id is required")
            else:
                if label_of.setdefault(ref, land_id) != land_id:
                    r.error(f"{label}: land_ref {ref} is called {land_id!r} here and {label_of[ref]!r} elsewhere")
                if ref_of.setdefault(land_id, ref) != ref:
                    r.error(f"{label}: land_id {land_id!r} stands for {ref} and {ref_of[land_id]}")
            _check_coded(r, label, row, CROP_CODED[section], lists)
            for field, allowed in CROP_OPTIONS.items():
                if row.get(field) is not None and row[field] not in allowed:
                    r.error(f"{label}: {field}={row[field]!r} is not an option of the form")
            for field in CROP_BOOLEANS & row.keys():
                if not isinstance(row[field], bool):
                    r.error(f"{label}: {field} must be true or false")
            for field, attr in CROP_LEFT_OUT.items():
                if field in row and attr not in lists:
                    r.error(f"{label}: {field} is set but the pack has no {attr} list; leave the field out")
            if section == "infestations" and "cluster_status" in row:
                r.error(f"{label}: cluster_status is a CLUSTER_STATUS list the pack lacks; leave it out")
            _check_free_text(r, label, row, CROP_FREE_TEXT)
            _check_staff(r, label, row)
            for field in CROP_DATES[section]:
                if row.get(field) is not None and _iso(row[field]) is None:
                    r.error(f"{label}: {field}={row[field]!r} is not an ISO date")
                elif _iso(row.get(field)) and field != "establishing_date" and _iso(row[field]).year != REFERENCE_YEAR:
                    r.error(f"{label}: {field}={row[field]} is not in {REFERENCE_YEAR}; the loader moves dates to the crop year")
            for field in ("planned_area", "actual_crop_area", "area_sown", "area_harvested", "cluster_area_sown",
                          "cluster_area_harvested", "qty_harvested", "cluster_qty_harvested"):
                if row.get(field) is not None and not (_number(row[field]) is not None and row[field] > 0):
                    r.error(f"{label}: {field}={row[field]!r} must be a positive number")
            # the season of the line is the season of the record
            for field in ("season", "cluster_season"):
                if row.get(field) is not None and row[field] != season:
                    r.error(f"{label}: {field}={row[field]} but the production season is {season}")
            if section in ("planning", "cultivation") and row.get("season") != season:
                r.error(f"{label}: season is required and must be the production season {season}")
            # the crop on the land is the crop of farms.json
            commodity = row.get("commodity") or row.get("primary_commodity")
            if commodity is not None and ref in farm_crop and commodity != farm_crop[ref].get("commodity"):
                r.error(f"{label}: {commodity} on {ref}, but farms.json has {farm_crop[ref].get('commodity')}")
            if section in ("planning", "cultivation") and not row.get("commodity"):
                r.error(f"{label}: commodity is required")

        # clusters named by sowing and harvest rows
        cluster_keys = {(c.get("land_ref"), c.get("cluster_name")) for c in rec["clusters"]}
        cultivation_cluster_keys = {(c.get("land_ref"), c.get("cluster_name")) for c in rec["cultivation_clusters"]}
        for section in ("clusters", "cultivation_clusters"):
            for i, c in enumerate(rec[section]):
                label = f"{who} {section}[{i}]"
                timad, area = _number(c.get("cluster_area_timad")), _number(c.get("cluster_area_hectare"))
                if not c.get("cluster_name"):
                    r.error(f"{label}: cluster_name is required")
                if timad is not None and area is not None and abs(timad * 0.25 - area) > 1e-6:
                    r.error(f"{label}: cluster_area_hectare={area} but {timad} timad is {timad * 0.25} ha")
                for field in ("cluster_plan", "collected_land"):
                    if _number(c.get(field)) is not None and area is not None and c[field] > area:
                        r.error(f"{label}: {field}={c[field]} exceeds cluster_area_hectare={area}")
                if _number(c.get("collected_by_combiner")) is not None and _number(c.get("cluster_plan")) is not None \
                        and c["collected_by_combiner"] > c["cluster_plan"]:
                    r.error(f"{label}: collected_by_combiner exceeds cluster_plan")
        independent = {}  # (land_ref, commodity) -> {stage: row}
        clustered = {}  # (land_ref, cluster_name) -> {stage: row}
        for section in ("sowing", "harvest"):
            for i, row in enumerate(rec[section]):
                label = f"{who} {section}[{i}]"
                status = row.get("cluster_status")
                if not (isinstance(status, list) and len(status) == 1 and status[0] in ("CLUSTER", "INDEPENDENT")):
                    r.error(f"{label}: cluster_status must be [\"CLUSTER\"] or [\"INDEPENDENT\"]")
                    continue
                if not row.get("commodity"):
                    r.error(f"{label}: commodity is required")
                key = (row.get("land_ref"), row.get("cluster_name") if status[0] == "CLUSTER" else row.get("commodity"))
                if status[0] == "CLUSTER":
                    if key not in cluster_keys or key not in cultivation_cluster_keys:
                        r.error(f"{label}: cluster {row.get('cluster_name')!r} on {row.get('land_ref')} has no matching "
                                f"clusters and cultivation_clusters rows")
                    if clustered.setdefault(key, {}).setdefault(section, row) is not row:
                        r.error(f"{label}: a second {section} row for the same cluster and land")
                    edge["cluster_" + section] += 1
                    needed = ("cluster_area_sown",) if section == "sowing" else ("cluster_harvest_date", "cluster_qty_harvested")
                else:
                    if row.get("cluster_name"):
                        r.error(f"{label}: an INDEPENDENT row carries a cluster_name")
                    if independent.setdefault(key, {}).setdefault(section, row) is not row:
                        r.error(f"{label}: a second {section} row for the same crop and land")
                    needed = ("area_sown", "sowing_date") if section == "sowing" else ("harvest_date", "qty_harvested")
                for field in needed:
                    if row.get(field) is None:
                        r.error(f"{label}: {field} is required on a {status[0]} row")
                if section == "harvest":
                    qty = row.get("qty_harvested") if status[0] == "INDEPENDENT" else row.get("cluster_qty_harvested")
                    stored = row.get("qty_stored" if status[0] == "INDEPENDENT" else "cluster_qty_stored") or 0
                    sold = row.get("qty_sold" if status[0] == "INDEPENDENT" else "cluster_qty_sold") or 0
                    if qty is not None and stored + sold > qty:
                        r.error(f"{label}: stored and sold together exceed the harvested quantity")
                    loss = row.get("post_harvest_loss_pct" if status[0] == "INDEPENDENT" else "cluster_post_harvest_loss_pct")
                    if loss is not None and not (_number(loss) is not None and 0 <= loss <= 100):
                        r.error(f"{label}: post harvest loss must be between 0 and 100")
                    if loss:
                        edge["loss"] += 1
        for section in ("planning", "cultivation"):
            for row in rec[section]:
                independent.setdefault((row.get("land_ref"), row.get("commodity")), {}).setdefault(section, row)
        for i, row in enumerate(rec["infestations"]):
            label = f"{who} infestations[{i}]"
            independent.setdefault((row.get("land_ref"), row.get("commodity")), {}).setdefault("infestation", row)
            kind = row.get("infestation_type")
            infestation_types.add(kind)
            name_field = {"INFT_PEST": "pest_name", "INFT_WEED": "weed_name", "INFT_DISEASE": "disease_name"}.get(kind)
            if name_field and not row.get(name_field):
                r.error(f"{label}: a {kind} row needs {name_field}")
            # a number: the registry stores it in a numeric column, where "12%" fails the save
            damage = row.get("estimated_damage_pct")
            if damage is not None and not (_number(damage) is not None and 0 <= damage <= 100):
                r.error(f"{label}: estimated_damage_pct={damage!r} must be a number from 0 to 100")

        # stages of a land follow one another
        for (ref, what), stages in sorted(independent.items(), key=lambda kv: str(kv[0])):
            label = f"{who} {ref} {what}"
            plan, cult, sow = stages.get("planning"), stages.get("cultivation"), stages.get("sowing")
            infest, harv = stages.get("infestation"), stages.get("harvest")
            if cult and not plan:
                r.error(f"{label}: cultivated but never planned")
            if sow and not cult:
                r.error(f"{label}: sown but never cultivated")
            if infest and not sow:
                r.error(f"{label}: an infestation without a sowing")
            if harv and not sow:
                r.error(f"{label}: harvested but never sown")
            if sow and bool(sow.get("has_pest_disease")) != bool(infest):
                r.error(f"{label}: has_pest_disease does not match the infestation rows")
            dates = [("planned_date", plan), ("actual_cultivation_date", cult), ("sowing_date", sow),
                     ("observation_date", infest), ("harvest_date", harv)]
            dates = [(f, _iso(row.get(f))) for f, row in dates if row and _iso(row.get(f))]
            for (fa, da), (fb, db) in zip(dates, dates[1:]):
                if not da < db:
                    r.error(f"{label}: {fa} {da} must be before {fb} {db}")
            window = plan or cult
            start, end = (_iso(window.get("start_gc")), _iso(window.get("end_gc"))) if window else (None, None)
            for f, d in dates:
                if (start and d < start) or (end and d > end):
                    r.error(f"{label}: {f} {d} is outside the season window {start} to {end}")
            if cult and sow and _number(sow.get("area_sown")) and _number(cult.get("actual_crop_area")) \
                    and sow["area_sown"] > cult["actual_crop_area"]:
                r.error(f"{label}: area_sown exceeds the cultivated area")
            if sow and harv and _number(harv.get("area_harvested")) and _number(sow.get("area_sown")) \
                    and harv["area_harvested"] > sow["area_sown"]:
                r.error(f"{label}: area_harvested exceeds the sown area")
        for (ref, name), stages in sorted(clustered.items(), key=lambda kv: str(kv[0])):
            label = f"{who} {ref} cluster {name}"
            plan = next((c for c in rec["clusters"] if (c.get("land_ref"), c.get("cluster_name")) == (ref, name)), None)
            cult = next((c for c in rec["cultivation_clusters"]
                         if (c.get("land_ref"), c.get("cluster_name")) == (ref, name)), None)
            harv = stages.get("harvest")
            plan_d, cult_d = _iso(plan.get("start_gc")) if plan else None, _iso(cult.get("start_gc")) if cult else None
            harv_d = _iso(harv.get("cluster_harvest_date")) if harv else None
            if plan_d and cult_d and not plan_d < cult_d:
                r.error(f"{label}: cultivation {cult_d} must be after planning {plan_d}")
            if cult_d and harv_d and not cult_d < harv_d:
                r.error(f"{label}: harvest {harv_d} must be after cultivation {cult_d}")
            end = _iso(cult.get("end_gc")) if cult else None
            if harv_d and end and harv_d > end:
                r.error(f"{label}: harvest {harv_d} is after the cluster's season end {end}")
            sown = stages.get("sowing", {}).get("cluster_area_sown")
            cultivated = sum(c.get("actual_crop_area") or 0 for c in rec["cultivation"] if c.get("land_ref") == ref)
            if _number(sown) and sown > cultivated:
                r.error(f"{label}: cluster_area_sown {sown} exceeds the {cultivated} ha cultivated on {ref}")
            if harv and _number(harv.get("cluster_area_harvested")) and _number(sown) \
                    and harv["cluster_area_harvested"] > sown:
                r.error(f"{label}: cluster_area_harvested exceeds cluster_area_sown")

        # the plot is only so large
        for ref, size in lands.items():
            if not _number(size):
                continue
            for section, field in (("planning", "planned_area"), ("cultivation", "actual_crop_area"),
                                   ("sowing", "area_sown")):
                total = sum(row.get(field) or 0 for row in rec[section] if row.get("land_ref") == ref)
                if section == "sowing":
                    total += sum(row.get("cluster_area_sown") or 0 for row in rec[section] if row.get("land_ref") == ref)
                if total > size + 1e-6:
                    r.error(f"{who} {ref}: {field} adds up to {total:g} ha on a {size:g} ha land")
            for section, cluster_section, field in (("planning", "clusters", "planned_area"),
                                                    ("cultivation", "cultivation_clusters", "actual_crop_area")):
                total = sum(row.get(field) or 0 for row in rec[section] if row.get("land_ref") == ref) + \
                    sum(c.get("cluster_area_hectare") or 0 for c in rec[cluster_section] if c.get("land_ref") == ref)
                if total > size + 1e-6:
                    r.error(f"{who} {ref}: {field} and the cluster area add up to {total:g} ha on a {size:g} ha land")
        # every crop of the season is planned
        for ref, crop in farm_crop.items():
            if crop.get("season") == season and not any(p.get("land_ref") == ref for p in rec["planning"]):
                r.error(f"{who}: the {crop.get('commodity')} on {ref} (in {season}) has no planning line")
        if len({p.get("land_ref") for p in rec["planning"]}) >= 2:
            edge["multi_land"] += 1
        edge["inter"] += sum(1 for p in rec["planning"] if p.get("cropping_system") == "INTER_CROPPING")
        if rec["planning"] and not any(rec[s] for s in CROP_SECTIONS if s not in ("planning", "clusters")):
            edge["planning_only"] += 1

    with_crops = {i for i, f in farms.items() if f.get("crops")}
    if with_crops - seen:
        r.error(f"crop_sown.json has no record for {sorted(with_crops - seen)}, who have crops in farms.json")
    if seen - with_crops:
        r.error(f"crop_sown.json has records for {sorted(seen - with_crops)}, who have no crops in farms.json")
    wanted = {"INFT_PEST", "INFT_WEED", "INFT_DISEASE"} & set(lists.get("INFESTATION_TYPE", {}))
    if wanted - infestation_types:
        r.error(f"crop_sown.json has no infestation of type {sorted(wanted - infestation_types)}")
    for what, ok, text in (
            ("multi_land", edge["multi_land"] >= 2, "two people with crop lines on different lands"),
            ("inter", edge["inter"] >= 1, "an INTER_CROPPING line"),
            ("cluster", edge["cluster_sowing"] >= 2 and edge["cluster_harvest"] >= 2, "two CLUSTER sowing and harvest lines"),
            ("loss", edge["loss"] >= 1, "a harvest with a post harvest loss"),
            ("planning_only", edge["planning_only"] >= 1, "a person with planning only")):
        if not ok:
            r.error(f"crop_sown.json must keep {text}, so registries see the case")


def check_livestock_samples(pack_dir, r):
    """Livestock samples: the animals of farms.json, and what happened to them.

    Heads per species equal farms.json head_count. An event names an animal of the
    same person and agrees with it: a mortality is a deceased animal, a disease a
    sick one, a birth or a breeding a female, and a vaccine belongs to the
    animal's species.
    """
    path = os.path.join(pack_dir, "samples", "livestock.json")
    if not os.path.exists(path):
        return
    if not os.path.exists(os.path.join(pack_dir, "samples", "farms.json")):
        r.error("%s needs samples/farms.json" % os.path.basename(path))
        return
    samples = os.path.join(pack_dir, "samples")
    farms = {f.get("individual_id"): f for f in read_json(os.path.join(samples, "farms.json"))}
    lists = _agriculture_lists(pack_dir)
    seen = set()
    edge = {"sick_disease": 0, "overdue": 0, "birth": 0, "mortality": 0, "ai": 0, "natural": 0, "animals_only": 0}
    tags_in_file, secondary_in_file = set(), set()
    for rec in read_json(path):
        ident = rec.get("individual_id")
        who = f"livestock.json {ident}"
        farm = farms.get(ident)
        if farm is None:
            r.error(f"{who}: no farms.json record for this person")
            continue
        if ident in seen:
            r.error(f"{who}: more than one record for this person")
        seen.add(ident)
        _check_location(r, who, rec.get("location"))
        staff = rec.get("survey_personnel") or {}
        for field in ("surveyor_name", "supervisor_name"):
            if not (isinstance(staff.get(field), str) and PERSON_NAME.match(staff[field])):
                r.error(f"{who}: survey_personnel.{field}={staff.get(field)!r} is not a person's name")
        for field in ("surveyor_mobile_number", "supervisor_mobile_number"):
            if not (isinstance(staff.get(field), str) and MOBILE.match(staff[field])):
                r.error(f"{who}: survey_personnel.{field}={staff.get(field)!r} is not an Ethiopian mobile number (09XXXXXXXX)")
        for section in LIVESTOCK_SECTIONS:
            if not isinstance(rec.get(section), list):
                r.error(f"{who}: {section} must be a list (empty when there are none)")
                rec[section] = []
        animals = {}  # normalised identifier -> animal
        heads = {}
        for i, a in enumerate(rec["animals"]):
            label = f"{who} animals[{i}]"
            for field in ("species", "breed", "gender", "health_status", "vaccination_status", "registration_date",
                          "weight", "quantity"):
                if a.get(field) is None:
                    r.error(f"{label}: {field} is required")
            tag = re.sub(r"\s+", "", str(a.get("ear_tag_id") or "")).upper()
            if not EAR_TAG.match(tag):
                r.error(f"{label}: ear_tag_id={a.get('ear_tag_id')!r} is not ET and ten digits (spaces allowed)")
            elif tag in tags_in_file:
                r.error(f"{label}: ear_tag_id {a.get('ear_tag_id')!r} is used twice in livestock.json")
            tags_in_file.add(tag)
            animals[tag] = a
            second = a.get("secondary_identifier")
            if second is not None:
                if second in secondary_in_file:
                    r.error(f"{label}: secondary_identifier {second!r} is used twice in livestock.json")
                secondary_in_file.add(second)
                animals[second] = a
            _check_coded(r, label, a, LIVESTOCK_CODED["animals"], lists)
            for field, allowed in LIVESTOCK_OPTIONS["animals"].items():
                if a.get(field) is not None and a[field] not in allowed:
                    r.error(f"{label}: {field}={a[field]!r} is not an option of the form")
            qty = a.get("quantity")
            if qty is not None and not (isinstance(qty, int) and not isinstance(qty, bool) and qty >= 1):
                r.error(f"{label}: quantity must be a whole number of at least 1")
            elif isinstance(qty, int):
                if qty > 1 and a.get("gender") != "MIXED":
                    r.error(f"{label}: a herd row (quantity {qty}) has gender MIXED")
                if qty == 1 and a.get("gender") == "MIXED":
                    r.error(f"{label}: gender MIXED belongs to a herd row (quantity above 1)")
                heads[(a.get("species"), a.get("breed"))] = heads.get((a.get("species"), a.get("breed")), 0) + qty
            if a.get("weight") is not None and not (_number(a["weight"]) is not None and a["weight"] > 0):
                r.error(f"{label}: weight must be a positive number")
            for field in LIVESTOCK_DATES["animals"]:
                if a.get(field) is not None and _iso(a[field]) is None:
                    r.error(f"{label}: {field}={a[field]!r} is not an ISO date")
            born, registered = _iso(a.get("date_of_birth")), _iso(a.get("registration_date"))
            if born and born > SAMPLE_AS_OF:
                r.error(f"{label}: born {born}, after {SAMPLE_AS_OF}")
            if registered and (registered.year != REFERENCE_YEAR or registered > LAST_REGISTRATION):
                r.error(f"{label}: registration_date {registered} must be in {REFERENCE_YEAR}, not after {LAST_REGISTRATION}")
            if born and registered and registered < born:
                r.error(f"{label}: registered {registered} before it was born {born}")
            if born is None and a.get("quantity") == 1:
                r.error(f"{label}: an individual animal needs date_of_birth")
            _check_free_text(r, label, a, LIVESTOCK_FREE_TEXT)
        # heads per species and breed are the farms.json head_count
        expected = {}
        for stock in farm.get("livestock", []):
            key = (LIVESTOCK_SPECIES_OF_TYPE.get(stock.get("livestock_type")), stock.get("breed"))
            expected[key] = expected.get(key, 0) + (stock.get("head_count") or 0)
        for key in sorted(set(expected) | set(heads), key=str):
            if expected.get(key, 0) != heads.get(key, 0):
                r.error(f"{who}: {heads.get(key, 0)} head of {key[0]} {key[1]}, farms.json has {expected.get(key, 0)}")

        def animal_of(section, i, row):
            label = f"{who} {section}[{i}]"
            key = re.sub(r"\s+", "", str(row.get("ear_tag_id") or "")).upper()
            animal = animals.get(key) or animals.get(row.get("ear_tag_id"))
            if animal is None:
                r.error(f"{label}: ear_tag_id {row.get('ear_tag_id')!r} is not an animal of this person")
            elif row.get("species") != animal.get("species"):
                r.error(f"{label}: species {row.get('species')!r}, but the animal is {animal.get('species')!r}")
            _check_coded(r, label, row, LIVESTOCK_CODED[section], lists)
            for field, allowed in LIVESTOCK_OPTIONS[section].items():
                if row.get(field) is not None and row[field] not in allowed:
                    r.error(f"{label}: {field}={row[field]!r} is not an option of the form")
            for field in LIVESTOCK_DATES[section]:
                if row.get(field) is not None:
                    d = _iso(row[field])
                    if d is None:
                        r.error(f"{label}: {field}={row[field]!r} is not an ISO date")
                    elif field not in ("next_due_date", "expected_calving_date") and d > SAMPLE_AS_OF:
                        r.error(f"{label}: {field} {d} is after {SAMPLE_AS_OF}")
            _check_free_text(r, label, row, LIVESTOCK_FREE_TEXT)
            for field in LIVESTOCK_NAMES:
                if row.get(field) is not None and not (isinstance(row[field], str) and PERSON_NAME.match(row[field])):
                    r.error(f"{label}: {field}={row[field]!r} is not a person's name (letters and spaces)")
            return label, animal

        vaccinated = set()
        for i, v in enumerate(rec["vaccinations"]):
            label, animal = animal_of("vaccinations", i, v)
            for field in ("vaccine_type", "vaccination_date"):
                if v.get(field) is None:
                    r.error(f"{label}: {field} is required")
            vaccine = lists.get("VACCINE_TYPE", {}).get(v.get("vaccine_type"))
            if vaccine and (vaccine.get("attributes") or {}).get("species") != v.get("species"):
                r.error(f"{label}: {v.get('vaccine_type')} is a vaccine for "
                        f"{(vaccine.get('attributes') or {}).get('species')}, not {v.get('species')}")
            if animal is not None:
                vaccinated.add(id(animal))
                born, given = _iso(animal.get("date_of_birth")), _iso(v.get("vaccination_date"))
                if born and given and given < born:
                    r.error(f"{label}: vaccinated {given} before the animal was born {born}")
                if animal.get("vaccination_status") != "UP_TO_DATE":
                    r.error(f"{label}: a vaccinated animal is UP_TO_DATE, this one is {animal.get('vaccination_status')}")
        for i, a in enumerate(rec["animals"]):
            if a.get("vaccination_status") == "UP_TO_DATE" and id(a) not in vaccinated:
                r.error(f"{who} animals[{i}]: UP_TO_DATE without a vaccination row")
            if a.get("vaccination_status") == "OVERDUE":
                edge["overdue"] += 1

        sick_by_event = set()
        for i, h in enumerate(rec["health_events"]):
            label, animal = animal_of("health_events", i, h)
            kind = h.get("event_type")
            if kind is None:
                r.error(f"{label}: event_type is required")
            if kind == "DISEASE" and not h.get("disease_type"):
                r.error(f"{label}: a DISEASE event needs disease_type")
            if kind != "RECOVERY" and not h.get("date_onset"):
                r.error(f"{label}: date_onset is required for {kind}")
            if kind not in ("TREATMENT", None) and not h.get("date_resolution"):
                r.error(f"{label}: date_resolution is required for {kind}")
            onset, resolved = _iso(h.get("date_onset")), _iso(h.get("date_resolution"))
            if onset and resolved and resolved < onset:
                r.error(f"{label}: resolved {resolved} before onset {onset}")
            if h.get("is_notifiable") is not None and not isinstance(h["is_notifiable"], bool):
                r.error(f"{label}: is_notifiable must be true or false")
            if animal is not None and kind in ("DISEASE", "INJURY"):
                sick_by_event.add(id(animal))
                if animal.get("health_status") != "SICK":
                    r.error(f"{label}: an animal with a {kind} event is SICK, this one is {animal.get('health_status')}")
                if kind == "DISEASE":
                    edge["sick_disease"] += 1
            if animal is not None and kind == "RECOVERY" and animal.get("health_status") != "HEALTHY":
                r.error(f"{label}: an animal that recovered is HEALTHY")

        died = set()
        for i, e in enumerate(rec["vital_events"]):
            label, animal = animal_of("vital_events", i, e)
            kind = e.get("event_type")
            for field in ("event_type", "event_date"):
                if e.get(field) is None:
                    r.error(f"{label}: {field} is required")
            if kind == "DISEASE" and not e.get("disease_type"):
                r.error(f"{label}: a DISEASE event needs disease_type")
            if kind != "BIRTH" and e.get("offspring_count"):
                r.error(f"{label}: offspring_count belongs to a BIRTH event")
            if kind == "BIRTH":
                edge["birth"] += 1
                if animal is not None and animal.get("gender") != "FEMALE":
                    r.error(f"{label}: a birth is logged against a female animal")
                if not (isinstance(e.get("offspring_count"), int) and e["offspring_count"] >= 1):
                    r.error(f"{label}: a BIRTH event needs offspring_count of at least 1")
                if not e.get("offspring_gender"):
                    r.error(f"{label}: a BIRTH event needs offspring_gender")
            if kind == "MORTALITY" and animal is not None:
                edge["mortality"] += 1
                if id(animal) in died:
                    r.error(f"{label}: a second MORTALITY event for the same animal")
                died.add(id(animal))
                if animal.get("health_status") != "DECEASED":
                    r.error(f"{label}: a MORTALITY event means the animal is DECEASED, this one is "
                            f"{animal.get('health_status')}")
            if kind == "DISEASE" and animal is not None:
                sick_by_event.add(id(animal))
                if animal.get("health_status") != "SICK":
                    r.error(f"{label}: an animal with a DISEASE event is SICK")
        for i, a in enumerate(rec["animals"]):
            if a.get("health_status") == "DECEASED" and id(a) not in died:
                r.error(f"{who} animals[{i}]: DECEASED without a MORTALITY event")

        last_breeding = {}
        for i, b in enumerate(rec["breeding"]):
            label, animal = animal_of("breeding", i, b)
            kind = b.get("event_type")
            if kind is None or b.get("breeding_date") is None:
                r.error(f"{label}: event_type and breeding_date are required")
            if animal is not None and animal.get("gender") != "FEMALE":
                r.error(f"{label}: breeding is logged against a female animal")
            if kind == "AI":
                edge["ai"] += 1
                for field in ("semen_batch_number", "ai_technician_name", "sire_or_semen_id"):
                    if not b.get(field):
                        r.error(f"{label}: an AI breeding needs {field}")
            if kind == "NATURAL":
                edge["natural"] += 1
                if not b.get("sire_or_semen_id"):
                    r.error(f"{label}: a NATURAL breeding names the sire (sire_or_semen_id)")
            served, calving = _iso(b.get("breeding_date")), _iso(b.get("expected_calving_date"))
            if served and calving and calving < served:
                r.error(f"{label}: expected calving {calving} before the breeding {served}")
            confirmed = _iso(b.get("pregnancy_confirmation_date"))
            if served and confirmed and confirmed < served:
                r.error(f"{label}: pregnancy confirmed {confirmed} before the breeding {served}")
            if b.get("pregnancy_confirmed") is not None and not isinstance(b["pregnancy_confirmed"], bool):
                r.error(f"{label}: pregnancy_confirmed must be true or false")
            if served and animal is not None:
                key = (id(animal), kind)
                if key in last_breeding and abs((served - last_breeding[key]).days) <= BREEDING_CYCLE_DAYS:
                    r.error(f"{label}: within {BREEDING_CYCLE_DAYS} days of another {kind} breeding of the same animal")
                last_breeding[key] = served
        if rec["animals"] and not any(rec[s] for s in LIVESTOCK_SECTIONS if s != "animals"):
            edge["animals_only"] += 1

    with_stock = {i for i, f in farms.items() if f.get("livestock")}
    if with_stock - seen:
        r.error(f"livestock.json has no record for {sorted(with_stock - seen)}, who have livestock in farms.json")
    if seen - with_stock:
        r.error(f"livestock.json has records for {sorted(seen - with_stock)}, who have no livestock in farms.json")
    for ok, text in ((edge["sick_disease"] >= 1, "a SICK animal with a DISEASE health event"),
                     (edge["overdue"] >= 1, "an OVERDUE vaccination status"),
                     (edge["birth"] >= 1, "a BIRTH vital event with offspring"),
                     (edge["mortality"] >= 1, "a MORTALITY event"),
                     (edge["ai"] >= 1, "an AI breeding with a semen batch"),
                     (edge["natural"] >= 1, "a NATURAL breeding"),
                     (edge["animals_only"] >= 1, "a person with animals only")):
        if not ok:
            r.error(f"livestock.json must keep {text}, so registries see the case")


def check_crop_livestock_locations(pack_dir, r, geoms):
    """A farm or herd stands in the woreda its owner lives in.

    The Crop Sown and Livestock forms place a pin and derive the administrative
    area from it, so a pin outside the woreda of the person's household would
    seed a record in a place the person is not registered in.
    """
    samples = os.path.join(pack_dir, "samples")
    households = {h.get("household_id"): h for h in read_json(os.path.join(samples, "households.json"))} \
        if os.path.exists(os.path.join(samples, "households.json")) else {}
    people = {i.get("individual_id"): i for i in read_json(os.path.join(samples, "individuals.json"))} \
        if os.path.exists(os.path.join(samples, "individuals.json")) else {}
    for name in ("crop_sown.json", "livestock.json"):
        path = os.path.join(samples, name)
        if not os.path.exists(path):
            continue
        for rec in read_json(path):
            ident = rec.get("individual_id")
            loc = rec.get("location") or {}
            person = people.get(ident)
            home = households.get(person.get("household_id")) if person else None
            if home is None or _number(loc.get("latitude")) is None or _number(loc.get("longitude")) is None:
                continue
            woreda = str(home.get("geo_pcode") or "")[:8]
            geom = geoms.get(woreda)
            if geom and not point_in_polygon((loc["longitude"], loc["latitude"]), geom):
                r.error(f"{name} {ident} is at ({loc['latitude']}, {loc['longitude']}), outside {woreda}, "
                        f"the woreda of its owner's household")


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
    check_farm_samples(pack_dir, r)
    check_farmer_ids(pack_dir, r)
    check_crop_sown_samples(pack_dir, r)
    check_livestock_samples(pack_dir, r)

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
    check_crop_livestock_locations(pack_dir, r, geoms)

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
