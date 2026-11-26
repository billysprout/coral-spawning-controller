"""Tests for tank_scan (plug IP drift check) with a fake scanner. Nothing listens
on the network and nothing is sent to a plug.

    python -m unittest tests.test_tank_scan -v       (from the repo root)
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import tank_plan                        # noqa: E402
import tank_plugs                       # noqa: E402
import tank_scan                        # noqa: E402
import tank_tick                        # noqa: E402
from tests.test_tank import CAL, FakeInputs, utc   # noqa: E402


def plug(name, dev_id, ip):
    return tank_plugs.TinyTuyaPlug(name, "unassigned", None, key="K", id=dev_id,
                                   ip=ip, version=3.3)


def scanner_for(found, seen=None):
    def scan(ids, timeout_s):
        if seen is not None:
            seen.append((list(ids), timeout_s))
        return dict(found)
    return scan


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.a = plug("plug-9", "ida", "192.0.2.9")
        self.b = plug("plug-10", "idb", "192.0.2.10")

    def status(self, found):
        return {r["plug"]: r["status"]
                for r in tank_scan.compare([self.a, self.b], found)}

    def test_each_plug_is_ok_drifted_or_not_seen(self):
        got = self.status({"ida": "192.0.2.9", "idb": "192.0.2.44"})
        self.assertEqual(got, {"plug-9": "ok", "plug-10": "drifted"})
        self.assertEqual(self.status({"ida": "192.0.2.9"})["plug-10"], "not_seen")

    def test_plugs_without_an_id_and_ip_are_skipped(self):
        fake = tank_plugs.FakePlug("fake", "white")
        rows = tank_scan.compare([fake, self.a], {"ida": "192.0.2.9"})
        self.assertEqual([r["status"] for r in rows], ["skipped", "ok"])
        self.assertEqual(tank_scan.tuya_plugs([fake, self.a]), [self.a])

    def test_scan_asks_only_for_the_tuya_ids_and_passes_the_timeout(self):
        seen = []
        rows = tank_scan.scan_plugs([self.a, tank_plugs.FakePlug("f", "white"), self.b],
                                    7, scanner_for({"ida": "192.0.2.9"}, seen))
        self.assertEqual(seen, [(["ida", "idb"], 7)])
        self.assertEqual([r["status"] for r in rows], ["ok", "skipped", "not_seen"])

    def test_a_scanner_that_raises_is_reported_not_raised(self):
        def boom(ids, t):
            raise OSError("udp port busy")
        rows = tank_scan.scan_plugs([self.a], 5, boom)
        self.assertEqual(rows[0]["status"], "scan_failed")
        self.assertIn("udp port busy", rows[0]["detail"])

    def test_no_tuya_plugs_means_no_scan(self):
        called = []
        rows = tank_scan.scan_plugs([tank_plugs.FakePlug("f", "white")], 5,
                                    scanner_for({}, called))
        self.assertEqual(called, [])
        self.assertEqual(rows[0]["status"], "skipped")


class FindingTests(unittest.TestCase):
    def setUp(self):
        self.a = plug("plug-9", "ida", "192.0.2.9")
        self.b = plug("plug-10", "idb", "192.0.2.10")

    def test_drift_alerts_and_says_how_to_fix_it(self):
        rows = tank_scan.compare([self.a, self.b],
                                 {"ida": "192.0.2.9", "idb": "192.0.2.44"})
        (f,) = tank_scan.findings(rows)
        self.assertEqual(f["key"], "plug_ip_drift:plug-10")
        self.assertTrue(f["alert"])
        self.assertIn("192.0.2.44", f["message"])
        self.assertIn("scan --write", f["message"])

    def test_one_plug_missing_only_warns(self):
        rows = tank_scan.compare([self.a, self.b], {"ida": "192.0.2.9"})
        (f,) = tank_scan.findings(rows)
        self.assertEqual((f["key"], f["level"], f["alert"]),
                         ("plug_not_seen:plug-10", "warn", False))

    def test_hearing_nothing_is_one_blind_finding_not_one_per_plug(self):
        fs = tank_scan.findings(tank_scan.compare([self.a, self.b], {}))
        self.assertEqual([f["key"] for f in fs], ["scan_blind"])

    def test_all_ok_gives_no_findings(self):
        rows = tank_scan.compare([self.a], {"ida": "192.0.2.9"})
        self.assertEqual(tank_scan.findings(rows), [])


class HealAndWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "plug_array.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def read_bytes(self):
        with open(self.path, "rb") as fh:
            return fh.read()

    def write(self, nl):
        spec = {"plugs": [
            {"name": "plug-9", "driver": "tinytuya", "id": "ida", "ip": "192.0.2.9"},
            {"name": "plug-10", "driver": "tinytuya", "id": "idb", "ip": "192.0.2.10"}]}
        with open(self.path, "wb") as fh:
            fh.write((json.dumps(spec, indent=2).replace("\n", nl) + nl).encode())

    def test_heal_changes_the_object_and_nothing_else(self):
        a, b = plug("plug-9", "ida", "192.0.2.9"), plug("plug-10", "idb", "192.0.2.10")
        rows = tank_scan.compare([a, b], {"ida": "192.0.2.9", "idb": "192.0.2.44"})
        self.assertEqual(tank_scan.heal([a, b], rows), ["plug-10"])
        self.assertEqual((a.ip, b.ip), ("192.0.2.9", "192.0.2.44"))

    def test_write_ips_updates_only_drifted_plugs_and_keeps_line_endings(self):
        for nl in ("\n", "\r\n"):
            self.write(nl)
            a, b = (plug("plug-9", "ida", "192.0.2.9"),
                    plug("plug-10", "idb", "192.0.2.10"))
            rows = tank_scan.compare([a, b], {"ida": "192.0.2.9", "idb": "192.0.2.44"})
            self.assertEqual(tank_scan.write_ips(self.path, rows), ["plug-10"])
            raw = self.read_bytes().decode()
            self.assertEqual("\r\n" in raw, nl == "\r\n")
            ips = {p["name"]: p["ip"] for p in json.loads(raw)["plugs"]}
            self.assertEqual(ips, {"plug-9": "192.0.2.9", "plug-10": "192.0.2.44"})

    def test_write_ips_does_nothing_when_nothing_drifted(self):
        self.write("\n")
        before = self.read_bytes()
        a = plug("plug-9", "ida", "192.0.2.9")
        rows = tank_scan.compare([a], {"ida": "192.0.2.9"})
        self.assertEqual(tank_scan.write_ips(self.path, rows), [])
        self.assertEqual(self.read_bytes(), before)


class ConductorTests(unittest.TestCase):
    NOON = utc("2026-11-02T20:00:00Z")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        self.cfg["plan"] = tank_plan.plan_config()
        self.cal = tank_plan.load_calendar(CAL)
        self.sent = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def tank(self, plugs, inputs):
        return tank_tick.Tank(
            self.cfg, self.cal, plugs, inputs, tank_tick.DryRunSink(self.tmp),
            notify=lambda msg, title="": self.sent.append(msg),
            deadman=lambda: None)

    def scan_tank(self, found, **scan_cfg):
        self.cfg["plug_scan"] = dict(self.cfg["plug_scan"], **scan_cfg)
        self.a = plug("plug-9", "ida", "192.0.2.9")
        self.b = plug("plug-10", "idb", "192.0.2.10")
        tank = self.tank([self.a, self.b], FakeInputs(26.1, 0.0))
        tank.startup_scan(scanner_for(found))
        return tank

    def test_default_config_scans_and_heals(self):
        self.assertEqual(tank_tick.DEFAULT_CONFIG["plug_scan"],
                         {"enabled": True, "timeout_s": 15.0, "heal": True})

    def test_drift_is_healed_for_the_run_and_alerted_once(self):
        tank = self.scan_tank({"ida": "192.0.2.9", "idb": "192.0.2.44"})
        self.assertEqual(self.b.ip, "192.0.2.44")
        first = tank.tick(self.NOON)
        keys = [f["key"] for f in first["findings"]]
        self.assertIn("plug_ip_drift:plug-10", keys)
        self.assertTrue(any("plug-10" in m for m in self.sent))
        self.sent.clear()
        second = tank.tick(self.NOON.replace(minute=1))
        again = [f for f in second["findings"] if f["key"] == "plug_ip_drift:plug-10"]
        self.assertEqual(len(again), 1)               # still shown in the record
        self.assertFalse(again[0]["alert"])           # but not re-alerted

    def test_heal_can_be_switched_off(self):
        self.scan_tank({"ida": "192.0.2.9", "idb": "192.0.2.44"}, heal=False)
        self.assertEqual(self.b.ip, "192.0.2.10")

    def test_disabled_scan_does_not_run(self):
        self.cfg["plug_scan"] = dict(self.cfg["plug_scan"], enabled=False)
        tank = self.tank([plug("p", "ida", "192.0.2.9")], FakeInputs(26.1, 0.0))

        def never(ids, t):
            raise AssertionError("scan ran")
        self.assertEqual(tank.startup_scan(never), [])

    def test_a_healthy_array_adds_no_findings(self):
        tank = self.scan_tank({"ida": "192.0.2.9", "idb": "192.0.2.10"})
        rec = tank.tick(self.NOON)
        self.assertFalse([f for f in rec["findings"]
                          if f["key"].startswith(("plug_ip", "plug_not", "scan_"))])

    def test_timeout_is_validated(self):
        bad = os.path.join(self.tmp, "cfg.json")
        with open(bad, "w") as fh:
            json.dump({"plug_scan": {"timeout_s": 1}}, fh)
        with self.assertRaises(ValueError):
            tank_tick.load_config(bad)


if __name__ == "__main__":
    unittest.main()
