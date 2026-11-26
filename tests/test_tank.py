"""Tests for the plug-array conductor: tank_plan, tank_plugs, tank_inputs,
tank_tick. Fully offline: no hardware, no network, no real tinytuya.

    python -m unittest tests.test_tank -v        (from the repo root)
"""
import csv
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

import cues                             # noqa: E402
import tank_inputs                      # noqa: E402
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


class CalendarPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = tank_plan.load_calendar(CAL, cues.load_params(FIX_PARAMS))
        cls.cfg = tank_plan.plan_config()

    def test_calendar_loads_clean(self):
        self.assertEqual(self.cal.problems, [])
        self.assertEqual(len(self.cal.rows), 365)

    def test_known_morning_transitions(self):
        # 2026-11-02 PST: blue and white together at 06:02 (no blue lead since the
        # 2026-10-30 cue decisions), lights off 17:10
        ev = {(w, k): v for w, k, v in
              tank_plan.day_events(self.cal, self.cfg, dt.date(2026, 11, 2))}
        self.assertIs(ev[("06:02", "blue_on")], True)
        self.assertIs(ev[("06:02", "white_on")], True)
        self.assertIs(ev[("17:10", "white_on")], False)
        self.assertIs(ev[("18:28", "full_dark")], True)

    def test_no_violations_all_year(self):
        t = utc("2026-11-01T12:00:00Z")
        end = utc("2027-10-30T12:00:00Z")
        while t < end:
            p = tank_plan.plan_at(self.cal, self.cfg, t)
            self.assertIsNotNone(p, t)
            self.assertEqual(p.violations, [], (t, p.violations))
            t += dt.timedelta(minutes=3)

    def test_full_dark_excludes_sun_and_moon(self):
        t = utc("2026-11-01T12:00:00Z")
        while t < utc("2027-01-30T12:00:00Z"):
            p = tank_plan.plan_at(self.cal, self.cfg, t)
            if p.full_dark:
                self.assertFalse(p.any_sun)
                self.assertFalse(p.any_moon)
            t += dt.timedelta(minutes=11)

    def test_full_dark_includes_dark_after_moonset(self):
        # 2026-11-12: the moon sets at 19:03, so the night's first full-dark
        # window is the one after moonset (astro_dark), and 21:00 is inside it.
        when = utc("2026-11-13T05:00:00Z")          # 21:00 PST on Nov 12
        self.assertTrue(tank_plan.plan_at(self.cal, self.cfg, when).full_dark)

    def test_full_dark_ends_at_astronomical_dawn(self):
        # Every night's last full-dark minute is the end of a calendar window
        # (astro dawn when the moon is down then); never the later sunrise.
        rows = [self.cal.rows[d] for d in sorted(self.cal.rows)]
        for row, nxt in zip(rows, rows[1:]):
            end = row["astro_dark2_end_utc"] or row["astro_dark_end_utc"]
            if end and end > nxt["blue_on_utc"]:
                self.fail(f"{row['date']}: dark ends {end}, after next sunrise lamp")

    def test_two_windows_when_moon_sets_then_rises_inside_the_night(self):
        for row in self.cal.rows.values():
            n = sum(bool(row[f"astro_dark{s}_start_utc"]) for s in ("", "2"))
            total = 0
            for s in ("", "2"):
                a, b = row[f"astro_dark{s}_start_utc"], row[f"astro_dark{s}_end_utc"]
                if a:
                    total += round((tank_plan._parse_utc(b) - tank_plan._parse_utc(a))
                                   .total_seconds() / 60)
            self.assertEqual(total, int(row["astro_dark_min"]), row["date"])
            self.assertLessEqual(n, 2)

    def test_moon_target_scales_with_illumination(self):
        low = tank_plan.plan_at(self.cal, self.cfg, utc("2026-11-03T09:00:00Z"))
        self.assertTrue(low.any_moon)
        lo, hi = self.cfg["moon_lux_range"]
        self.assertTrue(lo <= low.moon_target_lux <= hi)
        none = tank_plan.plan_at(self.cal, self.cfg, utc("2026-11-02T20:00:00Z"))
        self.assertIsNone(none.moon_target_lux)

    def test_temperature_daily_and_hourly(self):
        offs = [0.0] * 24
        offs[10], offs[11] = 0.2, 0.4
        cfg = tank_plan.plan_config({"hourly_offsets_c": offs})
        daily = float(self.cal.rows["2026-11-02"]["target_c"])
        # 10:30 local on 2026-11-02 (PST = UTC-8) is halfway 0.2 -> 0.4
        p = tank_plan.plan_at(self.cal, cfg, utc("2026-11-02T18:30:00Z"))
        self.assertAlmostEqual(p.temp_daily_c, daily, 3)
        self.assertAlmostEqual(p.temp_hourly_c, daily + 0.3, 3)

    def test_hourly_is_clamped_to_safety_band(self):
        cfg = tank_plan.plan_config({"hourly_offsets_c": [1.0] * 24})
        row = self.cal.rows["2026-11-01"]
        saved = row["target_c"]
        row["target_c"] = "28.8"
        try:
            p = tank_plan.plan_at(self.cal, cfg, utc("2026-11-01T20:00:00Z"))
        finally:
            row["target_c"] = saved
        self.assertEqual(p.temp_hourly_c, cues.safety_band_c()[1])

    def test_outside_calendar_returns_none(self):
        self.assertIsNone(
            tank_plan.plan_at(self.cal, self.cfg, utc("2025-01-01T00:00:00Z")))

    def test_dst_does_not_shift_events(self):
        # UTC columns drive everything: the clock reading moves, the instant
        # does not. Spring forward 2027-03-14: all events still ordered.
        ev = tank_plan.day_events(self.cal, self.cfg, dt.date(2027, 3, 14))
        times = [w for w, _, _ in ev]
        self.assertEqual(times, sorted(times))
        self.assertTrue(any(k == "white_on" for _, k, _ in ev))

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            tank_plan.plan_config({"hourly_offsets_c": [0.0] * 23})
        with self.assertRaises(ValueError):
            tank_plan.plan_config({"hourly_offsets_c": [2.0] * 24})
        with self.assertRaises(ValueError):
            tank_plan.plan_config({"dark_source": "bogus"})
        with self.assertRaises(ValueError):
            tank_plan.plan_config({"moon_lux_range": [0.5, 0.1]})

    def test_stale_calendar_is_reported_not_fatal(self):
        tmp = tempfile.mkdtemp()
        try:
            shutil.copy(CAL, os.path.join(tmp, "spawn_calendar_x.csv"))
            with open(CAL[:-4] + ".meta.json") as fh:
                meta = json.load(fh)
            meta["cue_params_sha256"] = "0" * 64
            with open(os.path.join(tmp, "spawn_calendar_x.meta.json"), "w") as fh:
                json.dump(meta, fh)
            cal = tank_plan.load_calendar(os.path.join(tmp, "spawn_calendar_x.csv"))
            self.assertTrue(any("params_stale" in p for p in cal.problems))
            self.assertIsNotNone(
                tank_plan.plan_at(cal, self.cfg, utc("2026-11-02T20:00:00Z")))
        finally:
            shutil.rmtree(tmp)


