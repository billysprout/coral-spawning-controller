#!/usr/bin/env python3
"""heat_report.py -- how much the lights (and the heater) move the tank temperature.

The plan never allowed for the heat the lamps put into the water. This pulls
everything that has been recorded about it into one table and one chart, so the
maths can be worked out later; it does no modelling beyond a plain rate of change.

Sources (all read-only, whatever exists is used):
  * data/heat/heat_log-*.jsonl        the granular log the conductor now writes
                                      every minute (tank_heat_log.py)
  * data/tank_inputs-*.jsonl          the older per-minute log: plug states and the
                                      Pi probe, since 2026-10-30
  * data/sensors/itc306a*.jsonl       the Inkbird readings (every 5 minutes), with the
                                      device's T1/T2 and, since the heat log began,
                                      whether it showed "Heating"
  * data/heat/lamp_map.json           which lamp was on which plug, by date
  * data/heat/equipment.json          things not on a plug that change the heat, today the
                                      PC fan on the tank (heat_equipment.py); before its
                                      first entry the state is "unknown", never "off"

The temperature that matters is the Inkbird's (the Pi probe was not in the water for
part of this and carries a hand-matched offset); the Pi probe is drawn dashed for
comparison. Lights are attributed by the plugs' own on/off state.

Heat delta: for each Inkbird reading, the change over the next 60 minutes
(60 min window, +-5), kept only when the same lamps were on for the whole window.
Rows are grouped by which lamps were on and whether the heater was (on, off, mixed,
unknown). The windows overlap, so the counts are not independent samples. Heater state
is only known from the heat log onwards (earlier rows are "unknown").

    python heat_report.py [--data DIR] [--out DIR] [--days N]

writes <out>/history.csv, <out>/heat_deltas.csv and <out>/heat_chart.html (one
self-contained page, no scripts, no network). Times are tank local (Pacific, fixed
UTC-8 here: no time-zone database is needed).
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import glob
import html
import json
import os
import statistics
import sys

import heat_equipment as he

UTC = dt.timezone.utc
PST = dt.timezone(dt.timedelta(hours=-8))
WINDOW_MIN = 60
WINDOW_TOL_MIN = 5

DEFAULT_LAMP_MAP = [
    # the plugs' physical lamps; edit data/heat/lamp_map.json to change history
    {"since": "2026-10-01T00:00:00+00:00",
     "plugs": {"plug-8": "200W", "plug-9": "LED9", "plug-10": "Halo"}},
    # plug-8 and plug-10 were swapped on the morning of 2026-11-03 (all three plugs
    # were off through that interval, so the exact moment does not matter)
    {"since": "2026-11-03T08:00:00+00:00",
     "plugs": {"plug-8": "Halo", "plug-9": "LED9", "plug-10": "200W"}},
]
LAMPS = ("200W", "Halo", "LED9")


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def _ts(text):
    try:
        t = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _jsonl(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return
    for line in data.splitlines():
        line = line.strip().strip("\x00")
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def load_lamp_map(data_dir: str) -> list[dict]:
    path = os.path.join(data_dir, "heat", "lamp_map.json")
    try:
        with open(path, encoding="utf-8") as fh:
            eras = json.load(fh)
    except (OSError, ValueError):
        eras = DEFAULT_LAMP_MAP
    return sorted(eras, key=lambda e: e["since"])


def lamp_of(plug: str, t: dt.datetime, eras: list[dict]):
    lamp = None
    for e in eras:
        if _ts(e["since"]) <= t:
            lamp = e["plugs"].get(plug)
    return lamp


def load_inkbird(data_dir: str) -> list[dict]:
    """Inkbird readings, oldest first, one per timestamp: ts, temp_c, t1_f, t2_f,
    heating (True/False/None)."""
    seen, out = {}, []
    for name in ("itc306a.jsonl", "itc306a_docker.jsonl"):
        for r in _jsonl(os.path.join(data_dir, "sensors", name)):
            t = _ts(r.get("ts"))
            if t is None or r.get("temp_c") is None:
                continue
            seen[t] = {"ts": t, "temp_c": float(r["temp_c"]), "t1_f": r.get("t1_f"),
                       "t2_f": r.get("t2_f"), "heating": r.get("heating")}
    out = [seen[k] for k in sorted(seen)]
    return out


def load_states(data_dir: str, eras: list[dict]) -> list[dict]:
    """Per-minute rows: ts, lamps (frozenset of lamps on), probe_c, lux. From the
    heat log where it exists, else from the older tank_inputs log."""
    rows = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "tank_inputs-*.jsonl"))):
        for r in _jsonl(path):
            t = _ts(r.get("ts"))
            plugs = r.get("plugs") or {}
            if t is None or not plugs:
                continue
            on, known = set(), True
            for name, p in plugs.items():
                if p.get("on") is None:
                    known = False
                    continue
                lamp = lamp_of(name, t, eras)
                if p["on"] and lamp:
                    on.add(lamp)
            rows[t.replace(second=0)] = {"ts": t, "lamps": frozenset(on),
                                         "known": known, "probe_c": r.get("temp_c"),
                                         "lux": r.get("lux")}
    for path in sorted(glob.glob(os.path.join(data_dir, "heat", "heat_log-*.jsonl"))):
        for r in _jsonl(path):
            t = _ts(r.get("ts"))
            if t is None:
                continue
            plugs = r.get("plugs") or {}
            on, known = set(), bool(plugs)
            for name, p in plugs.items():
                if p.get("on") is None:
                    known = False
                    continue
                lamp = lamp_of(name, t, eras)
                if p["on"] and lamp:
                    on.add(lamp)
            rows[t.replace(second=0)] = {"ts": t, "lamps": frozenset(on), "known": known,
                                         "probe_c": r.get("probe_c"), "lux": r.get("lux")}
    equip = he.load(data_dir, create=True)
    for r in rows.values():
        r["fan"] = he.state_at(equip, "pc_fan", r["ts"])
    return [rows[k] for k in sorted(rows)]


# ---------------------------------------------------------------------------
# analysis (pure, unit tested)
# ---------------------------------------------------------------------------
def _window(states: list[dict], t0: dt.datetime, t1: dt.datetime, times=None):
    """The states from one minute before t0 to one minute after t1. `states` is
    sorted by ts; pass `times` (their ts list) to avoid rebuilding it per call."""
    if times is None:
        times = [s["ts"] for s in states]
    lo = bisect.bisect_left(times, t0 - dt.timedelta(minutes=1))
    hi = bisect.bisect_right(times, t1 + dt.timedelta(minutes=1))
    return states[lo:hi]


def lamps_over(states: list[dict], t0: dt.datetime, t1: dt.datetime, times=None):
    """The one set of lamps that was on for the whole of [t0, t1], or None when it
    changed, was unknown, or there is too little data in the window."""
    inside = _window(states, t0, t1, times)
    if len(inside) < max(3, int((t1 - t0).total_seconds() / 60 * 0.6)):
        return None
    if any(not s["known"] for s in inside):
        return None
    sets = {s["lamps"] for s in inside}
    return next(iter(sets)) if len(sets) == 1 else None


def fan_over(states: list[dict], t0: dt.datetime, t1: dt.datetime, times=None):
    """The PC fan's state ("on", "off", "unknown") when it was the same for the whole
    window, else None (it changed, so the window is dropped like a lamp change)."""
    seen = {s.get("fan", "unknown") for s in _window(states, t0, t1, times)}
    return next(iter(seen)) if len(seen) == 1 else None


def heater_over(readings: list[dict], t0: dt.datetime, t1: dt.datetime) -> str:
    flags = [r["heating"] for r in readings if t0 <= r["ts"] <= t1
             and r.get("heating") is not None]
    if not flags:
        return "unknown"
    if all(flags):
        return "on"
    if not any(flags):
        return "off"
    return "mixed"


def delta_windows(readings: list[dict], states: list[dict], window_min=WINDOW_MIN,
                  tol_min=WINDOW_TOL_MIN) -> list[dict]:
    """One row per Inkbird reading that has a reading about `window_min` later and
    steady lamps in between: start, end, lamps, heater, rate in C per hour."""
    out = []
    times = [r["ts"] for r in readings]
    state_times = [s["ts"] for s in states]
    j = 0
    for i, r in enumerate(readings):
        target = r["ts"] + dt.timedelta(minutes=window_min)
        while j < len(readings) and times[j] < target - dt.timedelta(minutes=tol_min):
            j += 1
        if j >= len(readings):
            break
        cand = readings[j]
        if abs((cand["ts"] - target).total_seconds()) > tol_min * 60:
            continue
        lamps = lamps_over(states, r["ts"], cand["ts"], state_times)
        if lamps is None:
            continue
        fan = fan_over(states, r["ts"], cand["ts"], state_times)
        if fan is None:
            continue
        hours = (cand["ts"] - r["ts"]).total_seconds() / 3600.0
        out.append({"start": r["ts"], "end": cand["ts"], "lamps": lamps, "fan": fan,
                    "heater": heater_over(readings, r["ts"], cand["ts"]),
                    "start_c": r["temp_c"], "end_c": cand["temp_c"],
                    "rate_c_h": (cand["temp_c"] - r["temp_c"]) / hours})
    return out


def lamps_label(lamps) -> str:
    return " + ".join(l for l in LAMPS if l in lamps) or "all off"


def group_rates(windows: list[dict]) -> list[dict]:
    groups = {}
    for w in windows:
        groups.setdefault((tuple(l for l in LAMPS if l in w["lamps"]), w["heater"],
                           w.get("fan", "unknown")), []).append(w["rate_c_h"])
    rows = []
    for (lamps, heater, fan), rates in groups.items():
        rows.append({"lamps": " + ".join(lamps) or "all off", "n_lamps": len(lamps),
                     "heater": heater, "fan": fan, "windows": len(rates),
                     "hours": round(len(rates) * 5 / 60.0, 1),     # 5 min apart
                     "mean_c_h": statistics.mean(rates),
                     "median_c_h": statistics.median(rates),
                     "sd_c_h": statistics.pstdev(rates) if len(rates) > 1 else 0.0})
    rows.sort(key=lambda r: (r["n_lamps"], r["lamps"], r["heater"], r["fan"]))
    return rows


# ---------------------------------------------------------------------------
# chart (one self-contained SVG page)
# ---------------------------------------------------------------------------
COLORS = {"200W": "#d98e04", "Halo": "#2f7fd1", "LED9": "#8a4fd1"}


def _x(t, t0, t1, left, width):
    return left + width * (t - t0).total_seconds() / max(1.0, (t1 - t0).total_seconds())


def _line(points, color, dash="", clip=""):
    if len(points) < 2:
        return ""
    d = " ".join(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))
    return (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6" '
            f'{"stroke-dasharray=" + chr(34) + dash + chr(34) if dash else ""}'
            f'{" clip-path=" + chr(34) + "url(#" + clip + ")" + chr(34) if clip else ""}/>')


def _segments(readings, key, t0, t1, left, width, ymin, ymax, top, height, max_gap_min=20):
    """Polyline points split where the data has a gap."""
    segs, cur, last = [], [], None
    for r in readings:
        v = r.get(key)
        if v is None or not (t0 <= r["ts"] <= t1):
            continue
        if last and (r["ts"] - last).total_seconds() > max_gap_min * 60:
            segs.append(cur)
            cur = []
        y = top + height * (1 - (v - ymin) / max(1e-9, ymax - ymin))
        cur.append((_x(r["ts"], t0, t1, left, width), y))
        last = r["ts"]
    if cur:
        segs.append(cur)
    return segs


def _runs(states, t0, t1, pred):
    """[(start, end)] runs where pred(state) holds, allowing 3-minute holes."""
    out, start, last = [], None, None
    for s in states:
        if not (t0 <= s["ts"] <= t1):
            continue
        if pred(s):
            if start is None or (s["ts"] - last).total_seconds() > 180:
                if start is not None:
                    out.append((start, last))
                start = s["ts"]
            last = s["ts"]
        elif start is not None and (s["ts"] - last).total_seconds() > 180:
            out.append((start, last))
            start = None
    if start is not None:
        out.append((start, last))
    return out


def build_chart(readings, states, windows, groups, t0, t1, note="", pwindows=None) -> str:
    W, left, right = 1100, 120, 20
    width = W - left - right
    local = lambda t: t.astimezone(PST)
    # panel geometry
    pa_top, pa_h = 44, 230                   # temperature
    strip_top = pa_top + pa_h + 14           # lamp strips
    strip_h, strip_gap = 12, 4
    pb_top = strip_top + 5 * (strip_h + strip_gap) + 44     # rate panel
    pb_h = 180
    pc_top = pb_top + pb_h + 50
    H = pc_top + 240
    temps = [r["temp_c"] for r in readings if t0 <= r["ts"] <= t1]
    probes = [s["probe_c"] for s in states if t0 <= s["ts"] <= t1 and s.get("probe_c") is not None]
    allv = temps + [p for p in probes if temps and min(temps) - 3 < p < max(temps) + 3]
    ymin, ymax = (min(allv) - 0.3, max(allv) + 0.3) if allv else (24.0, 28.0)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
             f'width="100%" role="img" aria-label="Tank temperature, lamps and heat rate">',
             f'<defs><clipPath id="ta"><rect x="{left}" y="{pa_top}" width="{width}" height="{pa_h}"/></clipPath>'
             f'<clipPath id="tb"><rect x="{left}" y="{pb_top}" width="{width}" height="{pb_h}"/></clipPath></defs>']
    # day grid
    day = local(t0).replace(hour=0, minute=0, second=0, microsecond=0)
    while day < local(t1) + dt.timedelta(days=1):
        for hh, major in ((0, True), (12, False)):
            tt = (day + dt.timedelta(hours=hh)).astimezone(UTC)
            if t0 <= tt <= t1:
                x = _x(tt, t0, t1, left, width)
                parts.append(f'<line x1="{x:.1f}" y1="{pa_top}" x2="{x:.1f}" y2="{pc_top - 60}" '
                             f'stroke="{"#bbb" if major else "#e4e4e4"}" stroke-width="1"/>')
                if major:
                    parts.append(f'<text x="{x + 3:.1f}" y="{pa_top - 4}" font-size="11" '
                                 f'fill="#555">{day.strftime("%a %m-%d")}</text>')
        day += dt.timedelta(days=1)
    # temperature axis
    for k in range(5):
        v = ymin + (ymax - ymin) * k / 4
        y = pa_top + pa_h * (1 - k / 4)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{W - right}" y2="{y:.1f}" stroke="#eee"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
                     f'fill="#555">{v:.1f} C / {v * 9 / 5 + 32:.1f} F</text>')
    # band T1/T2 where known
    band = [r for r in readings if r.get("t1_f") is not None and t0 <= r["ts"] <= t1]
    for a, b in zip(band, band[1:]):
        if (b["ts"] - a["ts"]).total_seconds() > 1200:
            continue
        for key, col in (("t1_f", "#2a9d5c"), ("t2_f", "#c0392b")):
            c = (a[key] - 32) * 5 / 9
            if ymin <= c <= ymax:
                y = pa_top + pa_h * (1 - (c - ymin) / (ymax - ymin))
                parts.append(f'<line x1="{_x(a["ts"], t0, t1, left, width):.1f}" y1="{y:.1f}" '
                             f'x2="{_x(b["ts"], t0, t1, left, width):.1f}" y2="{y:.1f}" '
                             f'stroke="{col}" stroke-width="1" stroke-dasharray="2 3" opacity=".7"/>')
    for seg in _segments([{"ts": s["ts"], "v": s["probe_c"]} for s in states if s.get("probe_c") is not None],
                         "v", t0, t1, left, width, ymin, ymax, pa_top, pa_h):
        parts.append(_line(seg, "#999", "4 3", "ta"))
    for seg in _segments([{"ts": r["ts"], "v": r["temp_c"]} for r in readings],
                         "v", t0, t1, left, width, ymin, ymax, pa_top, pa_h):
        parts.append(_line(seg, "#111", "", "ta"))
    parts.append(f'<text x="{left}" y="16" font-size="13" font-weight="600" fill="#111">'
                 f'Inkbird temperature (solid), Pi probe (dashed grey), T1 green and T2 red when known</text>')
    # lamp strips
    names = list(LAMPS) + ["Heater", "PC fan"]
    for i, name in enumerate(names):
        y = strip_top + i * (strip_h + strip_gap)
        parts.append(f'<text x="{left - 6}" y="{y + strip_h - 2}" text-anchor="end" font-size="11" '
                     f'fill="#555">{name} {"heating" if name == "Heater" else ("on" if name != "PC fan" else "running")}</text>')
        parts.append(f'<rect x="{left}" y="{y}" width="{width}" height="{strip_h}" fill="#f4f4f4"/>')
        if name == "Heater":
            runs = []
            flags = [r for r in readings if r.get("heating") is not None]
            cur = None
            for a, b in zip(flags, flags[1:] + [None]):
                if a["heating"] and cur is None:
                    cur = a["ts"]
                if cur is not None and (not a["heating"] or b is None):
                    runs.append((cur, (a["ts"] if a["heating"] else a["ts"])))
                    cur = None
            col = "#c0392b"
        elif name == "PC fan":
            runs = _runs(states, t0, t1, lambda s: s.get("fan") == "on")
            col = "#2a9d8f"
        else:
            runs = _runs(states, t0, t1, lambda s, n=name: n in s["lamps"])
            col = COLORS[name]
        for a, b in runs:
            x1, x2 = _x(a, t0, t1, left, width), _x(b, t0, t1, left, width)
            parts.append(f'<rect x="{x1:.1f}" y="{y}" width="{max(1.5, x2 - x1):.1f}" '
                         f'height="{strip_h}" fill="{col}"/>')
    # rate panel
    rates = [w["rate_c_h"] for w in windows if t0 <= w["start"] <= t1]
    rmax = max([abs(r) for r in rates] + [0.5])
    parts.append(f'<text x="{left}" y="{pb_top - 8}" font-size="13" font-weight="600" fill="#111">'
                 f'Heat delta: change over the next 60 minutes, C per hour (lamps steady)</text>')
    for k in range(5):
        v = -rmax + 2 * rmax * k / 4
        y = pb_top + pb_h * (1 - k / 4)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{W - right}" y2="{y:.1f}" '
                     f'stroke="{"#999" if abs(v) < 1e-9 else "#eee"}"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
                     f'fill="#555">{v:+.2f}</text>')
    for w in (pwindows or []):
        if not (t0 <= w["start"] <= t1):
            continue
        x = _x(w["start"], t0, t1, left, width)
        y = pb_top + pb_h * (1 - (w["rate_c_h"] + rmax) / (2 * rmax))
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.6" fill="none" stroke="#999" '
                     f'stroke-width="0.8" opacity=".6" clip-path="url(#tb)"/>')
    for w in windows:
        if not (t0 <= w["start"] <= t1):
            continue
        key = max(w["lamps"], key=lambda l: {"200W": 3, "Halo": 2, "LED9": 1}[l]) if w["lamps"] else None
        col = COLORS.get(key, "#666")
        x = _x(w["start"], t0, t1, left, width)
        y = pb_top + pb_h * (1 - (w["rate_c_h"] + rmax) / (2 * rmax))
        heater = w["heater"] == "on"
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="{col}" '
                     f'{"stroke=" + chr(34) + "#c0392b" + chr(34) + " stroke-width=" + chr(34) + "1.5" + chr(34) if heater else ""}'
                     f' opacity=".75"/>')
    parts.append(f'<text x="{left}" y="{pb_top + pb_h + 16}" font-size="11" fill="#555">'
                 f'filled dot = Inkbird window, coloured by the brightest lamp on (grey = all off), red ring = heater on; '
                 f'small hollow dot = Pi probe window (older, less reliable)</text>')
    # group bars
    parts.append(f'<text x="{left}" y="{pc_top - 8}" font-size="13" font-weight="600" fill="#111">'
                 f'Mean heat delta by lamps on (C per hour; label: windows, hours)</text>')
    rows = [g for g in groups if g["heater"] in ("off", "unknown")] or groups
    gmax = max([abs(g["mean_c_h"]) for g in rows] + [0.3])
    row_h = 24
    for i, g in enumerate(rows[:8]):
        y = pc_top + i * row_h
        zero = left + 330
        scale = 300 / gmax
        x2 = zero + g["mean_c_h"] * scale
        col = "#2f7fd1" if g["mean_c_h"] >= 0 else "#888"
        parts.append(f'<text x="{left}" y="{y + 14}" font-size="12" fill="#222">'
                     f'{html.escape(g["lamps"])} / heater {g["heater"]} / fan {g.get("fan", "?")}</text>')
        parts.append(f'<rect x="{min(zero, x2):.1f}" y="{y + 2}" width="{abs(x2 - zero):.1f}" '
                     f'height="{row_h - 8}" fill="{col}" opacity=".85"/>')
        parts.append(f'<text x="{max(zero, x2) + 6:.1f}" y="{y + 14}" font-size="11" fill="#444">'
                     f'{g["mean_c_h"]:+.2f} ({g["windows"]} w, {g["hours"]} h)</text>')
    parts.append("</svg>")
    return "".join(parts)


def probe_readings(states: list[dict], every_min: int = 5) -> list[dict]:
    """The Pi probe as readings (about every 5 minutes), without the all-zero reads
    and anything that is not a plausible tank temperature."""
    out, last = [], None
    for s in states:
        v = s.get("probe_c")
        if v is None or v == 0.0 or not 10.0 < float(v) < 40.0:
            continue
        if last is None or (s["ts"] - last).total_seconds() >= every_min * 60 - 20:
            out.append({"ts": s["ts"], "temp_c": float(v), "heating": None})
            last = s["ts"]
    return out


def table_rows(groups) -> str:
    return "".join(
        f"<tr><td>{html.escape(g['lamps'])}</td><td>{g['heater']}</td><td>{g.get('fan', '')}</td>"
        f"<td>{g['windows']}</td>"
        f"<td>{g['hours']}</td><td>{g['mean_c_h']:+.3f}</td><td>{g['median_c_h']:+.3f}</td>"
        f"<td>{g['sd_c_h']:.3f}</td><td>{g['mean_c_h'] * 9 / 5:+.3f}</td></tr>" for g in groups)


def page(svg: str, groups, note: str, generated: str, span: str, pgroups=None,
         pspan: str = "") -> str:
    rows = table_rows(groups)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tank heat deltas</title>
<style>
:root{{--bg:#fff;--fg:#111;--mut:#555;--line:#ddd}}
@media (prefers-color-scheme: dark){{:root{{--bg:#16181b;--fg:#e8e8e8;--mut:#9aa;--line:#333}}
svg text{{fill:#cfcfcf !important}} svg path[stroke="#111"]{{stroke:#eee}} svg rect[fill="#f4f4f4"]{{fill:#262a2e}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;margin:0;padding:16px;max-width:1180px;margin-inline:auto}}
h1{{font-size:20px;margin:0 0 4px}} p.m{{color:var(--mut);margin:2px 0 12px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}}
th,td{{border-bottom:1px solid var(--line);padding:5px 8px;text-align:right}} th:first-child,td:first-child{{text-align:left}}
.w{{overflow-x:auto}} .n{{font-size:13px;color:var(--mut)}}
</style></head><body>
<h1>Tank heat deltas: lamps and heater against temperature</h1>
<p class="m">{html.escape(span)}. Generated {html.escape(generated)}. Times are Pacific (UTC-8).</p>
<div class="w">{svg}</div>
<h2 style="font-size:16px">Mean heat delta by lamps on</h2>
<div class="w"><table><tr><th>Lamps on</th><th>Heater</th><th>PC fan</th><th>Windows</th><th>Hours</th>
<th>Mean C/h</th><th>Median C/h</th><th>SD C/h</th><th>Mean F/h</th></tr>{rows}</table></div>
{("<h2 style='font-size:16px'>Older history: Pi probe (longer, less reliable)</h2><p class='n'>" + html.escape(pspan) + "</p><div class='w'><table><tr><th>Lamps on</th><th>Heater</th><th>PC fan</th><th>Windows</th><th>Hours</th><th>Mean C/h</th><th>Median C/h</th><th>SD C/h</th><th>Mean F/h</th></tr>" + table_rows(pgroups) + "</table></div>") if pgroups else ""}
<p class="n">{html.escape(note)}</p>
</body></html>"""


