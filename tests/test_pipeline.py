import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pipeline import missing_vehicle_types, normalize_status  # noqa: E402


class PipelineTest(unittest.TestCase):
    """Runs the real pipeline on a copy of the repo, so nothing here touches state/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name) / "recall-ops"
        shutil.copytree(ROOT, self.repo, ignore=shutil.ignore_patterns("state", "__pycache__", ".git", "docs"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_pipeline(self):
        return subprocess.run([sys.executable, "src/pipeline.py"], cwd=self.repo, capture_output=True, text=True)

    def objects(self):
        return json.loads((self.repo / "state" / "objects.json").read_text())["objects"]

    def test_a_vehicle_type_that_was_never_fetched_stops_the_run(self):
        manifest_path = self.repo / "data" / "raw" / "_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest_path.write_text(json.dumps([e for e in manifest if e["requested"]["model"] != "ESCAPE"]))

        result = self.run_pipeline()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FORD|ESCAPE|2019", result.stderr)
        self.assertFalse((self.repo / "state" / "objects.json").exists())

    def test_open_dates_survive_a_work_order_missing_one_run(self):
        self.assertEqual(self.run_pipeline().returncode, 0)
        ledger_path = self.repo / "state" / "first_seen.json"
        ledger = json.loads(ledger_path.read_text())
        order_id = next(k for k in ledger if "VEH-013" in k)
        ledger[order_id] = "2026-08-01"  # pretend it was first seen seven weeks ago
        ledger_path.write_text(json.dumps(ledger))

        fleet = self.repo / "data" / "fleet.csv"
        original = fleet.read_text()
        fleet.write_text("\n".join(line for line in original.splitlines() if not line.startswith("VEH-013,")) + "\n")
        self.assertEqual(self.run_pipeline().returncode, 0)
        self.assertNotIn(order_id, self.objects()["WorkOrder"])

        fleet.write_text(original)
        self.assertEqual(self.run_pipeline().returncode, 0)
        self.assertEqual(self.objects()["WorkOrder"][order_id]["openedAt"], "2026-08-01")

    def test_work_order_ids_include_the_whole_vehicle_id(self):
        self.assertEqual(self.run_pipeline().returncode, 0)
        orders = self.objects()["WorkOrder"].values()
        self.assertTrue(all(o["workOrderId"].startswith(f"WO-{o['vehicleId']}-") for o in orders))

    def test_the_fetcher_skips_rows_the_pipeline_would_reject(self):
        import fetch_recalls
        fleet = pathlib.Path(self.tmp.name) / "fleet.csv"
        fleet.write_text("vehicle_id,make,model,model_year\n"
                         "VEH-001,Ford,Transit,2020\n"
                         "VEH-002, ,Transit,2020\n"
                         "VEH-003,Ford,Escape,twenty19\n"
                         "VEH-004,Ford,Escape, 2019 \n")
        original = fetch_recalls.FLEET
        fetch_recalls.FLEET = fleet
        try:
            self.assertEqual(fetch_recalls.fleet_vehicle_types(),
                             [("FORD", "ESCAPE", "2019"), ("FORD", "TRANSIT", "2020")])
        finally:
            fetch_recalls.FLEET = original

    def test_missing_vehicle_types_compares_roster_and_manifest(self):
        vehicles = {"A": {"vehicleKey": "FORD|TRANSIT|2020"}, "B": {"vehicleKey": "FORD|ESCAPE|2019"}}
        manifest = [{"requested": {"make": "FORD", "model": "TRANSIT", "model_year": "2020"}}]
        self.assertEqual(missing_vehicle_types(vehicles, manifest), ["FORD|ESCAPE|2019"])

    def test_status_spellings_are_normalized_and_unknown_ones_flagged(self):
        self.assertEqual(normalize_status("In Service"), "in_service")
        self.assertEqual(normalize_status("in-service"), "in_service")
        self.assertEqual(normalize_status(" GROUNDED "), "grounded")
        self.assertIsNone(normalize_status("sold"))


if __name__ == "__main__":
    unittest.main()
