"""Tests for the thermal ladder above the 83 F cap: heat cut-off 83 F, the
"still warming" alarm, fans by 83.5 F, plugs cut and alarm at 84 F.

Offline. Fake plugs and inputs; nothing touches hardware.

    python -m unittest tests.test_tank_thermal -v       (from the repo root)
"""
import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import tank_cool                        # noqa: E402
import tank_heat                        # noqa: E402
import tank_plan                        # noqa: E402
import tank_tick                        # noqa: E402
from tests.test_tank import CAL, FakeInputs, utc   # noqa: E402

F83, F83_5, F84 = 28.33, 28.61, 28.89
NOON = utc("2026-11-02T20:00:00Z")


def mins(n):
    return NOON + dt.timedelta(minutes=n)


class LadderTests(unittest.TestCase):
    def test_defaults_are_the_83_835_84_ladder(self):
        lim = tank_tick.DEFAULT_CONFIG["limits"]
        self.assertEqual(lim["alarm_c"][1], F84)
        self.assertEqual(tank_heat.heater_config({})["cut_off_c"], F83)
        self.assertEqual(tank_cool.cooling_config({})["fans"]["on_by_c"], F83_5)
        self.assertEqual(lim["still_warming"]["above_c"], F83)

    def test_each_step_sits_between_the_neighbours(self):
        self.assertLess(F83, F83_5)
        self.assertLess(F83_5, F84)

    def test_heater_cut_off_is_capped_by_the_alarm_ceiling(self):
        # the actuator passes min(cut_off_c, alarm ceiling)
        h = tank_heat.heater_config({"cut_off_c": 30.0})
        cut = min(h["cut_off_c"], F84)
        self.assertEqual(cut, F84)

    def test_config_validation(self):
        for bad in ({"cut_off_c": 10}, {"cut_off_c": 50}):
            with self.assertRaises(ValueError, msg=bad):
                tank_heat.heater_config(bad)
        for bad in ({"above_c": 5}, {"grace_min": 0}, {"window_min": 1},
                    {"window_min": 999}, {"rise_c": 0.01}):
            with self.assertRaises(ValueError, msg=bad):
                tank_heat.still_warming_config(bad)
        with self.assertRaises(ValueError):
            tank_cool.cooling_config({"fans": {"on_by_c": 99}})
        tank_cool.cooling_config({"fans": {"on_by_c": None}})   # no cap is allowed


class GateAtTheCutOffTests(unittest.TestCase):
    H = tank_heat.heater_config({"inkbird_min_c": 29.0, "inkbird_max_c": 29.3})

    def gate(self, temp, allow=True):
        return tank_heat.decide_gate(temp, True, 28.0, True, 9999.0, self.H,
                                     allow, F83, 22.78)

    def test_gate_is_forced_off_at_83_f_when_off_is_allowed(self):
        d = self.gate(F83)
        self.assertFalse(d.gate_on)
        self.assertEqual(d.reason, "over_cut_off")
        self.assertTrue(d.forced)
        self.assertTrue(self.gate(F83 - 0.01).gate_on is False)   # above the set-point band

    def test_gate_stays_on_if_off_is_not_allowed(self):
        self.assertTrue(self.gate(F83 + 0.5, allow=False).gate_on)


