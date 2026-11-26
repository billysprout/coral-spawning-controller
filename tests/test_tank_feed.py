"""tank_feed.py: readings, feed text, and the Refresh button (no network)."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import tank_feed as tf  # noqa: E402

UTC = dt.timezone.utc


class Fixture(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="tank_feed_test_")
        os.makedirs(os.path.join(self.data, "itc"))

    def tearDown(self):
        shutil.rmtree(self.data, ignore_errors=True)

    def write_jsonl(self, name, rows):
        with open(os.path.join(self.data, name), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def write_json(self, rel, obj):
        with open(os.path.join(self.data, rel), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)


class Recorded(Fixture):
    def test_takes_last_row_of_newest_file(self):
        self.write_jsonl("tank_inputs-20261104.jsonl", [
            {"ts": "2026-11-04T23:59:00+00:00", "temp_c": 1.0, "lux": 1.0}])
        self.write_jsonl("tank_inputs-20261105.jsonl", [
            {"ts": "2026-11-05T00:35:00+00:00", "temp_c": 28.0, "lux": 961.0},
            {"ts": "2026-11-05T00:36:00+00:00", "temp_c": 28.111, "lux": 948.18}])
        rec = tf.latest_recorded(self.data)
        self.assertEqual(rec["temp_c"], 28.111)
        self.assertEqual(rec["lux"], 948.18)
        self.assertEqual(rec["ts"], dt.datetime(2026, 11, 5, 0, 36, tzinfo=UTC))

    def test_null_reading_stays_none_and_bad_lines_skipped(self):
        with open(os.path.join(self.data, "tank_inputs-20261105.jsonl"), "w") as fh:
            fh.write('{"ts": "2026-11-05T00:36:00+00:00", "temp_c": 28.2, "lux": null}\n')
            fh.write("not json\n")
        rec = tf.latest_recorded(self.data)
        self.assertEqual(rec["temp_c"], 28.2)
        self.assertIsNone(rec["lux"])

    def test_no_files(self):
        rec = tf.latest_recorded(self.data)
        self.assertIsNone(rec["ts"])
        self.assertIsNone(rec["temp_c"])


class Targets(Fixture):
    def test_plan_and_applied_band(self):
        self.write_json("tank_status.json", {
            "ts": "2026-11-05T00:36:00+00:00",
            "plan": {"temp_hourly_c": 26.07, "temp_daily_c": 25.22,
                     "moon_target_lux": None, "white_level": 1.0,
                     "blue_level": 0.5, "white_plus_level": 0.4095}})
        self.write_json("itc/applied.json", {
            "status": "success", "floor_f": 78.6, "ceil_f": 79.1,
            "changed_ts": "2026-11-04T20:01:48+00:00",
            "cue": {"temp_hourly_c": 25.974}})
        t = tf.targets(self.data)
        self.assertEqual(t["temp_c"], 26.07)
        self.assertEqual(t["floor_f"], 78.6)
        self.assertEqual(t["band_target_c"], 25.974)
        self.assertFalse(t["moon_on"])

    def test_moon_target_marks_moon_on(self):
        self.write_json("tank_status.json", {"plan": {"moon_target_lux": 0.2}})
        t = tf.targets(self.data)
        self.assertTrue(t["moon_on"])
        self.assertEqual(t["moon_target_lux"], 0.2)


class Text(unittest.TestCase):
    def snap(self, **over):
        rec = {"ts": dt.datetime(2026, 11, 5, 0, 36, tzinfo=UTC), "temp_c": 28.111, "lux": 948.18}
        tgt = {"temp_c": 26.07, "moon_on": False, "moon_target_lux": None,
               "white_level": 1.0, "blue_level": 0.5, "white_plus_level": None,
               "floor_f": 78.6, "ceil_f": 79.1,
               "band_ts": dt.datetime(2026, 11, 4, 20, 1, tzinfo=UTC)}
        base = {"now": dt.datetime(2026, 11, 4, 12, 0, tzinfo=tf.LOCAL_TZ),
                "recorded": rec, "target": tgt,
                "expected": {"total": 115400.0, "sun": 115400.0, "moon": 0.002,
                             "slot_jst": "2027-05-31T12:30",
                             "cue_date": dt.date(2027, 5, 31)}}
        base.update(over)
        return base

    def test_feed_lines(self):
        text = tf.format_feed(self.snap())
        self.assertEqual(text.splitlines(), [
            "<b>Tank feed</b> · Wed Nov 04, 12:00 PST",
            "",
            "<b>Now</b>",
            "28.1 °C · 82.6 °F · 948 lx",
            "read 16:36 PST",
            "",
            "<b>Target</b>",
            "26.1 °C · 78.9 °F · plan",
            "Band 78.6–79.1 °F · set 12:01 PST",
            "Lux 115,400 lx · expected, clear sky",
            "",
            "<b>Lamps</b> white 100% · blue 50%",
        ])

    def test_moon_target_line(self):
        s = self.snap()
        s["target"] = {**s["target"], "moon_on": True, "moon_target_lux": 0.2}
        self.assertIn("Moon target 0.20 lx", tf.format_feed(s))

    def test_expected_lux_maps_cue_date_and_slot(self):
        # 2026-11-04 12:00 PST is 20:00 UTC; Okinawa sees it on the cue date
        # 2027-05-31 at about 12:30 JST, near its solar noon.
        now = dt.datetime(2026, 11, 4, 12, 0, tzinfo=tf.LOCAL_TZ)
        exp = tf.expected_lux(os.path.join(ROOT, "data"), now)
        if exp is None:
            self.skipTest("calendar or light series not present")
        self.assertEqual(exp["cue_date"], dt.date(2027, 5, 31))
        self.assertEqual(exp["slot_jst"], "2027-05-31T12:30")
        self.assertGreater(exp["total"], 10000)

    def test_missing_values_read_as_no_reading(self):
        s = self.snap()
        s["recorded"] = {"ts": None, "temp_c": None, "lux": None}
        s["target"] = {**s["target"], "temp_c": None, "floor_f": None, "ceil_f": None}
        text = tf.format_feed(s)
        self.assertIn("no reading · no reading", text)
        self.assertIn("read —", text)
        self.assertIn("no plan target", text)
        self.assertNotIn("Band", text)


class Refresh(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.fail_edit = None

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "editMessageText" and self.fail_edit:
            raise tf.TelegramError(f"editMessageText: {self.fail_edit}")
        return True

    def callback(self, data="feed_refresh", chat="42"):
        return {"id": "cb1", "data": data,
                "message": {"message_id": 7, "chat": {"id": int(chat)}}}

    def test_press_edits_message_and_answers(self):
        edited = tf.handle_callback(self.call, "42", self.callback(),
                                    lambda: {"now": dt.datetime.now(tf.LOCAL_TZ),
                                             "recorded": {"ts": None, "temp_c": None, "lux": None},
                                             "target": {"temp_c": None, "moon_on": False,
                                                        "moon_target_lux": None,
                                                        "white_level": None, "blue_level": None,
                                                        "white_plus_level": None,
                                                        "floor_f": None, "ceil_f": None,
                                                        "band_ts": None}})
        self.assertTrue(edited)
        self.assertEqual([m for m, _ in self.calls], ["editMessageText", "answerCallbackQuery"])
        self.assertEqual(self.calls[0][1]["message_id"], 7)
        self.assertEqual(self.calls[1][1]["callback_query_id"], "cb1")

    def test_unchanged_message_still_answers(self):
        self.fail_edit = "Bad Request: message is not modified"
        edited = tf.handle_callback(self.call, "42", self.callback(),
                                    lambda: tf.snapshot(tempfile.gettempdir()))
        self.assertFalse(edited)
        self.assertEqual(self.calls[-1][0], "answerCallbackQuery")

    def test_ignores_other_chats_and_other_data(self):
        self.assertFalse(tf.handle_callback(self.call, "42", self.callback(chat="99"),
                                            lambda: None))
        self.assertFalse(tf.handle_callback(self.call, "42", self.callback(data="other"),
                                            lambda: None))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
