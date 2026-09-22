import json
import pathlib
import sys
import tempfile
import unittest
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kernel import ActionError, Kernel, LogCorrupted  # noqa: E402

TODAY = date(2026, 9, 21)
LEAD = {"name": "Dana Ortiz", "role": "safety_lead"}
DEPOT = {"name": "Sam Reyes", "role": "depot_manager"}
CRITICAL = "WO-VEH-001-20V001000"
NORMAL = "WO-VEH-001-20V002000"

EMPTY_ORDER = {
    "status": "open", "scheduledFor": None, "shop": None, "completedOn": None,
    "completionNotes": None, "dismissalReason": None, "dismissalEvidence": None, "lastActionAt": None,
}


def fixture():
    return {
        "generatedAt": "2026-09-21T00:00:00+00:00",
        "objects": {
            "Vehicle": {
                "VEH-001": {"vehicleId": "VEH-001", "status": "in_service", "groundedReason": None, "statusChangedAt": None},
            },
            "RecallCampaign": {
                "20V001000": {"campaignNumber": "20V001000", "priority": "critical"},
                "20V002000": {"campaignNumber": "20V002000", "priority": "normal"},
            },
            "WorkOrder": {
                CRITICAL: {"workOrderId": CRITICAL, "vehicleId": "VEH-001", "campaignNumber": "20V001000",
                           "priority": "critical", "dueBy": "2026-09-23", **EMPTY_ORDER},
                NORMAL: {"workOrderId": NORMAL, "vehicleId": "VEH-001", "campaignNumber": "20V002000",
                         "priority": "normal", "dueBy": "2026-10-21", **EMPTY_ORDER},
            },
        },
    }


def ground(kernel):
    return kernel.submit("groundVehicle", {"vehicle": "VEH-001", "reason": "Open critical recall"}, LEAD)


class KernelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.objects = self.dir / "objects.json"
        self.edits = self.dir / "edits.jsonl"
        self.objects.write_text(json.dumps(fixture()))
        self.kernel = self.make_kernel()

    def tearDown(self):
        self.tmp.cleanup()

    def make_kernel(self):
        return Kernel(ROOT / "ontology" / "ontology.json", self.objects, self.edits, today=TODAY)

    def test_return_to_service_is_blocked_until_critical_recall_is_resolved(self):
        ground(self.kernel)

        with self.assertRaises(ActionError) as blocked:
            self.kernel.submit("returnToService", {"vehicle": "VEH-001", "note": "Looks fine to me"}, LEAD)
        self.assertIn(CRITICAL, blocked.exception.message)

        self.kernel.submit("completeRepair", {"workOrder": CRITICAL, "completedOn": "2026-09-21",
                                              "notes": "Dealer replaced part"}, DEPOT)
        self.kernel.submit("returnToService", {"vehicle": "VEH-001", "note": "Critical recall closed"}, LEAD)
        self.assertEqual(self.kernel.get("Vehicle", "VEH-001")["status"], "in_service")

    def test_only_listed_roles_can_submit(self):
        with self.assertRaises(ActionError) as denied:
            self.kernel.submit("dismissWorkOrder", {
                "workOrder": NORMAL,
                "reason": "Vehicle sold or retired",
                "evidence": "Bill of sale dated 2026-09-01",
            }, DEPOT)
        self.assertIn("fleet safety lead", denied.exception.message)

    def test_parameters_are_validated_against_the_ontology(self):
        cases = [
            ("groundVehicle", {"vehicle": "VEH-001", "reason": "short"}, "reason"),
            ("groundVehicle", {"vehicle": "VEH-999", "reason": "Open critical recall"}, "vehicle"),
            ("scheduleRepair", {"workOrder": NORMAL, "scheduledFor": "2026-09-01", "shop": "Franchise dealer"},
             "scheduledFor"),
            ("scheduleRepair", {"workOrder": NORMAL, "scheduledFor": "2026-09-30", "shop": "Some guy"}, "shop"),
        ]
        for action, params, field in cases:
            with self.subTest(action=action, field=field):
                with self.assertRaises(ActionError) as rejected:
                    self.kernel.submit(action, params, LEAD)
                self.assertEqual(rejected.exception.field, field)
        self.assertEqual(self.kernel.edit_log(), [])

    def test_values_of_the_wrong_json_type_are_rejected_not_crashed_on(self):
        cases = [
            ({"vehicle": ["VEH-001"], "reason": "Open critical recall"}, "vehicle"),
            ({"vehicle": {"id": "VEH-001"}, "reason": "Open critical recall"}, "vehicle"),
            ({"vehicle": 1, "reason": "Open critical recall"}, "vehicle"),
            ({"vehicle": "VEH-001", "reason": 1234567890123}, "reason"),
        ]
        for params, field in cases:
            with self.subTest(params=params):
                with self.assertRaises(ActionError) as rejected:
                    self.kernel.submit("groundVehicle", params, LEAD)
                self.assertEqual(rejected.exception.field, field)
        with self.assertRaises(ActionError):
            self.kernel.submit("scheduleRepair", {"workOrder": NORMAL, "scheduledFor": 20260930,
                                                  "shop": "Franchise dealer"}, LEAD)
        for params, actor in [(["vehicle"], LEAD), ("vehicle", LEAD), ({}, "safety_lead"), ({}, {"role": 7})]:
            with self.subTest(params=params, actor=actor):
                with self.assertRaises(ActionError):
                    self.kernel.submit("groundVehicle", params, actor)

    def test_edits_survive_a_pipeline_rebuild(self):
        ground(self.kernel)

        rebuilt = fixture()
        rebuilt["generatedAt"] = "2026-09-22T00:00:00+00:00"
        self.objects.write_text(json.dumps(rebuilt))

        fresh = self.make_kernel()
        self.assertEqual(fresh.get("Vehicle", "VEH-001")["status"], "grounded")
        self.assertEqual(len(fresh.edit_log()), 1)

    def test_every_edit_records_before_and_after(self):
        edit = self.kernel.submit("scheduleRepair", {"workOrder": NORMAL, "scheduledFor": "2026-09-30",
                                                     "shop": "Franchise dealer"}, DEPOT)
        changed = {e["property"]: (e["before"], e["after"]) for e in edit["effects"]}
        self.assertEqual(changed["status"], ("open", "scheduled"))
        self.assertEqual(changed["scheduledFor"], (None, "2026-09-30"))
        self.assertEqual(edit["actor"], DEPOT)

    def test_a_torn_final_line_is_set_aside_and_the_rest_loads(self):
        first = ground(self.kernel)
        with self.edits.open("a") as handle:
            handle.write('{"id": "E-0002", "effe')  # the process died mid-append

        fresh = self.make_kernel()
        self.assertEqual([e["id"] for e in fresh.edit_log()], [first["id"]])
        self.assertIn("torn", fresh.load_warning)
        self.assertTrue((self.dir / "edits.jsonl.torn").exists())
        self.assertTrue(self.edits.read_text().endswith("\n"))

        second = fresh.submit("scheduleRepair", {"workOrder": NORMAL, "scheduledFor": "2026-09-30",
                                                 "shop": "Franchise dealer"}, DEPOT)
        self.assertEqual(second["id"], "E-0002")
        self.assertEqual(len(self.make_kernel().edit_log()), 2)

    def test_damage_before_the_last_line_stops_the_kernel(self):
        ground(self.kernel)
        self.edits.write_text("not json\n" + self.edits.read_text())
        with self.assertRaises(LogCorrupted) as stopped:
            self.make_kernel()
        self.assertIn("line 1", str(stopped.exception))

    def test_a_log_line_of_the_wrong_shape_stops_the_kernel_cleanly(self):
        for bad in ['[1, 2]', '{"id": "E-0001", "effects": "nope"}', '{"id": "E-0001", "effects": [{"after": 1}]}']:
            with self.subTest(line=bad):
                self.edits.write_text(bad + "\n")
                with self.assertRaises(LogCorrupted):
                    self.make_kernel()

    def test_a_reload_warning_clears_once_a_reload_succeeds(self):
        self.objects.write_text(json.dumps(fixture())[:200])  # a half-written rebuild
        self.assertIn("Reload failed", self.kernel.snapshot()["loadWarning"])
        self.assertEqual(self.kernel.snapshot()["objects"]["Vehicle"]["VEH-001"]["status"], "in_service")

        rebuilt = fixture()
        rebuilt["generatedAt"] = "2026-09-22T00:00:00+00:00"
        self.objects.write_text(json.dumps(rebuilt))
        snapshot = self.kernel.snapshot()
        self.assertIsNone(snapshot["loadWarning"])
        self.assertEqual(snapshot["generatedAt"], "2026-09-22T00:00:00+00:00")

    def test_deleting_the_log_while_running_resets_state_and_ids(self):
        ground(self.kernel)
        self.edits.unlink()

        self.assertEqual(self.kernel.snapshot()["objects"]["Vehicle"]["VEH-001"]["status"], "in_service")
        again = ground(self.kernel)
        self.assertEqual(again["id"], "E-0001")
        ids = [json.loads(line)["id"] for line in self.edits.read_text().splitlines()]
        self.assertEqual(ids, ["E-0001"])


if __name__ == "__main__":
    unittest.main()
