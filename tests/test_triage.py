import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from triage import assess  # noqa: E402

# Real consequence text from NHTSA recalls affecting the fleet.
CASES = [
    ("A fuel leak in the presence of an ignition source may increase the risk of a fire.", "critical"),
    ("A partially engaged park pawl can result in a vehicle rollaway, increasing the risk of a crash or injury.",
     "critical"),
    ("In the event of a fire, if the fire extinguisher does not function properly, it can increase the risk "
     "of injury.", "normal"),
    ("Fuel pump failure can cause an engine stall while driving, increasing the risk of a crash.", "high"),
    ("An unsecured occupant has an increased risk of injury in the event of a crash.", "high"),
    ("A rearview camera that displays a blank image can reduce the driver's view behind the vehicle.", "normal"),
]


class TriageTest(unittest.TestCase):
    def test_priority_follows_the_mechanism_of_harm(self):
        for consequence, expected in CASES:
            with self.subTest(consequence=consequence[:50]):
                self.assertEqual(assess(consequence)["priority"], expected)

    def test_nhtsa_park_it_flag_outranks_the_text(self):
        verdict = assess("A rearview camera may display a blank image.", park_it=True)
        self.assertEqual(verdict["priority"], "critical")
        self.assertEqual(verdict["rationale"], "NHTSA do-not-drive advisory")

    def test_unrecognized_text_is_escalated_for_a_person_not_filed_as_normal(self):
        # Real text from recall 18V214000: a rollaway, described without the word.
        verdict = assess("If the parking brake is not applied before the vehicle is exited, the vehicle may roll.")
        self.assertTrue(verdict["needsReview"])
        self.assertEqual(verdict["priority"], "high")
        self.assertEqual(verdict["slaDays"], 1)

    def test_power_steering_assist_is_not_a_stall(self):
        # Recall 20V373000.
        verdict = assess("A loss of power steering assist could increase steering effort at low vehicle speeds, "
                         "increasing the risk of a crash.")
        self.assertEqual(verdict["priority"], "high")
        self.assertEqual(verdict["rationale"], "Stability, anti-lock braking or steering assist degraded")

    def test_a_label_defect_is_capped_even_when_the_consequence_is_severe(self):
        # Recall 23V524000: a door label lists the wrong tire size.
        verdict = assess("An incorrectly sized tire may cause premature tread wear, or a tire blow out, "
                         "increasing the risk of a crash.", component="EQUIPMENT:OTHER:LABELS")
        self.assertEqual(verdict["priority"], "normal")
        self.assertEqual(verdict["cappedFrom"], "critical")
        self.assertIn("Tire failure at speed", verdict["signals"])

    def test_nhtsa_advisories_are_never_capped(self):
        verdict = assess("A label may be missing.", component="EQUIPMENT:OTHER:LABELS", park_it=True)
        self.assertEqual(verdict["priority"], "critical")
        self.assertNotIn("cappedFrom", verdict)


if __name__ == "__main__":
    unittest.main()
