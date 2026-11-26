"""Tests for the over-temperature cutoff and the cooling-fan stub: the pure rules
in tank_cool and their use by tank_out.Actuator.

Offline. Fake plugs; nothing touches hardware.

    python -m unittest tests.test_tank_cool -v       (from the repo root)
"""
import copy
import datetime as dt
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import tank_cool                        # noqa: E402
import tank_inputs                      # noqa: E402
import tank_out                         # noqa: E402
import tank_plan                        # noqa: E402
import tank_plugs                       # noqa: E402
import tank_tick                        # noqa: E402
from tests.test_tank_out import Base, NOON, utc   # noqa: E402

ALARM_HIGH = 28.89                      # tank_tick's default limits.alarm_c[1] (84 F)
C = tank_cool.cooling_config({"cutoff_above_c": 1.0})
CUTOFF = 26.0                           # set point 25.0 + n 1.0 in the actuator tests


def mins(n):
    return NOON + dt.timedelta(minutes=n)


def decide(temp, tripped=False, off_s=None, trusted=True, cutoff=CUTOFF, c=C):
    return tank_cool.decide_cutoff(temp, trusted, cutoff, tripped, off_s, c)


class CutoffRuleTests(unittest.TestCase):
    def test_cuts_at_and_above_the_cutoff_whatever_the_state(self):
        # Property: at or above the cutoff with a fresh probe, always cut, no
        # matter whether it was already cut or for how long.
        t = CUTOFF
        while t < 35.0:
            for tripped in (False, True):
                for off_s in (None, 0.0, 10.0, 99999.0):
                    d = decide(t, tripped, off_s)
                    self.assertTrue(d.cut, (t, tripped, off_s))
            t += 0.1

    def test_below_the_cutoff_nothing_new_is_cut(self):
        self.assertFalse(decide(CUTOFF - 0.01).cut)
        self.assertEqual(decide(CUTOFF - 0.01).reason, "ok")

    def test_release_needs_the_band_and_the_minimum_off_time(self):
        resume = CUTOFF - C["resume_band_c"]
        self.assertTrue(decide(resume + 0.01, True, 9999).cut)          # not cool enough
        self.assertEqual(decide(resume + 0.01, True, 9999).reason, "cooling_down")
        self.assertTrue(decide(resume - 0.5, True, C["min_off_s"] - 1).cut)  # too soon
        self.assertEqual(decide(resume - 0.5, True, 10).reason, "min_off")
        d = decide(resume - 0.01, True, C["min_off_s"])
        self.assertFalse(d.cut)
        self.assertEqual(d.reason, "cooled")

    def test_lost_probe_never_starts_a_cut_and_holds_one_for_a_while(self):
        self.assertFalse(decide(None, False, trusted=False).cut)
        self.assertFalse(decide(99.0, False, trusted=False).cut)    # untrusted value
        self.assertTrue(decide(None, True, 60, trusted=False).cut)
        self.assertEqual(decide(None, True, 60, trusted=False).reason,
                         "probe_lost_hold")
        late = C["stale_release_min"] * 60
        d = decide(None, True, late, trusted=False)
        self.assertFalse(d.cut)
        self.assertEqual(d.reason, "probe_lost_release")

    def test_cutoff_is_capped_at_the_alarm_ceiling_and_survives_no_setpoint(self):
        self.assertEqual(tank_cool.cutoff_c(25.0, 1.0, ALARM_HIGH), 26.0)
        self.assertEqual(tank_cool.cutoff_c(28.0, 1.0, ALARM_HIGH), ALARM_HIGH)
        self.assertEqual(tank_cool.cutoff_c(None, 1.0, ALARM_HIGH), ALARM_HIGH)

    def test_reference_sources(self):
        self.assertEqual(tank_cool.reference_c("plan", 26.2, 24.9), 26.2)
        self.assertEqual(tank_cool.reference_c("ramp", 26.2, 24.9), 24.9)
        self.assertEqual(tank_cool.reference_c("ramp", 26.2, None), 26.2)
        self.assertEqual(tank_cool.reference_c(25, 26.2, 24.9), 25.0)
        self.assertIsNone(tank_cool.reference_c("plan", None, None))

    def test_config_validation(self):
        for bad in ({"cutoff_above_c": 0.1}, {"cutoff_above_c": 9},
                    {"resume_band_c": 0.0}, {"resume_band_c": 1.0},
                    {"min_off_s": 5}, {"stale_release_min": 1},
                    {"plugs": "plug-8"}, {"setpoint": "inkbird"},
                    {"setpoint": True},
                    {"fans": {"driver": "gpio"}},
                    {"fans": {"on_above_c": 1.0}},
                    {"fans": {"on_above_c": 0.0}},
                    {"fans": {"band_c": 0.9}}):
            with self.assertRaises(ValueError, msg=bad):
                tank_cool.cooling_config(bad)

    def test_fans_default_to_half_of_n(self):
        self.assertEqual(tank_cool.cooling_config({"cutoff_above_c": 1.0})
                         ["fans"]["on_above_c"], 0.5)
        self.assertEqual(tank_cool.cooling_config({"cutoff_above_c": 2.4})
                         ["fans"]["on_above_c"], 1.2)
        self.assertEqual(tank_cool.cooling_config({"fans": {"on_above_c": 0.7}})
                         ["fans"]["on_above_c"], 0.7)


