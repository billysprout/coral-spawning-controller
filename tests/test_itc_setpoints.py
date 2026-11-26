"""Tests for scripts/itc_setpoints.py and scripts/tank_itc.py (no adb: a fake
phone that behaves like the sandbox app's T1/T2 screen)."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import cues                             # noqa: E402
import itc_setpoints as its             # noqa: E402
import tank_inputs                      # noqa: E402
import tank_itc                         # noqa: E402
import tank_plan                        # noqa: E402
import tank_tick                        # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 2, 18, 0, 0, tzinfo=UTC)
PKG = its.SANDBOX_PKG


def node(text, box, cls="android.widget.TextView", rid="", pkg=PKG):
    x1, y1, x2, y2 = box
    return (f'<node text="{text}" class="{cls}" resource-id="{rid}" package="{pkg}" '
            f'bounds="[{x1},{y1}][{x2},{y2}]"/>')


class FakeItc:
    """The sandbox screen as the phone reports it: a main page with T1 and T2,
    and a drawer with a three-column digit wheel (above, middle, below) that
    opens on a tap on a value. `fault` is what the next Complete does."""

    COLS = (200, 360, 520)
    ROWS = (300, 400, 500)

    def __init__(self, t1=77.3, t2=77.8, fault=None, foreground=PKG):
        self.pkg = PKG
        self.t1, self.t2 = round(t1 * 10), round(t2 * 10)
        self.fault = fault
        self.foreground = foreground
        self.alive = True
        self.drawer = None              # "T1 floor" / "T2 ceiling" / None
        self.digits = None
        self.error = ""
        self.popup = None               # "dialog" or "system": on top of the page
        self.popup_sticky = False       # survives a restart
        self.taps = []
        self.restarts = 0
        self.connected = 0

    # -- the Phone interface
    def connect(self):
        self.connected += 1

    def running(self):
        return self.alive

    def foreground_pkg(self):
        return self.foreground if self.alive else "launcher"

    def back(self):
        self.drawer = None

    def restart(self):
        self.restarts += 1
        self.alive, self.drawer, self.foreground = True, None, PKG
        if not self.popup_sticky:
            self.popup = None

    def screenshot(self):
        return b"PNG fake"

    def cat(self, path):
        return "0"                      # unreadable heartbeat: the check is skipped

    def inject_fault(self, mode):
        self.fault = mode

    def ui_xml(self):
        if not self.alive:
            return "<hierarchy/>"
        n = [node("ITC-306A V3.0", (0, 160, 720, 265)),
             node("77.7", (146, 610, 521, 866)),
             node("T1", (187, 1363, 224, 1406)),
             node(f"{self.t1 / 10:.1f}°F", (224, 1363, 380, 1406)),
             node("T2", (388, 1363, 425, 1406)),
             node(f"{self.t2 / 10:.1f}°F", (425, 1363, 533, 1406))]
        if self.drawer:
            n.append(node(self.drawer, (0, 100, 720, 160)))
            for ci, x in enumerate(self.COLS):
                d = self.digits[ci]
                for ri, text in enumerate((str(d + 1) if d < 9 else "", str(d),
                                           str(d - 1) if d > 0 else "")):
                    n.append(node(text, (x - 50, self.ROWS[ri] - 40,
                                         x + 50, self.ROWS[ri] + 40)))
            if self.error:
                n.append(node(self.error, (0, 560, 720, 600)))
            n.append(node("Cancel", (0, 700, 360, 760)))
            n.append(node("Complete", (360, 700, 720, 760)))
        if self.popup == "dialog":
            n.append(node("Try our IoT hub", (60, 300, 660, 900),
                          "android.app.Dialog", "com.x:id/iot_promo_dialog"))
        elif self.popup == "system":
            n.append(node("App isn't responding", (60, 300, 660, 900),
                          "android.widget.TextView", "android:id/alertTitle", "android"))
        return "<hierarchy>" + "".join(n) + "</hierarchy>"

    def tap(self, x, y):
        self.taps.append((x, y))
        if not self.alive:
            return
        if self.drawer is None:
            if 1363 <= y <= 1406 and 224 <= x <= 380:
                self.drawer, v = "T1 floor", self.t1
            elif 1363 <= y <= 1406 and 425 <= x <= 533:
                self.drawer, v = "T2 ceiling", self.t2
            else:
                return
            self.digits = [v // 100, (v // 10) % 10, v % 10]
            return
        if 700 <= y <= 760 and x < 360:
            self.drawer = None
            return
        if 700 <= y <= 760:
            self.complete()
            return
        for ci, cx in enumerate(self.COLS):
            if abs(x - cx) <= 50:
                for ri, cy in enumerate(self.ROWS):
                    if abs(y - cy) <= 40 and ri != 1:
                        d = self.digits[ci] + (1 if ri == 0 else -1)
                        if 0 <= d <= 9:
                            self.digits[ci] = d

    def complete(self):
        value = self.digits[0] * 100 + self.digits[1] * 10 + self.digits[2]
        fault, self.fault = self.fault, None
        if fault == "crash":
            self.alive = False
            return
        if fault == "reject":
            self.error = "Value rejected"
            return
        self.error = ""
        if fault != "drop":
            if self.drawer == "T1 floor":
                self.t1 = value
            else:
                self.t2 = value
        self.drawer = None


def request(floor, ceil, seq=1, age_s=0, **extra):
    return json.dumps({"seq": seq, "ts": (NOW - dt.timedelta(seconds=age_s)).isoformat(),
                       "floor_f": floor, "ceil_f": ceil, **extra})


def run(phone, text, **kw):
    cfg = its.config({"heartbeat_file": None, "recovery_wait_s": 0})
    return its.run(phone, text, cfg, NOW, sleep=lambda s: None, **kw)


class Pure(unittest.TestCase):
    def test_package_guard(self):
        its.check_package(PKG)
        with self.assertRaises(its.Fail) as cm:
            its.check_package("com.inkbird.inkbirdapp")
        self.assertEqual(cm.exception.status, "refused_package")
        its.check_package("com.inkbird.inkbirdapp", allow_real=True)

    def test_request_validation(self):
        cfg = its.config()
        ok = its.parse_request(request(76.04, 76.46), NOW, cfg)
        self.assertEqual((ok["floor_f"], ok["ceil_f"]), (76.0, 76.5))
        for bad in (request(76.5, 76.0), request(76.0, 76.0), request(60, 61),
                    request(80, 90), request(76, 77, age_s=9000), "not json",
                    json.dumps({"seq": 1, "ts": "2026-11-02T18:00:00",
                                "floor_f": 76, "ceil_f": 77})):
            with self.assertRaises(its.Fail) as cm:
                its.parse_request(bad, NOW, cfg)
            self.assertEqual(cm.exception.status, "rejected_request")

    def test_order_never_crosses(self):
        cfg = its.config()

        def order(w, h):
            return its.decide({"floor_f": w[0], "ceil_f": w[1]},
                              {"floor_f": h[0], "ceil_f": h[1]}, cfg)["order"]
        self.assertEqual(order((78.0, 78.5), (76.0, 76.5)), ["ceil", "floor"])
        self.assertEqual(order((76.0, 76.5), (78.0, 78.5)), ["floor", "ceil"])
        self.assertEqual(order((76.2, 76.7), (76.0, 76.5)), ["ceil", "floor"])
        self.assertEqual(order((75.8, 76.3), (76.0, 76.5)), ["floor", "ceil"])
        self.assertTrue(its.decide({"floor_f": 76.0, "ceil_f": 76.5},
                                   {"floor_f": 76.0, "ceil_f": 76.5}, cfg)["noop"])
        with self.assertRaises(its.Fail) as cm:
            order((82.0, 82.5), (76.0, 76.5))
        self.assertEqual(cm.exception.status, "change_exceeds_limit")

    def test_every_order_keeps_floor_below_ceiling(self):
        cfg = its.config()
        grid = [(f / 10, f / 10 + 0.5) for f in range(740, 820, 7)]
        for h in grid:
            for w in grid:
                if abs(w[0] - h[0]) > 5:
                    continue
                p = its.decide({"floor_f": w[0], "ceil_f": w[1]},
                               {"floor_f": h[0], "ceil_f": h[1]}, cfg)
                cur = {"floor": h[0], "ceil": h[1]}
                for name in p["order"]:
                    cur[name] = p["set"][name]
                    self.assertLess(cur["floor"], cur["ceil"], (h, w, p["order"]))


class Flow(unittest.TestCase):
    def test_lowering_writes_floor_then_ceiling(self):
        ph = FakeItc(77.3, 77.8)
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "success", res["reason"])
        self.assertEqual((ph.t1, ph.t2), (760, 765))
        self.assertEqual(res["after"], {"floor_f": 76.0, "ceil_f": 76.5})
        self.assertEqual([s["probe"] for s in res["steps"] if s["step"] == "set"],
                         ["floor", "ceil"])

    def test_raising_writes_ceiling_first(self):
        ph = FakeItc(76.0, 76.5)
        res = run(ph, request(78.0, 78.5))
        self.assertEqual(res["status"], "success", res["reason"])
        self.assertEqual((ph.t1, ph.t2), (780, 785))
        self.assertEqual([s["probe"] for s in res["steps"] if s["step"] == "set"],
                         ["ceil", "floor"])

    def test_no_change_taps_nothing(self):
        ph = FakeItc(76.0, 76.5)
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "no_change")
        self.assertEqual(ph.taps, [])

    def test_dry_run_taps_nothing(self):
        ph = FakeItc()
        res = run(ph, request(76.0, 76.5), dry_run=True)
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(ph.taps, [])
        self.assertEqual((ph.t1, ph.t2), (773, 778))

    def test_one_probe_only_when_the_other_already_matches(self):
        ph = FakeItc(76.0, 77.8)
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "success")
        self.assertEqual(list(res["plan"]["set"]), ["ceil"])

    def test_silent_drop_stops_at_the_first_probe(self):
        ph = FakeItc(77.3, 77.8, fault="drop")
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "failed_verify")
        self.assertTrue(res["retryable"])
        self.assertEqual((ph.t1, ph.t2), (773, 778))    # ceiling never touched

    def test_reject_leaves_the_drawer_cancelled(self):
        ph = FakeItc(fault="reject")
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "write_rejected")
        self.assertIn("rejected", res["reason"])
        self.assertIsNone(ph.drawer)

    def test_crash_on_complete(self):
        ph = FakeItc(fault="crash")
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "app_crashed")

    def test_app_not_in_front_is_restarted_before_any_tap(self):
        ph = FakeItc(foreground="com.inkbird.inkbirdapp")
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(ph.restarts, 1)
        self.assertEqual(res["status"], "success")

    def test_real_app_never_tapped_when_it_takes_the_screen_mid_run(self):
        ph = FakeItc()
        real_tap = ph.tap

        def steal(x, y):
            ph.foreground = "com.inkbird.inkbirdapp"
            real_tap(x, y)
        ph.tap = steal
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "app_not_foreground")
        self.assertEqual(len(ph.taps), 1)                # the one that was sent first

    def test_drawer_left_open_is_closed(self):
        ph = FakeItc()
        ph.drawer, ph.digits = "T1 floor", [7, 7, 3]
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "success")
        self.assertEqual(ph.restarts, 0)

    def test_stale_request_touches_nothing(self):
        ph = FakeItc()
        res = run(ph, request(76.0, 76.5, age_s=99999))
        self.assertEqual(res["status"], "rejected_request")
        self.assertEqual(ph.connected, 0)

    def test_already_done_seq_is_skipped(self):
        ph = FakeItc()
        res = run(ph, request(76.0, 76.5, seq=3), done_seq=3)
        self.assertEqual(res["status"], "already_done")
        self.assertEqual(ph.connected, 0)


class Popups(unittest.TestCase):
    def test_signs_need_a_dialog_or_a_foreign_window(self):
        ok = its.parse_ui(FakeItc().ui_xml())
        self.assertEqual(its.popup_signs(ok, PKG), [])
        ph = FakeItc()
        ph.popup = "dialog"
        self.assertEqual(its.popup_signs(its.parse_ui(ph.ui_xml()), PKG),
                         ["dialog:iot_promo_dialog"])
        ph.popup = "system"
        self.assertEqual(its.popup_signs(its.parse_ui(ph.ui_xml()), PKG),
                         ["dialog:alertTitle"])
        plain = '<hierarchy><node text="x" class="android.view.View" resource-id="" '                 'package="com.other" bounds="[0,0][1,1]"/></hierarchy>'
        self.assertEqual(its.popup_signs(its.parse_ui(plain), PKG),
                         ["other-package:com.other"])

    def test_the_drawer_is_not_a_popup(self):
        ph = FakeItc()
        ph.drawer, ph.digits = "T1 floor", [7, 7, 3]
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "success")

    def test_popup_is_cleared_by_a_restart_never_tapped_away(self):
        ph = FakeItc()
        ph.popup = "dialog"
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "success", res["reason"])
        self.assertEqual(ph.restarts, 1)
        self.assertTrue(any(s["step"] == "health" and "unexpected popup" in
                            " ".join(s.get("problems", [])) for s in res["steps"]))

    def test_popup_that_survives_a_restart_stops_the_run_with_nothing_tapped(self):
        ph = FakeItc()
        ph.popup, ph.popup_sticky = "dialog", True
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "escalate_app_unhealthy")
        self.assertIn("unexpected popup", res["reason"])
        self.assertEqual(ph.taps, [])
        self.assertEqual((ph.t1, ph.t2), (773, 778))

    def test_popup_after_complete_is_reported(self):
        ph = FakeItc()
        real = ph.complete

        def complete():
            real()
            ph.popup = "dialog"
        ph.complete = complete
        res = run(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "unexpected_popup")
        self.assertTrue(res["retryable"])

    def test_evidence_is_kept_and_pruned(self):
        tmp = tempfile.mkdtemp()
        try:
            ph = FakeItc()
            ph.popup, ph.popup_sticky = "dialog", True
            run(ph, request(76.0, 76.5), evidence_dir=tmp)
            files = os.listdir(os.path.join(tmp, "popups"))
            self.assertTrue(any(f.endswith(".xml") for f in files))
            self.assertTrue(any(f.endswith(".png") for f in files))
            for i in range(25):
                its.save_popup_evidence(tmp, "<x/>", b"p", keep=5)
                os.rename(os.path.join(tmp, "popups", sorted(os.listdir(
                    os.path.join(tmp, "popups")))[-1]),
                    os.path.join(tmp, "popups", f"2026{i:04d}.zzz"))
            self.assertLessEqual(len(os.listdir(os.path.join(tmp, "popups"))), 40)
        finally:
            shutil.rmtree(tmp)


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "target.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def apply(self, phone):
        cfg = its.config({"heartbeat_file": None, "recovery_wait_s": 0})
        return its.apply_once(phone, cfg, self.tmp, self.path, sleep=lambda s: None)

    def test_no_request_is_quiet(self):
        self.assertEqual(self.apply(FakeItc())["status"], "no_request")

    def test_success_marks_the_seq_done_and_a_second_pass_does_nothing(self):
        # the request is stamped now(): apply_once reads the real clock
        text = json.dumps({"seq": 2, "ts": its.iso(its.now_utc()),
                           "floor_f": 76.0, "ceil_f": 76.5})
        with open(self.path, "w") as fh:
            fh.write(text)
        ph = FakeItc()
        self.assertEqual(self.apply(ph)["status"], "success")
        with open(os.path.join(self.tmp, "applied.json")) as fh:
            applied = json.load(fh)
        self.assertEqual((applied["seq"], applied["floor_f"]), (2, 76.0))
        taps = len(ph.taps)
        self.assertEqual(self.apply(ph)["status"], "already_done")
        self.assertEqual(len(ph.taps), taps)

    def test_failure_does_not_mark_done(self):
        text = json.dumps({"seq": 2, "ts": its.iso(its.now_utc()),
                           "floor_f": 76.0, "ceil_f": 76.5})
        with open(self.path, "w") as fh:
            fh.write(text)
        self.assertEqual(self.apply(FakeItc(fault="drop"))["status"], "failed_verify")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "applied.json")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "result.json")))


# ---------------------------------------------------------------------------
# cue side
# ---------------------------------------------------------------------------
def plan(c, tank_date="2026-11-02"):
    return SimpleNamespace(temp_hourly_c=c, tank_date=tank_date,
                           local=f"{tank_date}T10:00:00-08:00")


class CueSide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cue = tank_itc.ItcCue({"enabled": True}, self.tmp)
        self.target = os.path.join(self.tmp, "itc", "target.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def req(self):
        with open(self.target) as fh:
            return json.load(fh)

    def test_off_by_default_writes_nothing(self):
        cue = tank_itc.ItcCue(None, self.tmp)
        view, found = cue.step(NOW, plan(25.0))
        self.assertEqual((view, found), ({"enabled": False}, []))
        self.assertFalse(os.path.exists(self.target))

    def test_band_sits_a_fifth_below_and_a_third_above_the_target(self):
        cfg = tank_itc.itc_config({})
        self.assertEqual(tank_itc.band_for(25.0, cfg), (76.8, 77.3))
        self.assertEqual(tank_itc.band_for(24.5, cfg), (75.9, 76.4))

    def test_band_stays_inside_the_bounds(self):
        cfg = tank_itc.itc_config({})
        lo, hi = tank_itc.band_for(10.0, cfg)
        self.assertEqual((lo, hi), (73.0, 73.5))
        lo, hi = tank_itc.band_for(40.0, cfg)
        self.assertEqual((lo, hi), (82.5, 83.0))
        lo, hi = tank_itc.band_for(28.33, cfg)         # the 83 F cap
        self.assertEqual(hi, 83.0)
        self.assertLess(lo, hi)

    def test_first_request_then_quiet_until_the_cue_moves(self):
        view, _ = self.cue.step(NOW, plan(25.0))
        self.assertEqual(view["wrote"], "first_request")
        self.assertEqual((self.req()["seq"], self.req()["floor_f"]), (1, 76.8))
        self.assertTrue(view["pending"])
        self.applied(1, 76.8, 77.3)
        later = NOW + dt.timedelta(hours=2)
        self.assertIsNone(self.cue.step(later, plan(25.0))[0]["wrote"])
        # tiny drift: no request
        self.assertIsNone(self.cue.step(later, plan(25.1))[0]["wrote"])
        # a real move: new request, next seq
        view, _ = self.cue.step(later, plan(26.0))
        self.assertEqual(view["wrote"], "cue_moved")
        self.assertEqual(self.req()["seq"], 2)
        self.assertEqual(self.req()["cue"]["temp_hourly_c"], 26.0)

    def test_requests_are_rate_limited(self):
        self.cue.step(NOW, plan(25.0))
        soon = NOW + dt.timedelta(minutes=10)
        self.assertIsNone(self.cue.step(soon, plan(27.0))[0]["wrote"])
        self.assertEqual(self.req()["seq"], 1)

    def test_raise_and_lower_both_request(self):
        self.cue.step(NOW, plan(25.0))
        self.applied(1, 76.8, 77.3)
        t = NOW + dt.timedelta(hours=2)
        self.assertEqual(self.cue.step(t, plan(26.0))[0]["wrote"], "cue_moved")
        self.applied(2, 78.6, 79.1)
        t += dt.timedelta(hours=2)
        view, _ = self.cue.step(t, plan(25.0))
        self.assertEqual(view["wrote"], "cue_moved")
        self.assertEqual(self.req()["floor_f"], 76.8)
        self.assertEqual(self.req()["seq"], 3)

    def applied(self, seq, floor, ceil):
        with open(os.path.join(self.tmp, "itc", "applied.json"), "w") as fh:
            json.dump({"seq": seq, "ts": NOW.isoformat(), "floor_f": floor,
                       "ceil_f": ceil}, fh)

    def test_applied_band_that_already_matches_needs_no_request(self):
        self.cue.step(NOW, plan(25.0))
        self.applied(1, 76.8, 77.3)
        view, found = self.cue.step(NOW + dt.timedelta(hours=3), plan(25.0))
        self.assertIsNone(view["wrote"])
        self.assertFalse(view["pending"])
        self.assertEqual(found, [])

    def test_unapplied_request_is_refreshed_and_then_alerts(self):
        self.cue.step(NOW, plan(25.0))
        view, found = self.cue.step(NOW + dt.timedelta(minutes=50), plan(25.0))
        self.assertEqual([f["key"] for f in found], ["itc_setpoint_pending"])
        self.assertTrue(found[0]["alert"])
        view, _ = self.cue.step(NOW + dt.timedelta(hours=1, minutes=1), plan(25.0))
        self.assertEqual(view["wrote"], "refresh_unapplied")
        self.assertEqual(self.req()["seq"], 2)

    def test_hard_failure_is_a_finding(self):
        self.cue.step(NOW, plan(25.0))
        with open(os.path.join(self.tmp, "itc", "result.json"), "w") as fh:
            json.dump({"status": "refused_package", "retryable": False,
                       "reason": "x"}, fh)
        _, found = self.cue.step(NOW + dt.timedelta(minutes=1), plan(25.0))
        self.assertIn("itc_setpoint_failed", [f["key"] for f in found])

    def test_no_plan_writes_nothing(self):
        view, found = self.cue.step(NOW, None)
        self.assertEqual(view["idle"], "no plan")
        self.assertFalse(os.path.exists(self.target))

    def test_config_is_validated(self):
        for bad in ({"below_f": 0}, {"bounds_f": [83, 73]}, {"min_interval_s": 5},
                    {"min_change_f": 0}):
            with self.assertRaises(ValueError):
                tank_itc.itc_config(bad)

    def test_cue_request_is_accepted_by_the_applier(self):
        """The two halves agree on the file format and the bounds."""
        for c in (10.0, 22.8, 24.5, 25.0, 26.17, 28.33, 28.89, 40.0):
            d = tempfile.mkdtemp()
            try:
                tank_itc.ItcCue({"enabled": True}, d).step(NOW, plan(c))
                with open(os.path.join(d, "itc", "target.json")) as fh:
                    text = fh.read()
                its.parse_request(text, NOW, its.config())
            finally:
                shutil.rmtree(d)


class InTheTick(unittest.TestCase):
    """The conductor's tick asks the cue side and puts its view in the record."""
    FIX = os.path.join(ROOT, "tests", "fixtures")

    def tank(self, tmp, itc):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {"itc_setpoints": itc})
        cfg["plan"] = tank_plan.plan_config()
        cal = tank_plan.load_calendar(
            os.path.join(self.FIX, "spawn_calendar_2026-11-01.csv"),
            cues.load_params(os.path.join(self.FIX, "cue_params_offset0.json")))

        class Inputs:
            def read_all(self, now):
                return {"temp": tank_inputs.Reading("temp", True, 26.0, now),
                        "lux": tank_inputs.Reading("lux", True, 0.0, now)}
        return tank_tick.Tank(cfg, cal, [], Inputs(), tank_tick.DryRunSink(tmp),
                              notify=lambda *a, **k: None, deadman=lambda: None,
                              data_dir=tmp)

    def test_tick_writes_the_request_from_the_plan(self):
        tmp = tempfile.mkdtemp()
        try:
            rec = self.tank(tmp, {"enabled": True}).tick(
                dt.datetime(2026, 11, 2, 20, 0, tzinfo=UTC))
            self.assertEqual(rec["itc"]["wrote"], "first_request")
            with open(os.path.join(tmp, "itc", "target.json")) as fh:
                req = json.load(fh)
            want = tank_itc.band_for(rec["plan"]["temp_hourly_c"], tank_itc.itc_config({}))
            self.assertEqual((req["floor_f"], req["ceil_f"]), want)
        finally:
            shutil.rmtree(tmp)

    def test_tick_with_it_off_leaves_no_folder(self):
        tmp = tempfile.mkdtemp()
        try:
            rec = self.tank(tmp, {}).tick(dt.datetime(2026, 11, 2, 20, 0, tzinfo=UTC))
            self.assertEqual(rec["itc"], {"enabled": False})
            self.assertFalse(os.path.exists(os.path.join(tmp, "itc")))
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# the real app's drawer: two scroll wheels read by OCR
# ---------------------------------------------------------------------------
REAL = its.REAL_PREFIX + ".inkbirdapp"


