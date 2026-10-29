# Country packs

A **pack** is the one artifact that describes a country's geography to the
platform: its administrative hierarchy, its P-codes, and its boundary geometry.
Master Data Service is seeded from a pack; registries derive their geo columns
from what MDS holds; the map surfaces draw the pack's boundaries. There is no
second path — if geography reaches the platform, it came from a pack.

| Pack | Country | Real? | Licence | Levels | Units |
|---|---|---|---|---|---|
| [`XKM`](XKM) | Kamuntu | **No — fictitious** | CC0-1.0 | country → region → district → ward → village | 699 |
| [`ETH`](ETH) | Ethiopia | Yes — OCHA COD-AB | CC BY-IGO (**attribution required**) | country → region → zone → woreda | 1,271 |

`XKM` is the default. A fresh install gets a country that does not exist, so
invented poverty and enrolment figures are never attached to a real place's
name. Real packs are opt-in, selected per environment by `geoSeed.countryPack`
in the `openg2p-master-data` chart — the single place a deployment declares which
country it carries.

## The contract

Every pack — real or synthetic — is exactly these four things:

```
levels.json                    the hierarchy: level_id, level_mnemonic, parent_level_id
values.json                    every unit: level_value_id, level_id, level_value_mnemonic,
                               parent_level_value_id, pcode, pcode_source, display_name
boundaries/<level>.geojson     one FeatureCollection per level, keyed on pcode
manifest.json                  provenance, licence, version, level names, unit counts

codelists/<attribute>.json     the country's code lists — gender, education, water
                               source. Values may carry `roles` (see ../roles.json)
domains/<domain>/*.json        lists that vary by domain AND country, e.g. crops.
                               A Farmer Registry reads agriculture/; NSR ignores it
address.json                   how an address is written BELOW the lowest level
samples/individuals.json       a few dozen people, coherent with this country
samples/households.json        and the households they form
samples/farms.json             their farm facts, for registries that hold farmers:
                               lands, crops, livestock, farm inputs and
                               membership per individual_id, in value_codes of
                               codelists/ and domains/agriculture/
samples/farmer_ids.json        the Farmer ID (FR- and twelve digits) of every sample
                               person, for registries that identify a farmer by it
samples/crop_sown.json         what the Crop Sown registry seeds for the people with
                               crops in farms.json: one season, and the planning,
                               cluster, cultivation, sowing, infestation and
                               harvest lines of their lands
samples/livestock.json         what the Livestock registry seeds for the people with
                               livestock in farms.json: animals, vaccinations,
                               health, vital and breeding events
```

Everything after `manifest.json` is optional — a pack with geography alone is
still valid — but `validate_pack.py` checks whatever is present.

Consumers are told nothing else. `load_geo_pack.py` reads only these files and
cannot tell a synthetic pack from a real one, which is the point: swapping
Ethiopia for Kamuntu is a value change, not a code change.

### Rules that are not negotiable

**The P-code *is* the identifier.** `level_value_id == pcode`. One id space, no
reconciliation, and the natural join key for analytics. Minting a separate opaque
id alongside the P-code would mean every consumer has to know which one to use.

**P-codes nest.** A child's code starts with its parent's, two digits per level:
`ET` → `ET01` → `ET0101` → `ET010101`. Code that relies on this is correct by
construction rather than by convention.

**One chain, not a tree.** Each level has at most one child level. A country has
one hierarchy; branching would make "the level below this one" ambiguous.

**An address is a P-code plus what is written below it — never both.** The
P-code already resolves the whole administrative chain, so nothing else in the
pack repeats it as text. `address.json` declares only the part beneath the lowest
level (Ethiopia: kebele, house number) and whether the country uses postal codes
or street names at all; samples carry those parts in `address_parts`.

This pack once stored `"address": "Tahtay Adiyabo, North Western, Tigray"` beside
`"geo_pcode": "ET010101"` — the same fact twice, one copy of which stops being
true the day a woreda is renamed or resplit. A sample may also carry
`latitude`/`longitude`, and the validator checks the point falls inside the unit
its P-code names, so the map and the tables cannot tell different stories.

**Never a property called `name`.** Boundary features use **`area_name`**.
Evidence backs each map input with a callable proxy, and a field called `name`
collides with the read-only `Function.name` — the map dies outright with
*"Cannot assign to read only property 'name'"*. This is checked, because it has
happened.

**Code lists carry meaning, not just values.** Platform logic must never test a
literal: `is_head = (relationship_to_head = 'SELF')` breaks the moment a country
names that value differently, and it breaks silently. A value instead carries a
`role` from the closed vocabulary in [`roles.json`](roles.json), and logic asks for
the role. The validator rejects an unknown role, a single-value role held twice,
and a role no value carries.