# ---------------------------------------------------------------------------
def build(data_dir: str, out_dir: str, days: float | None = None, now=None) -> dict:
    now = now or dt.datetime.now(UTC)
    map_path = os.path.join(data_dir, "heat", "lamp_map.json")
    if not os.path.exists(map_path):          # leave an editable copy of the default
        try:
            os.makedirs(os.path.dirname(map_path), exist_ok=True)
            with open(map_path, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_LAMP_MAP, fh, indent=2)
        except OSError:
            pass
    eras = load_lamp_map(data_dir)
    equip = he.load(data_dir, create=True)
    readings = load_inkbird(data_dir)
    states = load_states(data_dir, eras)
    if days:
        cut = now - dt.timedelta(days=days)
        readings = [r for r in readings if r["ts"] >= cut]
        states = [s for s in states if s["ts"] >= cut]
    windows = delta_windows(readings, states)
    groups = group_rates(windows)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "history.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_utc", "inkbird_c", "t1_f", "t2_f", "heating", "lamps_on", "pc_fan"])
        state_times = [s["ts"] for s in states]
        for r in readings:
            lamps = lamps_over(states, r["ts"] - dt.timedelta(minutes=1),
                               r["ts"] + dt.timedelta(minutes=1), state_times)
            w.writerow([r["ts"].isoformat(), r["temp_c"], r.get("t1_f"), r.get("t2_f"),
                        r.get("heating"), "" if lamps is None else lamps_label(lamps),
                        he.state_at(equip, "pc_fan", r["ts"])])
    with open(os.path.join(out_dir, "heat_deltas.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["start_utc", "end_utc", "lamps_on", "heater", "pc_fan", "start_c", "end_c",
                    "rate_c_per_h"])
        for x in windows:
            w.writerow([x["start"].isoformat(), x["end"].isoformat(), lamps_label(x["lamps"]),
                        x["heater"], x["fan"], round(x["start_c"], 3), round(x["end_c"], 3),
                        round(x["rate_c_h"], 4)])
    if readings:
        t0, t1 = readings[0]["ts"], readings[-1]["ts"]
    else:
        t0, t1 = now - dt.timedelta(days=1), now
    span = (f"{len(readings)} Inkbird readings, {t0.astimezone(PST):%Y-%m-%d %H:%M} to "
            f"{t1.astimezone(PST):%Y-%m-%d %H:%M}; {len(states)} minutes of lamp state; "
            f"{len(windows)} steady 60-minute windows")
    note = ("Rates are plain changes over 60 minutes with the same lamps on throughout; "
            "they mix the lamps' heat with the room, the heater and the tank's own drift, "
            "and the windows overlap, so the counts are not independent. Heater state is "
            "known only from the heat log onwards (earlier rows say unknown). The PC fan on the "
            "tank (data/heat/equipment.json) is cooling that no plug records: its state is a "
            "column of every table, and windows where it changed are dropped; before its first "
            "entry it is unknown, not off. The Inkbird "
            "reads in 0.056 C steps. Lamps are attributed by plug state and the date-based "
            "lamp map (data/heat/lamp_map.json); the container clock ran an hour slow until "
            "2026-11-03 about 07:30 UTC, which shifts those hours against real time but not "
            "the lamps against the temperature.")
    probe = probe_readings(states)
    pwindows = delta_windows(probe, states)
    pgroups = group_rates(pwindows)
    with open(os.path.join(out_dir, "heat_deltas_probe.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["start_utc", "end_utc", "lamps_on", "heater", "pc_fan", "start_c", "end_c",
                    "rate_c_per_h"])
        for x in pwindows:
            w.writerow([x["start"].isoformat(), x["end"].isoformat(), lamps_label(x["lamps"]),
                        x["heater"], x["fan"], round(x["start_c"], 3), round(x["end_c"], 3),
                        round(x["rate_c_h"], 4)])
    pspan = ""
    if probe:
        pspan = (f"{len(probe)} probe readings, {probe[0]['ts'].astimezone(PST):%Y-%m-%d %H:%M} to "
                 f"{probe[-1]['ts'].astimezone(PST):%Y-%m-%d %H:%M}; {len(pwindows)} steady windows. "
                 "The probe was in the tank for only part of this and has a hand-matched offset "
                 "(rates are unaffected by an offset, but they may be the room's, not the water's).")
    if probe and (not readings or probe[0]["ts"] < readings[0]["ts"]):
        t0 = min(t0, probe[0]["ts"])
    svg = build_chart(readings, states, windows, groups, t0, t1, pwindows=pwindows)
    with open(os.path.join(out_dir, "heat_chart.html"), "w", encoding="utf-8") as fh:
        fh.write(page(svg, groups, note, now.astimezone(PST).strftime("%Y-%m-%d %H:%M"), span,
                      pgroups, pspan))
    return {"readings": len(readings), "states": len(states), "windows": len(windows),
            "groups": groups, "pgroups": pgroups, "probe_windows": len(pwindows),
            "out": out_dir}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--data", default=os.environ.get("TANK_DATA", os.path.join(here, "data")))
    ap.add_argument("--out", default=None, help="default <data>/heat")
    ap.add_argument("--days", type=float, default=None)
    a = ap.parse_args(argv)
    res = build(a.data, a.out or os.path.join(a.data, "heat"), a.days)
    print(f"{res['readings']} readings, {res['states']} lamp-state minutes, "
          f"{res['windows']} windows -> {res['out']}")
    for title, groups in (("Inkbird", res["groups"]), ("Pi probe", res["pgroups"])):
        print(title)
        for g in groups:
            print(f"  {g['lamps']:<22} heater {g['heater']:<8} fan {g['fan']:<8} {g['windows']:>4} windows "
                  f"{g['hours']:>5} h  mean {g['mean_c_h']:+.3f} C/h  median {g['median_c_h']:+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
