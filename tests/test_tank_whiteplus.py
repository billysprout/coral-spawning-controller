"""Tests for the white_plus channel: the second light step that is on only for
the brighter hours of the simulated day (half-sine over the lit window).

    python -m unittest tests.test_tank_whiteplus -v     (from the repo root)
"""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import cues                             # noqa: E402
import tank_plan                        # noqa: E402
import tank_plugs                       # noqa: E402
from tests.test_tank_out import Base, NIGHT, utc   # noqa: E402

# A fixed winter calendar (season offset 0), so the expected clock times do not move with the live calendar.
FIX = os.path.join(ROOT, "tests", "fixtures")
CAL = os.path.join(FIX, "spawn_calendar_2026-11-01.csv")
FIX_PARAMS = os.path.join(FIX, "cue_params_offset0.json")
UTC = dt.timezone.utc
DAY = dt.date(2026, 11, 2)       # PST: white 06:02 to 17:10 local


def local(hhmm):
    h, m = hhmm.split(":")
    midnight = dt.datetime(2026, 11, 2, 8, 0, tzinfo=UTC)     # PST = UTC-8
    return midnight + dt.timedelta(hours=int(h), minutes=int(m))


class DayCurveTests(unittest.TestCase):
    def test_half_sine_over_the_window(self):
        iv = tank_plan.Interval(utc("2026-11-02T14:00:00Z"),
                                utc("2026-11-03T00:00:00Z"), "x")
        c = lambda s: tank_plan.day_curve(utc(s), iv)
        self.assertEqual(c("2026-11-02T13:59:00Z"), 0.0)            # before
        self.assertEqual(c("2026-11-02T14:00:00Z"), 0.0)            # sunrise
        self.assertAlmostEqual(c("2026-11-02T19:00:00Z"), 1.0, 3)   # the middle
        self.assertAlmostEqual(c("2026-11-02T16:00:00Z"),
                               c("2026-11-02T22:00:00Z"), 3)        # symmetric
        self.assertEqual(c("2026-11-03T00:00:00Z"), 0.0)            # after
        self.assertEqual(tank_plan.day_curve(utc("2026-11-02T19:00:00Z"), None), 0.0)


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = tank_plan.load_calendar(CAL, cues.load_params(FIX_PARAMS))

    def at(self, hhmm, **over):
        cfg = tank_plan.plan_config({"white_plus": over} if over else None)
        return tank_plan.plan_at(self.cal, cfg, local(hhmm))

    def test_on_only_in_the_brighter_middle_of_the_lit_day(self):
        self.assertTrue(self.at("06:30").white_on)
        self.assertFalse(self.at("06:30").white_plus_on)       # white up, plus not yet
        self.assertFalse(self.at("07:30").white_plus_on)       # curve under 0.5
        self.assertTrue(self.at("12:00").white_plus_on)
        self.assertFalse(self.at("16:30").white_plus_on)       # fading again
        self.assertFalse(self.at("20:00").white_plus_on)

    def test_flips_on_and_off_once_a_day_symmetrically(self):
        cfg = tank_plan.plan_config()
        ev = [(w, v) for w, k, v in tank_plan.day_events(self.cal, cfg, DAY)
              if k == "white_plus_on"]
        self.assertEqual([v for _, v in ev], [True, False])
        (on, _), (off, _) = ev
        # white runs 06:02 to 17:10, so the 0.5 points sit 1/6 in from each end
        # (on from 07:54; 15:19 is the first minute it is off again)
        self.assertEqual((on, off), ("07:54", "15:19"))

    def test_never_on_without_white_or_in_full_dark(self):
        t = utc("2026-11-02T00:00:00Z")
        for i in range(0, 24 * 60, 5):
            p = tank_plan.plan_at(self.cal, tank_plan.plan_config(),
                                  t + dt.timedelta(minutes=i))
            if p.white_plus_on:
                self.assertTrue(p.white_on and not p.full_dark)

    def test_threshold_moves_the_window(self):
        wide = self.at("07:30", threshold=0.2)
        self.assertTrue(wide.white_plus_on)
        tight = self.at("10:00", threshold=0.95)
        self.assertFalse(tight.white_plus_on)
        self.assertTrue(self.at("12:00", threshold=0.95).white_plus_on)

    def test_can_be_disabled_and_is_validated(self):
        self.assertFalse(self.at("12:00", enabled=False).white_plus_on)
        for bad in (0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                tank_plan.plan_config({"white_plus": {"threshold": bad}})

    def test_level_reports_the_simulated_curve(self):
        self.assertAlmostEqual(self.at("11:36").white_plus_level, 1.0, 2)
        self.assertEqual(self.at("20:00").white_plus_level, 0.0)


class PlugTests(unittest.TestCase):
    def test_hyphen_and_underscore_are_the_same_channel(self):
        a = tank_plugs.FakePlug("a", "white-plus")
        b = tank_plugs.FakePlug("b", "halo", "white-plus")
        c = tank_plugs.FakePlug("c", "white_plus")
        self.assertEqual((a.channel, b.channel, c.channel), ("white_plus",) * 3)

    def test_unknown_follows_still_rejected(self):
        with self.assertRaises(ValueError):
            tank_plugs.FakePlug("x", "halo", "white-minus")


class ActuatorTests(Base):
    def test_off_by_default_then_follows_the_curve_when_enabled(self):
        p = tank_plugs.FakePlug("plus", "white_plus", on=False)
        act = self.actuator([p])
        self.apply(act, [p], local("12:00"))
        self.assertEqual(p.commands, [])                  # channel switch is off
        self.out["channels"]["white_plus"] = True
        act = self.actuator([p])
        res = self.apply(act, [p], local("12:00"))
        self.assertEqual(p.commands, [(True, None)])
        self.assertEqual(res["commands"][0]["reason"], "plan:white_plus")
        self.apply(act, [p], local("16:45"))               # curve has dropped
        self.assertEqual(p.commands[-1], (False, None))

    def test_dark_protection_covers_it(self):
        self.out["channels"]["white_plus"] = True
        p = tank_plugs.FakePlug("plus", "white_plus", on=True)
        act = self.actuator([p])
        act._last_cmd["plus"] = False
        act._override["plus"] = {"until": NIGHT + dt.timedelta(hours=1),
                                 "desired": False}
        res = self.apply(act, [p], NIGHT)
        self.assertTrue(self.plan(NIGHT).full_dark)
        self.assertEqual(p.commands, [(False, None)])
        self.assertEqual(res["commands"][0]["reason"], "dark_protect")


if __name__ == "__main__":
    unittest.main()