class FanRuleTests(unittest.TestCase):
    F = tank_cool.cooling_config({"cutoff_above_c": 1.0})["fans"]   # on at +0.5

    def fans(self, temp, was_on, trusted=True, ref=25.0):
        return tank_cool.decide_fans(temp, trusted, ref, was_on, self.F)

    def test_hysteresis(self):
        self.assertTrue(self.fans(25.5, False))
        self.assertFalse(self.fans(25.49, False))
        self.assertTrue(self.fans(25.4, True))      # inside the band: keep
        self.assertFalse(self.fans(25.3, True))     # band_c 0.2 below on_at

    def test_no_probe_or_no_setpoint_keeps_the_state(self):
        for was in (True, False):
            self.assertEqual(self.fans(None, was), was)
            self.assertEqual(self.fans(30.0, was, trusted=False), was)

    def test_fans_come_on_by_the_absolute_cap_whatever_the_setpoint(self):
        cap = self.F["on_by_c"]
        self.assertEqual(cap, 28.61)                             # 83.5 F
        # cap-day target 28.33 + 0.5 would be 28.83: the cap wins
        self.assertTrue(self.fans(cap, False, ref=28.33))
        self.assertFalse(self.fans(cap - 0.01, False, ref=28.33))
        # a cooler day still uses the set point rule
        self.assertTrue(self.fans(26.5, False, ref=26.0))
        # no set point: only the cap applies
        self.assertTrue(self.fans(cap, False, ref=None))
        self.assertFalse(self.fans(cap - 0.5, False, ref=None))
        # no cap and no set point: keep the state
        nocap = dict(self.F, on_by_c=None)
        for was in (True, False):
            self.assertEqual(tank_cool.decide_fans(30.0, True, None, was, nocap), was)
        self.assertEqual(tank_cool.fan_on_at_c(None, nocap), None)

    def test_the_stub_records_and_switches_nothing(self):
        fan = tank_cool.make_fan(tank_cool.DEFAULT_COOLING["fans"])
        self.assertEqual(fan.driver, "stub")
        self.assertFalse(fan.actuates)
        fan.set_on(True)
        self.assertTrue(fan.wanted)
        base = tank_cool.Fan()
        with self.assertRaises(tank_cool.FansUnavailable):
            base.set_on(True)


