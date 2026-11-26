"""The Inkbird feed as the primary temperature input, the Pi probe as a second
opinion (2026-11-03). Offline."""
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
import tank_inputs as ti                # noqa: E402
import tank_plan                        # noqa: E402
import tank_tick                        # noqa: E402

UTC = dt.timezone.utc
FIX = os.path.join(ROOT, "tests", "fixtures")
T0 = dt.datetime(2026, 11, 3, 20, 0, 0, tzinfo=UTC)


def at(n_min):
    return T0 + dt.timedelta(minutes=n_min)


def feed_line(ts, temp_c=26.0, error=None):
    rec = {"ts": ts.isoformat(timespec="seconds"), "temp_c": temp_c}
    if error:
        rec = {"ts": ts.isoformat(timespec="seconds"), "temp_c": None, "error": error}
    return json.dumps(rec)


SPEC = {"driver": "jsonl", "field": "temp_c", "max_age_s": 900, "skip_errors": True,
        "label": "Inkbird"}


class FeedSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "feed.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, *lines):
        with open(self.path, "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def read(self, now, **over):
        spec = dict(SPEC, path=self.path, **over)
        return ti.Inputs.from_config({"temp": spec}).read_all(now)["temp"]

    def test_the_reading_carries_its_own_age_limit_and_label(self):
        self.write(feed_line(at(-2)))
        r = self.read(at(0))
        self.assertTrue(r.ok)
        self.assertEqual((r.max_age_s, r.label), (900, "Inkbird"))
        self.assertEqual(r.limit_s(180), 900)
        self.assertEqual(ti.Reading("x", True).limit_s(180), 180)

    def test_one_failed_poll_does_not_make_the_reading_missing(self):
        self.write(feed_line(at(-10), 25.9), feed_line(at(-5), 26.0),
                   feed_line(at(0), error="recovery: tapped the device card"))
        r = self.read(at(1))
        self.assertTrue(r.ok)
        self.assertEqual(r.value, 26.0)                       # the newest good one

    def test_failures_for_longer_than_the_limit_are_reported_with_the_reason(self):
        self.write(feed_line(at(-30), 26.0), feed_line(at(-10), error="adb refused"),
                   feed_line(at(-5), error="adb refused"))
        r = self.read(at(0))
        self.assertFalse(r.ok)
        self.assertIn("adb refused", r.detail)

    def test_a_feed_of_only_failures(self):
        self.write(feed_line(at(-5), error="adb refused"))
        r = self.read(at(0))
        self.assertFalse(r.ok)
        self.assertIn("adb refused", r.detail)

    def test_without_skip_errors_the_newest_line_decides(self):
        self.write(feed_line(at(-5), 26.0), feed_line(at(0), error="x"))
        self.assertFalse(self.read(at(1), skip_errors=False).ok)


class Inputs(unittest.TestCase):
    def test_probe_temp_is_built_beside_temp_when_configured(self):
        names = ti.Inputs.from_config({"temp": SPEC, "probe_temp": {"driver": "none"}}).sources
        self.assertEqual(sorted(names), ["lux", "probe_temp", "temp"])
        self.assertTrue(names["probe_temp"].reject_zero)      # a probe too: 0.0 is a bad read


class Fake:
    """temp (the Inkbird feed) and probe_temp (the Pi) that the test controls."""

    def __init__(self):
        self.temp_age_s, self.temp_ok, self.stamp = 120, True, None
        self.probe_ok, self.probe_age_s = False, 5

    def read_all(self, now):
        ts = self.stamp or (now - dt.timedelta(seconds=self.temp_age_s))
        temp = (ti.Reading("temp", True, 26.0, ts, max_age_s=900, label="Inkbird")
                if self.temp_ok else
                ti.Reading("temp", False, detail="sensor: adb refused", label="Inkbird",
                           max_age_s=900))
        probe = (ti.Reading("probe_temp", True, 26.3,
                            now - dt.timedelta(seconds=self.probe_age_s),
                            label="Pi probe") if self.probe_ok else
                 ti.Reading("probe_temp", False, detail="unreachable: timed out",
                            label="Pi probe"))
        return {"temp": temp, "lux": ti.Reading("lux", True, 0.0, now),
                "probe_temp": probe}


class InTheTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cal = tank_plan.load_calendar(
            os.path.join(FIX, "spawn_calendar_2026-11-01.csv"),
            cues.load_params(os.path.join(FIX, "cue_params_offset0.json")))
        self.sent = []
        self.inputs = Fake()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def tank(self):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        cfg["plan"] = tank_plan.plan_config()
        return tank_tick.Tank(cfg, self.cal, [], self.inputs, tank_tick.DryRunSink(self.tmp),
                              notify=lambda m, title="": self.sent.append(m),
                              deadman=lambda: None, data_dir=self.tmp)

    def keys(self, rec):
        return [f["key"] for f in rec["findings"]]

    def test_a_five_minute_old_inkbird_reading_is_not_stale(self):
        self.inputs.temp_age_s = 330
        rec = self.tank().tick(at(0))
        self.assertNotIn("temp_stale", self.keys(rec))
        self.assertNotIn("temp_frozen", self.keys(rec))

    def test_a_reading_older_than_its_own_limit_is_stale_and_names_the_source(self):
        self.inputs.temp_age_s = 1000
        rec = self.tank().tick(at(0))
        self.assertIn("temp_stale", self.keys(rec))
        self.assertTrue([m for m in self.sent if "temperature (Inkbird) reading is" in m])

    def test_the_stamp_may_sit_still_for_a_poll_interval(self):
        self.inputs.stamp = at(0)
        t = self.tank()
        rec = None
        for m in range(0, 8):                                # same stamp for 7 minutes
            rec = t.tick(at(m))
        self.assertNotIn("temp_frozen", self.keys(rec))
        for m in range(8, 17):                               # past 900 s: now it is stale
            rec = t.tick(at(m))
        self.assertIn("temp_stale", self.keys(rec))

    def test_a_missing_inkbird_reading_alerts_with_the_reason(self):
        self.inputs.temp_ok = False
        rec = self.tank().tick(at(0))
        self.assertIn("temp_missing", self.keys(rec))
        self.assertTrue([m for m in self.sent if "temperature (Inkbird): sensor: adb refused" in m])

    def test_the_pi_probe_missing_is_only_a_warning(self):
        rec = self.tank().tick(at(0))                        # probe_ok is False
        self.assertIn("probe_temp_missing", self.keys(rec))
        f = [f for f in rec["findings"] if f["key"] == "probe_temp_missing"][0]
        self.assertFalse(f["alert"])
        self.assertEqual(self.sent, [])

    def test_a_stale_pi_probe_is_only_a_warning(self):
        self.inputs.probe_ok, self.inputs.probe_age_s = True, 5000
        rec = self.tank().tick(at(0))
        self.assertIn("probe_temp_stale", self.keys(rec))
        self.assertEqual(self.sent, [])

    def test_the_difference_is_logged_probe_minus_inkbird(self):
        self.inputs.probe_ok = True
        t = self.tank()
        rec = t.tick(at(0))
        self.assertEqual(rec["probe_diff_c"], 0.3)
        with open(os.path.join(self.tmp, "tank_inputs-20261103.jsonl")) as fh:
            row = json.loads(fh.readlines()[-1])
        self.assertEqual((row["temp_c"], row["probe_c"], row["probe_diff_c"]),
                         (26.0, 26.3, 0.3))

    def test_the_status_calls_the_inkbird_the_water_so_the_deadman_pings(self):
        pings = []
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        cfg["plan"] = tank_plan.plan_config()
        t = tank_tick.Tank(cfg, self.cal, [], self.inputs, tank_tick.DryRunSink(self.tmp),
                           notify=lambda *a, **k: None, deadman=lambda: pings.append(1),
                           data_dir=self.tmp)
        t.tick(at(0))
        self.assertEqual(len(pings), 1)

    def test_the_watch_compares_the_inkbird_with_the_pi_probe_not_with_itself(self):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {"itc_watch": {"enabled": True}})
        cfg["plan"] = tank_plan.plan_config()
        seen = []

        class Spy:
            unknown_keys = set()

            def step(self, now, probe):
                seen.append(probe)
                return {"enabled": True}, []
        t = tank_tick.Tank(cfg, self.cal, [], self.inputs, tank_tick.DryRunSink(self.tmp),
                           notify=lambda *a, **k: None, deadman=lambda: None,
                           data_dir=self.tmp)
        t.itc_watch = Spy()
        t.tick(at(0))
        self.assertEqual(seen[0].name, "probe_temp")


if __name__ == "__main__":
    unittest.main()
