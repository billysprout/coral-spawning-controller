"""Tests for tank_lamp (the lamp-delta measurement) with a fake plug, a fake lux
sensor and a fake clock. Nothing touches hardware and nothing really waits.

    python -m unittest tests.test_tank_lamp -v       (from the repo root)
"""
import datetime as dt
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import tank_inputs                      # noqa: E402
import tank_lamp                        # noqa: E402
import tank_plugs                       # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 2, 20, 0, 0, tzinfo=UTC)


class World:
    """A clock that only moves when somebody sleeps, and a sensor on it that
    stamps a new reading every `period_s` seconds from the plug's state."""

    def __init__(self, plug, off_lux=5.0, on_lux=205.0, period_s=10, wobble=0.0,
                 lag_s=0, dead=False, drift=0.0):
        self.t = T0
        self.plug, self.off, self.on = plug, off_lux, on_lux
        self.period, self.wobble, self.lag, self.dead, self.drift = (
            period_s, wobble, lag_s, dead, drift)
        self.switched_at = T0
        self.n = 0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += dt.timedelta(seconds=s)

    def read(self, now):
        if self.dead:
            return tank_inputs.Reading("lux", False, detail="unreachable")
        stamp_s = (now - T0).total_seconds() // self.period * self.period
        ts = T0 + dt.timedelta(seconds=stamp_s)
        lit = self.plug.opts.get("on")
        value = self.on if lit else self.off
        self.n += 1
        value += self.wobble * (1 if self.n % 2 else -1) + self.drift * self.n
        return tank_inputs.Reading("lux", True, value, ts)


class Source:
    def __init__(self, world):
        self.world = world

    def read(self, now):
        return self.world.read(now)


def run(world, **kw):
    out = []
    rep = tank_lamp.measure(world.plug, Source(world), now_fn=world.now,
                            sleep=world.sleep, say=out.append, **kw)
    return rep, out


class SuggestTests(unittest.TestCase):
    def test_a_clear_lamp_gets_forty_percent_of_its_change(self):
        rep = tank_lamp.suggest([5.0, 5.1, 4.9], [205.0, 205.1, 204.9])
        self.assertTrue(rep["usable"])
        self.assertEqual(rep["suggested_min_lux_change"], 80)

    def test_noise_raises_the_floor_above_forty_percent(self):
        rep = tank_lamp.suggest([5, 25, 5, 25, 5, 25], [35, 55, 35, 55, 35, 55])
        # noise ~ 10.954, so 5x ~ 54.8 beats 40% of a 30 lux change
        self.assertTrue(rep["usable"] is False)          # 30 < 2 x 54.8
        self.assertIsNone(rep["suggested_min_lux_change"])

    def test_the_sensor_step_floor_applies_when_there_is_no_noise(self):
        rep = tank_lamp.suggest([5.0, 5.0, 5.0], [7.0, 7.0, 7.0])
        self.assertTrue(rep["usable"])
        self.assertEqual(rep["suggested_min_lux_change"], 0.8)    # 40% of 2.0
        rep = tank_lamp.suggest([5.0, 5.0, 5.0], [5.8, 5.8, 5.8])
        self.assertFalse(rep["usable"])                   # 0.8 < 2 x 0.5

    def test_no_change_or_a_darker_reading_is_not_usable(self):
        for on in ([5.0, 5.0, 5.0], [3.0, 3.0, 3.0]):
            rep = tank_lamp.suggest([5.0, 5.0, 5.0], on)
            self.assertFalse(rep["usable"])
            self.assertIsNone(rep["suggested_min_lux_change"])
            self.assertTrue(rep["warnings"])

    def test_cycles_that_disagree_raise_a_warning(self):
        rep = tank_lamp.suggest([5, 5, 5], [205, 205, 205], [200.0, 100.0])
        self.assertTrue(any("disagree" in w for w in rep["warnings"]))
        rep = tank_lamp.suggest([5, 5, 5], [205, 205, 205], [200.0, 190.0])
        self.assertFalse(any("disagree" in w for w in rep["warnings"]))

    def test_two_significant_figures(self):
        self.assertEqual(tank_lamp.sig2(83.7), 84)
        self.assertEqual(tank_lamp.sig2(0.8312), 0.83)
        self.assertEqual(tank_lamp.sig2(1234.5), 1200)


