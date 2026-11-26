"""Tests for the output stage: tank_heat (heater gate rules and setpoint ramp),
tank_out (lights and heater actuator) and plug switching in tank_plugs.

Offline. Fake plugs and a fake tinytuya; nothing touches hardware.

    python -m unittest tests.test_tank_out -v        (from the repo root)
"""
import copy
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import tank_heat                        # noqa: E402
import tank_inputs                      # noqa: E402
import tank_out                         # noqa: E402
import cues                             # noqa: E402
import tank_plan                        # noqa: E402
import tank_plugs                       # noqa: E402
import tank_tick                        # noqa: E402

UTC = dt.timezone.utc
# A fixed winter calendar (season offset 0), so the expected clock times do not move with the live calendar.
FIX = os.path.join(ROOT, "tests", "fixtures")
CAL = os.path.join(FIX, "spawn_calendar_2026-11-01.csv")
FIX_PARAMS = os.path.join(FIX, "cue_params_offset0.json")


def utc(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


NOON = utc("2026-11-02T20:00:00Z")      # 12:00 PST: white and blue on
NIGHT = utc("2026-11-03T05:00:00Z")     # 21:00 PST: full dark
H = tank_heat.heater_config({"inkbird_min_c": 26.8, "inkbird_max_c": 27.1})
ALARM_HIGH = 27.78
FLOOR = 22.78           # 73 F, limits.alarm_c[0]


def gate(temp, setpoint=26.0, prev=True, held=None, trusted=True, allow=True, h=H):
    return tank_heat.decide_gate(temp, trusted, setpoint, prev, held, h, allow,
                                 ALARM_HIGH, FLOOR)


class GateRuleTests(unittest.TestCase):
    def test_untrusted_probe_holds_the_gate_on(self):
        for prev in (True, False, None):
            d = gate(None, prev=prev)
            self.assertTrue(d.gate_on)
            self.assertEqual(d.reason, "probe_untrusted")
            self.assertTrue(gate(26.9, prev=prev, trusted=False).gate_on)

    def test_never_blocks_heat_near_the_hard_floor(self):
        # Property: from far below the hard floor up to floor + margin, ON,
        # always, whatever the setpoint, the previous state or the dwell.
        top = FLOOR + H["floor_margin_c"]
        t = 15.0
        while t <= top:
            for prev in (True, False):
                for held in (None, 0.0, 9999.0):
                    for sp in (24.0, 26.0, 28.0):
                        d = gate(t, sp, prev, held)
                        self.assertTrue(d.gate_on, (t, prev, held, sp))
                        self.assertTrue(d.forced)
            t += 0.05

    def test_never_blocks_heat_when_off_is_not_allowed(self):
        t = 15.0
        while t < 35.0:
            for prev in (True, False, None):
                for sp in (23.0, 26.0, 29.0):
                    self.assertTrue(gate(t, sp, prev, 9999.0, allow=False).gate_on,
                                    (t, prev, sp))
            t += 0.1

    def test_hysteresis(self):
        self.assertTrue(gate(25.8, 26.0, prev=False, held=999).gate_on)   # below
        self.assertEqual(gate(25.8, 26.0, prev=False, held=999).reason,
                         "below_setpoint")
        self.assertFalse(gate(26.2, 26.0, prev=True, held=999).gate_on)   # above
        self.assertTrue(gate(26.05, 26.0, prev=True, held=999).gate_on)   # inside
        self.assertFalse(gate(26.05, 26.0, prev=False, held=999).gate_on)
        self.assertEqual(gate(26.05, 26.0, prev=False, held=999).reason,
                         "within_band")

    def test_dwell_holds_a_flip_but_not_a_safety_rule(self):
        d = gate(26.4, 26.0, prev=True, held=10.0)          # wants off
        self.assertTrue(d.gate_on)
        self.assertEqual(d.reason, "dwell")
        self.assertFalse(gate(26.4, 26.0, prev=True, held=500.0).gate_on)
        # a safety rule ignores dwell
        self.assertTrue(gate(23.0, 26.0, prev=False, held=1.0).gate_on)
        self.assertFalse(gate(28.0, 26.0, prev=True, held=1.0).gate_on)

    def test_over_cut_off_forces_off_when_allowed(self):
        d = gate(27.9, 27.0, prev=True, held=0.0)
        self.assertFalse(d.gate_on)
        self.assertEqual(d.reason, "over_cut_off")
        self.assertTrue(gate(27.9, 27.0, allow=False).gate_on)   # not allowed

    def test_config_validation(self):
        for bad in ({"revert_after_s": 10}, {"revert_after_s": 99999},
                    {"on_below_c": 0.0}, {"off_above_c": 5},
                    {"min_dwell_s": 1}, {"floor_margin_c": -1},
                    {"inkbird_min_c": 26.0, "inkbird_max_c": 25.0},
                    {"ramp_c_per_day": 0}):
            with self.assertRaises(ValueError, msg=bad):
                tank_heat.heater_config(bad)


class RampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sp.json")
        self.h = tank_heat.heater_config({})
        self.t0 = utc("2026-11-02T20:00:00Z")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def ramp(self):
        return tank_heat.SetpointRamp(self.path, (23.0, 29.0), self.h)

    def test_starts_from_the_tank_not_the_plan(self):
        # The calendar starts at 26.17; the tank is at 24.5.
        self.assertEqual(self.ramp().step(self.t0, 26.17, 24.5), 24.5)

    def test_moves_toward_plan_no_faster_than_the_rate(self):
        r = self.ramp()
        r.step(self.t0, 26.17, 24.5)
        later = self.t0 + dt.timedelta(minutes=50)      # inside ramp_max_age_s
        v = r.step(later, 26.17, 24.5)
        self.assertAlmostEqual(v - 24.5, 0.1 * (50 * 60 / 86400), 3)
        self.assertLess(v, 24.51)

    def test_walks_down_too(self):
        r = self.ramp()
        r.step(self.t0, 24.0, 26.0)
        v = r.step(self.t0 + dt.timedelta(minutes=50), 24.0, 26.0)
        self.assertLess(v, 26.0)
        self.assertGreater(v, 25.99)

    def test_state_survives_restart_and_stale_state_is_dropped(self):
        r = self.ramp()
        r.step(self.t0, 26.17, 24.5)
        r.step(self.t0 + dt.timedelta(minutes=40), 26.17, 24.5)
        again = self.ramp()                      # restart 10 minutes later
        v = again.step(self.t0 + dt.timedelta(minutes=50), 26.17, 25.0)
        self.assertLess(v, 24.6)                 # kept the old state, not 25.0
        stale = self.ramp()                      # restart a day later
        self.assertEqual(stale.step(self.t0 + dt.timedelta(days=1), 26.17, 25.0),
                         25.0)

    def test_clamped_to_the_safety_band_and_no_probe_falls_back_to_plan(self):
        self.assertEqual(self.ramp().step(self.t0, 26.17, 40.0), 29.0)
        os.remove(self.path)
        self.assertEqual(self.ramp().step(self.t0, 26.17, None), 26.17)


class FakeTinytuyaRW(types.ModuleType):
    def __init__(self, reply=None):
        super().__init__("tinytuya")
        self.sets, self.values = [], []
        outer = self

        class OutletDevice:
            def __init__(self, dev_id, **kw):
                pass

            def set_status(self, on, switch=1):
                outer.sets.append((bool(on), switch))
                return reply

            def set_value(self, index, value):
                outer.values.append((index, value))
                return reply

        self.OutletDevice = OutletDevice


class TinyTuyaSwitchTests(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("tinytuya")

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("tinytuya", None)
        else:
            sys.modules["tinytuya"] = self._saved

    def plug(self, **extra):
        opts = dict(id="d", ip="192.0.2.9", version=3.3)
        opts.update(extra)
        return tank_plugs.TinyTuyaPlug("p", "heater", None, key="K", **opts)

    def test_switch_and_countdown_arm_and_clear(self):
        fake = sys.modules["tinytuya"] = FakeTinytuyaRW()
        p = self.plug(revert_dp=9, power_dp=1)
        p.set_on(False, revert_after_s=600)
        self.assertEqual(fake.sets[-1], (False, 1))
        self.assertEqual(fake.values[-1], (9, 600))        # dead-man armed
        p.set_on(True)
        self.assertEqual(fake.sets[-1], (True, 1))
        self.assertEqual(fake.values[-1], (9, 0))          # dead-man cleared

    def test_no_countdown_without_revert_dp_and_revert_request_is_refused(self):
        fake = sys.modules["tinytuya"] = FakeTinytuyaRW()
        p = self.plug()
        p.set_on(True)
        self.assertEqual(fake.values, [])
        with self.assertRaises(tank_plugs.PlugError):
            p.set_on(False, revert_after_s=600)
        self.assertEqual(fake.sets, [(True, 1)])           # nothing sent for the refusal

    def test_error_reply_raises(self):
        sys.modules["tinytuya"] = FakeTinytuyaRW({"Error": "nope", "Err": "905"})
        with self.assertRaises(tank_plugs.PlugError):
            self.plug().set_on(True)

    def test_reports_remaining_countdown(self):
        class Dev:
            def __init__(self, *a, **k):
                pass

            def status(self):
                return {"dps": {"1": False, "9": 420}}
        mod = types.ModuleType("tinytuya")
        mod.OutletDevice = Dev
        sys.modules["tinytuya"] = mod
        r = self.plug(revert_dp=9).status()
        self.assertEqual(r.revert_remaining_s, 420.0)
        self.assertTrue(self.plug(revert_dp=9).supports_revert)
        self.assertFalse(self.plug().supports_revert)


class CountdownPlug(tank_plugs.Plug):
    """Simulates a plug with a working (or broken) countdown on a virtual clock."""

    def __init__(self, restores=True, unreachable=False, ignores_off=False):
        super().__init__("heat", "heater")
        self.t = 0.0
        self.on = True
        self.deadline = None
        self.restores, self.unreachable, self.ignores_off = (
            restores, unreachable, ignores_off)
        self.restored_at_end = None

    supports_revert = True

    def status(self):
        if self.unreachable:
            return tank_plugs.PlugReading("heat", False, detail="timeout")
        if self.deadline is not None and self.t >= self.deadline and self.restores:
            self.on, self.deadline = True, None
        left = None if self.deadline is None else max(0.0, self.deadline - self.t)
        return tank_plugs.PlugReading("heat", True, on=self.on,
                                      revert_remaining_s=left)

    def set_on(self, on, revert_after_s=None):
        if on:
            self.on, self.deadline = True, None
            self.restored_at_end = True
            return
        if self.ignores_off:
            return
        self.on = False
        self.deadline = (self.t + revert_after_s) if revert_after_s else None


class DispatchTests(unittest.TestCase):
    def test_conductor_forwards_every_plug_array_command(self):
        import conductor
        self.assertEqual(tuple(conductor.TANK_COMMANDS), tuple(tank_tick.COMMANDS))

    def test_verify_countdown_reaches_tank_tick_not_the_legacy_run(self):
        import conductor
        seen = []
        orig = tank_tick.main
        tank_tick.main = lambda argv: seen.append(argv) or 0
        try:
            self.assertEqual(conductor.main(["verify-countdown", "--plug", "x"]), 0)
        finally:
            tank_tick.main = orig
        self.assertEqual(seen, [["verify-countdown", "--plug", "x"]])


class VerifyCountdownTests(unittest.TestCase):
    def run_check(self, plug, seconds=60):
        def sleep(n):
            plug.t += n
        return tank_tick.verify_countdown(plug, seconds, sleep=sleep,
                                          say=lambda *_: None)

    def test_pass_when_the_plug_restores_itself_on_time(self):
        p = CountdownPlug()
        r = self.run_check(p, 60)
        self.assertTrue(r["ok"])
        self.assertTrue(r["timing_ok"])
        self.assertAlmostEqual(r["came_back_after_s"], 60, delta=5)

    def test_fail_and_still_restores_when_the_countdown_does_nothing(self):
        p = CountdownPlug(restores=False)
        r = self.run_check(p, 60)
        self.assertFalse(r["ok"])
        self.assertIn("does NOT restore", r["why"])
        self.assertTrue(p.on)                       # left in the safe state

    def test_unreachable_and_unswitchable_plugs_are_not_verified(self):
        self.assertFalse(self.run_check(CountdownPlug(unreachable=True))["ok"])
        p = CountdownPlug(ignores_off=True)
        self.assertFalse(self.run_check(p)["ok"])
        self.assertTrue(p.on)

    def test_plug_without_countdown_support_is_refused(self):
        r = tank_tick.verify_countdown(tank_plugs.FakePlug("x", "heater",
                                                           supports_revert=False),
                                       60, sleep=lambda n: None, say=lambda *_: None)
        self.assertFalse(r["ok"])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cal = tank_plan.load_calendar(CAL, cues.load_params(FIX_PARAMS))
        self.pcfg = tank_plan.plan_config()
        self.out = copy.deepcopy(tank_out.DEFAULT_OUTPUTS)
        self.out["enabled"] = True
        self.out["min_command_interval_s"] = 30
        self.lim = copy.deepcopy(tank_tick.DEFAULT_CONFIG["limits"])
        self.lim["temp_calibrated"] = True

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def actuator(self, plugs):
        return tank_out.Actuator(self.out, self.lim, plugs, self.tmp)

    def plan(self, when):
        return tank_plan.plan_at(self.cal, self.pcfg, when)

    @staticmethod
    def temp(value, when, age_s=0, ok=True):
        return tank_inputs.Reading("temp", ok, value,
                                   when - dt.timedelta(seconds=age_s))

    def intents(self, plugs, reads, plan):
        t = tank_tick.Tank(copy.deepcopy(tank_tick.DEFAULT_CONFIG), self.cal,
                           plugs, None, None)
        return [t._intent(p, reads[p.name], plan) for p in plugs]

    def apply(self, act, plugs, when, temp=None):
        plan = self.plan(when)
        reads = {p.name: p.status() for p in plugs}
        return act.apply(when, plan, reads, temp or self.temp(26.0, when),
                         self.intents(plugs, reads, plan))

    def audit(self):
        path = os.path.join(self.tmp, "tank_commands.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(x) for x in fh]


class LightActuatorTests(Base):
    def test_master_switch_and_channel_switch(self):
        w = tank_plugs.FakePlug("w", "white", on=False)
        self.out["enabled"] = False
        self.out["channels"]["white"] = True
        self.apply(self.actuator([w]), [w], NOON)
        self.assertEqual(w.commands, [])
        self.out["enabled"] = True
        self.out["channels"]["white"] = False
        self.apply(self.actuator([w]), [w], NOON)
        self.assertEqual(w.commands, [])

    def test_commands_only_a_following_plug_never_unassigned_or_heater(self):
        self.out["channels"].update(white=True, blue=True, moon=True)
        plugs = [tank_plugs.FakePlug("a", "unassigned", on=False),
                 tank_plugs.FakePlug("h", "heater", on=False),
                 tank_plugs.FakePlug("fan", "fan", on=False),
                 tank_plugs.FakePlug("halo", "halo", on=False),     # no follows
                 tank_plugs.FakePlug("w", "white", on=False)]
        self.apply(self.actuator(plugs), plugs, NOON)
        self.assertEqual([p.name for p in plugs if p.commands], ["w"])

    def test_turns_on_verifies_and_audits(self):
        self.out["channels"]["white"] = True
        w = tank_plugs.FakePlug("w", "white", on=False)
        res = self.apply(self.actuator([w]), [w], NOON)
        self.assertEqual(w.commands, [(True, None)])
        self.assertTrue(res["commands"][0]["verified"])
        self.assertEqual(len(self.audit()), 1)
        self.assertEqual(self.audit()[0]["reason"], "plan:white")

    def test_halo_follows_its_channel(self):
        self.out["channels"]["white"] = True
        h = tank_plugs.FakePlug("halo", "halo", "white", on=False)
        self.apply(self.actuator([h]), [h], NOON)
        self.assertEqual(h.commands, [(True, None)])

    def test_unverified_and_failed_commands_alert(self):
        self.out["channels"]["white"] = True
        stuck = tank_plugs.FakePlug("w", "white", on=False, stuck=True)
        res = self.apply(self.actuator([stuck]), [stuck], NOON)
        self.assertIn("command_unverified:w", [f["key"] for f in res["findings"]])
        self.assertTrue(all(f["alert"] for f in res["findings"]))
        bad = tank_plugs.FakePlug("w", "white", on=False, fail_set=True)
        res = self.apply(self.actuator([bad]), [bad], NOON)
        self.assertIn("command_failed:w", [f["key"] for f in res["findings"]])

    def test_offline_plug_is_left_alone(self):
        self.out["channels"]["white"] = True
        w = tank_plugs.FakePlug("w", "white", on=False, ok=False)
        self.apply(self.actuator([w]), [w], NOON)
        self.assertEqual(w.commands, [])

    def test_manual_change_is_respected_then_reasserted(self):
        self.out["channels"]["white"] = True
        w = tank_plugs.FakePlug("w", "white", on=False)
        act = self.actuator([w])
        self.apply(act, [w], NOON)                       # we turn it on
        w.opts["on"] = False                             # someone turns it off
        r1 = self.apply(act, [w], NOON + dt.timedelta(minutes=1))
        self.assertIn("override:w", [f["key"] for f in r1["findings"]])
        r2 = self.apply(act, [w], NOON + dt.timedelta(minutes=10))
        self.assertEqual(len(w.commands), 1)             # still hands off
        self.assertEqual(r2["commands"], [])
        self.apply(act, [w], NOON + dt.timedelta(minutes=32))
        self.assertEqual(len(w.commands), 2)             # hold over: back on
        self.assertTrue(w.opts["on"])

    def test_override_ends_at_the_next_plan_transition(self):
        self.out["channels"]["white"] = True
        w = tank_plugs.FakePlug("w", "white", on=False)
        act = self.actuator([w])
        self.apply(act, [w], NOON)
        w.opts["on"] = False
        self.apply(act, [w], NOON + dt.timedelta(minutes=1))     # override starts
        evening = utc("2026-11-03T01:30:00Z")                    # white is off now
        self.apply(act, [w], evening)
        self.assertEqual(len(w.commands), 1)             # off already matches
        morning = utc("2026-11-03T14:30:00Z")            # white on again
        self.apply(act, [w], morning)
        self.assertEqual(len(w.commands), 2)             # reasserted, no stale hold

    def test_a_lit_light_in_full_dark_is_always_turned_off(self):
        self.out["channels"]["white"] = True
        w = tank_plugs.FakePlug("w", "white", on=True)
        act = self.actuator([w])
        act._last_cmd["w"] = False                       # as if we had set it off
        act._override["w"] = {"until": NIGHT + dt.timedelta(hours=1),
                              "desired": False}
        res = self.apply(act, [w], NIGHT)
        self.assertTrue(self.plan(NIGHT).full_dark)
        self.assertEqual(w.commands, [(False, None)])
        self.assertEqual(res["commands"][0]["reason"], "dark_protect")

    def test_dark_protection_can_be_disabled(self):
        self.out["channels"]["white"] = True
        self.out["protect_dark"] = False
        w = tank_plugs.FakePlug("w", "white", on=True)
        act = self.actuator([w])
        act._last_cmd["w"] = False
        self.apply(act, [w], NIGHT)
        self.assertEqual(w.commands, [])                 # treated as manual

    def test_min_interval_and_hourly_cap(self):
        self.out["channels"]["white"] = True
        w = tank_plugs.FakePlug("w", "white", on=False, fail_set=True)
        act = self.actuator([w])
        self.apply(act, [w], NOON)
        self.apply(act, [w], NOON + dt.timedelta(seconds=10))
        self.assertEqual(len(self.audit()), 1)           # second skipped: too soon
        self.out["max_commands_per_hour"] = 3
        keys = []
        for m in range(1, 6):
            res = self.apply(act, [w], NOON + dt.timedelta(minutes=m))
            keys += [f["key"] for f in res["findings"]]
        self.assertIn("command_rate_limit:w", keys)
        self.assertLessEqual(len(self.audit()), 3)


class LampDeadmanTests(Base):
    """Every ON command to a lamp also arms the plug's own countdown, which
    switches the lamp OFF if the controller stops refreshing it (2026-11-06)."""

    def setUp(self):
        super().setUp()
        self.out["channels"].update(white=True, blue=True)
        self.out["lamp_deadman"] = {"enabled": True, "revert_after_s": 900}

    def at(self, seconds):
        return NOON + dt.timedelta(seconds=seconds)

    def test_config_is_validated(self):
        for bad in ({"revert_after_s": 60}, {"revert_after_s": 9000},
                    {"plugs": "w"}, {"plugs": [1]}):
            with self.assertRaises(ValueError):
                tank_out.deadman_config(bad)
        self.assertFalse(tank_out.deadman_config()["enabled"])

    def test_off_by_default_so_nothing_is_armed(self):
        self.out["lamp_deadman"] = {"enabled": False}
        w = tank_plugs.FakePlug("w", "white", on=False)
        self.apply(self.actuator([w]), [w], NOON)
        self.assertEqual(w.commands, [(True, None)])

    def test_turning_on_arms_the_countdown(self):
        w = tank_plugs.FakePlug("w", "white", on=False)
        res = self.apply(self.actuator([w]), [w], NOON)
        self.assertEqual(w.commands, [(True, 900)])
        self.assertTrue(res["commands"][0]["verified"])
        self.assertEqual(res["commands"][0]["revert_after_s"], 900)

    def test_refresh_only_after_half_the_countdown_and_quietly(self):
        w = tank_plugs.FakePlug("w", "white", on=False)
        act = self.actuator([w])
        self.apply(act, [w], NOON)
        res = self.apply(act, [w], self.at(300))
        self.assertEqual(w.commands, [(True, 900)])              # too early
        res = self.apply(act, [w], self.at(460))
        self.assertEqual(w.commands, [(True, 900), (True, 900)])
        self.assertEqual(res["commands"], [])                    # not a plan command
        self.assertNotIn("rearm_deadman", [e.get("reason") for e in self.audit()])
        self.apply(act, [w], self.at(500))
        self.assertEqual(len(w.commands), 2)                     # the clock restarted

    def test_refreshes_do_not_use_up_the_hourly_allowance(self):
        w = tank_plugs.FakePlug("w", "white", on=False)
        act = self.actuator([w])
        self.apply(act, [w], NOON)
        for k in range(1, 8):
            self.apply(act, [w], self.at(k * 460))
        self.assertGreaterEqual(len(w.commands), 7)
        self.assertEqual(len(act._cmd_times["w"]), 1)

    def test_a_restart_re_arms_a_lamp_that_is_already_on(self):
        w = tank_plugs.FakePlug("w", "white", on=True)
        self.apply(self.actuator([w]), [w], NOON)
        self.assertEqual(w.commands, [(True, 900)])

    def test_the_plug_reporting_a_disarmed_countdown_is_re_armed(self):
        w = tank_plugs.FakePlug("w", "white", on=False)
        act = self.actuator([w])
        self.apply(act, [w], NOON)
        w.opts["off_s"] = 0                                      # cleared by hand
        self.apply(act, [w], self.at(60))
        self.assertEqual(len(w.commands), 2)

    def test_the_backstop_firing_is_not_taken_for_a_hand_switch(self):
        w = tank_plugs.FakePlug("w", "white", on=False)
        act = self.actuator([w])
        self.apply(act, [w], NOON)
        w.opts["on"] = False                                     # its own countdown fired
        res = self.apply(act, [w], self.at(900))
        keys = [f["key"] for f in res["findings"]]
        self.assertIn("lamp_backstop_fired:w", keys)
        self.assertNotIn("override:w", keys)
        self.assertEqual(w.commands[-1], (True, 900))            # back on, re-armed
        self.assertTrue(w.opts["on"])
        self.assertIn("deadman_fired", [e.get("event") for e in self.audit()])

    def test_a_hand_switch_soon_after_arming_is_still_an_override(self):
        w = tank_plugs.FakePlug("w", "white", on=False)
        act = self.actuator([w])
        self.apply(act, [w], NOON)
        w.opts["on"] = False
        res = self.apply(act, [w], self.at(120))
        keys = [f["key"] for f in res["findings"]]
        self.assertIn("override:w", keys)
        self.assertNotIn("lamp_backstop_fired:w", keys)
        self.assertEqual(len(w.commands), 1)

    def test_switching_off_at_the_plan_clears_it(self):
        w = tank_plugs.FakePlug("w", "white", on=True)
        act = self.actuator([w])
        act._armed_at["w"] = NIGHT
        self.apply(act, [w], NIGHT)
        self.assertEqual(w.commands[-1], (False, None))
        self.assertNotIn("w", act._armed_at)

    def test_only_named_plugs_and_only_plugs_that_can_revert(self):
        self.out["lamp_deadman"] = {"enabled": True, "plugs": ["a"]}
        a = tank_plugs.FakePlug("a", "white", on=False)
        b = tank_plugs.FakePlug("b", "blue", on=False)
        self.apply(self.actuator([a, b]), [a, b], NOON)
        self.assertEqual(a.commands, [(True, 900)])
        self.assertEqual(b.commands, [(True, None)])
        self.out["lamp_deadman"] = {"enabled": True}
        c = tank_plugs.FakePlug("c", "white", on=False, supports_revert=False)
        self.apply(self.actuator([c]), [c], NOON)
        self.assertEqual(c.commands, [(True, None)])

    def test_no_refresh_while_a_lit_lamp_is_being_dark_protected(self):
        w = tank_plugs.FakePlug("w", "white", on=True)
        act = self.actuator([w])
        self.apply(act, [w], NIGHT)                  # lit in full dark: forced off
        self.assertEqual(w.commands, [(False, None)])


class HeaterActuatorTests(Base):
    def setUp(self):
        super().setUp()
        # Inkbird heat-on (T1) above the setpoint, as it must be for the Pi to
        # regulate; the wide 73-82 F range is the alarm band, not T1/T2.
        self.out["heater"] = {"enabled": True, "countdown_verified": True,
                              "inkbird_resume_verified": True,
                              "inkbird_min_c": 26.8, "inkbird_max_c": 27.1}

    def seed_setpoint(self, c, when=NOON):
        with open(os.path.join(self.tmp, "tank_setpoint.json"), "w") as fh:
            json.dump({"c": c, "ts": when.isoformat()}, fh)

    def heater(self, **opts):
        return tank_plugs.FakePlug("heat", "heater", **opts)

    def drive(self, plug, temp_value, when=NOON, age_s=0):
        act = self._act
        plugs = [plug]
        reads = {plug.name: plug.status()}
        plan = self.plan(when)
        return act.apply(when, plan, reads,
                         self.temp(temp_value, when, age_s),
                         self.intents(plugs, reads, plan))

    def go(self, plug, temp_value, setpoint=26.0, **kw):
        self.seed_setpoint(setpoint)
        self._act = self.actuator([plug])
        return self.drive(plug, temp_value, **kw)

    def test_blocks_heat_only_when_every_condition_is_met(self):
        h = self.heater(on=True)
        res = self.go(h, 26.4)
        self.assertEqual(h.commands, [(False, 600)])      # off, with the dead-man
        self.assertFalse(res["heater"]["gate_on"])
        self.assertTrue(res["heater"]["allow_off"])
        self.assertEqual(self.audit()[0]["revert_after_s"], 600)

    def test_each_missing_precondition_keeps_the_gate_on(self):
        cases = {
            "countdown not verified": lambda: self.out["heater"].update(
                countdown_verified=False),
            "inkbird resume not verified": lambda: self.out["heater"].update(
                inkbird_resume_verified=False),
            "probe not calibrated": lambda: self.lim.update(temp_calibrated=False),
        }
        for label, mutate in cases.items():
            self.setUp()
            mutate()
            h = self.heater(on=True)
            res = self.go(h, 26.4)
            self.assertEqual(h.commands, [], label)
            self.assertTrue(res["heater"]["gate_on"], label)
            self.assertFalse(res["heater"]["allow_off"], label)
            self.tearDown()
        self.setUp()
        h = self.heater(on=True, supports_revert=False)   # plug lacks a countdown
        res = self.go(h, 26.4)
        self.assertEqual(h.commands, [])
        self.assertFalse(res["heater"]["allow_off"])

    def test_a_gate_found_off_when_off_is_not_allowed_is_switched_on(self):
        self.out["heater"]["countdown_verified"] = False
        h = self.heater(on=False)
        res = self.go(h, 26.4)
        self.assertEqual(h.commands, [(True, None)])
        self.assertTrue(res["commands"][0]["verified"])

    def test_near_the_floor_the_gate_comes_back_on_at_once(self):
        h = self.heater(on=False, revert_s=500)
        self.go(h, 23.0)                                  # <= 22.78 + 0.5
        self.assertEqual(h.commands, [(True, None)])
        self.assertEqual(self.audit()[0]["reason"], "heater:near_floor")

    def test_the_floor_guard_does_not_depend_on_where_the_inkbird_is_set(self):
        # A high Inkbird T1 must not make the guard fire all the time: at 26.4
        # the Pi still gets to block heat.
        h = self.heater(on=True)
        res = self.go(h, 26.4)
        self.assertEqual(res["heater"]["reason"], "above_setpoint")
        self.assertEqual(h.commands, [(False, 600)])

    def test_stale_or_missing_probe_turns_heat_on(self):
        for kwargs in ({"age_s": 1000}, {}):
            self.setUp()
            h = self.heater(on=False)
            if kwargs:
                self.go(h, 26.4, **kwargs)
            else:
                self.seed_setpoint(26.0)
                self._act = self.actuator([h])
                plan = self.plan(NOON)
                reads = {"heat": h.status()}
                self._act.apply(NOON, plan, reads,
                                tank_inputs.Reading("temp", False, detail="x"),
                                self.intents([h], reads, plan))
            self.assertEqual(h.commands, [(True, None)])
            self.tearDown()

    def test_dwell_prevents_rapid_flipping(self):
        h = self.heater(on=True)
        self.seed_setpoint(26.0)
        self._act = self.actuator([h])
        self._act._gate_changed = NOON - dt.timedelta(seconds=10)
        self.drive(h, 26.4)
        self.assertEqual(h.commands, [])

    def test_countdown_is_rearmed_while_the_gate_is_off(self):
        h = self.heater(on=False, revert_s=100)           # 100 < 600 * 0.5
        res = self.go(h, 26.4)
        self.assertEqual(h.commands, [(False, 600)])
        self.assertEqual(res["commands"], [])             # quiet when it worked

    def test_a_disarmed_gate_is_rearmed_at_once(self):
        # OFF with the countdown at 0: a reset plug. Nothing would turn it back on.
        h = self.heater(on=False, revert_s=0)
        self.go(h, 26.4)
        self.assertEqual(h.commands, [(False, 600)])

    def test_an_unknown_arm_time_is_refreshed_once_then_left_alone(self):
        # After a restart the Pi does not know when it last armed the plug, so it
        # refreshes once; then its own record takes over.
        h = self.heater(on=False, revert_s=590)
        self.go(h, 26.4)
        self.assertEqual(h.commands, [(False, 600)])
        h.commands.clear()
        self.drive(h, 26.4, when=NOON + dt.timedelta(seconds=60))
        self.assertEqual(h.commands, [])

    def test_a_countdown_that_does_not_tick_is_rearmed_by_the_pis_own_clock(self):
        # KMC 30153: the countdown data point holds its set value until it fires,
        # so "time left" never falls. The Pi must count the time itself.
        h = self.heater(on=True)
        self.go(h, 26.4)                                   # gate off, armed at NOON
        self.assertEqual(h.commands, [(False, 600)])
        h.commands.clear()
        for secs in (60, 120, 200, 290):                   # well inside half of 600
            self.drive(h, 26.4, when=NOON + dt.timedelta(seconds=secs))
        self.assertEqual(h.commands, [])
        self.assertEqual(h.opts["revert_s"], 600)          # still reads 600, static
        self.drive(h, 26.4, when=NOON + dt.timedelta(seconds=310))
        self.assertEqual(h.commands, [(False, 600)])       # refreshed before it fires
        h.commands.clear()
        self.drive(h, 26.4, when=NOON + dt.timedelta(seconds=400))
        self.assertEqual(h.commands, [])                   # the clock restarted

    def test_gate_off_never_lets_the_countdown_run_out(self):
        h = self.heater(on=True)
        self.go(h, 26.4)
        armed = NOON
        for secs in range(60, 1801, 60):
            n = len(h.commands)
            self.drive(h, 26.4, when=NOON + dt.timedelta(seconds=secs))
            if len(h.commands) > n:
                armed = NOON + dt.timedelta(seconds=secs)
            self.assertLess((NOON + dt.timedelta(seconds=secs) - armed).total_seconds(),
                            600, f"countdown would have fired by t+{secs}s")

    def test_turning_the_gate_on_ignores_the_rate_limits(self):
        self.out["max_commands_per_hour"] = 1
        h = self.heater(on=False)
        self.seed_setpoint(26.0)            # before the actuator loads its state
        act = self.actuator([h])
        act._cmd_times["heat"] = [NOON - dt.timedelta(seconds=5)] * 5
        self._act = act
        res = self.drive(h, 25.8)       # above the floor guard: an ordinary ON
        self.assertEqual(res["heater"]["reason"], "below_setpoint")
        self.assertFalse(res["heater"]["forced"])
        self.assertEqual(h.commands, [(True, None)])

    def test_heater_not_enabled_means_hands_off(self):
        self.out["heater"]["enabled"] = False
        h = self.heater(on=False)
        res = self.go(h, 26.4)
        self.assertEqual(h.commands, [])
        self.assertIsNone(res["heater"])

    def test_warns_when_the_inkbird_heat_on_setting_is_too_low_to_regulate(self):
        h = self.heater(on=True)
        res = self.go(h, 26.4, setpoint=28.0)       # needs T1 above 28.15
        self.assertIn("inkbird_window_low", [f["key"] for f in res["findings"]])
        self.setUp()
        h = self.heater(on=True)
        res = self.go(h, 26.4, setpoint=26.0)       # T1 26.8 is above 26.15
        self.assertNotIn("inkbird_window_low", [f["key"] for f in res["findings"]])
        self.setUp()
        self.out["heater"].update(inkbird_min_c=24.44, inkbird_max_c=24.72)
        h = self.heater(on=True)                     # the 76.0 / 76.5 F setting
        res = self.go(h, 26.4, setpoint=26.0)
        self.assertIn("inkbird_window_low", [f["key"] for f in res["findings"]])

    def test_failed_off_command_does_not_start_the_dwell_clock(self):
        h = self.heater(on=True, fail_set=True)
        res = self.go(h, 26.4)
        self.assertIn("command_failed:heat", [f["key"] for f in res["findings"]])
        self.assertIsNone(self._act._gate_changed)


class TickIntegrationTests(Base):
    def test_enabled_tick_switches_records_and_flags_executed(self):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        cfg["plan"] = tank_plan.plan_config()
        cfg["outputs"]["enabled"] = True
        cfg["outputs"]["channels"]["white"] = True
        w = tank_plugs.FakePlug("w", "white", on=False)
        sink = tank_tick.DryRunSink(self.tmp)
        act = tank_out.Actuator(cfg["outputs"], cfg["limits"], [w], self.tmp)

        class In:
            def read_all(self, now):
                return {"temp": tank_inputs.Reading("temp", True, 26.0, now),
                        "lux": tank_inputs.Reading("lux", True, 0.0, now)}
        sent = []
        t = tank_tick.Tank(cfg, self.cal, [w], In(), sink,
                           notify=lambda m, title="": sent.append(m),
                           deadman=lambda: None, actuator=act)
        rec = t.tick(NOON)
        self.assertTrue(rec["executed"])
        self.assertEqual(len(rec["commands"]), 1)
        self.assertTrue(rec["intents"][0]["executed"])
        self.assertTrue(w.opts["on"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "tank_commands.jsonl")))
        self.assertIn("command", tank_tick._summary(rec) or "commands")
        rec2 = t.tick(NOON + dt.timedelta(minutes=1))
        self.assertFalse(rec2["executed"])                # already correct

    def test_default_config_never_executes(self):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        cfg["plan"] = tank_plan.plan_config()
        w = tank_plugs.FakePlug("w", "white", on=False)
        act = tank_out.Actuator(cfg["outputs"], cfg["limits"], [w], self.tmp)
        t = tank_tick.Tank(cfg, self.cal, [w], tank_inputs.Inputs.from_config({}),
                           tank_tick.DryRunSink(self.tmp), notify=lambda *a, **k: None,
                           deadman=lambda: None, actuator=act)
        rec = t.tick(NOON)
        self.assertFalse(rec["executed"])
        self.assertEqual(w.commands, [])

    def test_bad_heater_config_is_rejected_at_load(self):
        p = os.path.join(self.tmp, "c.json")
        with open(p, "w") as fh:
            json.dump({"outputs": {"heater": {"revert_after_s": 5}}}, fh)
        with self.assertRaises(ValueError):
            tank_tick.load_config(p)


if __name__ == "__main__":
    unittest.main()