class CoolingBase(Base):
    def setUp(self):
        super().setUp()
        self.out["cooling"] = {"enabled": True, "plugs": ["plug-8"],
                               "setpoint": 25.0, "cutoff_above_c": 1.0,
                               "resume_band_c": 0.5, "min_off_s": 300,
                               "stale_release_min": 60}

    def plug(self, on=True, **kw):
        return tank_plugs.FakePlug("plug-8", "unassigned", on=on, **kw)

    def go(self, act, plugs, when, value, **kw):
        return self.apply(act, plugs, when, temp=self.temp(value, when, **kw))

    def keys(self, res):
        return [f["key"] for f in res["findings"]]

    def events(self):
        return [e.get("event") for e in self.audit() if "event" in e]


class CutoffActuatorTests(CoolingBase):
    def test_trips_cuts_audits_and_alerts(self):
        p = self.plug()
        act = self.actuator([p])
        res = self.go(act, [p], NOON, 26.1)
        self.assertEqual(p.commands, [(False, None)])
        self.assertTrue(res["commands"][0]["verified"])
        self.assertEqual(res["commands"][0]["reason"], "overtemp")
        self.assertIn("overtemp_cut:plug-8", self.keys(res))
        f = [x for x in res["findings"] if x["key"] == "overtemp_cut:plug-8"][0]
        self.assertTrue(f["alert"])
        self.assertTrue(res["cooling"]["plugs"]["plug-8"]["tripped"])
        self.assertEqual(res["cooling"]["cutoff_c"], 26.0)
        self.assertIn("overtemp_trip", self.events())
        with open(os.path.join(self.tmp, "tank_overtemp.json")) as fh:
            self.assertTrue(json.load(fh)["plug-8"]["was_on"])

    def test_holds_while_warm_then_restores_when_cooled(self):
        p = self.plug()
        act = self.actuator([p])
        self.go(act, [p], NOON, 26.1)
        res = self.go(act, [p], mins(2), 25.8)          # above resume 25.5
        self.assertEqual(p.commands, [(False, None)])   # nothing new
        self.assertTrue(res["cooling"]["plugs"]["plug-8"]["tripped"])
        res = self.go(act, [p], mins(3), 25.4)          # cool, but only 3 min off
        self.assertEqual(p.commands, [(False, None)])
        self.assertEqual(res["cooling"]["plugs"]["plug-8"]["reason"], "min_off")
        res = self.go(act, [p], mins(6), 25.4)          # cool and off > 300 s
        self.assertEqual(p.commands, [(False, None), (True, None)])
        self.assertFalse(res["cooling"]["plugs"]["plug-8"]["tripped"])
        self.assertIn("overtemp_released:plug-8", self.keys(res))
        self.assertIn("overtemp_release", self.events())
        with open(os.path.join(self.tmp, "tank_overtemp.json")) as fh:
            self.assertEqual(json.load(fh), {})

    def test_cuts_again_if_it_warms_up_again(self):
        p = self.plug()
        act = self.actuator([p])
        self.go(act, [p], NOON, 26.1)
        self.go(act, [p], mins(10), 25.0)               # restored
        self.assertTrue(p.opts["on"])
        self.go(act, [p], mins(20), 26.3)
        self.assertFalse(p.opts["on"])

    def test_plug_that_was_off_is_not_commanded_either_way(self):
        p = self.plug(on=False)
        act = self.actuator([p])
        self.go(act, [p], NOON, 26.5)
        self.go(act, [p], mins(10), 25.0)
        self.assertEqual(p.commands, [])
        self.assertEqual(act._trips, {})

    def test_a_hand_switch_on_while_cut_is_put_back_off(self):
        p = self.plug()
        act = self.actuator([p])
        self.go(act, [p], NOON, 26.1)
        p.opts["on"] = True                              # someone taps the app
        self.go(act, [p], mins(1), 26.1)
        self.assertEqual(p.commands, [(False, None), (False, None)])
        self.assertFalse(p.opts["on"])

    def test_alarm_ceiling_cuts_even_with_a_high_setpoint(self):
        self.out["cooling"]["setpoint"] = 28.0           # 28.0 + 1.0 > ceiling
        p = self.plug()
        act = self.actuator([p])
        self.go(act, [p], NOON, 28.8)
        self.assertTrue(p.opts["on"])
        self.go(act, [p], mins(1), ALARM_HIGH)
        self.assertFalse(p.opts["on"])

    def test_a_cap_day_target_is_cut_above_it_not_below(self):
        # a warm calendar target (28.33 C, the 83 F cap); the default alarm
        # ceiling (28.89 C, 84 F) leaves the lamp running until it
        import tank_tick
        ceiling = tank_tick.DEFAULT_CONFIG["limits"]["alarm_c"][1]
        self.assertEqual(ceiling, 28.89)
        self.assertGreater(tank_cool.cutoff_c(28.33, 1.0, ceiling), 28.33)
        self.assertEqual(tank_cool.cutoff_c(28.33, 1.0, ceiling), ceiling)
        self.assertEqual(tank_cool.cutoff_c(27.0, 1.0, ceiling), 28.0)

    def test_view_says_when_the_alarm_ceiling_set_the_cutoff(self):
        p = self.plug()
        act = self.actuator([p])
        self.out["cooling"]["setpoint"] = 25.0
        self.assertFalse(self.go(act, [p], NOON, 25.0)["cooling"]["limited_by_alarm"])
        self.out["cooling"]["setpoint"] = 28.0           # 28.0 + 1.0 > ceiling
        act = self.actuator([p])
        self.assertTrue(self.go(act, [p], mins(2), 25.0)["cooling"]["limited_by_alarm"])

    def test_setpoint_plan_uses_the_calendar(self):
        self.out["cooling"]["setpoint"] = "plan"
        want = self.plan(NOON).temp_hourly_c
        p = self.plug()
        act = self.actuator([p])
        res = self.go(act, [p], NOON, want + 0.5)
        self.assertEqual(res["cooling"]["setpoint_c"], want)
        self.assertTrue(p.opts["on"])
        self.go(act, [p], mins(1), min(want + 1.0, ALARM_HIGH) + 0.01)
        self.assertFalse(p.opts["on"])

    def test_not_armed_reports_but_never_switches(self):
        for off in ("cooling", "master"):
            if off == "cooling":
                self.out["cooling"]["enabled"] = False
            else:
                self.out["cooling"]["enabled"] = True
                self.out["enabled"] = False
            p = self.plug()
            act = self.actuator([p])
            res = self.go(act, [p], NOON, 27.0)
            self.assertEqual(p.commands, [], off)
            self.assertFalse(res["cooling"]["armed"])
            self.assertTrue(res["cooling"]["plugs"]["plug-8"]["would_cut"])
            self.assertEqual(act._trips, {})

    def test_lost_probe_does_not_cut_but_keeps_a_cut_then_releases_with_alert(self):
        p = self.plug()
        act = self.actuator([p])
        res = self.go(act, [p], mins(0), 25.0, ok=False)
        self.assertTrue(p.opts["on"])                    # no probe: nothing new
        self.go(act, [p], mins(1), 26.5)
        self.assertFalse(p.opts["on"])
        res = self.go(act, [p], mins(10), 20.0, age_s=999)   # stale reading
        self.assertFalse(p.opts["on"])
        self.assertIn("overtemp_probe_lost:plug-8", self.keys(res))
        res = self.go(act, [p], mins(62), 20.0, age_s=999)
        self.assertTrue(p.opts["on"])                    # released after 60 min
        self.assertIn("overtemp_probe_lost:plug-8", self.keys(res))
        self.assertEqual(act._trips, {})

    def test_cut_survives_a_restart(self):
        p = self.plug()
        self.go(self.actuator([p]), [p], NOON, 26.5)
        again = self.actuator([p])                       # new process, same folder
        self.assertIn("plug-8", again._trips)
        res = self.go(again, [p], mins(1), 25.9)
        self.assertEqual(p.commands, [(False, None)])
        self.assertTrue(res["cooling"]["plugs"]["plug-8"]["tripped"])

    def test_unverified_cut_alerts_and_is_retried(self):
        p = self.plug(stuck=True)
        act = self.actuator([p])
        res = self.go(act, [p], NOON, 26.5)
        self.assertIn("command_unverified:plug-8", self.keys(res))
        self.assertIn("plug-8", act._trips)              # still considered cut
        self.go(act, [p], mins(1), 26.5)
        self.assertEqual(len(p.commands), 2)             # tried again

    def test_unreachable_plug_alerts_and_stays_in_the_cut_state(self):
        p = self.plug(ok=False)
        act = self.actuator([p])
        res = self.go(act, [p], NOON, 26.5)
        self.assertIn("overtemp_unreachable:plug-8", self.keys(res))
        self.assertEqual(p.commands, [])

    def test_resume_failure_stays_cut_and_retries(self):
        p = self.plug()
        act = self.actuator([p])
        self.go(act, [p], NOON, 26.5)
        p.opts["stuck"] = True
        res = self.go(act, [p], mins(10), 25.0)
        self.assertIn("plug-8", act._trips)
        self.assertIn("command_unverified:plug-8", self.keys(res))
        p.opts["stuck"] = False
        self.go(act, [p], mins(11), 25.0)
        self.assertTrue(p.opts["on"])
        self.assertEqual(act._trips, {})

    def test_heater_and_missing_plugs_are_refused_loudly(self):
        self.out["cooling"]["plugs"] = ["gate", "nope"]
        gate = tank_plugs.FakePlug("gate", "heater", on=True)
        act = self.actuator([gate])
        res = self.go(act, [gate], NOON, 27.0)
        self.assertEqual(gate.commands, [])
        self.assertIn("overtemp_plug:gate", self.keys(res))
        self.assertIn("overtemp_plug:nope", self.keys(res))

    def test_unguarded_plugs_are_never_touched(self):
        guarded, other = self.plug(), tank_plugs.FakePlug("plug-9", "unassigned",
                                                          on=True)
        act = self.actuator([guarded, other])
        self.go(act, [guarded, other], NOON, 27.0)
        self.assertEqual(other.commands, [])

    def test_with_no_plan_the_alarm_ceiling_still_applies(self):
        self.out["cooling"]["setpoint"] = "plan"          # nothing to read today
        p = self.plug()
        act = self.actuator([p])
        when = utc("2035-01-01T00:00:00Z")               # outside the calendar
        reads = {"plug-8": p.status()}
        res = act.apply(when, None, reads, self.temp(27.0, when), [])
        self.assertEqual(res["cooling"]["cutoff_c"], ALARM_HIGH)
        self.assertTrue(p.opts["on"])
        act.apply(when, None, reads, self.temp(29.0, when), [])
        self.assertFalse(p.opts["on"])


