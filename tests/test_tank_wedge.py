"""Tests for scripts/tank_wedge.py and the wedged-plug alert in the conductor.
No network: the ping and TCP probes are fakes."""
import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import cues                             # noqa: E402
import tank_inputs                      # noqa: E402
import tank_plan                        # noqa: E402
import tank_plugs                       # noqa: E402
import tank_tick                        # noqa: E402
import tank_wedge as tw                 # noqa: E402

UTC = dt.timezone.utc
FIX = os.path.join(ROOT, "tests", "fixtures")
T0 = dt.datetime(2026, 11, 2, 20, 0, 0, tzinfo=UTC)


def minutes(n):
    return T0 + dt.timedelta(minutes=n)


class Reading:
    def __init__(self, ok, error=None):
        self.ok, self.error = ok, error


class Plug:
    name, role, ip = "plug-8", "white", "192.0.2.200"


class Classify(unittest.TestCase):
    def test_wedged_needs_a_network_error_a_live_lan_no_port_and_an_echo(self):
        v = tw.classify("unreachable", 2, "timeout", 1, 4)
        self.assertEqual(v["kind"], "wedged")
        self.assertIn("1/4", v["why"])
        self.assertEqual(tw.classify("timeout", 1, "refused", 3, 4)["kind"], "wedged")

    def test_no_echo_at_all_is_silent(self):
        self.assertEqual(tw.classify("unreachable", 2, "timeout", 0, 4)["kind"], "silent")

    def test_no_peer_means_the_lan_not_the_plug(self):
        self.assertEqual(tw.classify("unreachable", 0, "timeout", 3, 4)["kind"], "lan")

    def test_an_open_control_port_is_something_else(self):
        self.assertEqual(tw.classify("unreachable", 2, "open", 4, 4)["kind"], "other")

    def test_other_error_classes_are_not_a_wedge(self):
        for e in ("bad_key_or_version", "dp_missing", "bad_reply", "no_key", None):
            self.assertEqual(tw.classify(e, 2, "timeout", 3, 4)["kind"], "other", e)

    def test_no_ping_program_is_unknown(self):
        self.assertEqual(tw.classify("unreachable", 2, "timeout", None, 4)["kind"], "unknown")

    def test_config_is_validated(self):
        for bad in ({"min_down_min": 0}, {"probe_every_s": 5}, {"ping_count": 0},
                    {"tcp_port": 0}, {"tcp_timeout_s": 0}):
            with self.assertRaises(ValueError):
                tw.wedge_config(bad)


