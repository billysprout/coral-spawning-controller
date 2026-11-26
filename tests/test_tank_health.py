"""Tests for the read-only plug health and status check (tank_health) and the
fields tank_plugs now reports for it.

Offline. A fake tinytuya that refuses everything except status().

    python -m unittest tests.test_tank_health -v     (from the repo root)
"""
import contextlib
import copy
import datetime as dt
import io
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

import tank_health                      # noqa: E402
import tank_inputs                      # noqa: E402
import tank_plan                        # noqa: E402
import tank_plugs                       # noqa: E402
import tank_tick                        # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 2, 20, 0, tzinfo=UTC)
KEY = "SECRET-LOCAL-KEY"
CAL = os.path.join(ROOT, "data", "spawn_calendar_2026-11-01.csv")


def fake_tinytuya(reply, calls):
    """A tinytuya whose device allows status() and records anything else."""
    class Dev:
        def __init__(self, *a, **k):
            pass

        def status(self):
            calls.append("status")
            if isinstance(reply, Exception):
                raise reply
            return reply

        def __getattr__(self, name):            # set_status, set_value, ...
            calls.append(name)
            raise AssertionError(f"health check called {name}()")
    mod = types.ModuleType("tinytuya")
    mod.OutletDevice = Dev
    return mod


class WithFake(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("tinytuya")
        self.calls = []

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("tinytuya", None)
        else:
            sys.modules["tinytuya"] = self._saved

    def use(self, reply):
        sys.modules["tinytuya"] = fake_tinytuya(reply, self.calls)

    def plug(self, key=KEY, **extra):
        opts = dict(id="d", ip="192.0.2.8", version=3.3, revert_dp=9)
        opts.update(extra)
        return tank_plugs.TinyTuyaPlug("plug-8", "unassigned", None, key=key,
                                       **opts)


class ReadingFieldTests(WithFake):
    def test_ok_reply_carries_latency_and_raw_data_points(self):
        self.use({"dps": {"1": False, "9": 0, "38": "off"}})
        r = self.plug().status()
        self.assertTrue(r.ok)
        self.assertIsInstance(r.latency_ms, (int, float))
        self.assertEqual(r.dps, {"1": False, "9": 0, "38": "off"})
        self.assertIsNone(r.error)

    def test_error_codes_map_to_plain_classes(self):
        for code, want in (("905", "unreachable"), ("901", "unreachable"),
                           ("902", "timeout"), ("914", "bad_key_or_version"),
                           ("900", "bad_reply"), ("999", "error")):
            self.use({"Error": "x", "Err": code})
            r = self.plug().status()
            self.assertFalse(r.ok)
            self.assertEqual(r.error, want, code)

    def test_local_problems_have_their_own_classes(self):
        self.use({"dps": {"1": True}})
        self.assertEqual(self.plug(key=None).status().error, "no_key")
        self.assertEqual(self.plug(ip="").status().error, "not_configured")
        self.use({"dps": {"2": True}})
        r = self.plug().status()
        self.assertEqual((r.error, r.dps), ("dp_missing", {"2": True}))
        self.use(OSError("boom"))
        self.assertEqual(self.plug().status().error, "exception")
        self.use("not a dict")
        self.assertEqual(self.plug().status().error, "bad_reply")

    def test_status_sends_only_a_query(self):
        self.use({"dps": {"1": True, "9": 0}})
        self.plug().status()
        self.assertEqual(self.calls, ["status"])


class TrackerTests(unittest.TestCase):
    def reading(self, ok=True, on=True, ms=40, error=None, dps=None, name="p"):
        return tank_plugs.PlugReading(name, ok, on=on if ok else None,
                                      detail="" if ok else "gone",
                                      latency_ms=ms if ok else None,
                                      error=error, dps=dps)

    def feed(self, tracker, readings, start=T0):
        for i, r in enumerate(readings):
            tracker.observe(start + dt.timedelta(minutes=i), r)
        return tracker.snapshot(start + dt.timedelta(minutes=len(readings)))["p"]

    def test_unknown_until_it_has_been_read(self):
        self.assertEqual(tank_health.HealthTracker().snapshot(T0), {})

    def test_healthy_plug(self):
        h = self.feed(tank_health.HealthTracker(), [self.reading()] * 5)
        self.assertEqual(h["level"], "ok")
        self.assertEqual((h["consecutive_failures"], h["success_rate"]), (0, 1.0))
        self.assertEqual(h["latency_ms"], {"last": 40, "avg": 40, "max": 40})
        self.assertIsNone(h["hint"])
        self.assertEqual(h["in_state_s"], 300)

    def test_down_after_three_failures_with_a_hint_then_recovers(self):
        bad = self.reading(ok=False, error="unreachable")
        t = tank_health.HealthTracker()
        h = self.feed(t, [self.reading(), bad, bad])
        self.assertEqual(h["level"], "ok")                  # two misses: not yet
        h = self.feed(t, [bad], start=T0 + dt.timedelta(minutes=3))
        self.assertEqual(h["level"], "down")
        self.assertEqual(h["consecutive_failures"], 3)
        self.assertIn("phone app", h["hint"])
        self.assertEqual(h["last_error"], "unreachable")
        h = self.feed(t, [self.reading()], start=T0 + dt.timedelta(minutes=4))
        self.assertEqual(h["level"], "ok")
        self.assertEqual(h["consecutive_failures"], 0)

    def test_never_seen_working_is_down_straight_away(self):
        h = self.feed(tank_health.HealthTracker(),
                      [self.reading(ok=False, error="bad_key_or_version")])
        self.assertEqual(h["level"], "down")
        self.assertIn("wizard", h["hint"])

    def test_degraded_by_success_rate_and_by_slowness(self):
        ok, bad = self.reading(), self.reading(ok=False, error="timeout")
        pattern = [ok, ok, ok, bad, ok, ok, bad, ok, ok, bad, ok, ok]   # 9/12
        h = self.feed(tank_health.HealthTracker(), pattern)
        self.assertEqual(h["level"], "degraded")
        slow = self.reading(ms=tank_health.SLOW_MS + 500)
        self.assertEqual(self.feed(tank_health.HealthTracker(), [slow] * 3)["level"],
                         "degraded")

    def test_a_single_miss_in_a_long_run_is_still_ok(self):
        pattern = [self.reading()] * 20 + [self.reading(ok=False, error="timeout")]
        self.assertEqual(self.feed(tank_health.HealthTracker(), pattern)["level"],
                         "ok")

    def test_state_flips_are_counted(self):
        pattern = [self.reading(on=True), self.reading(on=False),
                   self.reading(on=True), self.reading(on=True)]
        self.assertEqual(self.feed(tank_health.HealthTracker(),
                                   pattern)["state_flips_1h"], 2)

    def test_findings_are_warnings_never_alerts(self):
        t = tank_health.HealthTracker()
        bad = self.reading(ok=False, error="unreachable")
        self.feed(t, [bad] * 4)
        found = t.findings(t.snapshot(T0 + dt.timedelta(minutes=5)))
        self.assertEqual([f["key"] for f in found], ["plug_down:p"])
        self.assertTrue(all(f["level"] == "warn" and not f["alert"] for f in found))

    def test_changed_data_points_are_flagged(self):
        t = tank_health.HealthTracker()
        self.feed(t, [self.reading(dps={"1": True, "9": 0})])
        t.observe(T0 + dt.timedelta(minutes=2),
                  self.reading(dps={"1": True, "9": 0, "21": 5}))
        found = t.findings(t.snapshot(T0 + dt.timedelta(minutes=3)))
        self.assertIn("plug_dps_changed:p", [f["key"] for f in found])


class OneShotCheckTests(WithFake):
    def test_check_is_read_only_and_never_reveals_the_key(self):
        self.use({"dps": {"1": False, "9": 0, "38": "off"}})
        rows = tank_health.check_plugs([self.plug()])
        self.assertEqual(self.calls, ["status"])
        r = rows[0]
        self.assertTrue(r["ok"])
        self.assertIs(r["key_present"], True)
        self.assertTrue(r["can_revert"])
        self.assertNotIn(KEY, json.dumps(rows))
        text = tank_health.format_report(rows)
        self.assertNotIn(KEY, text)
        self.assertIn("OFF", text)
        self.assertIn("38", text)

    def test_unreachable_plug_gets_a_hint_and_missing_key_is_called_out(self):
        self.use({"Error": "Check device key or version", "Err": "914"})
        rows = tank_health.check_plugs([self.plug(), self.plug(key=None)])
        self.assertEqual(rows[0]["error"], "bad_key_or_version")
        self.assertIn("wizard", rows[0]["hint"])
        text = tank_health.format_report(rows)
        self.assertIn("NO ANSWER", text)
        self.assertIn("MISSING", text)

    def test_a_plug_that_raises_does_not_stop_the_check(self):
        class Boom(tank_plugs.Plug):
            def status(self):
                raise RuntimeError("x")
        rows = tank_health.check_plugs([Boom("a"), tank_plugs.FakePlug("b", on=True)])
        self.assertEqual([r["ok"] for r in rows], [False, True])


class CliAndTickTests(WithFake):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def config(self):
        arr = os.path.join(self.tmp, "plug_array.json")
        dev = os.path.join(self.tmp, "devices.json")
        with open(arr, "w") as fh:
            json.dump({"plugs": [{"name": "plug-8", "driver": "tinytuya",
                                  "id": "d", "ip": "192.0.2.8", "version": 3.3,
                                  "revert_dp": 9}]}, fh)
        with open(dev, "w") as fh:
            json.dump([{"id": "d", "key": KEY}], fh)
        cfg = os.path.join(self.tmp, "cfg.json")
        with open(cfg, "w") as fh:
            json.dump({"plug_array": arr, "devices": dev}, fh)
        return cfg

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tank_tick.main(["health", "--config", self.config(), *args])
        return code, out.getvalue()

    def test_cli_prints_a_report_and_exits_zero_when_all_answer(self):
        self.use({"dps": {"1": True, "9": 0}})
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("plug-8", text)
        self.assertNotIn(KEY, text)
        self.assertEqual(self.calls, ["status"])

    def test_cli_json_and_exit_code_when_a_plug_does_not_answer(self):
        self.use({"Error": "Network Error: Unable to Connect", "Err": "901"})
        code, text = self.run_cli("--json")
        self.assertEqual(code, 1)
        rows = json.loads(text)
        self.assertEqual(rows[0]["error"], "unreachable")
        self.assertNotIn(KEY, text)

    def test_cli_is_registered_with_the_conductor(self):
        import conductor
        self.assertIn("health", conductor.TANK_COMMANDS)
        self.assertIn("health", tank_tick.COMMANDS)

    def test_tick_records_plug_health_without_alerting(self):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        cfg["plan"] = tank_plan.plan_config()
        flaky = tank_plugs.FakePlug("p", on=True, ok=False)

        class In:
            def read_all(self, now):
                return {"temp": tank_inputs.Reading("temp", True, 25.0, now),
                        "lux": tank_inputs.Reading("lux", True, 5.0, now)}
        sent = []
        t = tank_tick.Tank(cfg, tank_plan.load_calendar(CAL), [flaky], In(),
                           tank_tick.DryRunSink(self.tmp),
                           notify=lambda m, title="": sent.append(m),
                           deadman=lambda: None)
        base = dt.datetime(2026, 11, 2, 20, 0, tzinfo=UTC)
        for i in range(4):
            rec = t.tick(base + dt.timedelta(minutes=i))
        self.assertEqual(rec["plug_health"]["p"]["level"], "down")
        self.assertIn("plug_down:p", [f["key"] for f in rec["findings"]])
        self.assertEqual(rec["plugs"]["p"]["error"], None)   # FakePlug sets none
        self.assertFalse([f for f in rec["findings"]
                          if f["key"] == "plug_down:p" and f["alert"]])


if __name__ == "__main__":
    unittest.main()