class RiseWatchTests(unittest.TestCase):
    def watch(self, **over):
        return tank_heat.RiseWatch(tank_heat.still_warming_config(over))

    def feed(self, w, series, start=0):
        """series: temperature per minute. Returns the minute of the first hit."""
        for i, v in enumerate(series, start):
            hit = w.step(mins(i), v)
            if hit:
                return i, hit
        return None, None

    def test_below_the_cut_off_nothing_happens(self):
        w = self.watch()
        self.assertEqual(self.feed(w, [F83 - 0.01 + 0.001 * i for i in range(60)]),
                         (None, None))

    def test_a_tank_that_levels_off_at_the_cut_off_is_quiet(self):
        w = self.watch()
        series = [F83 + 0.0625 * min(i, 2) for i in range(90)]   # rises 2 steps, stops
        self.assertEqual(self.feed(w, series), (None, None))

    def test_probe_noise_over_the_cut_off_is_quiet(self):
        w = self.watch()
        series = [F83 + 0.1 + (0.0625 if i % 3 else 0) for i in range(120)]
        self.assertEqual(self.feed(w, series), (None, None))

    def test_a_fast_climb_alerts_after_the_grace_period(self):
        w = self.watch()
        series = [F83 + 0.04 * i for i in range(30)]               # 2.4 C/h
        minute, hit = self.feed(w, series)
        self.assertEqual(minute, 5)                                # grace_min 5
        self.assertGreaterEqual(hit["rise_c"], 0.15)

    def test_a_slow_climb_alerts_within_the_window(self):
        w = self.watch()
        series = [F83 + 0.01 * i for i in range(90)]               # 0.6 C/h
        minute, hit = self.feed(w, series)
        self.assertIsNotNone(minute)
        self.assertLessEqual(minute, 16)                           # 0.15 C / 0.01

    def test_falling_ends_the_episode_and_a_new_one_gets_its_own_grace(self):
        w = self.watch()
        self.feed(w, [F83 + 0.04 * i for i in range(10)])
        self.assertIsNone(w.step(mins(10), F83 - 0.2))             # back under
        self.assertIsNone(w.start)
        # over again: no alert in the first grace_min even while climbing fast
        self.assertEqual(self.feed(w, [F83 + 0.05 * i for i in range(5)], 11),
                         (None, None))

    def test_a_dropped_reading_is_ignored(self):
        w = self.watch()
        self.assertIsNone(w.step(mins(0), F83 + 0.1))
        self.assertIsNone(w.step(mins(1), None))
        self.assertIsNotNone(w.start)

    def test_disabled(self):
        w = self.watch(enabled=False)
        self.assertEqual(self.feed(w, [F83 + 0.1 * i for i in range(30)]),
                         (None, None))


class ConductorStillWarmingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        self.cfg["plan"] = tank_plan.plan_config()
        self.cal = tank_plan.load_calendar(CAL)
        self.sent = []
        self.inputs = FakeInputs(26.0, 0.0)
        self.tank = tank_tick.Tank(
            self.cfg, self.cal, [], self.inputs, tank_tick.DryRunSink(self.tmp),
            notify=lambda msg, title="": self.sent.append(msg),
            deadman=lambda: None)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_series(self, series):
        keys = []
        for i, v in enumerate(series):
            self.inputs.temp = v
            rec = self.tank.tick(mins(i))
            keys.append([f["key"] for f in rec["findings"]])
        return keys

    def test_alert_names_the_problem_once_warming_continues(self):
        keys = self.run_series([F83 + 0.04 * i for i in range(12)])
        hits = [i for i, k in enumerate(keys) if "still_warming" in k]
        self.assertTrue(hits and hits[0] == 5, hits)
        self.assertTrue(any("heat is not stopping" in m for m in self.sent))
        self.assertTrue(any("UNCALIBRATED" in m for m in self.sent))

    def test_no_alert_at_a_steady_83_f(self):
        keys = self.run_series([F83 + 0.01] * 40)
        self.assertFalse(any("still_warming" in k for k in keys))

    def test_stale_readings_are_not_fed_in(self):
        self.inputs.age_s = 600                     # older than input_max_age_s
        keys = self.run_series([F83 + 0.1 * i for i in range(12)])
        self.assertFalse(any("still_warming" in k for k in keys))

    def test_84_f_trips_the_existing_high_alarm(self):
        self.inputs.temp = F84 + 0.05
        rec = self.tank.tick(NOON)
        self.assertIn("temp_high", [f["key"] for f in rec["findings"]])
        self.inputs.temp = F84 - 0.05
        rec = self.tank.tick(mins(1))
        self.assertNotIn("temp_high", [f["key"] for f in rec["findings"]])

    def test_alert_text_is_fahrenheit(self):
        self.inputs.temp = F84 + 0.05
        rec = self.tank.tick(NOON)
        msg = [f["message"] for f in rec["findings"] if f["key"] == "temp_high"][0]
        self.assertIn(" F is above 84.0 F", msg)
        self.assertNotIn(" C", msg)
        self.inputs.temp = 21.2
        rec = self.tank.tick(mins(1))
        msg = [f["message"] for f in rec["findings"] if f["key"] == "temp_low"][0]
        self.assertIn("70.2 F is below 73.0 F", msg)


if __name__ == "__main__":
    unittest.main()