class CurveTests(unittest.TestCase):
    def setUp(self):
        self.iv = tank_plan.Interval(utc("2026-11-02T14:00:00Z"),
                                     utc("2026-11-03T00:00:00Z"), "x")
        self.spec = {"ramp_up_min": 30, "ramp_down_min": 30,
                     "shape": "smoothstep", "max": 1.0}

    def level(self, text, spec=None):
        return tank_plan.curve_level(utc(text), self.iv, spec or self.spec)

    def test_zero_outside_and_full_in_middle(self):
        self.assertEqual(self.level("2026-11-02T13:59:00Z"), 0.0)
        self.assertEqual(self.level("2026-11-02T20:00:00Z"), 1.0)
        self.assertEqual(self.level("2026-11-03T00:00:00Z"), 0.0)

    def test_ramp_is_monotonic_up_and_down(self):
        up = [self.level(f"2026-11-02T14:{m:02d}:00Z") for m in range(0, 31)]
        self.assertEqual(up, sorted(up))
        self.assertEqual(up[0], 0.0)
        self.assertEqual(up[-1], 1.0)
        down = [self.level(f"2026-11-02T23:{m:02d}:00Z") for m in range(30, 60)]
        self.assertEqual(down, sorted(down, reverse=True))

    def test_shapes_hit_midpoint_half(self):
        for shape in ("linear", "smoothstep", "cosine"):
            spec = dict(self.spec, shape=shape)
            self.assertAlmostEqual(self.level("2026-11-02T14:15:00Z", spec), 0.5, 3)

    def test_step_when_ramps_are_zero(self):
        spec = {"ramp_up_min": 0, "ramp_down_min": 0}
        self.assertEqual(self.level("2026-11-02T14:00:00Z", spec), 1.0)

    def test_max_caps_level(self):
        spec = dict(self.spec, max=0.6)
        self.assertEqual(self.level("2026-11-02T20:00:00Z", spec), 0.6)