class FakeRealItc:
    """The real ITC-306A page: T1/T2 as text, a drawer with a degrees wheel and
    a tenths wheel that move by swipes and can only be read by OCR."""
    STEP = 98

    def __init__(self, t1=76.0, t2=77.4, short_first_swipe=False, blind_reads=0):
        self.pkg = REAL
        self.t1, self.t2 = round(t1 * 10), round(t2 * 10)
        self.drawer = None
        self.sel = None                         # [degrees, tenths] while open
        self.foreground = REAL
        self.swipes, self.taps, self.backs = [], [], 0
        self.short_first = short_first_swipe
        self.blind = blind_reads
        self.reject = False

    def connect(self):
        pass

    def running(self):
        return True

    def foreground_pkg(self):
        return self.foreground

    def cat(self, path):
        return "0"

    def screenshot(self):
        return b"PNG"

    def back(self):
        self.backs += 1
        self.drawer = None

    def _n(self, text, box, rid="", cls="android.widget.TextView"):
        return node(text, box, cls, f"{REAL}:id/{rid}" if rid else "", REAL)

    def ui_xml(self):
        n = [self._n("ITC-306A V3.0", (160, 128, 560, 177), "tv_device_name"),
             self._n("T1", (85, 1280, 122, 1323)),
             self._n(f"{self.t1 / 10:.1f}°F", (202, 1280, 294, 1323), "tv_device_temp1"),
             self._n("", (85, 1247, 294, 1357), "ll_device_temp1",
                     "android.widget.LinearLayout"),
             self._n("T2", (425, 1280, 462, 1323)),
             self._n(f"{self.t2 / 10:.1f}°F", (542, 1280, 634, 1323), "tv_device_temp2"),
             self._n("", (425, 1247, 634, 1357), "ll_device_temp2",
                     "android.widget.LinearLayout")]
        if self.drawer:
            n += [self._n(f"{self.drawer} temp. setting(°F)", (254, 943, 465, 976),
                          "tv_device_choose_tempc_title"),
                  self._n("", (0, 1010, 360, 1410), "lv_device_tempc1", "android.view.View"),
                  self._n("", (360, 1010, 720, 1410), "lv_device_tempc2",
                          "android.view.View"),
                  self._n("", (0, 1420, 720, 1520), "rl_device_choose_tempc_complete",
                          "android.widget.RelativeLayout"),
                  self._n("Complete", (291, 1448, 428, 1491))]
        return "<hierarchy>" + "".join(n) + "</hierarchy>"

    def tap(self, x, y):
        self.taps.append((x, y))
        if self.drawer is None:
            for which, (x1, x2) in ((1, (85, 294)), (2, (425, 634))):
                if 1247 <= y <= 1357 and x1 <= x <= x2:
                    v = self.t1 if which == 1 else self.t2
                    self.drawer, self.sel = f"T{which}", [v // 10, v % 10]
            return
        if 1420 <= y <= 1520:                   # Complete
            v = self.sel[0] * 10 + self.sel[1]
            if not self.reject:
                if self.drawer == "T1":
                    self.t1 = v
                else:
                    self.t2 = v
            self.drawer = None

    def swipe(self, x1, y1, x2, y2, ms):
        self.swipes.append((x1, y1, x2, y2, ms))
        items = round((y1 - y2) / self.STEP)    # drag up = larger
        if self.short_first and len(self.swipes) == 1 and abs(items) > 1:
            items -= 1 if items > 0 else -1
        col = 0 if x1 < 360 else 1
        lim = (30, 120) if col == 0 else (0, 9)
        self.sel[col] = max(lim[0], min(lim[1], self.sel[col] + items))

    def ocr_digits(self, png, box):
        if self.blind:
            self.blind -= 1
            return "x"
        return str(self.sel[0] if box[0] < 360 else self.sel[1])


def run_real(phone, text, **kw):
    cfg = its.config({"heartbeat_file": None})
    return its.run(phone, text, cfg, NOW, sleep=lambda s: None, **kw)


class RealApp(unittest.TestCase):
    def test_reads_t1_t2_from_the_page_ids(self):
        flow = its.Flow(FakeRealItc(), its.config())
        self.assertEqual(flow.read(), {"floor_f": 76.0, "ceil_f": 77.4})

    def test_raise_both_by_wheel(self):
        ph = FakeRealItc()
        res = run_real(ph, request(78.9, 79.4))
        self.assertEqual(res["status"], "success", res["reason"])
        self.assertEqual((ph.t1, ph.t2), (789, 794))
        self.assertEqual(res["after"], {"floor_f": 78.9, "ceil_f": 79.4})

    def test_lower_both_by_wheel(self):
        ph = FakeRealItc(78.9, 79.4)
        res = run_real(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "success", res["reason"])
        self.assertEqual((ph.t1, ph.t2), (760, 765))

    def test_swipes_are_slow_and_inside_the_wheel(self):
        ph = FakeRealItc()
        run_real(ph, request(78.9, 79.4))
        self.assertTrue(ph.swipes)
        for x1, y1, x2, y2, ms in ph.swipes:
            self.assertGreaterEqual(ms, 650)
            self.assertTrue(1010 <= y1 <= 1410 and 1010 <= y2 <= 1410, (y1, y2))

    def test_a_short_swipe_is_corrected_by_looking_again(self):
        ph = FakeRealItc(short_first_swipe=True)
        res = run_real(ph, request(79.5, 80.0))
        self.assertEqual(res["status"], "success", res["reason"])
        self.assertEqual((ph.t1, ph.t2), (795, 800))

    def test_an_unreadable_wheel_is_looked_at_again(self):
        ph = FakeRealItc(blind_reads=3)
        res = run_real(ph, request(76.0, 76.5))
        self.assertEqual(res["status"], "success", res["reason"])

    def test_no_complete_until_the_wheel_shows_the_wanted_value(self):
        ph = FakeRealItc()
        ph.swipe = lambda *a: None               # the wheel never moves
        res = run_real(ph, request(78.9, 79.4))
        self.assertEqual(res["status"], "wheel_not_converged")
        self.assertEqual((ph.t1, ph.t2), (760, 774))
        self.assertGreaterEqual(ph.backs, 1)     # cancelled
        self.assertFalse([t for t in ph.taps if t[1] >= 1420])   # never Complete

    def test_rejected_write_is_reported_and_not_retried(self):
        ph = FakeRealItc()
        ph.reject = True
        res = run_real(ph, request(78.9, 79.4))
        self.assertEqual(res["status"], "failed_verify")
        self.assertEqual((ph.t1, ph.t2), (760, 774))

    def test_real_app_is_not_restarted_or_poked_when_off_the_page(self):
        ph = FakeRealItc()
        ph.foreground = "com.android.launcher3"
        ph.restart = lambda: self.fail("restarted the real app")
        res = run_real(ph, request(78.9, 79.4))
        self.assertEqual(res["status"], "escalate_app_unhealthy")
        self.assertEqual(ph.taps, [])

    def test_real_app_needs_the_opt_in(self):
        with self.assertRaises(its.Fail):
            its.check_package(REAL)

    def test_one_wheel_only_when_only_one_probe_differs(self):
        ph = FakeRealItc(76.0, 77.4)
        res = run_real(ph, request(76.0, 77.9))
        self.assertEqual(list(res["plan"]["set"]), ["ceil"])
        self.assertEqual((ph.t1, ph.t2), (760, 779))


class RequestBand(unittest.TestCase):
    def test_band_around_a_rise_above_the_measure(self):
        self.assertEqual(its.band_f(78.8 + 0.3, 0.2, 0.3, (73, 83)), (78.9, 79.4))
        self.assertEqual(its.band_f(82.9, 0.2, 0.3, (73, 83)), (82.5, 83.0))


class WheelGlyphs(unittest.TestCase):
    """Reading the real wheels from real screenshots (strips of the selected row,
    720 px wide, cut from screenshots taken from the Docker emulator)."""
    FIX = os.path.join(ROOT, "tests", "fixtures", "itc_wheel")
    # name: (degrees, tenths) shown in the selected row
    EXPECT = {"t2a": ("77", "4"), "t2b": ("80", "0"), "w3": ("77", "2"),
              "drawer_real": ("76", "0"), "s3": ("79", "3"), "s4": ("79", "4")}

    def read(self, name):
        from PIL import Image
        import io
        strip = Image.open(os.path.join(self.FIX, name + ".png"))
        canvas = Image.new("L", (720, 1520), 255)
        canvas.paste(strip, (0, 1162))
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        ph = its.Phone("x", REAL)
        return (ph.ocr_digits(buf.getvalue(), (40, 1162, 320, 1258)),
                ph.ocr_digits(buf.getvalue(), (400, 1162, 680, 1258)))

    def test_every_screenshot_reads_right(self):
        for name, want in self.EXPECT.items():
            self.assertEqual(self.read(name), want, name)

    def test_templates_cover_all_ten_digits(self):
        glyphs = its.load_glyphs()
        self.assertEqual(sorted(glyphs), list("0123456789"))

    def test_a_smudge_is_unreadable_not_a_wrong_digit(self):
        from PIL import Image
        gray = Image.new("L", (280, 96), 255)
        for x in range(120, 150):                  # one solid block
            for y in range(30, 66):
                gray.putpixel((x, y), 0)
        bits = its.glyph_segments(gray)
        self.assertEqual(len(bits), 1)
        self.assertIsNone(its.classify_glyph(bits[0], its.load_glyphs()))

    def test_blank_row_reads_nothing(self):
        from PIL import Image
        self.assertEqual(its.glyph_segments(Image.new("L", (280, 96), 255)), [])


if __name__ == "__main__":
    unittest.main()
