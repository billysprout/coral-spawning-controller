#!/usr/bin/env python3
"""spawn_calendar.py -- year-long cue CSV for the controller (read-only).

    python spawn_calendar.py --start 2026-11-01 --days 365 \\
        --out spawn_calendar_2026-11-01.csv

writes the CSV plus <name>.meta.json (generator version, sha256 of
cue_params.json, sha256 of the CSV, column list). Output is deterministic.
Units: degF for people, degC for the device (both columns are present).

ONE ROW PER NIGHT, anchored on the evening's tank date D. The lighting columns
describe the lit day of D (sunrise D morning -> sunset D evening); the
darkness, moon, watch and curtain columns describe the night that begins on
the evening of D (curtain_open is the next morning's first light).

Clock mapping (spawn-calendar-plan.md section B)
    cue_date C = D - offset(D)            (cues.py, stepped season shift)
    SUN events (sunrise, sunset, astro dusk/dawn) are computed at the Okinawa
    reference site for date C and expressed as whole minutes from Okinawa
    solar noon on C.
    MOON events: the moon is never shifted. Moonrise/moonset are computed at
    Okinawa for the 24 h starting at Okinawa solar noon on the tank date D
    itself (not C) and expressed as whole minutes from Okinawa solar noon on D.
    Moon phase (illumination, age, days_after_full) is evaluated at the middle
    of that same Okinawa night, so phase and rise times always agree. (This is
    the Okinawa sky of calendar date D, which differs by under a day of lunar
    phase from the tank's own instant.)
    Every relative-minute value is then added to the tank's solar noon on D
    (NOAA equation of time at lon -118.0, rounded to a whole minute) and
    converted to America/Los_Angeles wall time with zoneinfo, so DST changes
    the clock reading but not the light the animals see.
    With offset 0 the photoperiod and the sunset->moonrise gap are exactly
    Okinawa's (+/- rounding). With a non-zero offset the gap is
    moonrise(D) - sunset(C), both Okinawa values.

Lights: blue_on = sunrise - blue_before_sunrise_min; white_on = sunrise;
white_off = sunset; blue_off = sunset + blue_after_sunset_min (0, so blue and
white go off together at sunset).

Moon (moon_on/moon_off, moon2_on/moon2_off): intervals when the moon is above
the horizon while the sun is below it, clipped to [sunset, next sunrise]. If
the moon is already up at sunset, moon_on = sunset. Blank when the moon never
rises in the night.

Darkness (sunset based, Lin et al. 2021 framing, conflict X010):
    cue_dark_min = sunset -> first moonrise after sunset, in minutes. 0 when
                 the moon is up at sunset; if the moon stays down until
                 sunrise it is sunset -> next sunrise. The "post-sunset dark
                 interval" signal.
    dark_start/dark_end/dark_min = the FIRST moon-down block inside
                 [sunset, next sunrise]: it starts at sunset, or at moonset if
                 the moon is up at sunset, and ends at moonrise or the next
                 sunrise. Empty (dark_min 0) only if the moon is up from sunset
                 to sunrise. The moon LED (moon_on/moon_off) is off throughout.
    astro_dark_*, astro_dark2_*: the full_dark windows, the one definition
                 the controller uses: astronomical dusk to moonrise, then
                 moonset to astronomical dawn (sun below -18 deg AND moon
                 down). astro_dark is the first window and astro_dark2 the
                 second; either is blank when the moon is up for that part
                 of the night. astro_dark_min is the total of both.

Watch: cue_date inside the watch season (cue_params watch.season_start ..
season_end, May 30 - Jul 5: JAZA spawns Jun 2 - Jul 2 +/- 3 days) AND
days_after_full in [6, 9]; no temperature gate. watch_season is true on every
night whose cue date is in the season, whatever the moon. watch_start = sunset - 120 min, watch_end = sunset + 180 min.
Curtain: close at white_off/blue_off (last evening light off = sunset),
open at the next morning's blue_on. Time columns are blank when not
applicable.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sys
from zoneinfo import ZoneInfo

import cues
import dark_window as dw

GENERATOR_VERSION = "1.1"
SYNODIC = 29.530588

TIME_COLS = [
    "blue_on", "white_on", "white_off", "blue_off",
    "moon_on", "moon_off", "moon2_on", "moon2_off",
    "dark_start", "dark_end", "astro_dark_start", "astro_dark_end",
    "astro_dark2_start", "astro_dark2_end",
    "watch_start", "watch_end", "curtain_close", "curtain_open",
]


def _cols():
    out = ["date", "cue_date", "offset_days", "doy",
           "target_f", "target_c", "raw_f", "raw_c", "clamped", "delta_f"]

    def pair(name):
        return [f"{name}_local", f"{name}_utc"]
    out += pair("blue_on") + pair("white_on") + pair("white_off") + pair("blue_off")
    out += ["photoperiod_min", "moon_illum_pct", "moon_age_d", "days_after_full"]
    out += pair("moon_on") + pair("moon_off") + pair("moon2_on") + pair("moon2_off")
    out += ["cue_dark_min"] + pair("dark_start") + pair("dark_end") + ["dark_min"]
    out += pair("astro_dark_start") + pair("astro_dark_end")
    out += pair("astro_dark2_start") + pair("astro_dark2_end") + ["astro_dark_min"]
    out += ["watch_season", "watch"] + pair("watch_start") + pair("watch_end")
    out += pair("curtain_close") + pair("curtain_open")
    assert set(TIME_COLS) == {c[:-6] for c in out if c.endswith("_local")}
    return out


COLUMNS = _cols()


class _Ctx:
    """Per-run state: params, sites, cached 24 h scans."""

    def __init__(self, params):
        self.p = params
        self.oki = cues.reference_site(params)
        self.tank = cues.tank_site(params)
        self.tz = ZoneInfo(self.tank["tz"])
        self.blue_before = float(params["photoperiod"]["blue_before_sunrise_min"])
        self.blue_after = float(params["photoperiod"]["blue_after_sunset_min"])
        w = params["watch"]
        self.w_min, self.w_max = w["days_after_full_min"], w["days_after_full_max"]
        self.w_before = w["start_before_sunset_min"]
        self.w_after = w["end_after_sunset_min"]
        self._sun = {}
        self._moon = {}

    def oki_noon(self, d):
        return dw.solar_noon_utc(d, self.oki.lon)

    def tank_noon(self, d):
        t = dw.solar_noon_utc(d, self.tank["lon"])
        return (t + dt.timedelta(seconds=30)).replace(second=0, microsecond=0)

    def rel(self, t, d):
        return round((t - self.oki_noon(d)).total_seconds() / 60.0)

    def sun_scan(self, c):
        """Sun events (UTC instants) in the 24 h from Okinawa solar noon on
        date c; sunrise/dawn there are the NEXT morning's."""
        if c not in self._sun:
            ev = dw.day_events(self.oki_noon(c), self.oki, moon_events=False)
            self._sun[c] = {k: ev[k] for k in ("sunrise", "sunset", "dawn", "dusk")}
        return self._sun[c]

    def moon_scan(self, d):
        """Moon-up intervals (rel minutes from Okinawa solar noon on d) in the
        24 h from that noon, plus phase at the middle of that window."""
        if d not in self._moon:
            start = self.oki_noon(d)
            ev = dw.day_events(start, self.oki, sun_events=False)
            ma, h0 = dw.moon_altitude_and_h0(dw.jd_from_utc(start), self.oki)
            edges = sorted([(self.rel(t, d), "rise") for t in ev["moonrise"]]
                           + [(self.rel(t, d), "set") for t in ev["moonset"]])
            ivs, cur = [], (0 if ma > h0 else None)
            for m, kind in edges:
                if kind == "rise":
                    cur = m
                elif cur is not None:
                    ivs.append((cur, m))
                    cur = None
            if cur is not None:
                ivs.append((cur, 1440))
            illum, age = dw.moon_phase(
                dw.jd_from_utc(start + dt.timedelta(hours=12)))
            self._moon[d] = (ivs, illum, age)
        return self._moon[d]

    def stamp(self, d, rel_min):
        """(local HH:MM, UTC ISO) for the tank on date d at rel_min from noon."""
        if rel_min is None:
            return "", ""
        t = self.tank_noon(d) + dt.timedelta(minutes=rel_min)
        return (t.astimezone(self.tz).strftime("%H:%M"),
                t.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _dark(start, limit_end, ivs):
    """(dark_start, dark_end) in rel minutes for a night [start, limit_end]."""
    for a, b in ivs:
        if a <= start < b:          # moon already up when the night starts
            return start, start
        if a > start:
            return start, min(a, limit_end)
    return start, limit_end


def _moon_down_blocks(start, end, ivs):
    """Moon-down blocks inside [start, end] (rel minutes), in time order."""
    out, cur = [], start
    for a, b in sorted(ivs):
        if b <= cur:
            continue
        if a > cur:
            out.append((cur, min(a, end)))
        cur = max(cur, b)
        if cur >= end:
            return out
    if cur < end:
        out.append((cur, end))
    return out


def _first_moon_down_block(start, end, ivs):
    """First moon-down block inside [start, end] (rel minutes), or None."""
    blocks = _moon_down_blocks(start, end, ivs)
    return blocks[0] if blocks else None


def build_row(ctx: _Ctx, d: dt.date, prev_target_f=None) -> dict:
    p = ctx.p
    det = cues.target_detail(d, p)
    c = det["cue_date"]
    tgt_c = round(det["target_c"], 2)
    tgt_f_exact = cues.c_to_f(tgt_c)
    row = {
        "date": d.isoformat(), "cue_date": c.isoformat(),
        "offset_days": f"{det['offset_days']:.2f}".rstrip("0").rstrip("."),
        "doy": d.timetuple().tm_yday,
        "target_c": f"{tgt_c:.2f}", "target_f": f"{tgt_f_exact:.2f}",
        "raw_c": f"{det['raw_c']:.2f}",
        "raw_f": f"{cues.c_to_f(det['raw_c']):.2f}",
        "clamped": "true" if det["clamped"] else "false",
    }
    if prev_target_f is None:
        prev_target_f = cues.c_to_f(round(cues.target_c(d - dt.timedelta(days=1), p), 2))
    row["delta_f"] = f"{tgt_f_exact - prev_target_f:.3f}"

    # --- sun (Okinawa, cue date C) ---
    sun_morning = ctx.sun_scan(c - dt.timedelta(days=1))   # has morning of C
    sun_eve = ctx.sun_scan(c)                              # evening C, morning C+1
    rel = lambda t: ctx.rel(t, c)                          # noqa: E731
    sunrise = rel(sun_morning["sunrise"][0])
    sunset = rel(sun_eve["sunset"][0])
    next_sunrise = rel(sun_eve["sunrise"][0])
    dusk, dawn = rel(sun_eve["dusk"][0]), rel(sun_eve["dawn"][0])

    blue_on = round(sunrise - ctx.blue_before)
    blue_off = round(sunset + ctx.blue_after)
    times = {"blue_on": blue_on, "white_on": sunrise, "white_off": sunset,
             "blue_off": blue_off}
    row["photoperiod_min"] = sunset - sunrise

    # --- moon (Okinawa, tank date D, unshifted) ---
    ivs, illum, age = ctx.moon_scan(d)
    row["moon_illum_pct"] = f"{illum * 100:.1f}"
    row["moon_age_d"] = f"{age:.1f}"
    daf = (age - SYNODIC / 2.0) % SYNODIC
    row["days_after_full"] = f"{daf:.1f}"
    night = []
    for a, b in ivs:
        lo, hi = max(a, sunset), min(b, next_sunrise)
        if hi > lo:
            night.append((lo, hi))
    for i, key in enumerate(("moon", "moon2")):
        if i < len(night):
            times[f"{key}_on"], times[f"{key}_off"] = night[i]
    # --- darkness ---
    cs, ce = _dark(sunset, next_sunrise, ivs)
    row["cue_dark_min"] = ce - cs
    blk = _first_moon_down_block(sunset, next_sunrise, ivs)
    if blk:
        times.update(dark_start=blk[0], dark_end=blk[1])
    row["dark_min"] = (blk[1] - blk[0]) if blk else 0
    # full_dark: astronomical dusk to moonrise, then moonset to astronomical dawn.
    # Zero, one or two windows; astro_dark_min is their total.
    blocks = _moon_down_blocks(dusk, dawn, ivs)
    assert len(blocks) <= 2, blocks
    for key, blk2 in zip(("astro_dark", "astro_dark2"), blocks):
        times[f"{key}_start"], times[f"{key}_end"] = blk2
    row["astro_dark_min"] = sum(b - a for a, b in blocks)
    # --- watch ---
    in_season = cues.in_watch_season(c, p)
    watch = cues.is_watch_night(d, daf, p)
    row["watch_season"] = "true" if in_season else "false"
    row["watch"] = "true" if watch else "false"
    if watch:
        times["watch_start"] = sunset - ctx.w_before
        times["watch_end"] = sunset + ctx.w_after
    # --- curtain ---
    times["curtain_close"] = blue_off
    times["curtain_open"] = round(next_sunrise - ctx.blue_before)

    for name in TIME_COLS:
        loc, utc = ctx.stamp(d, times.get(name))
        row[f"{name}_local"], row[f"{name}_utc"] = loc, utc
    return {col: row[col] for col in COLUMNS}


def build_calendar(start: dt.date, days: int, params=None):
    params = params or cues.load_params()
    ctx = _Ctx(params)
    rows, prev = [], None
    for k in range(days):
        d = start + dt.timedelta(days=k)
        row = build_row(ctx, d, prev)
        prev = cues.c_to_f(float(row["target_c"]))
        rows.append(row)
    return rows


def write_csv(rows, path) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def write_meta(path, csv_path, csv_sha, params, start, days) -> str:
    meta_path = (path[:-4] if path.endswith(".csv") else path) + ".meta.json"
    meta = {
        "generator": "spawn_calendar.py",
        "generator_version": GENERATOR_VERSION,
        "csv": os.path.basename(csv_path),
        "csv_sha256": csv_sha,
        "cue_params_sha256": params.sha256,
        "start": start.isoformat(),
        "days": days,
        "rows": days,
        "columns": COLUMNS,
    }
    with open(meta_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return meta_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Year-long cue CSV for the controller")
    ap.add_argument("--start", default="2026-11-01", help="first tank date")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default=None, help="CSV path")
    ap.add_argument("--params", default=None, help="cue_params.json path")
    ns = ap.parse_args(argv)
    start = dt.date.fromisoformat(ns.start)
    out = ns.out or f"spawn_calendar_{start.isoformat()}.csv"
    params = cues.load_params(ns.params)
    rows = build_calendar(start, ns.days, params)
    sha = write_csv(rows, out)
    meta = write_meta(out, out, sha, params, start, ns.days)
    print(f"wrote {len(rows)} rows to {out} (+ {os.path.basename(meta)})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
