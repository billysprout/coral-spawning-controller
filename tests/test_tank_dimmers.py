"""Tests for dimmer support (2026-11-08): the tinytuya_dimmer driver, the fake
dimmer, and the actuator's brightness path (tank_out outputs.levels).

Offline: a fake tinytuya, fake dimmers, the fixed winter calendar of test_tank_out.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import tank_out                         # noqa: E402
import tank_plugs                       # noqa: E402
from tests.test_tank_out import Base, NIGHT, NOON, utc    # noqa: E402

# The fixture calendar, 2026-11-02 (Pacific): the white and blue window opens at
# 06:03 with a 30-minute smoothstep and the plan level is 0.0033 at 14:03Z, 0.1379
# at 14:09Z, 0.4006 at 14:15Z, 0.5499 at 14:18Z and 0.9873 from 14:30Z.
T_0 = utc("2026-11-02T14:03:00Z")
T_10 = utc("2026-11-02T14:09:00Z")
T_40 = utc("2026-11-02T14:15:00Z")
T_END = utc("2026-11-02T14:36:00Z")
AFTER_CLOSE = utc("2026-11-03T01:30:00Z")


class FakeDimmerDevice:
    """tinytuya.OutletDevice for a double dimmer: remembers what it was sent."""
    log = []
    dps = {}
    reply = None

    def __init__(self, dev_id, **kw):
        self.kw = kw

    def status(self):
        return {"dps": dict(FakeDimmerDevice.dps)}

    def set_multiple_values(self, data, nowait=False):
        FakeDimmerDevice.log.append(("multi", dict(data)))
        if FakeDimmerDevice.reply is not None:
            return FakeDimmerDevice.reply
        FakeDimmerDevice.dps.update({str(k): v for k, v in data.items()})
        return {"dps": dict(FakeDimmerDevice.dps)}

    def set_status(self, on, switch=1, nowait=False):
        FakeDimmerDevice.log.append(("switch", switch, bool(on)))
        FakeDimmerDevice.dps[str(switch)] = bool(on)
        return {"dps": dict(FakeDimmerDevice.dps)}

    def set_value(self, index, value, nowait=False):
        FakeDimmerDevice.log.append(("value", index, value))
        return {"dps": {}}


DIM_DPS = {"1": False, "2": 100, "3": 100, "4": "led", "6": 0,
           "7": False, "8": 100, "9": 100, "10": "led", "12": 0, "21": "relay"}


class DimmerDriver(unittest.TestCase):
    def setUp(self):
        FakeDimmerDevice.log = []
        FakeDimmerDevice.dps = dict(DIM_DPS)
        FakeDimmerDevice.reply = None
        self._real = sys.modules.get("tinytuya")
        sys.modules["tinytuya"] = types.SimpleNamespace(OutletDevice=FakeDimmerDevice)

    def tearDown(self):
        if self._real is None:
            sys.modules.pop("tinytuya", None)
        else:
            sys.modules["tinytuya"] = self._real

    @staticmethod
    def dimmer(gang=1, **opts):
        return tank_plugs.TinyTuyaDimmerPlug(f"dim-{gang}", "white", None, key="K",
                                             id="dev", ip="192.0.2.12", gang=gang, **opts)

    def test_each_gang_has_its_own_data_points_and_version_3_5(self):
        a, b = self.dimmer(1), self.dimmer(2)
        self.assertEqual((a.power_dp, a.level_dp, a.min_dp), ("1", "2", "3"))
        self.assertEqual((b.power_dp, b.level_dp, b.min_dp), ("7", "8", "9"))
        self.assertEqual(a.version, 3.5)
        self.assertTrue(a.supports_level)
        self.assertFalse(a.supports_revert)          # the countdown is untested on dimmers
        with self.assertRaises(ValueError):
            self.dimmer(3)
        with self.assertRaises(ValueError):
            self.dimmer(1, level_cap=0)
        with self.assertRaises(ValueError):
            self.dimmer(1, level_cap=1.5)

    def test_status_reads_level_and_the_devices_own_minimum(self):
        FakeDimmerDevice.dps.update({"7": True, "8": 640})
        r = self.dimmer(2).status()
        self.assertTrue(r.ok)
        self.assertEqual((r.on, r.level, r.level_min), (True, 0.64, 0.1))
        r1 = self.dimmer(1).status()
        self.assertEqual((r1.on, r1.level), (False, 0.1))

    def test_effective_level_is_zero_while_off(self):
        FakeDimmerDevice.dps.update({"1": False, "2": 800})
        d = self.dimmer(1)
        self.assertEqual(d.effective_level(d.status()), 0.0)      # the setting is kept: 0.8
        self.assertEqual(d.status().level, 0.8)
        FakeDimmerDevice.dps["1"] = True
        self.assertEqual(d.effective_level(d.status()), 0.8)

    def test_a_missing_brightness_data_point_is_an_error_not_a_guess(self):
        del FakeDimmerDevice.dps["2"]
        r = self.dimmer(1).status()
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "dp_missing")

    def test_set_level_sends_switch_and_brightness_in_one_message(self):
        self.dimmer(2).set_level(0.5)
        self.assertEqual(FakeDimmerDevice.log, [("multi", {"7": True, "8": 500})])

    def test_set_level_is_capped_floored_and_zero_means_off(self):
        d = self.dimmer(1, level_cap=0.6)
        d.set_level(1.0)
        self.assertEqual(FakeDimmerDevice.log[-1], ("multi", {"1": True, "2": 600}))
        d.set_level(0.011)                                    # above off_below, under the dp minimum
        self.assertEqual(FakeDimmerDevice.log[-1], ("multi", {"1": True, "2": 11}))
        d2 = self.dimmer(1, level_floor=20)
        d2.set_level(0.011)
        self.assertEqual(FakeDimmerDevice.log[-1], ("multi", {"1": True, "2": 20}))
        n = len(FakeDimmerDevice.log)
        d.set_level(0.0)
        self.assertEqual(FakeDimmerDevice.log[n:], [("switch", 1, False)])
        self.assertEqual(FakeDimmerDevice.dps["2"], 20)       # the brightness setting is untouched

    def test_a_refused_command_raises(self):
        FakeDimmerDevice.reply = {"Error": "Check device key or version", "Err": "914"}
        with self.assertRaises(tank_plugs.PlugError):
            self.dimmer(1).set_level(0.5)
        with self.assertRaises(tank_plugs.PlugError):
            tank_plugs.TinyTuyaDimmerPlug("x", "white", None, id="d", ip="1.2.3.4").set_level(0.5)

    def test_no_countdown_is_armed_and_a_revert_request_is_refused(self):
        d = self.dimmer(1)
        d.set_on(True)
        self.assertEqual([x for x in FakeDimmerDevice.log if x[0] == "value"], [])
        with self.assertRaises(tank_plugs.PlugError):
            d.set_on(True, revert_after_s=900)

    def test_the_array_builds_both_gangs_of_a_device_with_one_key(self):
        with tempfile.TemporaryDirectory() as d:
            arr = os.path.join(d, "plug_array.json")
            dev = os.path.join(d, "devices.json")
            with open(arr, "w") as fh:
                json.dump({"plugs": [
                    {"name": "dim-1a", "driver": "tinytuya_dimmer", "role": "white",
                     "id": "eb90", "ip": "192.0.2.12", "gang": 1},
                    {"name": "dim-1b", "driver": "tinytuya_dimmer", "role": "blue",
                     "id": "eb90", "ip": "192.0.2.12", "gang": 2, "level_cap": 0.5}]}, fh)
            with open(dev, "w") as fh:
                json.dump([{"id": "eb90", "key": "SECRET"}], fh)
            plugs = tank_plugs.load_plugs(arr, dev)
        self.assertEqual([type(p).__name__ for p in plugs], ["TinyTuyaDimmerPlug"] * 2)
        self.assertEqual([p.level_dp for p in plugs], ["2", "8"])
        self.assertEqual(plugs[1].level_cap, 0.5)
        self.assertNotIn("SECRET", repr(plugs))


class LevelBase(Base):
    def setUp(self):
        super().setUp()
        for ch in ("white", "white_plus", "blue", "moon"):
            self.out["channels"][ch] = True

    def dimmer(self, name="d", role="white", **opts):
        opts.setdefault("on", False)
        opts.setdefault("level", 0.1)
        opts.setdefault("level_min", 0.1)
        return tank_plugs.FakeDimmerPlug(name, role, **opts)

    def commands(self):
        return [e for e in self.audit() if "desired_level" in e]


class LevelPath(LevelBase):
    def test_the_window_opening_is_a_command_only_once_the_plan_reaches_the_minimum(self):
        d = self.dimmer()
        act = self.actuator([d])
        self.apply(act, [d], T_0)                       # plan level 0.003: nothing yet
        self.assertEqual(d.commands, [])
        self.apply(act, [d], T_10)                      # 0.138 -> 0.14
        self.assertEqual(d.commands, [("level", 0.14)])
        self.assertEqual((d.opts["on"], d.opts["level"]), (True, 0.14))

    def test_the_command_is_read_back_and_audited_with_both_levels(self):
        d = self.dimmer()
        self.apply(self.actuator([d]), [d], T_40)
        e = self.commands()[0]
        self.assertEqual((e["desired_on"], e["desired_level"], e["observed_level"]), (True, 0.4, 0.4))
        self.assertTrue(e["verified"])
        self.assertEqual(e["reason"], "plan:white")

    def test_a_whole_dawn_is_a_modest_run_of_steps_that_never_trips_the_rate_limit(self):
        d = self.dimmer()
        act = self.actuator([d])
        t, seen, findings = T_0, [], []
        while t <= T_END:
            res = self.apply(act, [d], t)
            findings += [f["key"] for f in res["findings"]]
            seen.append(d.opts["level"] if d.opts["on"] else 0.0)
            t += dt.timedelta(minutes=1)
        self.assertEqual(seen, sorted(seen))            # never steps back down
        self.assertEqual(seen[-1], 1.0)                 # the plan is at full from 14:34Z
        self.assertLessEqual(len(d.commands), 50)
        self.assertFalse([k for k in findings if k.startswith("command_rate_limit")])
        self.assertFalse([k for k in findings if k.startswith("command_")])

    def test_a_change_smaller_than_a_step_sends_nothing(self):
        d = self.dimmer(on=True, level=0.4)
        act = self.actuator([d])
        self.apply(act, [d], T_40)                      # target 0.40
        self.assertEqual(d.commands, [])
        d.opts["level"] = 0.41                          # within a step of 0.40: no command
        self.apply(act, [d], T_40)
        self.assertEqual(d.commands, [])
        d.opts["level"] = 0.43                          # a step away: now it corrects
        self.apply(act, [d], T_40 + dt.timedelta(seconds=40))
        self.assertEqual(d.commands, [("level", 0.4)])

    def test_level_cap_is_a_ceiling_whatever_the_plan_asks(self):
        d = self.dimmer(level_cap=0.5)
        self.apply(self.actuator([d]), [d], NOON)       # plan is at 1.0
        self.assertEqual(d.commands, [("level", 0.5)])

    def test_below_the_devices_own_minimum_the_lamp_stays_off(self):
        d = self.dimmer(level_min=0.3)
        act = self.actuator([d])
        self.apply(act, [d], T_10)                      # plan 0.14 < 0.30
        self.assertEqual(d.commands, [])
        self.apply(act, [d], T_40)                      # plan 0.40
        self.assertEqual(d.commands, [("level", 0.4)])

    def test_min_level_keeps_a_lamp_out_of_its_bad_range(self):
        # the Halo's driver buzzes below about 90 percent: off under the floor, never dim
        d = self.dimmer(min_level=0.9)
        act = self.actuator([d])
        self.apply(act, [d], T_40)                      # plan 0.40: under the floor
        self.assertEqual(d.commands, [])
        self.apply(act, [d], NOON)                      # plan 1.0
        self.assertEqual(d.commands, [("level", 1.0)])
        with self.assertRaises(ValueError):
            self.dimmer(min_level=0.9, level_cap=0.8)
        with self.assertRaises(ValueError):
            self.dimmer(min_level=-0.1)

    def test_fixed_level_replaces_the_plans_level_while_the_plan_has_the_lamp_on(self):
        # the moon lamp: the plan's moon level is an illumination fraction, not a brightness
        d = self.dimmer(role="white", fixed_level=0.10)
        act = self.actuator([d])
        self.apply(act, [d], T_40)                      # plan level 0.40 -> still 0.10
        self.assertEqual(d.commands, [("level", 0.1)])
        self.apply(act, [d], NOON)                      # plan 1.0 -> unchanged, no new command
        self.assertEqual(d.commands, [("level", 0.1)])
        self.apply(act, [d], AFTER_CLOSE)               # the plan has it off: off
        self.assertEqual(d.commands[-1], ("level", 0.0))
        with self.assertRaises(ValueError):
            self.dimmer(fixed_level=0.0)
        with self.assertRaises(ValueError):
            self.dimmer(fixed_level=0.9, level_cap=0.5)

    def test_blue_and_white_plus_have_their_own_levels(self):
        b = self.dimmer("b", "blue")
        wp = self.dimmer("wp", "white_plus")
        act = self.actuator([b, wp])
        self.apply(act, [b, wp], T_40)
        self.assertEqual(b.commands, [("level", 0.4)])
        self.assertEqual(wp.commands, [])               # the day curve is under its threshold
        self.apply(act, [b, wp], NOON)
        self.assertEqual(wp.commands, [("level", 1.0)])      # 0.9936 rounds to a whole step

    def test_the_end_of_the_window_turns_it_off(self):
        d = self.dimmer(on=True, level=0.98)
        self.apply(self.actuator([d]), [d], AFTER_CLOSE)
        self.assertEqual(d.commands, [("level", 0.0)])
        self.assertFalse(d.opts["on"])

    def test_dark_protect_forces_a_lit_dimmer_off(self):
        d = self.dimmer(on=True, level=0.6)
        act = self.actuator([d])
        self.apply(act, [d], NIGHT)
        self.assertFalse(d.opts["on"])
        self.assertEqual(self.commands()[0]["reason"], "dark_protect")

    def test_a_hand_on_the_wall_switch_is_held_then_the_plan_resumes(self):
        d = self.dimmer()
        act = self.actuator([d])
        self.apply(act, [d], T_40)                      # we set 0.40
        d.opts["level"] = 0.9                           # somebody turns it up
        n = len(d.commands)
        res = self.apply(act, [d], T_40 + dt.timedelta(minutes=1))
        self.assertEqual(len(d.commands), n)
        self.assertIn("override:d", [f["key"] for f in res["findings"]])
        res = self.apply(act, [d], T_40 + dt.timedelta(minutes=10))
        self.assertEqual(len(d.commands), n)            # still held
        self.apply(act, [d], T_40 + dt.timedelta(minutes=32))
        self.assertGreater(len(d.commands), n)          # hold over: the plan is back
        self.assertTrue(any(e.get("event") == "override_detected" for e in self.audit()))

    def test_a_dimmer_that_ignores_commands_raises_unverified(self):
        d = self.dimmer(stuck=True)
        res = self.apply(self.actuator([d]), [d], T_40)
        keys = [f["key"] for f in res["findings"]]
        self.assertIn("command_unverified:d", keys)

    def test_a_failing_dimmer_raises_command_failed_and_the_tick_goes_on(self):
        d = self.dimmer(fail_set=True)
        w = tank_plugs.FakePlug("w", "white", on=False)
        res = self.apply(self.actuator([d, w]), [d, w], NOON)
        self.assertIn("command_failed:d", [f["key"] for f in res["findings"]])
        self.assertEqual(w.commands[0][0], True)         # the on/off plug was still handled

    def test_the_level_command_limit_is_its_own_and_can_alert(self):
        self.out["levels"] = {"max_commands_per_hour": 3}
        d = self.dimmer()
        act = self.actuator([d])
        t, keys = T_10, []
        for i in range(8):                               # a ramp: a new step every minute
            res = self.apply(act, [d], t + dt.timedelta(minutes=i))
            keys += [f["key"] for f in res["findings"]]
        self.assertEqual(len(d.commands), 3)
        self.assertIn("command_rate_limit:d", keys)

    def test_on_off_plugs_still_use_the_on_off_path_and_limit(self):
        w = tank_plugs.FakePlug("w", "white", on=False)
        self.apply(self.actuator([w]), [w], NOON)
        self.assertEqual(w.commands, [(True, None)])

    def test_the_master_and_channel_switches_still_gate_dimmers(self):
        d = self.dimmer()
        self.out["channels"]["white"] = False
        self.apply(self.actuator([d]), [d], NOON)
        self.assertEqual(d.commands, [])
        self.out["channels"]["white"] = True
        self.out["enabled"] = False
        self.apply(self.actuator([d]), [d], NOON)
        self.assertEqual(d.commands, [])

    def test_an_offline_dimmer_is_left_alone(self):
        d = self.dimmer(ok=False)
        self.apply(self.actuator([d]), [d], NOON)
        self.assertEqual(d.commands, [])


class LevelConfig(unittest.TestCase):
    def test_defaults_and_validation(self):
        cfg = tank_out.levels_config(None)
        self.assertEqual((cfg["step"], cfg["max_commands_per_hour"]), (0.02, 120))
        for bad in ({"step": 0.0001}, {"step": 0.5}, {"tolerance": 0.05},
                    {"override_delta": 0.001}, {"max_commands_per_hour": 0}):
            with self.assertRaises(ValueError, msg=str(bad)):
                tank_out.levels_config(bad)


if __name__ == "__main__":
    unittest.main()