class LightPlugCutoffTests(CoolingBase):
    """A guarded plug that follows a lighting channel goes back to the plan."""

    def setUp(self):
        super().setUp()
        self.out["channels"]["white"] = True

    def test_light_plan_cannot_turn_it_on_while_cut_then_resumes(self):
        w = tank_plugs.FakePlug("plug-8", "white", on=True)    # NOON: white is on
        act = self.actuator([w])
        self.go(act, [w], NOON, 26.5)
        self.assertFalse(w.opts["on"])
        self.go(act, [w], mins(1), 26.5)       # plan wants on; the cut wins
        self.go(act, [w], mins(4), 25.9)
        self.assertEqual([c[0] for c in w.commands], [False])
        self.go(act, [w], mins(8), 25.0)       # released: plan switches it on
        self.assertTrue(w.opts["on"])
        reasons = [e.get("reason") for e in self.audit() if "reason" in e
                   and e.get("event") is None]
        self.assertEqual(reasons, ["overtemp", "plan:white"])


class FanStubActuatorTests(CoolingBase):
    def setUp(self):
        super().setUp()
        self.out["cooling"]["fans"] = {"enabled": True}

    def test_wants_fans_at_half_n_and_cuts_at_n(self):
        p = self.plug()
        act = self.actuator([p])
        res = self.go(act, [p], NOON, 25.2)
        self.assertFalse(res["cooling"]["fans"]["wanted"])
        res = self.go(act, [p], mins(1), 25.5)          # set point 25 + n/2
        fans = res["cooling"]["fans"]
        self.assertTrue(fans["wanted"])
        self.assertFalse(fans["executed"])               # stub: nothing happens
        self.assertEqual((fans["driver"], fans["on_at_c"]), ("stub", 25.5))
        self.assertTrue(p.opts["on"])                    # plug not yet cut
        res = self.go(act, [p], mins(2), 26.0)
        self.assertTrue(res["cooling"]["fans"]["wanted"])
        self.assertFalse(p.opts["on"])                   # now cut as well
        self.assertIn("fan", self.events())

    def test_fans_off_again_below_the_band(self):
        p = self.plug()
        act = self.actuator([p])
        self.go(act, [p], NOON, 25.6)
        self.assertTrue(act._fan_on)
        self.go(act, [p], mins(1), 25.4)
        self.assertTrue(act._fan_on)                     # inside the band
        self.go(act, [p], mins(2), 25.2)
        self.assertFalse(act._fan_on)

    def test_stub_runs_in_dry_run_too_but_never_acts(self):
        self.out["enabled"] = False
        p = self.plug()
        act = self.actuator([p])
        res = self.go(act, [p], NOON, 25.9)
        self.assertTrue(res["cooling"]["fans"]["wanted"])
        self.assertEqual(p.commands, [])

    def test_disabled_fans_are_not_wanted(self):
        self.out["cooling"]["fans"] = {"enabled": False}
        p = self.plug()
        act = self.actuator([p])
        res = self.go(act, [p], NOON, 25.9)
        self.assertFalse(res["cooling"]["fans"]["wanted"])
        self.assertFalse(res["cooling"]["fans"]["enabled"])


