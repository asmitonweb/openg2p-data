"""OAN country-pack additions retain every native ETH value and identifier."""
import json
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT/"packs/ETH"
OAN = ROOT/"packs/ETH-OAN"
ADDITIONS = ROOT/"scripts/packs/eth_oan_additions"
def read(path):
    return json.loads(path.read_text(encoding="utf-8"))
class CatalogueContractTests(unittest.TestCase):
    def test_existing_eth_values_are_retained_unchanged(self):
        for directory in ["codelists", "domains"]:
            for path in (BASE/directory).rglob("*.json"):
                with self.subTest(path=path.relative_to(BASE)):
                    native = read(path)
                    current = read(OAN/path.relative_to(BASE))
                    self.assertEqual(current["attribute_id"], native["attribute_id"])
                    values = {value["value_id"]:value for value in current["values"]}
                    for value in native["values"]:
                        self.assertEqual(values[value["value_id"]], value)
    def test_additions_are_materialized_without_duplicate_ids_or_codes(self):
        for path in (ADDITIONS/"values").rglob("*.json"):
            relative = path.relative_to(ADDITIONS/"values")
            with self.subTest(path=relative):
                current = read(OAN/relative)["values"]
                for value in read(path)["values"]:
                    self.assertEqual(current.count(value), 1)
                self.assertEqual(len(current), len({v["value_id"] for v in current}))
                self.assertEqual(len(current), len({v["value_code"] for v in current}))
    def test_seed_retains_pinned_native_loader_and_local_source(self):
        dockerfile = (ROOT/"docker/master-data-db-seed/Dockerfile").read_text()
        self.assertIn("@sha256:cfa91c1bcc1798a8a82454073ac5454c8099041511bf6d0c7380b492232d809f", dockerfile)
        self.assertIn("COPY . /openg2p-data", dockerfile)
        self.assertNotIn("git clone", dockerfile)

    def test_every_source_machinery_choice_remains_distinct_and_label_proven(self):
        source = read(ROOT/"reference/ati-catalogue-source.json")
        original, revised = source["source_workbooks"]
        original_rows = [r for r in original["choices"] if r["list_name"] == "choices_machinery"]
        revised_rows = [r for r in revised["choices"] if r["list_name"] == "choices_machinery"]
        self.assertEqual(original_rows, revised_rows)
        crosswalk = source["machinery_crosswalk"]
        self.assertEqual({r["name"] for r in revised_rows}, {r["source_name"] for r in crosswalk})
        self.assertEqual(len(crosswalk), len({r["target_value_id"] for r in crosswalk}))
        target = {v["value_id"]:v for v in read(OAN/"domains/agriculture/farm_machinery.json")["values"]}
        for row in crosswalk:
            value = target[row["target_value_id"]]
            self.assertEqual(value["value_display"].casefold(), row["source_label_en"].casefold())
            self.assertEqual(value["value_id"], value["value_code"])
        self.assertNotEqual(target["MACH_PLANTER"]["value_id"], target["MACH_ROW_PLANTER"]["value_id"])

    def test_new_machinery_translations_are_exact_source_labels(self):
        source = read(ROOT/"reference/ati-catalogue-source.json")
        rows = {r["name"]:r for r in source["source_workbooks"][1]["choices"] if r["list_name"] == "choices_machinery"}
        target = {v["value_id"]:v for v in read(OAN/"domains/agriculture/farm_machinery.json")["values"]}
        for row in source["machinery_crosswalk"]:
            if row["decision"] == "distinct OAN addition":
                value = target[row["target_value_id"]]
                self.assertEqual(value["display_i18n"], {"am":rows[row["source_name"]]["label::Amharic (am)"], "om":rows[row["source_name"]]["label::Afaan Oromoo (om)"]})
