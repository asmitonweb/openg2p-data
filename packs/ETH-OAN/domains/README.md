# Domain lists

Code lists that vary by **domain as well as by country**. Crops are agricultural
*and* Ethiopian; a social registry install has no use for them.

Keeping them here rather than in `../codelists/` means one artifact per country
without every install loading every domain's lists — a Farmer Registry reads
`agriculture/`, NSR ignores it. Master Data loads a domain only when its chart
names it (`geoSeed.domains: [agriculture]`); registries then read the lists live
from Master Data.

The file format is identical to `../codelists/`, and `validate_pack.py` checks
both. An `attribute_id` must be unique across `codelists/` and every domain:
Master Data holds them in one table. Roles (`../../roles.json`) apply here too if
platform logic ever needs to reason about a domain value's meaning; none do today.

## agriculture/

| Lists | Used by |
|---|---|
| `CROP_COMMODITY`, `CROP_END_USE`, `CROP_SEASON`, `CURRENT_LAND_USE`, `FARMER_CLUSTER_ROLE`, `FARMING_TYPE`, `LAND_OWNERSHIP_TYPE`, `LAND_SIZE_UNIT`, `LIVESTOCK_BREED`, `LIVESTOCK_SYSTEM`, `LIVESTOCK_TYPE`, `MEANS_OF_ACQUISITION`, `SOURCE_OF_INCOME`, `WATER_SOURCE` | Farmer Registry |
| `CROP_COMMODITY` (with `CROP_CATEGORY`), `CROP_SEASON`, `SOIL_FERTILITY`, `WATER_SOURCE`, and the crop-season lists: `CROPPING_SYSTEM`, `LAND_PREPARATION_METHOD`, `IRRIGATION_SOURCE`, `IRRIGATION_METHOD`, `SEED_TYPE`, `SEED_SOURCE`, `SEED_VARIETY`, `SOWING_METHOD`, `FERTILIZER_TYPE`, `FARM_MACHINERY`, `CROP_GROWTH_STAGE`, `CROP_CONDITION`, `INFESTATION_TYPE`, `INFESTATION_AGENT`, `INFESTATION_SEVERITY`, `PEST_CONTROL_ACTION`, `CROP_DAMAGE_CAUSE`, `AGRO_ECOLOGICAL_ZONE` | Crop Sown Registry |

Seasons follow Ethiopia's CSA Agricultural Sample Survey: Meher (main rains),
Belg (short rains) and irrigated dry-season production. Each list's codes carry a
short list prefix (`SEASON_MEHER`, `SEV_HIGH`) so a code names its list on its own.

Crops follow the Ministry of Agriculture's crop variety list: `CROP_COMMODITY` has
one entry per MoA crop, plus `CROP_MALT_BARLEY` and `CROP_OTHER`. A list may carry
typed attributes: an `attribute_schema` (JSON Schema) and `attributes` per value,
where `x-list-ref` names another list (a crop's `category` is a `CROP_CATEGORY`
code; a variety's `crop` is a `CROP_COMMODITY` code). `validate_pack.py` checks
them. On an existing install, approve the `CROP_COMMODITY` draft before
`SEED_VARIETY`'s, whose varieties refer to the new crops.
