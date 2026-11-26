"""Tests for tank_verify (cue -> action -> verify) and its wiring into tank_out
and the tick. Offline: fake plugs and fake readings only.

    python -m unittest tests.test_tank_verify -v     (from the repo root)
"""
import copy
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import tank_inputs                      # noqa: E402
import tank_out                         # noqa: E402
import tank_plan                        # noqa: E402
import tank_plugs                       # noqa: E402
import tank_tick                        # noqa: E402
import tank_verify                      # noqa: E402
from test_tank_out import Base, NOON, NIGHT   # noqa: E402

S = dt.timedelta(seconds=1)


def lux(value, when, ok=True):
    return tank_inputs.Reading("lux", ok, value, when)


class ConfigTests(unittest.TestCase):
    def test_defaults_judge_nothing_by_lux(self):
        c = tank_verify.verify_config()
        self.assertTrue(c["enabled"])
        self.assertTrue(all(v["min_lux_change"] is None
                            for v in c["channels"].values()))

    def test_bad_values_are_rejected(self):
        for bad in ({"settle_s": 1}, {"window_s": 10},
                    {"settle_s": 100, "window_s": 100},
                    {"channels": {"red": {}}},
                    {"channels": {"white": {"min_lux_change": 0}}}):
            with self.assertRaises(ValueError, msg=bad):
                tank_verify.verify_config(bad)

    def test_tick_config_load_validates(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.json")
            with open(p, "w") as fh:
                json.dump({"outputs": {"verify": {"settle_s": 1}}}, fh)
            with self.assertRaises(ValueError):
                tank_tick.load_config(p)


class ConfoundTests(unittest.TestCase):
    """Two lamps commanded together: one lux sensor cannot judge either."""

    def make(self):
        cfg = tank_verify.verify_config({"channels": {
            "white": {"min_lux_change": 100.0}, "moon": {"min_lux_change": 20.0}}})
        v = tank_verify.Verifier.__new__(tank_verify.Verifier)
        v.cfg = cfg
        v.pending = {}
        return v

    def exp(self, plug, ch, on, at):
        return {"plug": plug, "channel": ch, "desired_on": on, "at": at.isoformat(),
                "lux_before": 100.0, "evidence": []}

    def test_simultaneous_commands_are_confounded_separate_ones_are_not(self):
        t = dt.datetime(2026, 11, 3, 18, 0, tzinfo=dt.timezone.utc)
        v = self.make()
        v.pending["plug-10"] = self.exp("plug-10", "white", True, t)
        v.pending["plug-9"] = self.exp("plug-9", "moon", False, t)
        self.assertTrue(v._confounded(v.pending["plug-9"]))
        v.pending["plug-9"] = self.exp("plug-9", "moon", False, t + dt.timedelta(minutes=4))
        self.assertFalse(v._confounded(v.pending["plug-9"]))
        v.pending["plug-9"] = self.exp("plug-9", "moon", False, t + dt.timedelta(seconds=60))
        self.assertTrue(v._confounded(v.pending["plug-9"]))

    def test_a_confounded_lamp_is_not_failed_by_lux(self):
        class Lux:
            ok, configured, value = True, True, 1200.0
        class Plug:
            name, channel = "plug-9", "moon"
        t = dt.datetime(2026, 11, 3, 18, 0, tzinfo=dt.timezone.utc)
        v = self.make()
        v.pending["plug-10"] = self.exp("plug-10", "white", True, t)
        moon = self.exp("plug-9", "moon", False, t)
        v.pending["plug-9"] = moon
        out = v._evidence(t + dt.timedelta(seconds=400), moon, Plug, None, Lux)
        self.assertEqual([e for e in out if e["kind"] == "lux"], [])      # lux rose, not judged
        v.pending.pop("plug-10")                                          # alone: it is a failure
        out = v._evidence(t + dt.timedelta(seconds=400), moon, Plug, None, Lux)
        self.assertFalse([e for e in out if e["kind"] == "lux"][0]["agrees"])

    def test_confound_window_is_validated(self):
        with self.assertRaises(ValueError):
            tank_verify.verify_config({"confound_s": 900})


class MeasuredLampTests(unittest.TestCase):
    def test_measured_lamp_lux_gives_a_fraction_threshold(self):
        cfg = tank_verify.verify_config({"channels": {"white": {"lamp_lux": 100.0}}})
        self.assertAlmostEqual(tank_verify.effective_min_lux(cfg, "white"), 40.0)
        cfg = tank_verify.verify_config({"channels": {"white": {"lamp_lux": 100.0,
                                                                "fraction": 0.25}}})
        self.assertAlmostEqual(tank_verify.effective_min_lux(cfg, "white"), 25.0)

    def test_an_explicit_threshold_wins_and_nothing_means_no_lux_check(self):
        cfg = tank_verify.verify_config({"channels": {"blue": {"min_lux_change": 3.0,
                                                               "lamp_lux": 100.0}}})
        self.assertEqual(tank_verify.effective_min_lux(cfg, "blue"), 3.0)
        self.assertIsNone(tank_verify.effective_min_lux(cfg, "moon"))
        self.assertIsNone(tank_verify.effective_min_lux({}, "white"))

    def test_bad_measured_values_are_rejected(self):
        for bad in ({"lamp_lux": 0}, {"lamp_lux": -2}, {"fraction": 0.01}, {"fraction": 1.5}):
            with self.assertRaises(ValueError):
                tank_verify.verify_config({"channels": {"white": bad}})


class VerifyBase(Base):
    def setUp(self):
        super().setUp()
        self.out["channels"]["white"] = True
        self.out["verify"] = {"settle_s": 60, "window_s": 300,
                              "channels": {"white": {"min_lux_change": 50}}}
        self.w = tank_plugs.FakePlug("w", "white", on=False)

    def run_apply(self, act, when, lux_reading=None, plugs=None):
        plugs = plugs or [self.w]
        plan = self.plan(when)
        reads = {p.name: p.status() for p in plugs}
        return act.apply(when, plan, reads, self.temp(26.0, when),
                         self.intents(plugs, reads, plan), lux=lux_reading)

    @staticmethod
    def verdicts(act):
        return {n: v["verdict"] for n, v in act.verify.last.items()}


class EffectTests(VerifyBase):
    def test_command_leaves_an_expectation(self):
        act = self.actuator([self.w])
        self.run_apply(act, NOON, lux(10.0, NOON))
        self.assertIn("w", act.verify.pending)
        self.assertEqual(act.verify.pending["w"]["lux_before"], 10.0)

    def test_lux_rise_confirms_but_not_before_settle(self):
        act = self.actuator([self.w])
        self.run_apply(act, NOON, lux(10.0, NOON))
        t = NOON + 30 * S
        self.run_apply(act, t, lux(500.0, t))
        self.assertEqual(self.verdicts(act), {})                 # too early
        t = NOON + 90 * S
        res = self.run_apply(act, t, lux(500.0, t))
        self.assertEqual(self.verdicts(act), {"w": "confirmed"})
        self.assertFalse([f for f in res["findings"]
                          if f["key"].startswith("effect")])

    def test_no_rise_by_the_window_is_a_failure_and_an_alert(self):
        act = self.actuator([self.w])
        self.run_apply(act, NOON, lux(10.0, NOON))
        t = NOON + 100 * S
        self.run_apply(act, t, lux(11.0, t))
        self.assertEqual(self.verdicts(act), {})                 # still waiting
        t = NOON + 310 * S
        res = self.run_apply(act, t, lux(11.0, t))
        self.assertEqual(self.verdicts(act), {"w": "failed"})
        f = [x for x in res["findings"] if x["key"] == "effect_failed:w"]
        self.assertEqual(len(f), 1)
        self.assertTrue(f[0]["alert"])
        self.assertIn("lux", f[0]["message"])

    def test_off_command_expects_a_fall(self):
        w = tank_plugs.FakePlug("w", "white", on=True)
        act = self.actuator([w])
        self.run_apply(act, NIGHT, lux(400.0, NIGHT), plugs=[w])
        self.assertIs(act.verify.pending["w"]["desired_on"], False)
        t = NIGHT + 90 * S
        self.run_apply(act, t, lux(20.0, t), plugs=[w])
        self.assertEqual(self.verdicts(act), {"w": "confirmed"})

    def test_channel_without_a_threshold_is_not_judged_by_lux(self):
        self.out["verify"]["channels"]["white"]["min_lux_change"] = None
        act = self.actuator([self.w])
        self.run_apply(act, NOON, lux(10.0, NOON))
        t = NOON + 310 * S
        self.run_apply(act, t, lux(10.0, t))
        self.assertEqual(self.verdicts(act), {"w": "plug_only"})

    def test_missing_lux_sensor_falls_back_to_plug_only(self):
        act = self.actuator([self.w])
        self.run_apply(act, NOON, lux(0.0, NOON, ok=False))
        t = NOON + 310 * S
        self.run_apply(act, t, lux(0.0, t, ok=False))
        self.assertEqual(self.verdicts(act), {"w": "plug_only"})

    def test_watts_that_disagree_fail_on_the_next_tick(self):
        self.out["verify"]["channels"]["white"]["min_lux_change"] = None
        w = tank_plugs.FakePlug("w", "white", on=False, power_w=0.0)
        act = self.actuator([w])
        self.run_apply(act, NOON, None, plugs=[w])
        w.opts["power_w"] = 0.2           # told ON, reads ON, draws nothing
        res = self.run_apply(act, NOON + 60 * S, None, plugs=[w])
        self.assertEqual(self.verdicts(act), {"w": "failed"})
        self.assertTrue(any(f["key"] == "effect_failed:w" and f["alert"]
                            for f in res["findings"]))

    def test_watts_that_agree_confirm_at_the_window(self):
        self.out["verify"]["channels"]["white"]["min_lux_change"] = None
        w = tank_plugs.FakePlug("w", "white", on=False, power_w=0.0)
        act = self.actuator([w])
        self.run_apply(act, NOON, None, plugs=[w])
        w.opts["power_w"] = 190.0
        self.run_apply(act, NOON + 60 * S, None, plugs=[w])
        self.assertEqual(self.verdicts(act), {})
        self.run_apply(act, NOON + 310 * S, None, plugs=[w])
        self.assertEqual(self.verdicts(act), {"w": "confirmed"})

    def test_plug_that_changes_is_reported_not_failed(self):
        act = self.actuator([self.w])
        self.run_apply(act, NOON, lux(10.0, NOON))
        self.w.opts["on"] = False         # someone switched it off by hand
        t = NOON + 60 * S
        res = self.run_apply(act, t, lux(10.0, t))
        self.assertEqual(self.verdicts(act), {"w": "plug_changed"})
        self.assertFalse(any(f["key"].startswith("effect_failed")
                             for f in res["findings"]))

    def test_new_command_supersedes_an_unjudged_one(self):
        act = self.actuator([self.w])
        self.run_apply(act, NOON, lux(10.0, NOON))
        act.verify.expect(NOON + 10 * S, self.w, False, "test",
                          lux(10.0, NOON), 0)
        self.assertEqual(self.verdicts(act), {"w": "superseded"})
        self.assertIs(act.verify.pending["w"]["desired_on"], False)

    def test_expectation_survives_a_restart(self):
        act = self.actuator([self.w])
        self.run_apply(act, NOON, lux(10.0, NOON))
        act2 = self.actuator([self.w])                 # fresh process, same folder
        self.assertIn("w", act2.verify.pending)
        t = NOON + 90 * S
        self.run_apply(act2, t, lux(500.0, t))
        self.assertEqual(self.verdicts(act2), {"w": "confirmed"})

    def test_verdict_is_audited(self):
        act = self.actuator([self.w])
        self.run_apply(act, NOON, lux(10.0, NOON))
        t = NOON + 90 * S
        self.run_apply(act, t, lux(500.0, t))
        events = [e for e in self.audit() if e.get("event") == "verify"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["verdict"], "confirmed")
        self.assertEqual(events[0]["plug"], "w")

    def test_disabled_verification_leaves_nothing(self):
        self.out["verify"]["enabled"] = False
        act = self.actuator([self.w])
        self.run_apply(act, NOON, lux(10.0, NOON))
        self.assertEqual(act.verify.pending, {})

    def test_still_judges_after_outputs_are_switched_off(self):
        act = self.actuator([self.w])
        self.run_apply(act, NOON, lux(10.0, NOON))
        act.cfg["enabled"] = False
        t = NOON + 90 * S
        self.run_apply(act, t, lux(500.0, t))
        self.assertEqual(self.verdicts(act), {"w": "confirmed"})

    def test_command_entry_carries_the_cue_lag(self):
        act = self.actuator([self.w])
        res = self.run_apply(act, NOON, lux(10.0, NOON))
        self.assertEqual(res["commands"][0]["cue_lag_s"], 0.0)


class CueLagTests(unittest.TestCase):
    def test_clock_starts_at_first_disagreement_and_clears_on_agreement(self):
        v = tank_verify.Verifier({}, "/nonexistent-tank-verify", [])
        t0 = dt.datetime(2026, 11, 2, 20, 0, tzinfo=dt.timezone.utc)
        v.note_cue("w", True, False, t0)
        v.note_cue("w", True, False, t0 + 60 * S)
        self.assertEqual(v.cue_lag_s("w", True, t0 + 90 * S), 90.0)
        self.assertEqual(v.cue_lag_s("w", False, t0 + 90 * S), 0.0)
        v.note_cue("w", True, True, t0 + 120 * S)
        self.assertEqual(v.cue_lag_s("w", True, t0 + 130 * S), 0.0)


class TickWiringTests(VerifyBase):
    def test_tick_record_has_the_verify_block_and_sends_the_alert(self):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        cfg["plan"] = tank_plan.plan_config()
        cfg["outputs"]["enabled"] = True
        cfg["outputs"]["channels"]["white"] = True
        cfg["outputs"]["verify"] = self.out["verify"]
        w = self.w
        act = tank_out.Actuator(cfg["outputs"], cfg["limits"], [w], self.tmp)

        class In:
            def read_all(self, now):
                return {"temp": tank_inputs.Reading("temp", True, 26.0, now),
                        "lux": tank_inputs.Reading("lux", True, 10.0, now)}
        sent = []
        t = tank_tick.Tank(cfg, self.cal, [w], In(),
                           tank_tick.DryRunSink(self.tmp),
                           notify=lambda m, title="": sent.append(m),
                           deadman=lambda: None, actuator=act)
        r1 = t.tick(NOON)
        self.assertTrue(r1["executed"])
        self.assertEqual(r1["verify"]["pending"][0]["plug"], "w")
        t.tick(NOON + 100 * S)
        r3 = t.tick(NOON + 310 * S)
        self.assertEqual(r3["verify"]["last"]["w"]["verdict"], "failed")
        self.assertTrue(any("effect_failed" in f["key"] for f in r3["findings"]))
        self.assertTrue(any("effect was not seen" in m for m in sent))


if __name__ == "__main__":
    unittest.main()
