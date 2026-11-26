"""Tests for the second (extra plugs) array, tbd placeholders, and wattage sanity.

Offline.

    python -m unittest tests.test_tank_array -v     (from the repo root)
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import tank_health                      # noqa: E402
import tank_inputs                      # noqa: E402
import tank_plan                        # noqa: E402
import tank_plugs                       # noqa: E402
import tank_tick                        # noqa: E402

UTC = dt.timezone.utc
CAL = os.path.join(ROOT, "data", "spawn_calendar_2026-11-01.csv")
T0 = dt.datetime(2026, 11, 2, 20, 0, tzinfo=UTC)


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, obj):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            json.dump(obj, fh)
        return path


class LoaderTests(Files):
    def test_extra_plugs_is_loaded_after_the_plug_array_and_defaults_to_tbd(self):
        a = self.write("a.json", {"plugs": [{"name": "p1", "driver": "fake"}]})
        e = self.write("e.json", {"plugs": [{"name": "e1"}, {"name": "e2",
                                                             "driver": "fake"}]})
        plugs = tank_plugs.load_plugs(a, None, e)
        self.assertEqual([p.name for p in plugs], ["p1", "e1", "e2"])
        self.assertEqual([p.array for p in plugs], ["plugs", "extra", "extra"])
        self.assertEqual(type(plugs[1]).__name__, "TbdPlug")

    def test_missing_extra_file_is_fine(self):
        a = self.write("a.json", {"plugs": [{"name": "p1", "driver": "fake"}]})
        plugs = tank_plugs.load_plugs(a, None, os.path.join(self.tmp, "none.json"))
        self.assertEqual([p.name for p in plugs], ["p1"])

    def test_names_are_unique_across_both_arrays(self):
        a = self.write("a.json", {"plugs": [{"name": "same", "driver": "fake"}]})
        e = self.write("e.json", {"plugs": [{"name": "same"}]})
        with self.assertRaises(ValueError):
            tank_plugs.load_plugs(a, None, e)

    def test_tbd_is_a_placeholder_but_a_typo_is_an_error(self):
        a = self.write("a.json", {"plugs": [{"name": "spare", "driver": "tbd"},
                                            {"name": "typo", "driver": "tinytya"}]})
        spare, typo = tank_plugs.load_plugs(a)
        r = spare.status()
        self.assertFalse(r.ok)
        self.assertFalse(r.configured)
        self.assertEqual(r.error, "not_selected")
        r = typo.status()
        self.assertFalse(r.ok)
        self.assertTrue(r.configured)
        self.assertEqual(r.error, "unknown_driver")
        self.assertIn("tinytya", r.detail)

    def test_expect_w_is_validated(self):
        with self.assertRaises(ValueError):
            tank_plugs.FakePlug("x", expect_w=[10, 5])
        with self.assertRaises(ValueError):
            tank_plugs.FakePlug("x", expect_w=[5])
        self.assertEqual(tank_plugs.FakePlug("x", expect_w=[5, 10]).expect_w,
                         (5.0, 10.0))


class PowerFindingTests(unittest.TestCase):
    def run_one(self, plug):
        return tank_health.power_findings([plug], {plug.name: plug.status()})

    def keys(self, plug):
        return [f["key"] for f in self.run_one(plug)]

    def test_plug_without_watts_is_skipped(self):
        self.assertEqual(self.keys(tank_plugs.FakePlug("p", on=True,
                                                       expect_w=[50, 80])), [])

    def test_off_but_drawing(self):
        self.assertEqual(self.keys(tank_plugs.FakePlug("p", on=False, power_w=40)),
                         ["plug_watts_while_off:p"])
        self.assertEqual(self.keys(tank_plugs.FakePlug("p", on=False, power_w=0.4)),
                         [])

    def test_on_below_and_above_the_expected_range(self):
        self.assertEqual(self.keys(tank_plugs.FakePlug("p", on=True, power_w=0.5,
                                                       expect_w=[50, 80])),
                         ["plug_watts_low:p"])
        self.assertEqual(self.keys(tank_plugs.FakePlug("p", on=True, power_w=95,
                                                       expect_w=[50, 80])),
                         ["plug_watts_high:p"])
        self.assertEqual(self.keys(tank_plugs.FakePlug("p", on=True, power_w=60,
                                                       expect_w=[50, 80])), [])

    def test_no_range_means_no_on_check(self):
        self.assertEqual(self.keys(tank_plugs.FakePlug("p", on=True, power_w=0.0)),
                         [])

    def test_findings_are_warnings_only(self):
        f = self.run_one(tank_plugs.FakePlug("p", on=False, power_w=40))[0]
        self.assertFalse(f["alert"])
        self.assertEqual(f["level"], "warn")

    def test_offline_plug_is_skipped(self):
        p = tank_plugs.FakePlug("p", on=True, ok=False, expect_w=[50, 80])
        self.assertEqual(self.keys(p), [])


class TickTests(Files):
    def tank(self, plugs, sent):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        cfg["plan"] = tank_plan.plan_config()

        class In:
            def read_all(self, now):
                return {"temp": tank_inputs.Reading("temp", True, 25.0, now),
                        "lux": tank_inputs.Reading("lux", True, 5.0, now)}
        return tank_tick.Tank(cfg, tank_plan.load_calendar(CAL), plugs, In(),
                              tank_tick.DryRunSink(self.tmp),
                              notify=lambda m, title="": sent.append(m),
                              deadman=lambda: None)

    def test_tbd_placeholder_never_alarms_or_counts_as_down(self):
        a = self.write("a.json", {"plugs": [{"name": "p1", "driver": "fake",
                                             "on": True}]})
        e = self.write("e.json", {"plugs": [{"name": "later"}]})
        plugs = tank_plugs.load_plugs(a, None, e)
        sent = []
        t = self.tank(plugs, sent)
        for i in range(12):                 # well past plug_offline_min
            rec = t.tick(T0 + dt.timedelta(minutes=i))
        keys = [f["key"] for f in rec["findings"]]
        self.assertFalse([k for k in keys if k.startswith("plug_")], keys)
        self.assertEqual(rec["plug_health"].keys(), {"p1"})
        self.assertFalse(rec["plugs"]["later"]["configured"])
        self.assertEqual(rec["plugs"]["later"]["array"], "extra")
        self.assertEqual(sent, [])

    def test_unknown_driver_does_alarm(self):
        a = self.write("a.json", {"plugs": [{"name": "typo", "driver": "nope"}]})
        sent = []
        t = self.tank(tank_plugs.load_plugs(a), sent)
        for i in range(7):
            rec = t.tick(T0 + dt.timedelta(minutes=i))
        self.assertIn("plug_offline:typo", [f["key"] for f in rec["findings"]])

    def test_watt_findings_and_minute_line_in_the_tick(self):
        plug = tank_plugs.FakePlug("lamp", on=False, power_w=40)
        t = self.tank([plug], [])
        rec = t.tick(T0)
        self.assertIn("plug_watts_while_off:lamp",
                      [f["key"] for f in rec["findings"]])
        day = T0.strftime("%Y%m%d")
        with open(os.path.join(self.tmp, f"tank_inputs-{day}.jsonl")) as fh:
            line = json.loads(fh.readlines()[-1])
        self.assertEqual(line["plugs"]["lamp"]["w"], 40)
        self.assertIn("ms", line["plugs"]["lamp"])

    def test_cli_loads_the_extra_plugs(self):
        a = self.write("a.json", {"plugs": [{"name": "p1", "driver": "fake",
                                             "on": True}]})
        e = self.write("e.json", {"plugs": [{"name": "later"}]})
        cfg = tank_tick.load_config(self.write("c.json", {
            "plug_array": a, "devices": os.path.join(self.tmp, "d.json"),
            "extra_plugs": e}))
        self.assertEqual([p.name for p in tank_tick.load_array(cfg)],
                         ["p1", "later"])

    def test_health_report_marks_the_placeholder_and_exit_stays_zero(self):
        a = self.write("a.json", {"plugs": [{"name": "p1", "driver": "fake",
                                             "on": True}]})
        e = self.write("e.json", {"plugs": [{"name": "later"}]})
        rows = tank_health.check_plugs(tank_plugs.load_plugs(a, None, e))
        text = tank_health.format_report(rows)
        self.assertIn("NOT SET", text)
        self.assertIn("[extra]", text)
        self.assertTrue(all(r["ok"] or not r["configured"] for r in rows))


if __name__ == "__main__":
    unittest.main()
