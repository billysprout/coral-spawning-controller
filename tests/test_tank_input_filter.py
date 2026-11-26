"""Range, setpoint-slip and spike screening of the temperature inputs
(2026-11-06 audit). Offline."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import tank_inputs as ti                # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 3, 23, 0, 0, tzinfo=UTC)
SPEC = {"driver": "jsonl", "field": "temp_c", "max_age_s": 900, "skip_errors": True,
        "label": "Inkbird"}


def f_of(c):
    return round(c * 1.8 + 32, 1)


def line(n_min, temp_c, t1=77.1, t2=77.6, error=None):
    ts = (T0 + dt.timedelta(minutes=n_min)).isoformat(timespec="seconds")
    if error:
        return json.dumps({"ts": ts, "temp_c": None, "error": error})
    return json.dumps({"ts": ts, "temp_c": temp_c, "t1_f": t1, "t2_f": t2})


class Screen(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "feed.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self, lines, at_min, spec=None):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        src = ti.build_source("temp", dict(spec or SPEC, path=self.path))
        return src.read(T0 + dt.timedelta(minutes=at_min))

    # -- range ------------------------------------------------------------
    def test_out_of_range_value_is_not_a_reading(self):
        for bad in (-3.0, 3.9, 46.0, 250.0):
            r = self.read([line(0, bad)], 1)
            self.assertFalse(r.ok, bad)
            self.assertIn("plausible", r.detail)

    def test_in_range_values_pass(self):
        for good in (5.0, 25.333, 45.0):
            self.assertTrue(self.read([line(0, good)], 1).ok, good)

    def test_out_of_range_line_is_skipped_for_an_older_good_one(self):
        r = self.read([line(0, 26.0), line(5, 250.0)], 6)
        self.assertTrue(r.ok)
        self.assertEqual(r.value, 26.0)

    def test_lux_is_not_range_checked(self):
        src = ti.build_source("lux", {"driver": "jsonl", "field": "lux",
                                      "path": self.path})
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": T0.isoformat(), "lux": 90000.0}) + "\n")
        self.assertTrue(src.read(T0).ok)

    def test_http_source_range(self):
        src = ti.build_source("temp", {"driver": "http_json", "url": "http://x/"})
        r = ti.Reading("temp", True, 99.0, T0)
        self.assertFalse(src.in_range(r).ok)

    # -- setpoint slip ----------------------------------------------------
    def test_the_2026_11_03_slip_is_held_not_believed(self):
        # tank 26.611 C (79.9 F); the reader then repeats T2 = 77.6 F (25.333 C)
        lines = [line(0, 26.611), line(5, 26.611),
                 line(10, 25.333), line(15, 25.333), line(20, 25.333)]
        r = self.read(lines, 21)
        self.assertTrue(r.ok)
        self.assertEqual(r.value, 26.611)          # the last distinct reading
        self.assertIn("setpoint misread", r.detail)
        self.assertEqual(r.ts, T0 + dt.timedelta(minutes=5))   # keeps its old stamp

    def test_slip_goes_stale_so_the_stale_alert_fires(self):
        lines = [line(0, 26.611), line(5, 25.333), line(10, 25.333)]
        r = self.read(lines, 40)
        self.assertGreater(r.age_s(T0 + dt.timedelta(minutes=40)), 900)

    def test_a_genuine_arrival_at_t2_is_accepted(self):
        # slowly falling onto T2: small step, so it is not a slip
        lines = [line(0, 25.5), line(5, 25.4), line(10, f_to_c(77.6))]
        r = self.read(lines, 11)
        self.assertEqual(r.value, f_to_c(77.6))
        self.assertEqual(r.detail, "")

    def test_a_tank_that_has_sat_at_t2_all_along_is_accepted(self):
        lines = [line(i * 5, f_to_c(77.6)) for i in range(6)]
        r = self.read(lines, 26)
        self.assertEqual(r.value, f_to_c(77.6))

    # -- spike ------------------------------------------------------------
    def test_one_off_spike_is_held_until_confirmed(self):
        r = self.read([line(0, 26.0), line(5, 27.5, t1=70.0, t2=71.0)], 6)
        self.assertEqual(r.value, 26.0)
        self.assertIn("not confirmed", r.detail)

    def test_a_real_step_is_accepted_one_poll_later(self):
        lines = [line(0, 26.0), line(5, 27.5, t1=70.0, t2=71.0),
                 line(10, 27.5, t1=70.0, t2=71.0)]
        r = self.read(lines, 11)
        self.assertEqual(r.value, 27.5)

    def test_normal_drift_is_untouched(self):
        lines = [line(0, 26.0), line(5, 26.1), line(10, 26.2)]
        self.assertEqual(self.read(lines, 11).value, 26.2)

    def test_a_jump_after_a_long_gap_is_accepted(self):
        # more than step_window_s since the previous reading: nothing to compare
        r = self.read([line(0, 26.0), line(30, 27.5, t1=70.0, t2=71.0)], 31)
        self.assertEqual(r.value, 27.5)

    def test_screen_can_be_turned_off(self):
        spec = dict(SPEC, max_step_c=0)
        r = self.read([line(0, 26.0), line(5, 27.5, t1=70.0, t2=71.0)], 6, spec)
        self.assertEqual(r.value, 27.5)

    def test_error_lines_still_use_the_newest_good_reading(self):
        r = self.read([line(0, 26.0), line(5, None, error="boom")], 6)
        self.assertTrue(r.ok)
        self.assertEqual(r.value, 26.0)

    def test_real_feed_replay_of_the_slip_window(self):
        """The recorded 2026-11-03 16:12-17:28 PST window, if the feed is here."""
        feed = os.path.join(ROOT, "data", "sensors", "itc306a_docker.jsonl")
        if not os.path.exists(feed):
            self.skipTest("no feed file")
        with open(feed, encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh if x.strip()]
        start = dt.datetime(2026, 11, 4, 0, 12, tzinfo=UTC)
        end = dt.datetime(2026, 11, 4, 1, 28, tzinfo=UTC)
        window = [r for r in rows
                  if start <= dt.datetime.fromisoformat(r["ts"]) <= end]
        if len(window) < 5:
            self.skipTest("feed has been rotated")
        src = ti.build_source("temp", dict(SPEC, path=self.path))
        wrongly_believed = 0
        for i in range(3, len(window) + 1):
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(json.dumps(r) for r in window[:i]) + "\n")
            now = dt.datetime.fromisoformat(window[i - 1]["ts"])
            got = src.read(now)
            if got.ok and abs(got.value - 25.333) < 0.001 and not got.detail:
                wrongly_believed += 1
        self.assertEqual(wrongly_believed, 0)


def f_to_c(f):
    return round((f - 32) / 1.8, 3)


if __name__ == "__main__":
    unittest.main()
