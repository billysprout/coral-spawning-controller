"""Tests for the manual `conductor.py switch` helper (offline, FakePlug only).

    python -m unittest tests.test_tank_switch -v     (from the repo root)
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import tank_health                      # noqa: E402
import tank_plugs                       # noqa: E402
import tank_tick                        # noqa: E402


def quiet(*_a, **_k):
    pass


class SwitchPlug(unittest.TestCase):
    def test_off_then_on_reads_back(self):
        p = tank_plugs.FakePlug("p", on=True)
        r = tank_tick.switch_plug(p, False, say=quiet)
        self.assertTrue(r["ok"])
        self.assertIs(r["after_on"], False)
        r = tank_tick.switch_plug(p, True, say=quiet)
        self.assertTrue(r["ok"])
        self.assertEqual(p.commands, [(False, None), (True, None)])

    def test_revert_is_passed_for_off(self):
        p = tank_plugs.FakePlug("p", on=True)
        r = tank_tick.switch_plug(p, False, revert_after_s=30, say=quiet)
        self.assertTrue(r["ok"])
        self.assertEqual(p.commands, [(False, 30)])
        self.assertEqual(r["countdown_s"], 30)

    def test_stuck_plug_is_not_ok(self):
        p = tank_plugs.FakePlug("p", on=True, stuck=True)
        self.assertFalse(tank_tick.switch_plug(p, False, say=quiet)["ok"])

    def test_unreachable_plug_sends_nothing(self):
        p = tank_plugs.FakePlug("p", ok=False)
        r = tank_tick.switch_plug(p, False, say=quiet)
        self.assertFalse(r["ok"])
        self.assertEqual(p.commands, [])

    def test_failed_set_is_reported_not_raised(self):
        p = tank_plugs.FakePlug("p", on=True, fail_set=True)
        r = tank_tick.switch_plug(p, False, say=quiet)
        self.assertFalse(r["ok"])
        self.assertIn("PlugError", r["why"])


class CountdownNotArmed(unittest.TestCase):
    def find(self, **opts):
        role = opts.pop("role", "heater")
        p = tank_plugs.FakePlug("p", role=role, **opts)
        return p, tank_health.countdown_findings([p], {"p": p.status()})

    def test_heater_off_with_countdown_zero_warns(self):
        _, f = self.find(on=False, revert_s=0)
        self.assertEqual([x["key"] for x in f], ["countdown_not_armed:p"])
        self.assertEqual(f[0]["level"], "warn")
        self.assertFalse(f[0]["alert"])

    def test_armed_countdown_is_fine(self):
        self.assertEqual(self.find(on=False, revert_s=600)[1], [])

    def test_on_is_fine(self):
        self.assertEqual(self.find(on=True, revert_s=0)[1], [])

    def test_a_light_off_with_no_countdown_is_normal(self):
        self.assertEqual(self.find(role="white", on=False, revert_s=0)[1], [])

    def test_option_marks_any_plug(self):
        self.assertEqual(len(self.find(role="unassigned", revert_to_on=True,
                                       on=False, revert_s=0)[1]), 1)

    def test_unknown_countdown_is_not_guessed(self):
        self.assertEqual(self.find(on=False)[1], [])

    def test_health_row_and_report_carry_the_warning(self):
        p = tank_plugs.FakePlug("p", role="heater", on=False, revert_s=0)
        rows = tank_health.check_plugs([p])
        self.assertEqual(len(rows[0]["warnings"]), 1)
        self.assertIn("WARNING", tank_health.format_report(rows))


if __name__ == "__main__":
    unittest.main()