class TickIntegrationTests(CoolingBase):
    def test_tick_records_the_cutoff_and_shows_it_in_the_summary(self):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        cfg["plan"] = tank_plan.plan_config()
        cfg["outputs"] = self.out
        cfg["limits"]["temp_calibrated"] = True
        p = self.plug()
        act = tank_out.Actuator(cfg["outputs"], cfg["limits"], [p], self.tmp)
        value = [26.4]

        class In:
            def read_all(self, now):
                return {"temp": tank_inputs.Reading("temp", True, value[0], now),
                        "lux": tank_inputs.Reading("lux", True, 5.0, now)}
        sent = []
        t = tank_tick.Tank(cfg, self.cal, [p], In(),
                           tank_tick.DryRunSink(self.tmp),
                           notify=lambda m, title="": sent.append(m),
                           deadman=lambda: None, actuator=act)
        rec = t.tick(NOON)
        self.assertTrue(rec["cooling"]["plugs"]["plug-8"]["tripped"])
        self.assertTrue(rec["executed"])
        self.assertIn("CUT: plug-8", tank_tick._summary(rec))
        self.assertTrue(any("cutoff" in m for m in sent))
        self.assertFalse(p.opts["on"])

    def test_default_config_has_the_cutoff_off_and_validates(self):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        self.assertFalse(tank_cool.cooling_config(
            cfg["outputs"].get("cooling"))["enabled"])
        path = os.path.join(self.tmp, "c.json")
        with open(path, "w") as fh:
            json.dump({"outputs": {"cooling": {"cutoff_above_c": 99}}}, fh)
        with self.assertRaises(ValueError):
            tank_tick.load_config(path)


if __name__ == "__main__":
    unittest.main()
