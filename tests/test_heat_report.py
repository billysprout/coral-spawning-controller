"""heat_report.py (analysis and chart) and tank_heat_log.py (the granular log)."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import heat_report as hr                # noqa: E402
import tank_heat_log as hl              # noqa: E402
import tank_inputs as ti                # noqa: E402
import tank_plugs                       # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 3, 18, 0, 0, tzinfo=UTC)


def at(minutes):
    return T0 + dt.timedelta(minutes=minutes)


def readings(n=30, step=5, start_c=26.0, per_step=0.1, heating=None):
    return [{"ts": at(i * step), "temp_c": start_c + per_step * i, "t1_f": 77.1,
             "t2_f": 77.6, "heating": heating} for i in range(n)]


def states(minutes=200, lamps=("200W",), known=True):
    return [{"ts": at(m), "lamps": frozenset(lamps), "known": known,
             "probe_c": None, "lux": None} for m in range(minutes)]


class Analysis(unittest.TestCase):
    def test_lamp_attribution_follows_the_swap_date(self):
        eras = hr.DEFAULT_LAMP_MAP
        before = dt.datetime(2026, 11, 2, 12, tzinfo=UTC)
        after = dt.datetime(2026, 11, 3, 17, tzinfo=UTC)
        self.assertEqual(hr.lamp_of("plug-8", before, eras), "200W")
        self.assertEqual(hr.lamp_of("plug-10", before, eras), "Halo")
        self.assertEqual(hr.lamp_of("plug-8", after, eras), "Halo")
        self.assertEqual(hr.lamp_of("plug-10", after, eras), "200W")
        self.assertEqual(hr.lamp_of("plug-9", after, eras), "LED9")
        self.assertIsNone(hr.lamp_of("plug-99", after, eras))

    def test_lamps_over_needs_one_steady_known_set(self):
        s = states()
        self.assertEqual(hr.lamps_over(s, at(10), at(70)), frozenset({"200W"}))
        changed = states()
        for x in changed[40:]:
            x["lamps"] = frozenset({"200W", "Halo"})
        self.assertIsNone(hr.lamps_over(changed, at(10), at(70)))
        self.assertIsNone(hr.lamps_over(states(known=False), at(10), at(70)))
        self.assertIsNone(hr.lamps_over(states(minutes=20), at(10), at(70)))   # too little data

    def test_a_steady_rise_gives_its_rate_per_hour(self):
        w = hr.delta_windows(readings(), states())
        self.assertTrue(w)
        for x in w:
            self.assertAlmostEqual(x["rate_c_h"], 1.2, places=6)     # 0.1 C / 5 min
            self.assertEqual(x["lamps"], frozenset({"200W"}))
            self.assertEqual(x["heater"], "unknown")

    def test_windows_across_a_lamp_change_are_dropped(self):
        s = states()
        for x in s[100:]:
            x["lamps"] = frozenset()
        w = hr.delta_windows(readings(), s)
        for x in w:
            self.assertFalse(x["start"] < at(100) < x["end"])

    def test_heater_state_comes_from_the_readings(self):
        r = readings(heating=True)
        self.assertEqual(hr.heater_over(r, at(0), at(60)), "on")
        r = readings(heating=False)
        self.assertEqual(hr.heater_over(r, at(0), at(60)), "off")
        r = readings()
        r[3]["heating"], r[4]["heating"] = True, False
        self.assertEqual(hr.heater_over(r, at(0), at(60)), "mixed")
        self.assertEqual(hr.heater_over(readings(), at(0), at(60)), "unknown")

    def test_grouping_by_lamps_and_heater(self):
        w = hr.delta_windows(readings(), states(lamps=("200W", "Halo")))
        g = hr.group_rates(w)
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["lamps"], "200W + Halo")
        self.assertAlmostEqual(g[0]["mean_c_h"], 1.2, places=6)
        self.assertEqual(hr.lamps_label(frozenset()), "all off")

    def test_probe_readings_drop_zero_and_implausible_values(self):
        s = states(minutes=30)
        vals = [0.0, 22.5, 85.0, 22.6, 4.0, 22.7]
        for i, v in enumerate(vals):
            s[i * 5]["probe_c"] = v
        p = hr.probe_readings(s)
        self.assertEqual([x["temp_c"] for x in p], [22.5, 22.6, 22.7])


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, rel, rows):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_files_and_chart_are_built_from_what_exists(self):
        base = dt.datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
        self.write("sensors/itc306a_docker.jsonl",
                   [{"ts": (base + dt.timedelta(minutes=5 * i)).isoformat(),
                     "temp_c": 26.0 + 0.05 * i, "t1_f": 77.1, "t2_f": 77.6, "heating": False}
                    for i in range(40)])
        self.write("tank_inputs-20261103.jsonl",
                   [{"ts": (base + dt.timedelta(minutes=m)).isoformat(), "temp_c": 22.0,
                     "lux": 1.0, "plugs": {"plug-10": {"on": True}, "plug-8": {"on": False},
                                           "plug-9": {"on": False}}}
                    for m in range(200)])
        res = hr.build(self.tmp, os.path.join(self.tmp, "heat"), now=base + dt.timedelta(hours=4))
        self.assertEqual(res["readings"], 40)
        self.assertGreater(res["windows"], 5)
        self.assertEqual(res["groups"][0]["lamps"], "200W")          # plug-10 is the 200 W by then
        self.assertEqual(res["groups"][0]["heater"], "off")
        out = os.path.join(self.tmp, "heat")
        for name in ("history.csv", "heat_deltas.csv", "heat_deltas_probe.csv", "heat_chart.html"):
            self.assertTrue(os.path.exists(os.path.join(out, name)), name)
        page = open(os.path.join(out, "heat_chart.html"), encoding="utf-8").read()
        self.assertIn("<svg", page)
        self.assertIn("200W", page)
        self.assertNotIn("<script", page)

    def test_an_empty_data_folder_still_builds(self):
        res = hr.build(self.tmp, os.path.join(self.tmp, "heat"))
        self.assertEqual((res["readings"], res["windows"]), (0, 0))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "heat", "heat_chart.html")))

    def test_the_heat_log_takes_over_where_it_has_lines(self):
        base = dt.datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
        self.write("heat/heat_log-20261103.jsonl",
                   [{"ts": (base + dt.timedelta(minutes=m)).isoformat(), "probe_c": None,
                     "lux": 5.0, "plugs": {"plug-10": {"on": True, "role": "white"},
                                           "plug-8": {"on": True, "role": "white_plus"}}}
                    for m in range(5)])
        eras = hr.load_lamp_map(self.tmp)
        st = hr.load_states(self.tmp, eras)
        self.assertEqual(st[0]["lamps"], frozenset({"200W", "Halo"}))


class Plug:
    def __init__(self, name, role, channel):
        self.name, self.role, self.channel = name, role, channel


class Read:
    def __init__(self, ok=True, on=None):
        self.ok, self.on = ok, on


class HeatLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.now = dt.datetime(2026, 11, 3, 20, 0, 0, tzinfo=UTC)
        os.makedirs(os.path.join(self.tmp, "sensors"))
        with open(os.path.join(self.tmp, "sensors", "itc306a_docker.jsonl"), "w") as fh:
            fh.write(json.dumps({"ts": "2026-11-03T19:58:00+00:00", "temp_c": 26.0,
                                 "t1_f": 77.1, "t2_f": 77.6, "heating": True}) + "\n")
            fh.write(json.dumps({"ts": "2026-11-03T19:59:00+00:00", "temp_c": None,
                                 "error": "x"}) + "\n")
        self.plugs = [Plug("plug-10", "white", "white"), Plug("plug-8", "white_plus", "white_plus"),
                      Plug("plug-9", "blue", "blue")]
        self.reads = {"temp": ti.Reading("temp", True, 26.0, self.now - dt.timedelta(seconds=120)),
                      "lux": ti.Reading("lux", True, 1206.0, self.now),
                      "probe_temp": ti.Reading("probe_temp", False, detail="x")}
        self.plug_reads = {"plug-10": Read(True, True), "plug-8": Read(True, True),
                           "plug-9": Read(False, None)}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def plan(self):
        class P:
            white_on, white_plus_on, blue_on, moon_on, full_dark = True, True, True, False, False
        return P()

    def test_one_line_a_tick_with_lights_heater_and_band(self):
        log = hl.HeatLog({"chart_every_min": 5000 if False else 60}, self.tmp, builder=lambda *a: None)
        row = log.step(self.now, self.reads, self.plugs, self.plug_reads, self.plan())
        self.assertEqual(row["lights"], {"white": True, "white_plus": True, "blue": None, "moon": None})
        self.assertEqual(row["temp_c"], 26.0)
        self.assertEqual(row["temp_age_s"], 120)
        self.assertIsNone(row["probe_c"])
        self.assertEqual(row["band_f"], [77.1, 77.6])
        self.assertIs(row["heating"], True)                       # the newest GOOD feed line
        self.assertEqual(row["plugs"]["plug-9"], {"on": None, "role": "blue"})
        path = os.path.join(self.tmp, "heat", "heat_log-20261103.jsonl")
        with open(path) as fh:
            self.assertEqual(json.loads(fh.readline())["lux"], 1206.0)

    def test_the_chart_is_rebuilt_once_an_hour(self):
        calls = []
        log = hl.HeatLog({}, self.tmp, builder=lambda *a: calls.append(a))
        for m in (0, 1, 30, 59, 61, 62):
            log.step(self.now + dt.timedelta(minutes=m), self.reads, self.plugs,
                     self.plug_reads, self.plan())
        self.assertEqual(len(calls), 2)

    def test_a_failing_builder_or_folder_never_raises(self):
        def boom(*a):
            raise RuntimeError("x")
        log = hl.HeatLog({}, self.tmp, builder=boom)
        self.assertIsNotNone(log.step(self.now, self.reads, self.plugs, self.plug_reads, None))
        self.assertIsNone(hl.HeatLog({}, None).step(self.now, self.reads, self.plugs,
                                                    self.plug_reads, None))

    def test_disabled_writes_nothing_and_config_is_validated(self):
        log = hl.HeatLog({"enabled": False}, self.tmp)
        self.assertIsNone(log.step(self.now, self.reads, self.plugs, self.plug_reads, None))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "heat")))
        with self.assertRaises(ValueError):
            hl.heat_log_config({"chart_every_min": 1})

    def test_missing_feed_leaves_heater_unknown(self):
        os.remove(os.path.join(self.tmp, "sensors", "itc306a_docker.jsonl"))
        row = hl.HeatLog({}, self.tmp, builder=lambda *a: None).step(
            self.now, self.reads, self.plugs, self.plug_reads, None)
        self.assertIsNone(row["heating"])
        self.assertIsNone(row["band_f"])


if __name__ == "__main__":
    unittest.main()
