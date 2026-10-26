# openg2p-data

Sample and reference data for the OpenG2P platform, plus the generators that
produce it. Registry and Master Data db-seed images clone this repo at build
time, so what is committed here is what a seeded install contains.

## Layout

| Directory | What it holds |
|---|---|
| [`packs/`](packs) | **Country packs** — administrative hierarchy, P-codes and boundary geometry, one directory per country. The only supported way geography reaches the platform. Start with its [README](packs/README.md). |
| [`scripts/packs/`](scripts/packs) | Pack tooling: build a real pack from OCHA COD-AB, redraw the fictitious one, and validate either. |
| [`demography/`](demography) | Shared synthetic people: `individuals.csv`, `households.csv`, ~500 portrait images. Registry-agnostic — person `i0001` is the same individual in every registry, so cross-registry scenarios line up. |
| [`household-info/`](household-info) | The same population as consolidated wide CSVs (housing, assets, livelihoods, scores). |
| [`users/`](users) | Sample portal users. |
| [`geo/`](geo) | **Legacy.** The pre-pack flat `geo.csv`. Superseded — see its [README](geo/README.md). |
| [`scripts/`](scripts) | Generators for everything above. |

## Which country does an install get?

Kamuntu — a **fictitious** country ([`packs/XKM`](packs/XKM)) — by default.

That default is deliberate. A demo install attaches invented poverty, enrolment
and payment figures to whatever geography it holds, and doing that to a real
country's name and outline is not something to do by accident. Real country packs
are first-class and one Helm value away (`geoSeed.countryPack` in the
`openg2p-master-data` chart), and they carry the licence obligations their
`manifest.json` records.

## Generators

Nothing here is generated at install time — the outputs are committed, so seed
data is reviewable in a diff and reproducible. Regenerate deliberately:

```bash
cd scripts/packs
python3 generate_synthetic_pack.py --preset kamuntu --out ../../packs/XKM --version 2026-07-30
python3 validate_pack.py ../../packs/ETH ../../packs/XKM     # always
```

The pack tools are pure Python — no shapely, no GEOS. Everything else needs
`pip install -r scripts/requirements.txt`.

## What does not belong here

Data and generators here must be usable by more than one registry or service. A
generator that writes one registry's own tables belongs with that registry, beside
the schema it targets.

NSR's bulk sample generator, its `distributions.json` and the query that rebuilds
them used to live here and now sit in
`national-social-registry/docker/db-seed/`. They write NSR's extension tables and
draw every enum-backed value from NSR's enums, and keeping them a repo apart from
those enums had a concrete cost: a commit reworking the programme enums could not
show the generator going stale, so it and `reporting_views.sql` drifted into
agreeing with each other on eleven values the schema never defined. NSR's
`test/test_enum_values.py` guards that now — a test only writable once the two
live together.

`generate_nsr_data.py` and `generate_farmer_data.py` do stay: they read the shared
demography above and write committed fixture files, so they move with
`demography/`'s columns rather than with a registry's schema.

## OAN catalogue delivery

The ETH-OAN ATI machinery additions, exact workbook provenance and remaining catalogue gaps are documented in [ATI catalogue alignment](docs/ati-catalogue.md). Run `bash ci/validate.sh` before building the standard Master Data seed image. CI builds without publication; the existing release-tag workflow is separately gated by the same validation.
