#!/usr/bin/env python3
"""itc_setpoints.py -- raise and lower the ITC-306A's T1 (floor) and T2 (ceiling)
through the Android app, on request from the cue side (tank_itc.py).

The cue side writes a request file (data/itc/target.json):

    {"seq": 4, "ts": "2026-11-02T18:00:00+00:00", "floor_f": 76.0, "ceil_f": 76.5,
     "target_c": 24.58, "cue": {...}}

This script is the only thing that touches the phone for a write. One run:

  1. request   age, bounds, floor below ceiling, not already done (seq)
  2. health    target app running, resumed, main page up, drawer closed
               (one force-stop and relaunch if not)
  3. read      T1 and T2 off the page (uiautomator dump)
  4. plan      what differs by more than the tolerance; limit per run; order
               that never lets floor and ceiling cross on the way
  5. act       tap the value, step the wheel, Complete, read it back; stop at the
               first value that did not take
  6. verify    read T1 and T2 again

It never retries a write: a failure comes back as a status with a `retryable`
hint in data/itc/result.json, and the next run (the next request, or `apply`
again) decides. A request is marked done by `seq` only on success or no_change.

THE TARGET APP IS A SETTING, NOT A GUESS. ITC_PKG defaults to the test app
com.example.itcsandbox (source: mitm-inkbird/sandbox-app, a copy of the ITC-306A
screen with a heater sim). Any com.inkbird* package is refused unless
ITC_ALLOW_REAL=1.

The two apps are driven differently. The sandbox's drawer is a column of tap-to-
step cells. The real app's drawer (mapped 2026-11-02) is a pair of scroll wheels,
degrees and tenths, with no per-item accessibility nodes: the value is read by
OCR on a crop of the selected row, moved by slow swipes (one item is about a
quarter of the wheel's height), checked again after each swipe, and committed
with Complete; the page's own T1/T2 text (tv_device_temp1/2) is the readback.
Cancelling is the Back key. The real flow never restarts the app (the poller's
recovery knows how to reach the device page); an unhealthy page is escalated.

    python itc_setpoints.py apply [--dry-run] [--request PATH]
    python itc_setpoints.py read              print T1/T2 as the page shows them
    python itc_setpoints.py watch             apply whenever a new request arrives
    python itc_setpoints.py request --measure-f 78.8 --rise-f 0.3
                                              write a request: target = measure + rise
    python itc_setpoints.py request --target-f 79.1     (or an explicit target)

Environment: ITC_ADB_ADDR (default anemones-android:5555), ITC_PKG,
ITC_ALLOW_REAL, TANK_DATA (request/result folder is <data>/itc), ITC_POLL_S
(watch, default 30). Needs adb on PATH.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

UTC = dt.timezone.utc
SANDBOX_PKG = "com.example.itcsandbox"
REAL_PREFIX = "com.inkbird"

DEFAULTS = {
    "bounds_f": [73.0, 83.0],        # never write outside this (the alarm band)
    "tolerance_f": 0.05,             # a probe within this of the request is done
    "max_change_f": 5.0,             # one probe moves at most this far in one run
    "max_age_s": 7200,               # an older request is rejected
    "page_marker": "ITC-306A V3.0",  # text only the main page shows
    "heartbeat_file": "/sdcard/Android/data/com.example.itcsandbox/files/heartbeat.txt",
    "heartbeat_check_s": 4.5,        # None/"" in the config skips the check
    "recovery_wait_s": 8,
}

BOUNDS_RX = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
DECIMAL_RX = re.compile(r"\d+\.\d")
CELL_RX = re.compile(r"^\d{0,2}$")
LABELS = {"Cancel", "Complete", "T1", "T2", "Power", "Settings",
          "Temp. trend chart", "."}


class AdbError(Exception):
    pass


class Fail(Exception):
    """A run that ends early. `status` is the result status."""

    def __init__(self, status: str, reason: str, retryable: bool = False):
        super().__init__(reason)
        self.status, self.reason, self.retryable = status, reason, retryable


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def c_to_f(c: float) -> float:
    return float(c) * 9.0 / 5.0 + 32.0


def f_to_c(f: float) -> float:
    return (float(f) - 32.0) * 5.0 / 9.0


def band_f(target_f: float, below_f: float, above_f: float, bounds_f) -> tuple[float, float]:
    """(floor_f, ceil_f) in tenths of a degree F around a target: below_f under
    it, above_f over it, kept inside bounds_f and at least below_f + above_f
    apart."""
    lo, hi = float(bounds_f[0]), float(bounds_f[1])
    gap = float(below_f) + float(above_f)
    ceil = min(hi, max(lo + gap, float(target_f) + float(above_f)))
    floor = max(lo, min(float(target_f) - float(below_f), ceil - gap))
    return round(floor, 1), round(ceil, 1)


def config(over: dict | None = None) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(over or {})
    lo, hi = cfg["bounds_f"]
    if not 30 <= float(lo) < float(hi) <= 120:
        raise ValueError("bounds_f must be two temperatures 30..120 F, low first")
    return cfg


# ---------------------------------------------------------------------------
# PURE PARTS (unit tested)
# ---------------------------------------------------------------------------
def check_package(pkg: str, allow_real: bool = False) -> None:
    if pkg.startswith(REAL_PREFIX) and not allow_real:
        raise Fail("refused_package",
                   f"{pkg} is the real Inkbird app; set ITC_ALLOW_REAL=1 only "
                   "once its T1/T2 screen has been mapped and tested")


def parse_request(text: str, now: dt.datetime, cfg: dict) -> dict:
    """The validated request: seq, floor_f, ceil_f (tenths of a degree F)."""
    try:
        req = json.loads(text)
        seq = int(req["seq"])
        ts = dt.datetime.fromisoformat(str(req["ts"]))
        floor_f, ceil_f = float(req["floor_f"]), float(req["ceil_f"])
    except (ValueError, KeyError, TypeError) as err:
        raise Fail("rejected_request", f"unreadable request: {err}")
    if ts.tzinfo is None:
        raise Fail("rejected_request", "request ts has no time zone")
    age = (now - ts).total_seconds()
    if age < -300 or age > float(cfg["max_age_s"]):
        raise Fail("rejected_request", f"request is {age:.0f}s old "
                   f"(limit {cfg['max_age_s']}s)")
    lo, hi = cfg["bounds_f"]
    floor_f, ceil_f = round(floor_f, 1), round(ceil_f, 1)
    if not (float(lo) <= floor_f < ceil_f <= float(hi)):
        raise Fail("rejected_request",
                   f"band {floor_f}-{ceil_f} F outside {lo}-{hi} or not floor < ceiling")
    return {"seq": seq, "ts": iso(ts), "floor_f": floor_f, "ceil_f": ceil_f,
            "cue": req.get("cue")}


def decide(want: dict, have: dict, cfg: dict) -> dict:
    """What to change. `want` and `have` hold floor_f and ceil_f. Floor and
    ceiling are written in an order that never lets them cross on the way."""
    tol, lim = float(cfg["tolerance_f"]), float(cfg["max_change_f"])
    change = {}
    for name in ("floor", "ceil"):
        w, h = want[f"{name}_f"], have[f"{name}_f"]
        if abs(w - h) > tol:
            if abs(w - h) > lim:
                raise Fail("change_exceeds_limit",
                           f"{name} {h} -> {w} is more than {lim} F in one run")
            change[name] = w
    if "floor" in change and "ceil" in change:
        # Whatever is written first must leave floor below ceiling.
        if want["ceil_f"] >= have["ceil_f"]:          # moving up: ceiling first
            order = (["ceil", "floor"] if have["floor_f"] < want["ceil_f"]
                     else ["floor", "ceil"])
        else:                                         # moving down: floor first
            order = (["floor", "ceil"] if want["floor_f"] < have["ceil_f"]
                     else ["ceil", "floor"])
    else:
        order = list(change)
    return {"noop": not change, "set": change, "order": order,
            "from": {"floor": have["floor_f"], "ceil": have["ceil_f"]},
            "to": {"floor": want["floor_f"], "ceil": want["ceil_f"]}}


def parse_resumed(text: str) -> tuple[str, str] | None:
    m = re.search(r"mResumedActivity:.*?\s([\w.]+)/([\w.$]+)", text)
    return (m.group(1), m.group(2)) if m else None


def parse_ui(xml_text: str) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for n in root.iter("node"):
        b = BOUNDS_RX.match(n.get("bounds", ""))
        if not b:
            continue
        x1, y1, x2, y2 = map(int, b.groups())
        out.append({"text": n.get("text", ""), "cls": n.get("class", ""),
                    "id": n.get("resource-id", ""), "pkg": n.get("package", ""),
                    "box": (x1, y1, x2, y2),
                    "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2})
    return out


def find(nodes, text):
    return next((n for n in nodes if n["text"] == text), None)


def probe_value(nodes, label):
    """The value node follows its label: 'T1' then '77.3°F'."""
    for i, n in enumerate(nodes[:-1]):
        if n["text"] == label:
            m = DECIMAL_RX.search(nodes[i + 1]["text"])
            return float(m.group()) if m else None
    return None


def node_id(nodes, suffix):
    """First node whose resource id ends with `suffix` (the app's own package
    prefix is not repeated)."""
    return next((n for n in nodes if n.get("id", "").endswith(suffix)), None)


def real_probe_value(nodes, which: int):
    """T1/T2 on the real page: the text of tv_device_temp1 / tv_device_temp2."""
    n = node_id(nodes, f":id/tv_device_temp{which}")
    m = DECIMAL_RX.search(n["text"]) if n else None
    return float(m.group()) if m else None


def ocr_digits(text: str, lo: int, hi: int):
    """An integer from OCR text, or None unless it is inside lo..hi."""
    m = re.fullmatch(r"\s*(\d{1,3})\s*", text or "")
    if not m:
        return None
    v = int(m.group(1))
    return v if lo <= v <= hi else None


def wheel_state(nodes, title):
    """(columns, digits): three columns of [above, middle, below] cells and the
    middle digits, e.g. [7, 7, 3] for 77.3."""
    tnode, cnode = find(nodes, title), find(nodes, "Complete")
    if tnode is None or cnode is None:
        raise Fail("write_failed", f"drawer nodes missing for {title!r}", True)
    top, bottom = tnode["box"][3], cnode["box"][1]
    cells = [n for n in nodes
             if n["box"][1] >= top and n["box"][3] <= bottom
             and n["box"][2] - n["box"][0] <= 150      # the error label is wide
             and CELL_RX.match(n["text"]) and n["cls"] == "android.widget.TextView"]
    cells.sort(key=lambda n: n["cx"])
    cols: list[list[dict]] = []
    for n in cells:
        if cols and abs(n["cx"] - cols[-1][-1]["cx"]) <= 25:
            cols[-1].append(n)
        else:
            cols.append([n])
    if len(cols) != 3 or any(len(c) != 3 for c in cols):
        raise Fail("wheel_not_converged",
                   f"unexpected wheel layout {[len(c) for c in cols]}", True)
    cols = [sorted(c, key=lambda n: n["cy"]) for c in cols]
    digits = []
    for c in cols:
        if not c[1]["text"].isdigit():
            raise Fail("wheel_not_converged",
                       f"wheel middle is not a digit: {c[1]['text']!r}", True)
        digits.append(int(c[1]["text"]))
    return cols, digits


def error_text(nodes) -> str:
    hits = [n["text"] for n in nodes
            if n["text"] and n["text"] not in LABELS and "°" not in n["text"]
            and not n["text"].replace(".", "").isdigit()
            and not n["text"].startswith(("ITC-306A", "T1 ", "T2 "))]
    return hits[-1] if hits else ""


GLYPH_W, GLYPH_H = 16, 24
GLYPH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "itc_wheel_glyphs.json")


def glyph_segments(gray, dark: int = 140) -> list[str]:
    """Split a grayscale crop of one wheel row into digit bitmaps, left to right.
    Each is a GLYPH_W x GLYPH_H string of 0/1 (the glyph's bounding box scaled to
    a fixed size). Faded neighbours are lighter than `dark` and drop out."""
    from PIL import Image
    w, h = gray.size
    px = gray.load()
    cols = [any(px[x, y] < dark for y in range(h)) for x in range(w)]
    out, x = [], 0
    while x < w:
        if not cols[x]:
            x += 1
            continue
        x0 = x
        while x < w and cols[x]:
            x += 1
        if x - x0 < 3:                                  # a speck, not a digit
            continue
        ys = [y for y in range(h) if any(px[i, y] < dark for i in range(x0, x))]
        glyph = gray.crop((x0, ys[0], x, ys[-1] + 1)).resize(
            (GLYPH_W, GLYPH_H), Image.LANCZOS)
        out.append("".join("1" if glyph.getpixel((i, j)) < 170 else "0"
                           for j in range(GLYPH_H) for i in range(GLYPH_W)))
    return out


def classify_glyph(bits: str, glyphs: dict, max_frac: float = 0.15,
                   margin_frac: float = 0.04):
    """The digit whose template is nearest by Hamming distance, or None when the
    nearest is too far or another digit is nearly as close."""
    best = {}
    for label, samples in glyphs.items():
        d = min(sum(a != b for a, b in zip(bits, t)) for t in samples)
        best[label] = d
    ranked = sorted(best.items(), key=lambda kv: kv[1])
    n = GLYPH_W * GLYPH_H
    if not ranked or ranked[0][1] > max_frac * n:
        return None
    if len(ranked) > 1 and ranked[1][1] - ranked[0][1] < margin_frac * n:
        return None
    return ranked[0][0]


def load_glyphs(path: str = GLYPH_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            glyphs = json.load(fh)["glyphs"]
    except (OSError, ValueError, KeyError) as err:
        raise AdbError(f"wheel glyph templates unusable ({path}): {err}")
    return glyphs


_POPUP_IDS = {"android:id/alertTitle", "android:id/parentPanel",
              "android:id/button1", "android:id/button2", "android:id/button3"}
_POPUP_WORDS = ("dialog", "popup", "dlg", "bottomsheet", "interstitial")


def popup_signs(nodes, pkg: str) -> list[str]:
    """Signs in an accessibility dump that something other than the app's own
    page is on top: a dialog or popup class or id, or a window from another
    package (a system dialog, an "app isn't responding" box). Only meaningful
    where the plain page is expected: the T1/T2 drawer is a legitimate dialog,
    so callers skip this while the drawer is open."""
    signs, seen = [], set()
    for n in nodes:
        cls, rid, npkg = n.get("cls", ""), n.get("id", ""), n.get("pkg", "")
        low = (cls + " " + rid).lower()
        why = None
        if rid in _POPUP_IDS or any(w in low for w in _POPUP_WORDS):
            why = f"dialog:{(rid or cls).split('/')[-1][:40]}"
        elif npkg and npkg not in (pkg, "com.android.systemui"):
            why = f"other-package:{npkg}"
        if why and why not in seen:
            seen.add(why)
            signs.append(why)
    return signs


def save_popup_evidence(folder_: str, xml_text: str, png: bytes | None,
                        keep: int = 20) -> None:
    """Keep the dump and screenshot of an unexpected popup, so the next one can
    be recognised by sight. Never raises; keeps the newest `keep` sets."""
    try:
        d = os.path.join(folder_, "popups")
        os.makedirs(d, exist_ok=True)
        stamp = now_utc().strftime("%Y%m%dT%H%M%S")
        with open(os.path.join(d, stamp + ".xml"), "w", encoding="utf-8") as fh:
            fh.write(xml_text)
        if png:
            with open(os.path.join(d, stamp + ".png"), "wb") as fh:
                fh.write(png)
        names = sorted({os.path.splitext(f)[0] for f in os.listdir(d)})
        for old in names[:-keep]:
            for ext in (".xml", ".png"):
                try:
                    os.remove(os.path.join(d, old + ext))
                except OSError:
                    pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# THE DEVICE
# ---------------------------------------------------------------------------
class Phone:
    """adb calls. Tests replace this class."""

    def __init__(self, addr: str, pkg: str):
        self.addr, self.pkg = addr, pkg
        self._glyphs = None

    def _run(self, args, timeout=30) -> bytes:
        try:
            p = subprocess.run(args, capture_output=True, timeout=timeout)
        except FileNotFoundError as err:
            raise AdbError(f"{args[0]} not installed: {err}")
        except subprocess.TimeoutExpired:
            raise AdbError(f"timeout after {timeout}s: {' '.join(args[:5])}")
        if p.returncode != 0:
            msg = (p.stderr or p.stdout).decode("utf-8", "replace").strip()
            raise AdbError(f"{' '.join(args[:5])} exit {p.returncode}: {msg[:120]}")
        return p.stdout

    def connect(self) -> None:
        if not self.addr.startswith("emulator-"):
            out = self._run(["adb", "connect", self.addr], timeout=15).decode()
            if "connected" not in out:
                raise AdbError(f"adb connect {self.addr}: {out.strip()[:100]}")

    def shell(self, cmd: str, timeout=30) -> str:
        return self._run(["adb", "-s", self.addr, "shell", cmd],
                         timeout).decode("utf-8", "replace")

    def ui_xml(self) -> str:
        self.shell("uiautomator dump /data/local/tmp/itc_ui.xml >/dev/null", 30)
        return self._run(["adb", "-s", self.addr, "exec-out", "cat",
                          "/data/local/tmp/itc_ui.xml"], 30).decode("utf-8", "replace")

    def screenshot(self) -> bytes:
        return self._run(["adb", "-s", self.addr, "exec-out", "screencap", "-p"], 40)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int) -> None:
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {ms}")

    def ocr_digits(self, png: bytes, box) -> str:
        """The digits shown in a crop of a screenshot, read by matching each glyph
        against captured templates of the wheel's own font (Pillow only). Empty
        string when any glyph is not clearly one digit."""
        import io
        try:
            from PIL import Image
        except ImportError as err:
            raise AdbError(f"Pillow is not installed: {err}")
        if self._glyphs is None:
            self._glyphs = load_glyphs()
        gray = Image.open(io.BytesIO(png)).convert("L").crop(tuple(box))
        out = [classify_glyph(g, self._glyphs) for g in glyph_segments(gray)]
        return "".join(out) if out and None not in out else ""

    def foreground_pkg(self) -> str | None:
        r = parse_resumed(self.shell("dumpsys activity activities | grep -m1 mResumedActivity"))
        return r[0] if r else None

    def running(self) -> bool:
        return bool(self.shell(f"pidof {self.pkg} || true").strip())

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {x} {y}")

    def back(self) -> None:
        self.shell("input keyevent KEYCODE_BACK")

    def restart(self) -> None:
        self.shell(f"am force-stop {self.pkg}")
        # monkey goes through the launcher and brings the app to the front;
        # `am start` can deliver to a background task and leave another app on top.
        self.shell(f"monkey -p {self.pkg} -c android.intent.category.LAUNCHER 1")

    def cat(self, path: str) -> str:
        return self.shell(f"cat {path}").strip()

    def inject_fault(self, mode: str) -> None:
        self.shell(f"echo {mode} > /sdcard/Android/data/{self.pkg}/files/fault_next_commit.txt")


# ---------------------------------------------------------------------------
# THE FLOW
# ---------------------------------------------------------------------------
class Flow:
    def __init__(self, phone, cfg, step=lambda *a, **k: None, sleep=time.sleep,
                 evidence_dir=None):
        self.p, self.cfg, self.step, self.sleep = phone, cfg, step, sleep
        self.evidence_dir = evidence_dir
        self.real = phone.pkg.startswith(REAL_PREFIX)

    def probe(self, nodes, which: int):
        return (real_probe_value(nodes, which) if self.real
                else probe_value(nodes, f"T{which}"))

    def popups(self, xml_text: str) -> list[str]:
        """Popup signs on the plain page; keeps evidence the first time."""
        signs = popup_signs(parse_ui(xml_text), self.p.pkg)
        if signs and self.evidence_dir:
            try:
                png = self.p.screenshot()
            except (AdbError, AttributeError):
                png = None
            save_popup_evidence(self.evidence_dir, xml_text, png)
        return signs

    def nodes(self):
        return parse_ui(self.p.ui_xml())

    def tap(self, node):
        # Taps are coordinates: never send one unless the target app is visible.
        # A crash can hand the screen to another app, the real Inkbird app included.
        if self.p.foreground_pkg() != self.p.pkg:
            raise Fail("app_not_foreground", "target app not resumed before tap", True)
        self.p.tap(node["cx"], node["cy"])

    def problems(self) -> list[str]:
        out = []
        if not self.p.running():
            out.append("not running")
        if self.p.foreground_pkg() != self.p.pkg:
            out.append("not foreground")
        try:
            xml_text = self.p.ui_xml()
            texts = {n["text"] for n in parse_ui(xml_text)}
        except (AdbError, ET.ParseError) as err:
            return out + [f"ui dump failed: {err}"]
        if self.cfg["page_marker"] not in texts:
            out.append("main page marker missing")
        if "Complete" in texts:
            out.append("drawer open")
        elif out == []:
            out += [f"unexpected popup ({s})" for s in self.popups(xml_text)]
        hb = self.cfg.get("heartbeat_file")
        if hb and not out:
            try:
                a = self.p.cat(hb)
                self.sleep(float(self.cfg["heartbeat_check_s"]))
                b = self.p.cat(hb)
            except AdbError:
                a = b = ""
            if not a.isdigit():
                # Android 11's scoped storage hides Android/data from the adb
                # shell on the Docker emulator. The page dump above already
                # proves the UI thread answers, so go on without this check.
                self.step("heartbeat", ok=None, note="unreadable, skipped")
            elif a == b:
                out.append("heartbeat stalled")
        return out

    def ensure_healthy(self) -> None:
        bad = self.problems()
        if not bad:
            self.step("health", ok=True)
            return
        if bad == ["drawer open"]:
            self.p.back()
            self.sleep(1)
            bad = self.problems()
            if not bad:
                self.step("health", ok=True, note="closed drawer")
                return
        if self.real:
            # The poller's recovery knows how to reach the device page; this does
            # not tap around the real app looking for it.
            self.step("health", ok=False, problems=bad, action="none")
            raise Fail("escalate_app_unhealthy",
                       "; ".join(bad) + " (real app: not restarted here)", True)
        self.step("health", ok=False, problems=bad, action="restart")
        self.p.restart()
        self.sleep(float(self.cfg["recovery_wait_s"]))
        bad = self.problems()
        if bad:
            raise Fail("escalate_app_unhealthy", "; ".join(bad), True)
        self.step("health", ok=True, note="after restart")

    def read(self) -> dict:
        nodes = self.nodes()
        have = {"floor_f": self.probe(nodes, 1), "ceil_f": self.probe(nodes, 2)}
        if have["floor_f"] is None or have["ceil_f"] is None:
            raise Fail("read_failed", f"T1/T2 unreadable: {have}", True)
        lo, hi = self.cfg["bounds_f"]
        for k, v in have.items():
            if not float(lo) - 50 <= v <= float(hi) + 50:
                raise Fail("read_failed", f"{k} {v} is not a plausible setting", True)
        return have

    def set_wheel(self, title: str, tenths: int) -> None:
        want = [tenths // 100, (tenths // 10) % 10, tenths % 10]
        for _ in range(40):
            cols, vals = wheel_state(self.nodes(), title)
            if vals == want:
                return
            c = next(i for i in range(3) if vals[i] != want[i])
            cell = cols[c][0] if want[c] > vals[c] else cols[c][2]
            if cell["text"] == "":
                raise Fail("wheel_not_converged",
                           f"{title}: column {c} cannot step from {vals} to {want}", True)
            self.tap(cell)
            self.sleep(0.5)
        raise Fail("wheel_not_converged", f"{title}: no convergence to {want}", True)

    # -- the real app's drawer: two scroll wheels read by OCR ----------------
    def read_wheels(self, boxes):
        """(degrees, tenths) now selected, by OCR on the middle row of each wheel."""
        png = self.p.screenshot()
        out = []
        for (x1, y1, x2, y2), lo, hi in zip(boxes, (30, 0), (120, 9)):
            cy = (y1 + y2) // 2
            band = (x1 + 40, cy - 48, x2 - 40, cy + 48)
            out.append(ocr_digits(self.p.ocr_digits(png, band), lo, hi))
        return out[0], out[1]

    def move_wheel(self, box, items: int) -> None:
        """Scroll one wheel by `items` (positive = to a larger value) with a slow
        swipe so it does not fling; the loop in set_probe_wheel checks the result."""
        x1, y1, x2, y2 = box
        cx, cy, step = (x1 + x2) // 2, (y1 + y2) // 2, (y2 - y1) * 0.245
        k = min(abs(items), 3)              # stays inside the wheel (about 4 items tall)
        if items > 0:                       # drag up: the list moves up, larger shows
            a = cy + int(step * 1.5)
            b = a - int(step * k)
        else:
            a = cy - int(step * 1.5)
            b = a + int(step * k)
        self.p.swipe(cx, a, cx, b, 500 + 150 * k)
        self.sleep(1.2)

    def set_probe_wheel(self, which: int, value_f: float) -> None:
        nodes = self.nodes()
        opener = node_id(nodes, f":id/ll_device_temp{which}")
        if opener is None:
            raise Fail("write_failed", f"no T{which} control on the page", True)
        self.tap(opener)
        for _ in range(12):
            nodes = self.nodes()
            done = node_id(nodes, ":id/rl_device_choose_tempc_complete")
            title = node_id(nodes, ":id/tv_device_choose_tempc_title")
            if done and title:
                break
            self.sleep(0.5)
        else:
            raise Fail("write_failed", f"T{which} drawer did not open", True)
        try:
            if not title["text"].startswith(f"T{which} "):
                raise Fail("write_failed", f"wrong drawer: {title['text']!r}", True)
            boxes = [node_id(nodes, ":id/lv_device_tempc1"),
                     node_id(nodes, ":id/lv_device_tempc2")]
            if None in boxes:
                raise Fail("wheel_not_converged", "wheel views not found", True)
            boxes = [b["box"] for b in boxes]
            want = (int(value_f + 1e-9), round((value_f - int(value_f + 1e-9)) * 10))
            seen = None
            for _ in range(14):
                seen = self.read_wheels(boxes)
                if seen == want:
                    break
                for col, (have_v, want_v) in enumerate(zip(seen, want)):
                    if have_v is None:
                        break               # an unreadable column: look again
                    if have_v != want_v:
                        self.move_wheel(boxes[col], want_v - have_v)
                        break
            else:
                raise Fail("wheel_not_converged", f"wheel at {seen}, wanted {want}", True)
            self.step("wheel", probe=f"T{which}", selected=list(seen))
            self.tap(node_id(self.nodes(), ":id/rl_device_choose_tempc_complete"))
        except Fail:
            self.p.back()                   # cancel: nothing was committed
            self.sleep(1.0)
            raise
        self.sleep(1.5)
        nodes = self.nodes()
        if node_id(nodes, ":id/rl_device_choose_tempc_complete"):
            self.p.back()
            raise Fail("write_rejected", "drawer stayed open after Complete", True)
        if self.cfg["page_marker"] not in {n["text"] for n in nodes}:
            raise Fail("app_crashed", "device page gone after Complete", True)
        signs = popup_signs(nodes, self.p.pkg)
        if signs:
            self.popups(self.p.ui_xml())
            raise Fail("unexpected_popup", f"after Complete: {', '.join(signs)}", True)

    def set_probe(self, which: int, value_f: float) -> None:
        if self.real:
            return self.set_probe_wheel(which, value_f)
        label = f"T{which}"
        title = "T1 floor" if which == 1 else "T2 ceiling"
        nodes = self.nodes()
        i = next((k for k, n in enumerate(nodes[:-1]) if n["text"] == label), None)
        if i is None:
            raise Fail("write_failed", f"no {label} value on the page", True)
        self.tap(nodes[i + 1])
        for _ in range(12):
            nodes = self.nodes()
            if find(nodes, title) and find(nodes, "Complete"):
                break
            self.sleep(0.5)
        else:
            raise Fail("write_failed", f"{title} drawer did not open", True)
        self.set_wheel(title, round(value_f * 10))
        self.tap(find(self.nodes(), "Complete"))
        self.sleep(1.0)
        nodes = self.nodes()
        texts = {n["text"] for n in nodes}
        if "Complete" not in texts and self.cfg["page_marker"] not in texts:
            raise Fail("app_crashed", "main page gone after Complete", True)
        if find(nodes, "Complete"):
            err = error_text(nodes)
            self.tap(find(nodes, "Cancel"))
            raise Fail("write_rejected", err or "drawer stayed open after Complete", True)
        signs = popup_signs(nodes, self.p.pkg)
        if signs:
            self.popups(self.p.ui_xml())            # keep the evidence
            raise Fail("unexpected_popup", f"after Complete: {', '.join(signs)}", True)

    def act(self, plan: dict) -> None:
        tol = float(self.cfg["tolerance_f"])
        for name in plan["order"]:
            which = 1 if name == "floor" else 2
            want = plan["set"][name]
            self.set_probe(which, want)
            got = self.probe(self.nodes(), which)
            self.step("set", probe=name, value_f=want, readback_f=got)
            if got is None:
                raise Fail("readback_failed", f"{name} not readable after Complete", True)
            if abs(got - want) > tol:       # stop: no partial band from a silent drop
                raise Fail("failed_verify", f"{name} want {want} got {got}", True)

    def verify(self, plan: dict) -> dict:
        self.sleep(1.5)
        have = self.read()
        bad = [f"{n} want {w} got {have[n + '_f']}" for n, w in plan["set"].items()
               if abs(have[n + "_f"] - w) > float(self.cfg["tolerance_f"])]
        if bad:
            raise Fail("failed_verify", "; ".join(bad), True)
        return have


def run(phone, request_text: str, cfg: dict, now: dt.datetime, *, dry_run=False,
        inject: str | None = None, done_seq: int | None = None,
        sleep=time.sleep, evidence_dir: str | None = None) -> dict:
    """One control cycle -> a result dict. Never raises."""
    result = {"ts": iso(now), "mode": "dry_run" if dry_run else "live",
              "pkg": phone.pkg, "status": None, "retryable": False,
              "reason": None, "seq": None, "steps": []}

    def step(name, **kw):
        result["steps"].append({"step": name, **kw})

    try:
        req = parse_request(request_text, now, cfg)
        result["seq"], result["request"] = req["seq"], req
        step("request", **{k: req[k] for k in ("seq", "floor_f", "ceil_f")})
        if done_seq is not None and req["seq"] <= done_seq and not dry_run:
            result["status"] = "already_done"
            return result
        phone.connect()
        flow = Flow(phone, cfg, step, sleep, evidence_dir)
        flow.ensure_healthy()
        before = flow.read()
        result["before"] = before
        step("read", **before)
        plan = decide(req, before, cfg)
        result["plan"] = plan
        step("plan", noop=plan["noop"], order=plan["order"], set=plan["set"])
        if plan["noop"]:
            result["status"] = "no_change"
        elif dry_run:
            result["status"] = "dry_run"
        else:
            if inject:
                phone.inject_fault(inject)
                step("inject", fault=inject)
            flow.act(plan)
            result["after"] = flow.verify(plan)
            result["status"] = "success"
    except Fail as f:
        result.update(status=f.status, retryable=f.retryable, reason=f.reason)
        step("fail", status=f.status, reason=f.reason)
    except AdbError as err:
        result.update(status="escalate_adb", retryable=True, reason=str(err))
        step("fail", status="escalate_adb", reason=str(err))
    except Exception as err:  # noqa: BLE001 - a run must always end in a result
        result.update(status="error", retryable=True,
                      reason=f"{type(err).__name__}: {err}"[:200])
    return result


OK_STATUSES = ("success", "no_change", "already_done", "dry_run")


class Retry:
    """How often one request may be tried: up to `max_tries` times, `retry_s`
    apart, and only while the failures are marked retryable. A new key (a new
    request, or a different applied state) starts over. Times are any monotonic
    clock the caller passes in."""

    def __init__(self, retry_s: float = 600.0, max_tries: int = 3):
        self.retry_s, self.max_tries = float(retry_s), int(max_tries)
        self._state: dict = {}

    def due(self, key, now: float) -> bool:
        st = self._state.get(key)
        if st is None:
            return True
        tries, last, done = st
        return (not done) and tries < self.max_tries and now - last >= self.retry_s

    def record(self, key, result: dict, now: float) -> None:
        tries = (self._state.get(key) or (0, 0.0, False))[0] + 1
        done = (result.get("status") in OK_STATUSES or not result.get("retryable")
                or tries >= self.max_tries)
        self._state[key] = (tries, now, done)
        while len(self._state) > 8:                 # keep it small: drop the oldest
            self._state.pop(next(iter(self._state)))


# ---------------------------------------------------------------------------
# FILES AND CLI
# ---------------------------------------------------------------------------
def folder() -> str:
    data = os.environ.get("TANK_DATA", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
    return os.path.join(data, "itc")


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def write_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def append_log(path: str, rec: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def done_seq(dir_: str) -> int | None:
    try:
        return int(json.loads(read_text(os.path.join(dir_, "applied.json")) or "{}")["seq"])
    except (ValueError, KeyError, TypeError):
        return None


def apply_once(phone, cfg, dir_, request_path, *, dry_run=False, inject=None,
               sleep=time.sleep) -> dict:
    """Read the request file, run the flow, write result.json (and applied.json
    when the request is done). A missing request is not an error."""
    text = read_text(request_path)
    if text is None:
        return {"status": "no_request", "retryable": False, "reason": None}
    res = run(phone, text, cfg, now_utc(), dry_run=dry_run, inject=inject,
              done_seq=done_seq(dir_), sleep=sleep, evidence_dir=dir_)
    if res["status"] != "already_done":
        write_json(os.path.join(dir_, "result.json"), res)
        append_log(os.path.join(dir_, "results.jsonl"),
                   {k: res.get(k) for k in ("ts", "mode", "pkg", "seq", "status",
                                            "retryable", "reason")})
    if res["status"] in ("success", "no_change") and not dry_run:
        rec = {"seq": res["seq"], "ts": res["ts"], "status": res["status"],
               "floor_f": res["request"]["floor_f"], "ceil_f": res["request"]["ceil_f"],
               "previous": None, "changed_ts": res["ts"], "cue": res["request"].get("cue")}
        if res["status"] == "success":
            # What the device held before this change: the band an automatic
            # reversal goes back to (tank_itc_watch.py).
            rec["previous"] = {"floor_f": res["before"]["floor_f"],
                               "ceil_f": res["before"]["ceil_f"]}
            append_log(os.path.join(dir_, "history.jsonl"),
                       {k: rec[k] for k in ("ts", "seq", "previous", "floor_f", "ceil_f",
                                            "cue")})
        else:                       # nothing changed: keep the last change's record
            try:
                old = json.loads(read_text(os.path.join(dir_, "applied.json")) or "{}")
                if (old.get("floor_f"), old.get("ceil_f")) == (rec["floor_f"], rec["ceil_f"]):
                    rec["previous"] = old.get("previous")
                    rec["changed_ts"] = old.get("changed_ts", rec["ts"])
            except ValueError:
                pass
        write_json(os.path.join(dir_, "applied.json"), rec)
    return res


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "apply"
    pkg = os.environ.get("ITC_PKG", SANDBOX_PKG)
    addr = os.environ.get("ITC_ADB_ADDR", "anemones-android:5555")
    try:
        check_package(pkg, os.environ.get("ITC_ALLOW_REAL") == "1")
    except Fail as err:
        print(f"{err.status}: {err.reason}", file=sys.stderr)
        return 2
    cfg = config()
    if not pkg == SANDBOX_PKG:
        cfg["heartbeat_file"] = None
    dir_ = folder()
    req_path = os.path.join(dir_, "target.json")
    if "--request" in argv:
        req_path = argv[argv.index("--request") + 1]
    inject = argv[argv.index("--inject") + 1] if "--inject" in argv else None
    if inject not in (None, "reject", "drop", "crash"):
        print("--inject is reject, drop or crash", file=sys.stderr)
        return 2
    phone = Phone(addr, pkg)
    if os.path.exists(os.path.join(dir_, "HALT")):
        print("halted: HALT file present", file=sys.stderr)
        return 3
    if cmd == "request":
        def arg(name):
            return float(argv[argv.index(name) + 1]) if name in argv else None
        target = arg("--target-f")
        if target is None and arg("--measure-f") is not None:
            target = arg("--measure-f") + (arg("--rise-f") or 0.0)
        if target is None:
            print("request needs --target-f, or --measure-f with --rise-f",
                  file=sys.stderr)
            return 2
        below, above = arg("--below-f") or 0.2, arg("--above-f") or 0.3
        floor_f, ceil_f = band_f(target, below, above, cfg["bounds_f"])
        prev = None
        try:
            prev = int(json.loads(read_text(req_path) or "{}")["seq"])
        except (ValueError, KeyError, TypeError):
            pass
        seq = max(prev or 0, done_seq(dir_) or 0) + 1
        req = {"seq": seq, "ts": iso(now_utc()), "floor_f": floor_f, "ceil_f": ceil_f,
               "target_f": round(target, 2),
               "cue": {"manual": True, "measure_f": arg("--measure-f"),
                       "rise_f": arg("--rise-f")}}
        write_json(req_path, req)
        print(json.dumps(req, indent=2))
        return 0
    if cmd == "read":
        try:
            phone.connect()
            print(json.dumps(Flow(phone, cfg).read()))
            return 0
        except (AdbError, Fail) as err:
            print(f"read failed: {err}", file=sys.stderr)
            return 1
    if cmd == "apply":
        res = apply_once(phone, cfg, dir_, req_path, dry_run="--dry-run" in argv,
                         inject=inject)
        print(json.dumps({k: res.get(k) for k in ("status", "retryable", "reason",
                                                  "before", "plan", "after")}, indent=2))
        return 0 if res["status"] in OK_STATUSES + ("no_request",) else 1
    if cmd == "watch":
        every = max(5, int(os.environ.get("ITC_POLL_S", "30")))
        retry = Retry()
        while True:
            if not os.path.exists(os.path.join(dir_, "HALT")):
                text = read_text(req_path)
                key = (done_seq(dir_), text)
                if text and retry.due(key, time.monotonic()):
                    res = apply_once(phone, cfg, dir_, req_path)
                    print(json.dumps({k: res.get(k) for k in ("ts", "seq", "status",
                                                              "reason")}), flush=True)
                    retry.record(key, res, time.monotonic())
            time.sleep(every)
    print(f"unknown command {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
