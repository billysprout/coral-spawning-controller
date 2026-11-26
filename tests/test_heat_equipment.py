"""The PC fan (equipment not on a plug) in the heat data."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import heat_equipment as he             # noqa: E402
import heat_report as hr                # noqa: E402
import tank_heat_log as hl              # noqa: E402
import tank_inputs as ti                # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 3, 22, 0, 0, tzinfo=UTC)


def at(m):
    return T0 + dt.timedelta(minutes=m)


class Equipment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_before_the_first_entry_it_is_unknown_never_off(self):
        eq = he.load(self.tmp)                              # the default
        self.assertEqual(he.state_at(eq, "pc_fan", dt.datetime(2026, 11, 3, 12, tzinfo=UTC)),
                         "unknown")
        self.assertEqual(he.state_at(eq, "pc_fan", dt.datetime(2026, 11, 4, tzinfo=UTC)), "on")
        self.assertEqual(he.state_at(eq, "no_such_thing", T0), "unknown")

    def test_default_is_written_when_asked_and_survives_a_reload(self):
        he.load(self.tmp, create=True)
        self.assertTrue(os.path.exists(he.path_for(self.tmp)))
        self.assertEqual(he.state_at(he.load(self.tmp), "pc_fan", at(0)), "on")

    def test_set_state_adds_an_era_with_its_voltage_and_keeps_order(self):
        he.set_state(self.tmp, "pc_fan", "off", at(60), None, "switched off")
        he.set_state(self.tmp, "pc_fan", "on", at(120), 5.0, "back on at 5 V")
        eq = he.load(self.tmp)
        # the guessed default was replaced by the first real entry, so before it: unknown
        self.assertEqual(he.state_at(eq, "pc_fan", at(30)), "unknown")
        self.assertEqual(he.state_at(eq, "pc_fan", at(90)), "off")
        self.assertEqual(he.view_at(eq, at(130))["pc_fan"],
                         {"state": "on", "voltage_v": 5.0, "power_pct": None})
        he.set_state(self.tmp, "pc_fan", "on", at(200), None, "adjuster", 20.0)
        self.assertEqual(he.view_at(he.load(self.tmp), at(210))["pc_fan"]["power_pct"], 20.0)
        times = [e["since"] for e in eq["pc_fan"]]
        self.assertEqual(times, sorted(times))

    def test_bad_entries_are_ignored_and_a_bad_state_refused(self):
        with open(he.path_for(self.tmp), "w") if os.makedirs(os.path.dirname(he.path_for(self.tmp)),
                                                              exist_ok=True) is None else None as fh:
            json.dump({"pc_fan": [{"since": "nonsense", "state": "on"},
                                  {"since": "2026-11-01T00:00:00+00:00", "state": "maybe"},
                                  {"since": "2026-11-01T00:00:00+00:00", "state": "off"}]}, fh)
        self.assertEqual(he.state_at(he.load(self.tmp), "pc_fan", T0), "off")
        with self.assertRaises(ValueError):
            he.set_state(self.tmp, "pc_fan", "half", T0)

    def test_cli_show_and_set(self):
        self.assertEqual(he.main(["--data", self.tmp, "set", "pc_fan", "on", "--voltage", "5",
                                  "--at", "2026-11-03T20:00:00+00:00"]), 0)
        self.assertEqual(he.main(["--data", self.tmp, "show"]), 0)
        self.assertEqual(he.view_at(he.load(self.tmp), at(0))["pc_fan"]["voltage_v"], 5.0)


class InTheReport(unittest.TestCase):
    def states(self, fan_by_minute):
        return [{"ts": at(m), "lamps": frozenset({"200W"}), "known": True, "probe_c": None,
                 "lux": None, "fan": fan_by_minute(m)} for m in range(200)]

    def readings(self):
        return [{"ts": at(i * 5), "temp_c": 26.0 - 0.02 * i, "t1_f": 77.1, "t2_f": 77.6,
                 "heating": False} for i in range(30)]

    def test_windows_carry_the_fan_state_and_group_by_it(self):
        w = hr.delta_windows(self.readings(), self.states(lambda m: "on"))
        self.assertTrue(w)
        self.assertTrue(all(x["fan"] == "on" for x in w))
        g = hr.group_rates(w)
        self.assertEqual((len(g), g[0]["fan"]), (1, "on"))

    def test_a_window_where_the_fan_changed_is_dropped(self):
        w = hr.delta_windows(self.readings(), self.states(lambda m: "on" if m >= 60 else "unknown"))
        for x in w:
            self.assertFalse(x["start"] < at(60) < x["end"])
        self.assertEqual({x["fan"] for x in w}, {"unknown", "on"})

    def test_the_same_lamps_with_and_without_the_fan_are_separate_rows(self):
        a = hr.delta_windows(self.readings(), self.states(lambda m: "on"))
        b = hr.delta_windows(self.readings(), self.states(lambda m: "off"))
        g = hr.group_rates(a + b)
        self.assertEqual(sorted(x["fan"] for x in g), ["off", "on"])

    def test_states_loaded_from_files_take_the_fan_from_equipment_json(self):
        tmp = tempfile.mkdtemp()
        try:
            he.set_state(tmp, "pc_fan", "off", dt.datetime(2026, 11, 1, tzinfo=UTC))
            he.set_state(tmp, "pc_fan", "on", at(0), 5.0)
            with open(os.path.join(tmp, "tank_inputs-20261103.jsonl"), "w") as fh:
                for m in (-30, 30):
                    fh.write(json.dumps({"ts": at(m).isoformat(), "temp_c": 22.0, "lux": 1.0,
                                         "plugs": {"plug-10": {"on": True}}}) + "\n")
            st = hr.load_states(tmp, hr.DEFAULT_LAMP_MAP)
            self.assertEqual([s["fan"] for s in st], ["off", "on"])
        finally:
            shutil.rmtree(tmp)

    def test_the_page_and_csv_have_a_fan_column(self):
        tmp = tempfile.mkdtemp()
        try:
            res = hr.build(tmp, os.path.join(tmp, "heat"), now=at(300))
            page = open(os.path.join(tmp, "heat", "heat_chart.html"), encoding="utf-8").read()
            self.assertIn("PC fan", page)
            with open(os.path.join(tmp, "heat", "heat_deltas.csv")) as fh:
                self.assertIn("pc_fan", fh.readline())
            self.assertEqual(res["windows"], 0)
        finally:
            shutil.rmtree(tmp)


class InTheLog(unittest.TestCase):
    class Plug:
        name, role, channel = "plug-10", "white", "white"

    class Read:
        ok, on = True, True

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reads = {"temp": ti.Reading("temp", True, 26.0, T0),
                      "lux": ti.Reading("lux", True, 1.0, T0)}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def step(self, log, now):
        return log.step(now, self.reads, [self.Plug()], {"plug-10": self.Read()}, None)

    def test_each_line_records_the_fan_and_follows_changes_to_the_file(self):
        log = hl.HeatLog({}, self.tmp, builder=lambda *a: None)
        self.assertEqual(self.step(log, at(0))["equipment"]["pc_fan"],
                         {"state": "on", "voltage_v": None, "power_pct": None})
        he.set_state(self.tmp, "pc_fan", "on", at(0), 5.0, "now at 5 V")
        os.utime(he.path_for(self.tmp), (T0.timestamp() + 99, T0.timestamp() + 99))   # newer file
        self.assertEqual(self.step(log, at(1))["equipment"]["pc_fan"],
                         {"state": "on", "voltage_v": 5.0, "power_pct": None})
        before = dt.datetime(2026, 11, 3, 6, tzinfo=UTC)
        self.assertEqual(self.step(log, before)["equipment"]["pc_fan"]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
