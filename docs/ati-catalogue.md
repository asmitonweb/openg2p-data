# ATI Farmer catalogue alignment

The OAN fork adds six distinct machinery values and three distinct crop values to ETH-OAN through the standard `scripts/packs/eth_oan_additions/values/domains/agriculture/` files. The committed materialized country pack contains those additions. The existing generator applies this directory after copying ETH lists. Native ETH, geography, the upstream seed loader and all existing values remain unchanged.

## Source and crosswalk

The original ATI Farmers Profile v2 and candidate v3 workbooks have identical machinery and finance names and labels in the choices sheet. Exact filenames, SHA-256 hashes, rows and English/Amharic/Afaan Oromoo source labels are in [the source excerpt](../reference/ati-catalogue-source.json). The source workbooks are owned by the Farmer repository and are not copied or changed here. The v3 SHA-256 is `c7b022fc4007bf50a3d8dc9e8555d3eec536156fa16e72b501e031a45d92cfae`.

| Source name | English source label | Choice row | Target value ID/code | Decision |
| --- | --- | --- | --- | --- |
| tractor | Tractor | 283 | MACH_TRACTOR | Existing |
| combine_harvester | Combine Harvester | 284 | MACH_COMBINE | Existing exact label |
| planter | Planter | 285 | MACH_PLANTER | New; Row planter is more specific |
| harvester | Harvester | 286 | MACH_HARVESTER | New; distinct from combine |
| combiner | Combiner | 287 | MACH_COMBINER | New; source meaning not collapsed |
| multi_crop_trasher | Multi-crop trasher | 288 | MACH_MULTI_CROP_TRASHER | New; source spelling preserved |
| maize_sheller | Maize sheller | 289 | MACH_MAIZE_SHELLER | New |
| rice_polisher | Rice polisher | 290 | MACH_RICE_POLISHER | New |

The existing MACH_ROW_PLANTER, MACH_THRESHER, MACH_COMBINE and other original IDs remain unchanged. No `standaloneharvester` choice occurs in either workbook. That alias remains unmapped until its source and meaning are established. New translations reproduce the source labels exactly; repeated English text in the source translations is preserved rather than translated speculatively.

## Additional source crop choices

| Source name | English source label | Choice row | Target value ID/code | Typed category |
| --- | --- | --- | --- | --- |
| food_barley | Food Barley | 227 | CROP_FOOD_BARLEY | CROPCAT_CEREAL |
| tea | Tea | 259 | CROP_TEA | CROPCAT_STIMULANT |
| cumin | Cumin | 264 | CROP_CUMIN | CROPCAT_SPICE_MEDICINAL_AROMATIC |

The source English/Amharic/Afaan Oromoo labels are retained exactly. Food barley remains distinct from generic Barley; generic Cumin remains distinct from Black/White cumin. No scientific name is inferred. The standard typed schema requires a category, which the workbook does not provide. Classification provenance is recorded separately: [FAO lists barley among cereals](https://www.fao.org/fileadmin/templates/ess/documents/meetings_and_workshops/WCA_2020_expert_meeting_2014/WCA2020_Volume_1_full_24102014.pdf), [FAO classifies tea as a stimulant crop](https://www.fao.org/4/x9892e/X9892e06.htm), and [UN Statistics lists cumin seeds among spices](https://unstats.un.org/unsd/classifications/Econ/Structure/Detail/EN/32/0909). These broad categories do not assert equivalence with another catalogue value.

## Other choice inventory

The [read-only inventory](../reference/ati-choice-inventory.json) compares actual source choices with the current Farmer mapper and both MDS value IDs and codes. Code presence does not establish semantic correctness or deployed state.

- Finance rows 293-296 contain loan/Loans, insurance/Insurance, saving/Savings and digital_banking/Digital Banking. These are supported registry-owned static schema options; the registry owner retains their validation. No shared MDS finance list is needed or introduced.
- The original mapper gaps for CROP_FOOD_BARLEY, CROP_TEA and CROP_CUMIN are closed by the three distinct additions above. The inventory preserves baseline membership alongside the resulting pack membership.
- The v3 choices include SEASON_MEHER, SEASON_BELG and SEASON_IRRIGATION, already present in CROP_SEASON. BEGA is absent from both inspected choice sheets. No BEGA-to-irrigation equivalence is asserted.
- Audited language, gender, farming type, land ownership, water, livestock, income, education, marital status, household relationship and cluster-role mapper codes resolve existing pack values. LANGUAGE uses LANG_* IDs but unprefixed value codes; the inventory checks both.
- Boolean questions, source institutional union names, geographic entities and identity-document configuration are separate contracts. They are inventoried rather than invented as new shared catalogues.

Registry mappers and models are not edited here. The receiving mapper must use this crosswalk explicitly. Unknown values continue to fail native validation until their catalogue decision is implemented. The nine codes here become available to consumers only after a separately reviewed seed-image rollout and the normal MDS approval process.

## Validation and delivery

Run `bash ci/validate.sh` with Python 3.12, or run its Python commands with UTF-8 enabled on Windows. CI validates the OAN pack, preserves every native ETH catalogue value, checks distinct source-backed machinery mappings/translations, and builds the standard native seed image without publication. The separate existing release-tag workflow also requires validation and pins its third-party actions.

The unchanged Dockerfile derives from `openg2p/master-data-db-seed:0.0.0-develop.118@sha256:cfa91c1bcc1798a8a82454073ac5454c8099041511bf6d0c7380b492232d809f` and copies this exact checkout into `/openg2p-data`. CI labels the image with the repository and immutable source commit. Loader/schema compatibility with the target Master Data API remains a deployment prerequisite. No release tag, image publication, database seed, MDS mutation or grants change is performed by this source change.

Baseline ETH-OAN validation has zero errors and one pre-existing partial boundary coverage warning (ET children cover 19%). The native XKM baseline fails its head_of_household role requirement, and ETH has the documented same role gap; these packs are untouched and are outside the OAN delivery gate. This is reported rather than hidden by modifying upstream data. Full live registry and provider acceptance is separate from source/pack/image checks.