**Sample people must be placeable and coded correctly.** Every `geo_pcode` must be
a unit in this pack, every coded field a value of its list, and a household's
headship must agree with its head's gender.

**Nothing hardcodes level names or depth.** Ethiopia has four levels and calls
them regions, zones and woredas; Kamuntu has five and calls them regions,
districts, wards and villages. Consumers read the names from `levels.json` and
the depth from the chain. Two packs that disagree is deliberate — it is what
keeps this honest instead of accidentally Ethiopia-shaped.

## Tools

All three live in [`../scripts/packs/`](../scripts/packs) and are pure Python —
no shapely, no GEOS.

```bash
cd scripts/packs

# Build a real country pack from OCHA COD-AB via HDX.
python3 fetch_country_pack.py --country ETH \
    --level-names region,zone,woreda --out ../../packs/ETH

# Redraw the fictitious country. Deterministic: same --seed and --version
# produce byte-identical output, so an unchanged pack shows an empty diff.
python3 generate_synthetic_pack.py --preset kamuntu \
    --out ../../packs/XKM --version 2026-07-30

# Gate. Run this after regenerating anything.
python3 validate_pack.py ../../packs/ETH ../../packs/XKM
```

### Why validate

The pack contract binds three things that never run in the same process: a
generator writes it, MDS seeds a database from it, and the map surface draws it.
A subtly wrong pack does not fail loudly — it seeds a hierarchy with orphans, or
draws a choropleth with holes, and the damage surfaces days later as figures
that do not add up. `validate_pack.py` checks hierarchy integrity, P-code
nesting, geometry/hierarchy agreement, ring validity, `unit_counts`, and the two
invariants drill-down actually depends on: **every child lies inside its parent**,
and **a parent's children account for its area**.

Every check in it earned its place by catching a real bug — the first synthetic
generator inset children at render time rather than at assignment, so every child
at every level poked outside its parent and drill-down looked fine until you
zoomed in.

Current state:

```
[ETH] WARN  1/1269 units (0.1%) are not inside their parent
[ETH] ok  (0 errors, 1 warnings)
[XKM] ok  (0 errors, 0 warnings)
```

The Ethiopia warning is simplification noise, not a defect: levels are simplified
independently, so a child and its parent disagree slightly along their shared
boundary. Kamuntu is exact because its children are constructed by clipping their
parent.

## Kamuntu

Fictitious, and fictitious on purpose. Relabelling a real country's outline does
not solve the problem — the silhouette is the identification, and it breaches
the attribution the real boundaries came with. So Kamuntu is drawn from nothing:

- **Codes that cannot collide.** `XKM` / `XK` come from the ISO 3166
  user-assigned ranges (`XAA`–`XZZ`, `XA`–`XZ`). ISO will never assign them to a
  real country, and the leading `X` is a standing signal that the place is not
  real.
- **Geometry, not rectangles.** The coastline is fractal midpoint displacement
  over a lopsided skeleton, so it has genuine bays and peninsulas. Each level
  partitions its parent by Voronoi cells clipped to the parent polygon, then
  internal borders are perturbed into wandering lines — both sides of a shared
  border get the identical polyline, so the partition stays exact while the
  borders stop looking machine-drawn. Children nest **exactly**; areas sum to
  **100.00%** at every level.
- **Sized so the demo is honest.** 530 villages, not Ethiopia's 1,148 woredas.
  A percentage needs a denominator: at the shipped sample size a Kamuntu village
  holds around a hundred households, where one household moves a rate by ~1
  point. Mirroring a real country's granularity instead leaves about a dozen
  households per unit, where one household moves a rate by 8 points and every
  leaf-level percentage has to be suppressed as unreliable. Nothing forces the
  synthetic pack into that, so it is sized to be reportable all the way down.
- **Placed in open ocean** (South Atlantic). If a basemap is ever shown behind
  the map, a landmass there is unmistakably not a real place rather than quietly
  overlapping someone's territory.

## Adding a country

```bash
python3 fetch_country_pack.py --country KEN --level-names county,subcounty,ward \
    --out ../../packs/KEN
python3 validate_pack.py ../../packs/KEN
```

Then set `geoSeed.countryPack: KEN` in the `openg2p-master-data` values. Level
names are cosmetic labels — the hierarchy itself comes from COD-AB's P-codes, so
getting the names wrong mislabels columns but cannot corrupt the structure.

Real packs carry obligations. `manifest.json` records the source, licence and
`license_note` so the surfaces can display attribution from the pack rather than
hardcoding it, and boundaries are rebuilt from HDX rather than vendored from
someone else's redistribution.
