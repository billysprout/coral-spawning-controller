"""Tests for the tank alert handling: recovery notices, cooldowns that survive a
restart, retry of an undelivered alert, and the input-clock checks. Offline."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import cues                             # noqa: E402
import tank_inputs                      # noqa: E402
import tank_plan                        # noqa: E402
import tank_tick                        # noqa: E402

UTC = dt.timezone.utc
FIX = os.path.join(ROOT, "tests", "fixtures")
T0 = dt.datetime(2026, 11, 2, 20, 0, 0, tzinfo=UTC)         # 12:00 PST, sun up


def minutes(n):
    return T0 + dt.timedelta(minutes=n)


class Inputs:
    """temp/lux that can be switched off, skewed or frozen."""

    def __init__(self):
        self.temp_ok = True
        self.skew_s = 0                 # reading timestamp = now + skew
        self.fixed_ts = None            # a frozen stamp

    def read_all(self, now):
        ts = self.fixed_ts or (now + dt.timedelta(seconds=self.skew_s))
        temp = (tank_inputs.Reading("temp", True, 26.0, ts) if self.temp_ok else
                tank_inputs.Reading("temp", False, detail="unreachable: timed out"))
        return {"temp": temp, "lux": tank_inputs.Reading("lux", True, 0.0, ts)}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cal = tank_plan.load_calendar(
            os.path.join(FIX, "spawn_calendar_2026-11-01.csv"),
            cues.load_params(os.path.join(FIX, "cue_params_offset0.json")))
        self.sent = []
        self.deliver = True             # what notify returns
        self.inputs = Inputs()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def notify(self, msg, title=""):
        self.sent.append(msg)
        return self.deliver

    def tank(self, limits=None, inputs=None):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {"limits": limits or {}})
        cfg["plan"] = tank_plan.plan_config()
        return tank_tick.Tank(cfg, self.cal, [], inputs or self.inputs,
                              tank_tick.DryRunSink(self.tmp), notify=self.notify,
                              deadman=lambda: None, data_dir=self.tmp)

    def texts(self, needle):
        return [m for m in self.sent if needle in m]


class Recovery(Base):
    def test_recovery_notice_after_the_confirm_period_once(self):
        t = self.tank()
        self.inputs.temp_ok = False
        rec = t.tick(minutes(0))
        self.assertEqual(rec["alerts_sent"], ["temp_missing"])
        self.inputs.temp_ok = True
        t.tick(minutes(1))                                  # clear, not yet confirmed
        self.assertEqual(self.texts("Recovered"), [])
        rec = t.tick(minutes(4))
        self.assertEqual(rec["alerts_sent"], ["recovered:temp_missing"])
        self.assertIn("temperature probe", self.texts("Recovered")[0])
        t.tick(minutes(10))
        self.assertEqual(len(self.texts("Recovered")), 1)

    def test_a_flapping_alert_does_not_recover_or_repeat(self):
        t = self.tank()
        self.inputs.temp_ok = False
        t.tick(minutes(0))
        self.inputs.temp_ok = True
        t.tick(minutes(1))
        self.inputs.temp_ok = False                         # back before it confirmed
        t.tick(minutes(2))
        self.inputs.temp_ok = True
        t.tick(minutes(3))
        self.assertEqual(self.texts("Recovered"), [])
        self.assertEqual(len(self.texts("unreachable")), 1)   # the cooldown held

    def test_recovery_can_be_switched_off(self):
        t = self.tank({"recovery_alerts": False})
        self.inputs.temp_ok = False
        t.tick(minutes(0))
        self.inputs.temp_ok = True
        t.tick(minutes(1))
        t.tick(minutes(10))
        self.assertEqual(self.texts("Recovered"), [])

    def test_muted_alerts_never_recover(self):
        t = self.tank({"mute_alerts": ["temp_missing"]})
        self.inputs.temp_ok = False
        t.tick(minutes(0))
        self.inputs.temp_ok = True
        t.tick(minutes(10))
        self.assertEqual(self.sent, [])


class OnceADay(Base):
    """A finding marked daily goes out once per local calendar day, not every cooldown."""
    def finding(self, daily=True):
        return {"key": "itc_not_stopping", "level": "alert", "alert": True, "daily": daily,
                "message": "Inkbird reads 80.3 F, above T2"}

    def at(self, day, hour):
        return dt.datetime(2026, 11, day, hour, 0).astimezone()     # local wall clock

    def test_one_a_day_then_again_the_next_day(self):
        t = self.tank()
        self.assertEqual(t._send_alerts([self.finding()], self.at(3, 9)), ["itc_not_stopping"])
        self.assertEqual(t._send_alerts([self.finding()], self.at(3, 10)), [])      # past 30 min, same day
        self.assertEqual(t._send_alerts([self.finding()], self.at(3, 23)), [])
        self.assertEqual(t._send_alerts([self.finding()], self.at(4, 9)), ["itc_not_stopping"])
        self.assertEqual(len(self.texts("80.3")), 2)

    def test_not_daily_keeps_the_normal_cooldown(self):
        t = self.tank()
        self.assertEqual(t._send_alerts([self.finding(False)], self.at(3, 9)), ["itc_not_stopping"])
        self.assertEqual(t._send_alerts([self.finding(False)], self.at(3, 10)), ["itc_not_stopping"])

    def test_the_alarm_level_breaks_through_a_daily_quiet(self):
        t = self.tank()
        t._send_alerts([self.finding(True)], self.at(3, 9))
        self.assertEqual(t._send_alerts([self.finding(True)], self.at(3, 12)), [])
        self.assertEqual(t._send_alerts([self.finding(False)], self.at(3, 13)), ["itc_not_stopping"])

    def test_the_daily_quiet_survives_a_restart(self):
        self.tank()._send_alerts([self.finding()], self.at(3, 9))
        again = self.tank()
        self.assertEqual(again._send_alerts([self.finding()], self.at(3, 15)), [])


class AcrossARestart(Base):
    def test_cooldown_survives_a_restart(self):
        self.inputs.temp_ok = False
        self.tank().tick(minutes(0))
        self.assertEqual(len(self.sent), 1)
        again = self.tank()                                  # the container restarted
        rec = again.tick(minutes(5))
        self.assertEqual(rec["alerts_sent"], [])
        self.assertEqual(len(self.sent), 1)
        again.tick(minutes(31))                              # past the cooldown: repeat
        self.assertEqual(len(self.texts("unreachable")), 2)

    def test_recovery_survives_a_restart(self):
        self.inputs.temp_ok = False
        self.tank().tick(minutes(0))
        self.inputs.temp_ok = True
        again = self.tank()
        again.tick(minutes(1))
        again.tick(minutes(5))
        self.assertEqual(len(self.texts("Recovered")), 1)

    def test_state_file_is_plain_json(self):
        self.inputs.temp_ok = False
        self.tank().tick(minutes(0))
        with open(os.path.join(self.tmp, "tank_alert_state.json")) as fh:
            st = json.load(fh)
        self.assertIn("temp_missing", st["last"])
        self.assertIn("temp_missing", st["active"])

    def test_damaged_state_file_starts_clean(self):
        with open(os.path.join(self.tmp, "tank_alert_state.json"), "w") as fh:
            fh.write("{not json")
        self.inputs.temp_ok = False
        self.assertEqual(self.tank().tick(minutes(0))["alerts_sent"], ["temp_missing"])


class Undelivered(Base):
    def test_a_failed_send_is_retried_soon_not_every_tick_and_not_cooled_down(self):
        self.deliver = False
        t = self.tank()
        self.inputs.temp_ok = False
        rec = t.tick(minutes(0))
        self.assertEqual(rec["alerts_sent"], [])
        t.tick(minutes(1))                                   # inside the 120 s backoff
        self.assertEqual(len(self.sent), 1)
        self.deliver = True
        rec = t.tick(minutes(3))                             # backoff over: tried again
        self.assertEqual(rec["alerts_sent"], ["temp_missing"])
        self.assertEqual(len(self.sent), 2)
        t.tick(minutes(4))
        self.assertEqual(len(self.sent), 2)                  # now the cooldown applies

    def test_a_notifier_that_returns_nothing_counts_as_delivered(self):
        self.deliver = None
        self.inputs.temp_ok = False
        self.assertEqual(self.tank().tick(minutes(0))["alerts_sent"], ["temp_missing"])

    def test_a_failed_recovery_notice_is_tried_again(self):
        t = self.tank()
        self.inputs.temp_ok = False
        t.tick(minutes(0))
        self.inputs.temp_ok = True
        self.deliver = False
        t.tick(minutes(1))
        t.tick(minutes(4))                                   # tries, fails
        self.deliver = True
        rec = t.tick(minutes(5))
        self.assertEqual(rec["alerts_sent"], ["recovered:temp_missing"])


class Clocks(Base):
    def test_a_reading_from_the_future_is_flagged(self):
        self.inputs.skew_s = 3599                           # the Pi an hour ahead
        rec = self.tank().tick(minutes(0))
        keys = [f["key"] for f in rec["findings"]]
        self.assertIn("clock_skew:temp", keys)
        self.assertIn("clock_skew:lux", keys)
        self.assertIn("clock_skew:temp", rec["alerts_sent"])
        self.assertTrue(self.texts("in the future"))

    def test_small_skew_is_tolerated(self):
        self.inputs.skew_s = 30
        rec = self.tank().tick(minutes(0))
        self.assertFalse([f for f in rec["findings"] if f["key"].startswith("clock_skew")])

    def test_a_frozen_stamp_is_flagged_even_when_it_looks_fresh(self):
        self.inputs.fixed_ts = minutes(0) + dt.timedelta(hours=1)   # skewed AND frozen
        t = self.tank()
        keys = []
        for m in range(0, 6):
            rec = t.tick(minutes(m))
            keys = [f["key"] for f in rec["findings"]]
        self.assertIn("temp_frozen", keys)
        self.assertIn("temp_frozen", [k for k in rec["alerts_sent"] + keys])

    def test_a_moving_stamp_is_not_frozen(self):
        t = self.tank()
        for m in range(0, 8):
            rec = t.tick(minutes(m))
        self.assertFalse([f for f in rec["findings"] if f["key"].endswith("_frozen")])

    def test_config_is_validated(self):
        for bad in ({"alert_retry_s": 1}, {"clock_skew_s": 5}, {"recovery_confirm_min": -1}):
            path = os.path.join(self.tmp, "c.json")
            with open(path, "w") as fh:
                json.dump({"limits": bad}, fh)
            with self.assertRaises(ValueError):
                tank_tick.load_config(path)


class NotJudgeable(Base):
    def test_a_lost_sensor_does_not_recover_a_clock_alert(self):
        t = self.tank()
        self.inputs.skew_s = 3599
        t.tick(minutes(0))
        self.assertIn("stamped", self.texts("stamped")[0])
        self.inputs.temp_ok = False                         # the Pi went away
        for m in (1, 4, 8):
            t.tick(minutes(m))
        self.assertEqual([m for m in self.sent if m.startswith("Recovered")], [])
        with open(os.path.join(self.tmp, "tank_alert_state.json")) as fh:
            self.assertIn("clock_skew:temp", json.load(fh)["active"])
        # back with the clocks agreeing: now it really recovered
        self.inputs.temp_ok, self.inputs.skew_s = True, 0
        t.tick(minutes(9))
        t.tick(minutes(12))
        self.assertTrue([m for m in self.sent if m.startswith("Recovered: clock_skew:temp")])

    def test_an_alert_that_stays_unjudgeable_for_hours_is_dropped_quietly(self):
        t = self.tank({"unknown_expire_min": 60})
        self.inputs.skew_s = 3599
        t.tick(minutes(0))
        self.inputs.temp_ok = False
        for m in (1, 30, 59):
            t.tick(minutes(m))
        with open(os.path.join(self.tmp, "tank_alert_state.json")) as fh:
            self.assertIn("clock_skew:temp", json.load(fh)["active"])
        t.tick(minutes(75))                                  # unjudgeable for over 60 min
        with open(os.path.join(self.tmp, "tank_alert_state.json")) as fh:
            self.assertNotIn("clock_skew:temp", json.load(fh)["active"])
        self.assertEqual([m for m in self.sent if m.startswith("Recovered: clock_skew:temp")], [])

    def test_a_gap_restarts_the_confirm_period(self):
        t = self.tank()
        self.inputs.skew_s = 3599
        t.tick(minutes(0))
        self.inputs.skew_s = 0
        t.tick(minutes(1))                                  # clear, confirming
        self.inputs.temp_ok = False
        t.tick(minutes(2))                                  # cannot judge: clock restarts
        self.inputs.temp_ok = True
        t.tick(minutes(3))
        self.assertEqual([m for m in self.sent
                          if m.startswith("Recovered: clock_skew:temp")], [])
        t.tick(minutes(6))
        self.assertTrue([m for m in self.sent if m.startswith("Recovered: clock_skew:temp")])


if __name__ == "__main__":
    unittest.main()