class FakeTinytuya(types.ModuleType):
    """Stands in for the tinytuya package."""

    def __init__(self, reply=None, raises=None, reject_retry_kwargs=False):
        super().__init__("tinytuya")
        outer = self
        self.calls = []

        class OutletDevice:
            def __init__(self, dev_id, **kwargs):
                if reject_retry_kwargs and "connection_retry_limit" in kwargs:
                    raise TypeError("unexpected keyword")
                outer.calls.append((dev_id, kwargs))

            def status(self):
                if raises:
                    raise raises
                return reply

        self.OutletDevice = OutletDevice


class PlugTests(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("tinytuya")

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("tinytuya", None)
        else:
            sys.modules["tinytuya"] = self._saved

    def plug(self, **extra):
        opts = dict(id="dev1", ip="192.0.2.9", version=3.3)
        opts.update(extra)
        return tank_plugs.TinyTuyaPlug("p", "white", None, key="SECRETKEY", **opts)

    def test_on_off_and_watts(self):
        sys.modules["tinytuya"] = FakeTinytuya(
            {"dps": {"1": True, "19": 1234}})
        r = self.plug(watt_dp=19, watt_scale=10).status()
        self.assertTrue(r.ok)
        self.assertTrue(r.on)
        self.assertEqual(r.power_w, 123.4)
        sys.modules["tinytuya"] = FakeTinytuya({"dps": {"1": False}})
        r = self.plug().status()
        self.assertTrue(r.ok)
        self.assertFalse(r.on)
        self.assertIsNone(r.power_w)

    def test_error_reply_is_not_ok(self):
        sys.modules["tinytuya"] = FakeTinytuya(
            {"Error": "Network Error: Unable to Connect", "Err": "901"})
        r = self.plug().status()
        self.assertFalse(r.ok)
        self.assertIn("901", r.detail)

    def test_exception_is_contained(self):
        sys.modules["tinytuya"] = FakeTinytuya(raises=OSError("boom"))
        r = self.plug().status()
        self.assertFalse(r.ok)
        self.assertIn("OSError", r.detail)

    def test_missing_dp_missing_key_missing_package(self):
        sys.modules["tinytuya"] = FakeTinytuya({"dps": {"2": True}})
        self.assertFalse(self.plug().status().ok)
        nokey = tank_plugs.TinyTuyaPlug("p", id="d", ip="1.2.3.4")
        self.assertIn("local key", nokey.status().detail)
        sys.modules["tinytuya"] = None          # makes `import tinytuya` fail
        self.assertIn("not installed", self.plug().status().detail)

    def test_old_tinytuya_without_retry_kwargs(self):
        fake = FakeTinytuya({"dps": {"1": True}}, reject_retry_kwargs=True)
        sys.modules["tinytuya"] = fake
        self.assertTrue(self.plug().status().ok)

    def test_key_never_in_repr_or_reading(self):
        sys.modules["tinytuya"] = FakeTinytuya({"dps": {"1": True}})
        p = self.plug()
        self.assertNotIn("SECRETKEY", repr(p))
        self.assertNotIn("SECRETKEY", repr(p.status()))

    def test_tbd_cannot_switch_and_tinytuya_needs_its_key(self):
        with self.assertRaises(tank_plugs.OutputsDisabled):
            tank_plugs.TbdPlug("x").set_on(True)
        with self.assertRaises(tank_plugs.PlugError):
            tank_plugs.TinyTuyaPlug("x", id="d", ip="1.2.3.4").set_on(True)

    def test_default_config_switches_nothing(self):
        cfg = tank_tick.DEFAULT_CONFIG["outputs"]
        self.assertFalse(cfg["enabled"])
        self.assertFalse(any(cfg["channels"].values()))
        self.assertFalse(cfg["heater"].get("enabled", False))

    def test_channel_mapping(self):
        self.assertEqual(tank_plugs.FakePlug("a", "white").channel, "white")
        self.assertEqual(tank_plugs.FakePlug("a", "halo", "white").channel, "white")
        self.assertIsNone(tank_plugs.FakePlug("a", "halo").channel)
        self.assertIsNone(tank_plugs.FakePlug("a", "heater").channel)
        with self.assertRaises(ValueError):
            tank_plugs.FakePlug("a", "halo", "sun")

    def test_load_plugs_and_keys(self):
        tmp = tempfile.mkdtemp()
        try:
            arr = os.path.join(tmp, "plug_array.json")
            dev = os.path.join(tmp, "devices.json")
            with open(arr, "w") as fh:
                json.dump({"plugs": [
                    {"name": "w", "driver": "tinytuya", "role": "white",
                     "id": "dev1", "ip": "192.0.2.10"},
                    {"name": "kasa", "driver": "kasa-someday", "role": "blue"},
                    {"name": "x", "driver": "fake", "on": True},
                ]}, fh)
            with open(dev, "w") as fh:
                json.dump([{"id": "dev1", "key": "K1", "name": "n"}], fh)
            plugs = tank_plugs.load_plugs(arr, dev)
            self.assertEqual([type(p).__name__ for p in plugs],
                             ["TinyTuyaPlug", "TbdPlug", "FakePlug"])
            self.assertEqual(plugs[0]._key, "K1")
            self.assertFalse(plugs[1].status().ok)      # unknown driver degrades
            with open(arr, "w") as fh:
                json.dump({"plugs": [{"name": "a"}, {"name": "a"}]}, fh)
            with self.assertRaises(ValueError):
                tank_plugs.load_plugs(arr, dev)
        finally:
            shutil.rmtree(tmp)


class InputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.now = utc("2026-11-02T20:00:00Z")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def w1(self, text, name="28-abc"):
        os.makedirs(os.path.join(self.tmp, name), exist_ok=True)
        with open(os.path.join(self.tmp, name, "w1_slave"), "w") as fh:
            fh.write(text)

    def ds(self, **kw):
        return tank_inputs.Ds18b20Sysfs("temp", path=self.tmp, **kw)

    def test_ds18b20_good_and_offset(self):
        self.w1("4b 01 4b 46 7f ff 05 10 e1 : crc=e1 YES\n"
                "4b 01 4b 46 7f ff 05 10 e1 t=24687\n")
        r = self.ds().read(self.now)
        self.assertTrue(r.ok)
        self.assertEqual(r.value, 24.687)
        self.assertEqual(self.ds(offset_c=-0.22).read(self.now).value, 24.467)

    def test_ds18b20_rejects_crc_poweron_and_fault(self):
        self.w1("xx : crc=00 NO\nxx t=24687\n")
        self.assertFalse(self.ds().read(self.now).ok)
        self.w1("xx : crc=e1 YES\nxx t=85000\n")
        self.assertIn("bogus", self.ds().read(self.now).detail)
        self.w1("xx : crc=e1 YES\nxx t=-127000\n")
        self.assertFalse(self.ds().read(self.now).ok)

    def test_ds18b20_absent(self):
        self.assertFalse(self.ds().read(self.now).ok)

    def jsonl(self, lines, **kw):
        path = os.path.join(self.tmp, "lux.jsonl")
        with open(path, "w") as fh:
            fh.write(lines)
        return tank_inputs.JsonlSource("lux", path=path, field="lux", **kw)

    def test_jsonl_reads_newest_and_skips_half_written_line(self):
        src = self.jsonl('{"ts":"2026-11-02T19:58:00+00:00","lux":0.2}\n'
                         '{"ts":"2026-11-02T19:59:00+00:00","lux":0.31}\n'
                         '{"ts":"2026-11-02T20:00:0')
        r = src.read(self.now)
        self.assertTrue(r.ok)
        self.assertEqual(r.value, 0.31)
        self.assertEqual(r.age_s(self.now), 60.0)

    def test_jsonl_bad_input(self):
        self.assertFalse(self.jsonl("").read(self.now).ok)
        self.assertFalse(self.jsonl('{"ts":"2026-11-02T19:59:00","lux":1}\n')
                         .read(self.now).ok)                  # no timezone
        self.assertFalse(self.jsonl('{"ts":"2026-11-02T19:59:00Z","lux":NaN}\n')
                         .read(self.now).ok)
        missing = tank_inputs.JsonlSource("lux", path=os.path.join(self.tmp, "no"))
        self.assertFalse(missing.read(self.now).ok)

    def test_jsonl_failure_line_reports_the_writers_error(self):
        src = self.jsonl('{"ts":"2026-11-02T19:59:00+00:00","lux":null,'
                         '"error":"adb screenshot failed"}\n')
        r = src.read(self.now)
        self.assertFalse(r.ok)
        self.assertIn("adb screenshot failed", r.detail)

    def test_inkbird_input_only_exists_when_configured(self):
        self.assertNotIn("inkbird_temp", tank_inputs.Inputs.from_config({}).sources)
        spec = {"inkbird_temp": {"driver": "jsonl", "path": "x.jsonl",
                                 "field": "temp_c"}}
        self.assertIn("inkbird_temp", tank_inputs.Inputs.from_config(spec).sources)

    def test_read_all_contains_a_raising_source(self):
        class Boom(tank_inputs.Source):
            def read(self, now):
                raise RuntimeError("x")
        out = tank_inputs.Inputs({"temp": Boom("temp"),
                                  "lux": tank_inputs.NoSource("lux")}).read_all(self.now)
        self.assertFalse(out["temp"].ok)
        self.assertFalse(out["lux"].ok)


class FakeInputs:
    def __init__(self, temp=None, lux=None, age_s=0):
        self.temp, self.lux, self.age_s = temp, lux, age_s

    def read_all(self, now):
        ts = now - dt.timedelta(seconds=self.age_s)

        def mk(name, v):
            if v is None:
                return tank_inputs.Reading(name, False, detail="absent")
            return tank_inputs.Reading(name, True, v, ts)
        return {"temp": mk("temp", self.temp), "lux": mk("lux", self.lux)}


class FakeInputsInkbird(FakeInputs):
    """FakeInputs plus the optional Inkbird thermometer (its own age)."""

    def __init__(self, temp, ink, ink_age_s=60):
        super().__init__(temp, 0.0)
        self.ink, self.ink_age_s = ink, ink_age_s

    def read_all(self, now):
        out = super().read_all(now)
        if self.ink is None:
            out["inkbird_temp"] = tank_inputs.Reading(
                "inkbird_temp", False, detail="sensor: adb failed")
        else:
            out["inkbird_temp"] = tank_inputs.Reading(
                "inkbird_temp", True, self.ink,
                now - dt.timedelta(seconds=self.ink_age_s))
        return out


class TickTests(unittest.TestCase):
    NOON = utc("2026-11-02T20:00:00Z")          # 12:00 PST: sun up, no moon
    NIGHT = utc("2026-11-03T05:00:00Z")         # 21:00 PST Nov 2: full dark

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        self.cfg["plan"] = tank_plan.plan_config()
        self.cal = tank_plan.load_calendar(CAL, cues.load_params(FIX_PARAMS))
        self.sent = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def tank(self, plugs, inputs, deadman=None):
        return tank_tick.Tank(
            self.cfg, self.cal, plugs, inputs, tank_tick.DryRunSink(self.tmp),
            notify=lambda msg, title="": self.sent.append(msg),
            deadman=deadman or (lambda: None))

    def test_inkbird_difference_is_logged_not_acted_on(self):
        t = self.tank([], FakeInputsInkbird(25.12, 25.33, ink_age_s=240))
        rec = t.tick(self.NOON)
        self.assertEqual(rec["inkbird_diff_c"], -0.21)      # probe minus Inkbird
        self.assertEqual(rec["inputs"]["inkbird_temp"]["value"], 25.33)
        self.assertEqual(rec["findings"], [f for f in rec["findings"]
                                           if "inkbird" not in f["key"]])
        self.assertEqual(self.sent, [])
        with open(os.path.join(self.tmp, "tank_inputs-20261102.jsonl")) as fh:
            row = json.loads(fh.readlines()[-1])
        self.assertEqual(row["inkbird_c"], 25.33)
        self.assertEqual(row["inkbird_diff_c"], -0.21)

    def test_inkbird_difference_skipped_when_stale_or_failed(self):
        rec = self.tank([], FakeInputsInkbird(25.1, 25.3, ink_age_s=1200)).tick(self.NOON)
        self.assertIsNone(rec["inkbird_diff_c"])
        rec = self.tank([], FakeInputsInkbird(25.1, None)).tick(self.NOON)
        self.assertIsNone(rec["inkbird_diff_c"])
        self.assertEqual(self.sent, [])

    def test_no_inkbird_input_leaves_records_unchanged(self):
        t = self.tank([], FakeInputs(25.1, 0.0))
        rec = t.tick(self.NOON)
        self.assertNotIn("inkbird_temp", rec["inputs"])
        self.assertIsNone(rec["inkbird_diff_c"])
        with open(os.path.join(self.tmp, "tank_inputs-20261102.jsonl")) as fh:
            self.assertNotIn("inkbird_c", json.loads(fh.readlines()[-1]))

    def test_intents_follow_the_plan_and_nothing_executes(self):
        plugs = [tank_plugs.FakePlug("w", "white", on=False),
                 tank_plugs.FakePlug("b", "blue", on=True),
                 tank_plugs.FakePlug("m", "moon", on=False),
                 tank_plugs.FakePlug("h", "halo", on=True),
                 tank_plugs.FakePlug("heat", "heater", on=True)]
        rec = self.tank(plugs, FakeInputs(26.1, 0.0)).tick(self.NOON)
        act = {i["plug"]: i["action"] for i in rec["intents"]}
        self.assertEqual(act, {"w": "would_turn_on", "b": "none", "m": "none",
                               "h": "observe_only", "heat": "observe_only"})
        self.assertFalse(rec["executed"])
        self.assertFalse(any(i["executed"] for i in rec["intents"]))

    def test_halo_follows_white(self):
        plugs = [tank_plugs.FakePlug("h", "halo", "white", on=False)]
        rec = self.tank(plugs, FakeInputs(26.1)).tick(self.NOON)
        self.assertEqual(rec["intents"][0]["action"], "would_turn_on")

    def test_light_leak_alerts_only_in_full_dark(self):
        rec = self.tank([], FakeInputs(26.1, 2.0)).tick(self.NIGHT)
        self.assertTrue(rec["plan"]["full_dark"])
        self.assertIn("light_leak", [f["key"] for f in rec["findings"]])
        self.assertTrue(any("lux" in m for m in self.sent))
        self.sent.clear()
        rec = self.tank([], FakeInputs(26.1, 2.0)).tick(self.NOON)
        self.assertNotIn("light_leak", [f["key"] for f in rec["findings"]])

    def test_light_leak_needs_a_run_of_minutes_when_configured(self):
        # audit 2026-11-06: the quiet floor wanders near the limit, so a run of
        # dark minutes above it means a lamp; one reading does not
        self.cfg["limits"]["dark_lux_confirm_min"] = 3
        t = self.tank([], FakeInputs(26.1, 2.0))
        keys = []
        for k in range(3):
            rec = t.tick(self.NIGHT + dt.timedelta(minutes=k))
            keys.append("light_leak" in [f["key"] for f in rec["findings"]])
        self.assertEqual(keys, [False, False, True])
        self.assertIn("for 3 min", [f["message"] for f in rec["findings"]
                                    if f["key"] == "light_leak"][0])

    def test_a_quiet_minute_restarts_the_light_leak_run(self):
        self.cfg["limits"]["dark_lux_confirm_min"] = 3
        inp = FakeInputs(26.1, 2.0)
        t = self.tank([], inp)
        t.tick(self.NIGHT)
        t.tick(self.NIGHT + dt.timedelta(minutes=1))
        inp.lux = 0.1                                   # back to the floor
        t.tick(self.NIGHT + dt.timedelta(minutes=2))
        inp.lux = 2.0
        rec = t.tick(self.NIGHT + dt.timedelta(minutes=3))
        self.assertNotIn("light_leak", [f["key"] for f in rec["findings"]])

    def test_light_leak_confirm_is_validated(self):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG,
                               {"limits": {"dark_lux_confirm_min": 0}})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"limits": {"dark_lux_confirm_min": 0}}, fh)
        try:
            with self.assertRaises(ValueError):
                tank_tick.load_config(fh.name)
        finally:
            os.unlink(fh.name)

    def test_muted_alert_keeps_the_finding_but_sends_nothing(self):
        self.cfg["limits"]["mute_alerts"] = ["light_leak"]
        rec = self.tank([], FakeInputs(26.1, 2.0)).tick(self.NIGHT)
        self.assertIn("light_leak", [f["key"] for f in rec["findings"]])
        self.assertFalse(any("lux" in m for m in self.sent))

    def test_mute_matches_the_key_before_the_colon(self):
        self.cfg["limits"]["mute_alerts"] = ["calendar"]
        t = self.tank([], FakeInputs(26.1, 0.0))
        now = self.NOON
        sent = t._send_alerts(
            [{"key": "calendar:gap", "alert": True, "message": "a"},
             {"key": "calendar_exhausted", "alert": True, "message": "b"}], now)
        self.assertEqual(sent, ["calendar_exhausted"])      # only the exact key

    def test_mute_config_must_be_a_list_of_keys(self):
        import json
        path = os.path.join(self.tmp, "cfg.json")
        for bad in ("calendar_exhausted", [1], [""]):
            with open(path, "w") as fh:
                json.dump({"limits": {"mute_alerts": bad}}, fh)
            with self.assertRaises(ValueError, msg=bad):
                tank_tick.load_config(path)

    def test_temperature_alarms_use_the_inkbird_band(self):
        for value, key in ((29.0, "temp_high"), (22.0, "temp_low")):
            rec = self.tank([], FakeInputs(value, 0.0)).tick(self.NOON)
            self.assertIn(key, [f["key"] for f in rec["findings"]])
        rec = self.tank([], FakeInputs(26.0, 0.0)).tick(self.NOON)
        self.assertNotIn("temp_high", [f["key"] for f in rec["findings"]])
        self.assertTrue(any("UNCALIBRATED" in m for m in self.sent))

    def test_unconfigured_inputs_warn_but_never_alert(self):
        unconfigured = tank_inputs.Inputs.from_config({})
        rec = self.tank([], unconfigured).tick(self.NIGHT)
        self.assertEqual(sorted(f["key"] for f in rec["findings"]),
                         ["lux_unconfigured", "temp_unconfigured"])
        self.assertEqual(self.sent, [])

    def test_configured_but_failing_probe_still_alerts(self):
        rec = self.tank([], FakeInputs(None, 0.0)).tick(self.NOON)
        self.assertIn("temp_missing", [f["key"] for f in rec["findings"]])
        self.assertTrue(self.sent)

    def test_stale_and_missing_temperature_alert(self):
        rec = self.tank([], FakeInputs(26.0, 0.0, age_s=600)).tick(self.NOON)
        self.assertIn("temp_stale", [f["key"] for f in rec["findings"]])
        rec = self.tank([], FakeInputs(None, 0.0)).tick(self.NOON)
        self.assertIn("temp_missing", [f["key"] for f in rec["findings"]])

    def test_plug_offline_alerts_after_the_grace_period(self):
        t = self.tank([tank_plugs.FakePlug("w", "white", ok=False)],
                      FakeInputs(26.0, 0.0))
        keys = lambda r: [f["key"] for f in r["findings"]]      # noqa: E731
        self.assertNotIn("plug_offline:w", keys(t.tick(self.NOON)))
        later = t.tick(self.NOON + dt.timedelta(minutes=6))
        self.assertIn("plug_offline:w", keys(later))

    def test_alert_cooldown(self):
        t = self.tank([], FakeInputs(None, 0.0))
        t.tick(self.NOON)
        n = len(self.sent)
        t.tick(self.NOON + dt.timedelta(minutes=1))
        self.assertEqual(len(self.sent), n)                 # suppressed
        t.tick(self.NOON + dt.timedelta(minutes=31))
        self.assertGreater(len(self.sent), n)                # after cooldown

    def test_outside_calendar_alerts_and_does_not_crash(self):
        rec = self.tank([tank_plugs.FakePlug("w", "white", on=True)],
                        FakeInputs(26.0, 0.0)).tick(utc("2030-01-01T00:00:00Z"))
        self.assertIsNone(rec["plan"])
        self.assertIn("calendar_exhausted", [f["key"] for f in rec["findings"]])
        self.assertEqual(rec["intents"], [])

    def test_deadman_pings_only_when_the_water_is_visible(self):
        pings = []
        self.tank([], FakeInputs(26.0, 0.0), deadman=lambda: pings.append(1)).tick(self.NOON)
        self.assertEqual(len(pings), 1)
        self.tank([], FakeInputs(None, 0.0), deadman=lambda: pings.append(1)).tick(self.NOON)
        self.assertEqual(len(pings), 1)

    def test_sink_files(self):
        t = self.tank([tank_plugs.FakePlug("w", "white", on=True)],
                      FakeInputs(26.0, 0.31))
        t.tick(self.NOON)
        t.tick(self.NOON + dt.timedelta(minutes=1))
        names = sorted(os.listdir(self.tmp))
        self.assertIn("tank_status.json", names)
        self.assertIn("tank_events.jsonl", names)
        inputs = [n for n in names if n.startswith("tank_inputs-")]
        self.assertEqual(len(inputs), 1)
        with open(os.path.join(self.tmp, inputs[0])) as fh:
            lines = [json.loads(x) for x in fh]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["lux"], 0.31)
        with open(os.path.join(self.tmp, "tank_events.jsonl")) as fh:
            self.assertEqual(len(fh.readlines()), 1)     # 2nd tick unchanged

    def test_config_merge_and_validation(self):
        p = os.path.join(self.tmp, "c.json")
        with open(p, "w") as fh:
            json.dump({"limits": {"dark_lux_max": 0.2}, "interval_s": 30}, fh)
        cfg = tank_tick.load_config(p)
        self.assertEqual(cfg["limits"]["dark_lux_max"], 0.2)
        self.assertEqual(cfg["limits"]["plug_offline_min"], 5)    # default kept
        with open(p, "w") as fh:
            json.dump({"plan": {"dark_source": "nope"}}, fh)
        with self.assertRaises(ValueError):
            tank_tick.load_config(p)


if __name__ == "__main__":
    unittest.main()
