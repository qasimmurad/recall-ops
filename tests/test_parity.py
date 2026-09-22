"""The browser kernel (web/kernel.js) must behave exactly like src/kernel.py.

Runs the same scenario through both, then compares every result, every error
message, and the final state. Skipped when Node.js isn't installed.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import kernel as kernel_module  # noqa: E402
from kernel import ActionError, Kernel  # noqa: E402
from test_kernel import CRITICAL, DEPOT, EMPTY_ORDER, LEAD, NORMAL, TODAY, fixture  # noqa: E402
from views import fleet_view  # noqa: E402

NOW = "2026-09-21T12:00:00+00:00"
MANY = [f"WO-VEH-002-21V00{n}000" for n in range(1, 6)]


def parity_fixture():
    base = fixture()
    objects = base["objects"]
    for vehicle_id in ("VEH-002", "VEH-003"):
        objects["Vehicle"][vehicle_id] = {"vehicleId": vehicle_id, "status": "in_service",
                                          "groundedReason": None, "statusChangedAt": None}
    for n, order_id in enumerate(MANY, start=1):
        number = f"21V00{n}000"
        objects["RecallCampaign"][number] = {"campaignNumber": number, "priority": "critical"}
        objects["WorkOrder"][order_id] = {"workOrderId": order_id, "vehicleId": "VEH-002", "campaignNumber": number,
                                          "priority": "critical", "dueBy": "2026-09-19", **EMPTY_ORDER}
    return base


SCHEDULE = {"workOrder": CRITICAL, "scheduledFor": "2026-09-30", "shop": "Franchise dealer"}
DISMISS = {"workOrder": NORMAL, "reason": "Vehicle sold or retired", "evidence": "Bill of sale dated 2026-09-01"}

STEPS = [
    ["groundVehicle", {"vehicle": "VEH-001", "reason": "short"}, LEAD],
    ["groundVehicle", {"vehicle": "VEH-001", "reason": "  Open critical recall  "}, LEAD],
    ["groundVehicle", {"vehicle": "VEH-001", "reason": "Open critical recall"}, LEAD],
    ["returnToService", {"vehicle": "VEH-001", "note": "Looks fine to me"}, LEAD],
    ["returnToService", {"vehicle": "VEH-001", "note": "Looks fine to me"}, DEPOT],
    ["scheduleRepair", {**SCHEDULE, "scheduledFor": "2026-09-20"}, DEPOT],
    ["scheduleRepair", {**SCHEDULE, "scheduledFor": "2026-02-30"}, DEPOT],
    ["scheduleRepair", {**SCHEDULE, "scheduledFor": "20260930"}, DEPOT],
    ["scheduleRepair", {**SCHEDULE, "scheduledFor": "0000-01-01"}, DEPOT],
    ["scheduleRepair", {**SCHEDULE, "scheduledFor": 20260930}, DEPOT],
    ["scheduleRepair", {**SCHEDULE, "shop": "Some guy"}, DEPOT],
    ["scheduleRepair", {**SCHEDULE, "extra": 1, "another": True}, DEPOT],
    ["scheduleRepair", SCHEDULE, DEPOT],
    ["scheduleRepair", SCHEDULE, DEPOT],
    ["completeRepair", {"workOrder": CRITICAL, "completedOn": "2026-09-22", "notes": "Dealer replaced part"}, DEPOT],
    ["completeRepair", {"workOrder": CRITICAL, "completedOn": "2026-09-21", "notes": "Dealer replaced part"},
     {"name": "   ", "role": "depot_manager"}],
    ["returnToService", {"vehicle": "VEH-001", "note": "Critical recall closed"}, LEAD],
    ["dismissWorkOrder", {**DISMISS, "evidence": "short"}, LEAD],
    ["dismissWorkOrder", DISMISS, DEPOT],
    ["dismissWorkOrder", DISMISS, LEAD],
    ["dismissWorkOrder", DISMISS, LEAD],
    ["returnToService", {"vehicle": "VEH-002", "note": "Never grounded here"}, LEAD],
    ["groundVehicle", {"vehicle": "VEH-002", "reason": "Five critical recalls open"}, LEAD],
    ["returnToService", {"vehicle": "VEH-002", "note": "Trying it anyway"}, LEAD],
    ["launchRocket", {}, LEAD],
    ["__proto__", {}, LEAD],
    ["groundVehicle", ["vehicle"], LEAD],
    ["groundVehicle", None, LEAD],
    ["groundVehicle", {}, "safety_lead"],
    ["groundVehicle", {}, {"role": 7}],
    ["groundVehicle", {"vehicle": ["VEH-003"], "reason": "Open critical recall"}, LEAD],
    ["groundVehicle", {"vehicle": "VEH-999", "reason": "Open critical recall"}, LEAD],
    ["groundVehicle", {"vehicle": "constructor", "reason": "Open critical recall"}, LEAD],
    ["groundVehicle", {"reason": "Open critical recall"}, LEAD],
    ["groundVehicle", {"vehicle": "VEH-003", "reason": "x" * 1001}, LEAD],
    ["groundVehicle", {"vehicle": "VEH-003", "reason": "🚗🚗🚗🚗🚗🚗🚗🚗🚗"}, LEAD],
    ["groundVehicle", {"vehicle": "VEH-003", "reason": " Präventiv: défaut 🚗 moteur "}, {"role": "safety_lead"}],
]


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class ParityTest(unittest.TestCase):
    def run_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            objects = pathlib.Path(tmp) / "objects.json"
            objects.write_text(json.dumps(parity_fixture()))
            kernel = Kernel(ROOT / "ontology" / "ontology.json", objects, pathlib.Path(tmp) / "edits.jsonl",
                            today=TODAY)
            results = []
            with mock.patch.object(kernel_module, "_now", return_value=NOW):
                for action, params, actor in STEPS:
                    try:
                        results.append({"edit": kernel.submit(action, params, actor)})
                    except ActionError as error:
                        results.append({"error": error.message, "field": error.field})
            return results, fleet_view(kernel.snapshot())

    def run_javascript(self):
        payload = {
            "ontology": json.loads((ROOT / "ontology" / "ontology.json").read_text()),
            "objects": parity_fixture()["objects"],
            "generatedAt": parity_fixture()["generatedAt"],
            "steps": STEPS,
            "today": TODAY.isoformat(),
            "now": NOW,
        }
        completed = subprocess.run(["node", str(ROOT / "tests" / "parity_runner.js")], input=json.dumps(payload),
                                   capture_output=True, text=True, check=True)
        output = json.loads(completed.stdout)
        return output["results"], output["view"]

    def test_both_kernels_agree_on_every_step_and_the_final_state(self):
        python_results, python_view = self.run_python()
        js_results, js_view = self.run_javascript()

        self.assertEqual(len(python_results), len(js_results))
        for step, python, javascript in zip(STEPS, python_results, js_results):
            with self.subTest(action=step[0], params=str(step[1])[:60]):
                self.assertEqual(javascript, python)

        # Round-trip through JSON so both sides compare as plain data.
        self.assertEqual(js_view, json.loads(json.dumps(python_view)))

    def test_the_scenario_exercises_both_successes_and_failures(self):
        python_results, _ = self.run_python()
        successes = sum("edit" in r for r in python_results)
        self.assertGreaterEqual(successes, 8)
        self.assertGreaterEqual(len(python_results) - successes, 20)


if __name__ == "__main__":
    unittest.main()