class Probes(unittest.TestCase):
    def run_ping(self, text, nt):
        proc = subprocess.CompletedProcess([], 0, stdout=text.encode(), stderr=b"")
        with mock.patch("tank_wedge.subprocess.run", return_value=proc), \
                mock.patch("tank_wedge.os.name", "nt" if nt else "posix"):
            return tw.icmp_probe("192.0.2.200", 4)

    def test_linux_ping_output(self):
        out = "4 packets transmitted, 1 received, 75% packet loss, time 3050ms"
        self.assertEqual(self.run_ping(out, nt=False), 1)

    def test_windows_counts_only_replies_from_the_plug(self):
        out = ("Reply from 192.0.2.200: bytes=32 time=95ms TTL=64\n"
               "Reply from 192.0.2.2: Destination host unreachable.\n"
               "Request timed out.\nRequest timed out.\n"
               "    Packets: Sent = 4, Received = 2, Lost = 2 (50% loss)")
        self.assertEqual(self.run_ping(out, nt=True), 1)

    def test_all_lost(self):
        self.assertEqual(self.run_ping("4 packets transmitted, 0 received, 100% packet loss",
                                       nt=False), 0)

    def test_no_ping_program(self):
        with mock.patch("tank_wedge.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(tw.icmp_probe("192.0.2.200", 4))

    def test_tcp_probe_classes(self):
        with mock.patch("tank_wedge.socket.socket") as sock:
            sock.return_value.connect.side_effect = TimeoutError
            self.assertEqual(tw.tcp_probe("1.2.3.4", 6668, 1), "timeout")
            sock.return_value.connect.side_effect = ConnectionRefusedError
            self.assertEqual(tw.tcp_probe("1.2.3.4", 6668, 1), "refused")
            sock.return_value.connect.side_effect = None
            self.assertEqual(tw.tcp_probe("1.2.3.4", 6668, 1), "open")


class Watch(unittest.TestCase):
    def watch(self, echoes=1, tcp="timeout", **cfg):
        self.calls = []

        def icmp(ip, n):
            self.calls.append(("icmp", ip))
            return echoes

        def tcpp(ip, port, t):
            self.calls.append(("tcp", ip, port))
            return tcp
        return tw.WedgeWatch(cfg, icmp=icmp, tcp=tcpp)

    def test_not_judged_before_it_has_been_down_long_enough(self):
        w = self.watch()
        self.assertIsNone(w.judge(Plug, Reading(False, "unreachable"), 2, minutes(0),
                                  minutes(3)))
        self.assertEqual(self.calls, [])

    def test_judged_once_then_cached_until_the_probe_interval(self):
        w = self.watch()
        r = Reading(False, "unreachable")
        v = w.judge(Plug, r, 2, minutes(0), minutes(6))
        self.assertEqual(v["kind"], "wedged")
        self.assertEqual(v["ip"], "192.0.2.200")
        n = len(self.calls)
        w.judge(Plug, r, 2, minutes(0), minutes(8))
        self.assertEqual(len(self.calls), n)                 # cached, no new probe
        w.judge(Plug, r, 2, minutes(0), minutes(12))
        self.assertGreater(len(self.calls), n)               # 5 min later: probed again

    def test_a_working_plug_is_never_judged_and_clears_the_cache(self):
        w = self.watch()
        w.judge(Plug, Reading(False, "unreachable"), 2, minutes(0), minutes(6))
        self.assertIsNone(w.judge(Plug, Reading(True), 2, None, minutes(7)))
        self.assertEqual(self.calls[:2], [("tcp", "192.0.2.200", 6668),
                                          ("icmp", "192.0.2.200")][:2])

    def test_a_bad_key_is_not_probed(self):
        w = self.watch()
        v = w.judge(Plug, Reading(False, "bad_key_or_version"), 2, minutes(0), minutes(9))
        self.assertEqual(v["kind"], "other")
        self.assertEqual(self.calls, [])

    def test_disabled(self):
        w = self.watch(enabled=False)
        self.assertIsNone(w.judge(Plug, Reading(False, "unreachable"), 2, minutes(0),
                                  minutes(9)))

    def test_message_says_what_to_do(self):
        w = self.watch(echoes=1)
        v = w.judge(Plug, Reading(False, "unreachable"), 2, minutes(0), minutes(17))
        msg = tw.wedge_message(Plug, v, 17)
        for part in ("WEDGED", "plug-8 (white)", "17 min", "1/4", "timeout",
                     "192.0.2.200", "Unplug it", "SmartLife"):
            self.assertIn(part, msg)


class DeadPlug(tank_plugs.Plug):
    """A plug whose status query fails the way plug-8 did."""
    driver = "dead"

    def __init__(self, name, role="white", error="unreachable"):
        super().__init__(name, role)
        self.ip = "192.0.2.200"
        self.error = error
        self.up = False

    def status(self):
        if self.up:
            return tank_plugs.PlugReading(self.name, True, on=True)
        return tank_plugs.PlugReading(
            self.name, False, error=self.error,
            detail="Network Error: Device Unreachable (Err 905)")


class InTheTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cal = tank_plan.load_calendar(
            os.path.join(FIX, "spawn_calendar_2026-11-01.csv"),
            cues.load_params(os.path.join(FIX, "cue_params_offset0.json")))
        self.sent = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def tank(self, plugs, echoes=1, tcp="timeout"):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        cfg["plan"] = tank_plan.plan_config()

        class Inputs:
            def read_all(self, now):
                return {"temp": tank_inputs.Reading("temp", True, 26.0, now),
                        "lux": tank_inputs.Reading("lux", True, 0.0, now)}
        t = tank_tick.Tank(cfg, self.cal, plugs, Inputs(), tank_tick.DryRunSink(self.tmp),
                           notify=lambda m, title="": self.sent.append(m),
                           deadman=lambda: None, data_dir=self.tmp)
        t.wedge = tw.WedgeWatch({}, icmp=lambda ip, n: echoes,
                                tcp=lambda ip, p, s: tcp)
        return t

    def run_minutes(self, tank, upto, start=0):
        rec = None
        for m in range(start, upto + 1):
            rec = tank.tick(minutes(m))
        return rec

    def test_a_wedged_plug_gets_its_own_alert_instead_of_offline(self):
        plugs = [DeadPlug("plug-8"), tank_plugs.FakePlug("plug-9", "moon", on=True)]
        t = self.tank(plugs, echoes=1)
        rec = self.run_minutes(t, 7)
        keys = [f["key"] for f in rec["findings"]]
        self.assertIn("plug_wedged:plug-8", keys)
        self.assertNotIn("plug_offline:plug-8", keys)
        self.assertEqual([m for m in self.sent if "WEDGED" in m].__len__(), 1)
        self.assertEqual(rec["plug_wedge"]["plug-8"]["kind"], "wedged")
        self.assertFalse([m for m in self.sent if "offline" in m])

    def test_a_silent_plug_keeps_the_offline_alert_with_the_reason(self):
        plugs = [DeadPlug("plug-8"), tank_plugs.FakePlug("plug-9", "moon", on=True)]
        rec = self.run_minutes(self.tank(plugs, echoes=0), 7)
        keys = [f["key"] for f in rec["findings"]]
        self.assertIn("plug_offline:plug-8", keys)
        self.assertNotIn("plug_wedged:plug-8", keys)
        self.assertTrue([m for m in self.sent if "no ping reply" in m])

    def test_everything_down_is_the_network_not_a_wedge(self):
        plugs = [DeadPlug("plug-8"), DeadPlug("plug-9", "moon")]
        rec = self.run_minutes(self.tank(plugs, echoes=2), 7)
        keys = [f["key"] for f in rec["findings"]]
        self.assertFalse([k for k in keys if k.startswith("plug_wedged")])
        self.assertTrue([m for m in self.sent if "the network" in m])

    def test_recovery_notice_when_the_plug_comes_back(self):
        plug = DeadPlug("plug-8")
        plugs = [plug, tank_plugs.FakePlug("plug-9", "moon", on=True)]
        t = self.tank(plugs, echoes=1)
        self.run_minutes(t, 7)
        plug.up = True
        self.run_minutes(t, 12, start=8)
        self.assertTrue([m for m in self.sent
                         if m.startswith("Recovered: plug_wedged:plug-8")])

    def test_no_alert_before_the_down_time(self):
        plugs = [DeadPlug("plug-8"), tank_plugs.FakePlug("plug-9", "moon", on=True)]
        self.run_minutes(self.tank(plugs), 3)
        self.assertEqual(self.sent, [])

    def test_a_probe_that_raises_does_not_stop_the_tick(self):
        plugs = [DeadPlug("plug-8"), tank_plugs.FakePlug("plug-9", "moon", on=True)]
        t = self.tank(plugs)
        t.wedge = tw.WedgeWatch({}, icmp=lambda ip, n: 1 / 0, tcp=lambda *a: "timeout")
        rec = self.run_minutes(t, 7)
        self.assertIn("plug_offline:plug-8", [f["key"] for f in rec["findings"]])


if __name__ == "__main__":
    unittest.main()