class MeasureTests(unittest.TestCase):
    def plug(self, on=False, **kw):
        return tank_plugs.FakePlug("lamp", "white", on=on, **kw)

    def test_a_visible_lamp_is_measured_and_the_plug_put_back_off(self):
        p = self.plug(on=False)
        w = World(p, wobble=0.1)
        rep, out = run(w, settle_s=20, sample_s=40, cycles=2)
        self.assertTrue(rep["usable"])
        self.assertAlmostEqual(rep["change_lux"], 200.0, delta=1)
        self.assertEqual(rep["suggested_min_lux_change"], 80)
        self.assertEqual(rep["cycles"], 2)
        self.assertFalse(p.opts["on"])
        self.assertEqual([c[0] for c in p.commands], [False, True, False, True, False])
        self.assertFalse(rep["restored_on"])

    def test_a_plug_found_on_is_put_back_on(self):
        p = self.plug(on=True)
        rep, _ = run(World(p), cycles=1)
        self.assertTrue(p.opts["on"])
        self.assertTrue(rep["restored_on"])

    def test_only_readings_stamped_after_the_settle_time_count(self):
        p = self.plug(on=False)
        w = World(p, off_lux=5.0, on_lux=205.0)
        # a sensor that lags: its stamp is the old one for 15 s after a switch
        original = w.read

        def laggy(now):
            r = original(now)
            ts = r.ts
            if (now - w.switched_at).total_seconds() < 15:
                return tank_inputs.Reading("lux", True, 5.0, ts)   # still the old light
            return r
        w.read = laggy
        rep, _ = run(w, settle_s=20, sample_s=40, cycles=1)
        self.assertAlmostEqual(rep["change_lux"], 200.0, delta=1)

    def test_a_lamp_the_sensor_cannot_see_is_reported_not_usable(self):
        p = self.plug(on=False)
        rep, _ = run(World(p, off_lux=5.0, on_lux=5.0, wobble=0.05))
        self.assertFalse(rep["usable"])
        self.assertIsNone(rep["suggested_min_lux_change"])
        text = tank_lamp.format_report(rep, "white")
        self.assertIn("No threshold suggested", text)
        self.assertIn("put back OFF", text)

    def test_report_gives_a_config_snippet_that_is_valid_json(self):
        p = self.plug(on=False)
        rep, _ = run(World(p))
        text = tank_lamp.format_report(rep, "white")
        line = [x for x in text.splitlines() if x.strip().startswith('{"outputs"')][0]
        cfg = json.loads(line)
        self.assertEqual(cfg["outputs"]["verify"]["channels"]["white"]["min_lux_change"], 80)

    def test_unreachable_plug_sends_nothing(self):
        p = self.plug(ok=False)
        with self.assertRaises(tank_lamp.LampError):
            run(World(p))
        self.assertEqual(p.commands, [])

    def test_unreadable_sensor_sends_nothing(self):
        p = self.plug(on=False)
        with self.assertRaises(tank_lamp.LampError) as ctx:
            run(World(p, dead=True))
        self.assertIn("lux sensor", str(ctx.exception))
        self.assertEqual(p.commands, [])

    def test_too_few_readings_is_an_error_and_the_plug_is_restored(self):
        p = self.plug(on=False)
        with self.assertRaises(tank_lamp.LampError) as ctx:
            run(World(p, period_s=60), settle_s=20, sample_s=40)
        self.assertIn("lux logger", str(ctx.exception))
        self.assertFalse(p.opts["on"])
        self.assertEqual(p.commands[-1], (False, None))

    def test_a_plug_that_will_not_switch_aborts_and_restores(self):
        p = self.plug(on=False, stuck=True)
        with self.assertRaises(tank_lamp.LampError) as ctx:
            run(World(p))
        self.assertIn("did not go", str(ctx.exception))

    def test_an_error_midway_still_puts_the_plug_back(self):
        p = self.plug(on=True)
        w = World(p)
        calls = {"n": 0}
        original = w.read

        def failing(now):
            calls["n"] += 1
            if calls["n"] > 12:
                raise RuntimeError("sensor fell over")
            return original(now)
        w.read = failing
        with self.assertRaises(RuntimeError):
            run(w, cycles=2)
        self.assertTrue(p.opts["on"])                      # found ON, left ON


class CliTests(unittest.TestCase):
    def test_without_yes_nothing_is_switched(self):
        import shutil
        import tempfile
        import tank_tick
        tmp = tempfile.mkdtemp()
        try:
            array = os.path.join(tmp, "plug_array.json")
            with open(array, "w") as fh:
                json.dump({"plugs": [{"name": "plug-8", "driver": "fake",
                                      "role": "unassigned"}]}, fh)
            cfg = os.path.join(tmp, "cfg.json")
            with open(cfg, "w") as fh:
                json.dump({"plug_array": array, "devices": os.path.join(tmp, "d.json"),
                           "extra_plugs": os.path.join(tmp, "e.json")}, fh)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tank_tick.main(["measure-lamp", "--plug", "plug-8", "--config", cfg])
        finally:
            shutil.rmtree(tmp)
        self.assertEqual(rc, 0)
        self.assertIn("Rerun with --yes", buf.getvalue())

    def test_bad_numbers_are_refused(self):
        import tank_tick
        for bad in (["--cycles", "0"], ["--sample-s", "1"], ["--settle-s", "-1"]):
            with self.assertRaises(SystemExit):
                tank_tick.main(["measure-lamp", "--plug", "plug-8"] + bad)


if __name__ == "__main__":
    unittest.main()
