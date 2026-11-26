#!/usr/bin/env python3
"""tank_plan.py -- minute-precise desired tank state from the spawn calendar.

Pure planning: no hardware, no network. Given the year calendar written by
spawn_calendar.py and an instant, answer "what should the tank be doing at
this minute?":

    temperature   daily target (calendar) and an hourly profile on top of it
    booleans      full_dark, any_sun, any_moon (plus white/blue/moon, watch,
                  curtain)
    curves        white_level / blue_level (dawn and dusk ramps) and a moon
                  target in lux scaled by moon illumination

Scheduling uses the *_utc columns of the CSV, never the *_local ones, so a DST
change cannot shift an event. The calendar's offset (cues.py offset_schedule)
is already baked into the rows; nothing here does offset arithmetic.

Data rule (TANK-NOTES.md): no synthetic environmental data. The hourly profile
therefore defaults to flat (all zeros). Fill hourly_offsets_c from a measured
diel pattern, never from a made-up curve.

Standard library plus cues.py.
"""
from __future__ import annotations

import bisect
import copy
import csv
import datetime as dt
import glob
import hashlib
import json
import math
import os
from dataclasses import dataclass, field, asdict
from zoneinfo import ZoneInfo

import cues

UTC = dt.timezone.utc
_HERE = os.path.dirname(os.path.abspath(__file__))

# kind -> (start column, end column). Each is a list of non-overlapping UTC
# intervals across the year.
KINDS = {
    "white": ("white_on_utc", "white_off_utc"),
    "blue": ("blue_on_utc", "blue_off_utc"),
    "moon": ("moon_on_utc", "moon_off_utc"),
    "moon2": ("moon2_on_utc", "moon2_off_utc"),
    "dark": ("dark_start_utc", "dark_end_utc"),
    # full_dark: astronomical dusk to moonrise, then moonset to astronomical dawn.
    "astro_dark": ("astro_dark_start_utc", "astro_dark_end_utc"),
    "astro_dark2": ("astro_dark2_start_utc", "astro_dark2_end_utc"),
    "watch": ("watch_start_utc", "watch_end_utc"),
    "curtain": ("curtain_close_utc", "curtain_open_utc"),
}

SHAPES = ("linear", "smoothstep", "cosine")

DEFAULT_PLAN_CONFIG = {
    # "Full dark" has one meaning: sun below -18 deg AND moon down, i.e.
    # astronomical dusk to moonrise, then moonset to astronomical dawn (the
    # calendar's astro_dark and astro_dark2 windows). Kept as a key only so
    # existing configs that say "astro" still load; no other value is accepted.
    "dark_source": "astro",
    # Added to the day's target at each local hour mark, linearly interpolated
    # between marks. Flat until a measured profile exists.
    "hourly_offsets_c": [0.0] * 24,
    "max_hourly_offset_c": 1.0,
    # Moon target at the water surface, scaled by the night's illumination.
    # Build plan: 0.1 to 0.3 lux. Provisional until the lux sensor is chosen.
    "moon_lux_range": [0.1, 0.3],
    # The second light step (a plug that can only switch, not dim). Over the lit
    # day the brightness follows a half sine, sin(pi * fraction of the day), the
    # shape of the sun's height; white_plus is ON while that is at or above
    # `threshold`, so it covers the brighter middle of the day and shrinks and
    # grows with the day length. 0.5 = the middle two thirds of the day.
    "white_plus": {"enabled": True, "threshold": 0.5},
    # Dawn/dusk shape inside each channel's on-window. Provisional.
    "curves": {
        "white": {"ramp_up_min": 30, "ramp_down_min": 30,
                  "shape": "smoothstep", "max": 1.0},
        "blue": {"ramp_up_min": 30, "ramp_down_min": 30,
                 "shape": "smoothstep", "max": 1.0},
    },
}


def plan_config(overrides: dict | None = None) -> dict:
    """Defaults merged with overrides, validated. Raises ValueError."""
    cfg = copy.deepcopy(DEFAULT_PLAN_CONFIG)
    for key, val in (overrides or {}).items():
        if key == "curves":
            for ch, spec in val.items():
                cfg["curves"].setdefault(ch, {}).update(spec)
        elif key == "white_plus":
            cfg["white_plus"].update(val)
        else:
            cfg[key] = val
    if cfg["dark_source"] != "astro":
        raise ValueError("dark_source must be 'astro' (the only definition of full dark)")
    offs = cfg["hourly_offsets_c"]
    if len(offs) != 24:
        raise ValueError("hourly_offsets_c needs 24 values")
    cap = float(cfg["max_hourly_offset_c"])
    if any(abs(float(o)) > cap for o in offs):
        raise ValueError(f"hourly offset beyond +/-{cap} C")
    lo, hi = cfg["moon_lux_range"]
    if not 0 <= float(lo) <= float(hi):
        raise ValueError("moon_lux_range must be [lo, hi] with 0 <= lo <= hi")
    if not 0 < float(cfg["white_plus"]["threshold"]) <= 1:
        raise ValueError("white_plus.threshold must be above 0 and at most 1")
    for ch, spec in cfg["curves"].items():
        if spec.get("shape", "smoothstep") not in SHAPES:
            raise ValueError(f"curve {ch}: shape must be one of {SHAPES}")
        if min(float(spec.get("ramp_up_min", 0)),
               float(spec.get("ramp_down_min", 0))) < 0:
            raise ValueError(f"curve {ch}: ramps cannot be negative")
        if not 0 <= float(spec.get("max", 1.0)) <= 1:
            raise ValueError(f"curve {ch}: max must be within 0..1")
    return cfg


# ---------------------------------------------------------------------------
# CALENDAR
# ---------------------------------------------------------------------------
def _parse_utc(text: str):
    text = (text or "").strip()
    if not text:
        return None
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class Interval:
    start: dt.datetime
    end: dt.datetime
    row_date: str          # the calendar row this interval came from


class Calendar:
    """The year CSV, indexed by date and by interval kind."""

    def __init__(self, path: str, rows: dict, problems: list):
        self.path = path
        self.rows = rows
        self.problems = list(problems)
        self._iv: dict[str, list[Interval]] = {}
        self._starts: dict[str, list] = {}
        for kind, (c0, c1) in KINDS.items():
            ivs = []
            for date, row in rows.items():
                a, b = _parse_utc(row.get(c0)), _parse_utc(row.get(c1))
                if a is None or b is None:
                    continue
                if b < a:
                    self.problems.append(f"calendar_bad_interval:{kind}:{date}")
                    continue
                if b == a:          # a zero-minute window: "none this night"
                    continue
                ivs.append(Interval(a, b, date))
            ivs.sort(key=lambda i: i.start)
            for prev, cur in zip(ivs, ivs[1:]):
                if cur.start < prev.end:
                    self.problems.append(
                        f"calendar_overlap:{kind}:{prev.row_date}/{cur.row_date}")
            self._iv[kind] = ivs
            self._starts[kind] = [i.start for i in ivs]
        dates = sorted(rows)
        self.first_date = dt.date.fromisoformat(dates[0]) if dates else None
        self.last_date = dt.date.fromisoformat(dates[-1]) if dates else None

    def interval_at(self, kind: str, t: dt.datetime):
        starts = self._starts[kind]
        i = bisect.bisect_right(starts, t) - 1
        if i >= 0 and t < self._iv[kind][i].end:
            return self._iv[kind][i]
        return None

    def days_left(self, local_date: dt.date) -> int:
        if self.last_date is None:
            return -1
        return (self.last_date - local_date).days


def find_calendar(path: str | None = None) -> str | None:
    """Explicit path, else TANK_CALENDAR, else the newest spawn_calendar_*.csv
    in the data dir (TANK_DATA, else <repo>/data) or beside this file."""
    path = path or os.environ.get("TANK_CALENDAR")
    if path:
        return path
    data = os.environ.get("TANK_DATA", os.path.join(os.path.dirname(_HERE), "data"))
    # The calendar the setup page confirmed (setup_core.apply_calendar) wins
    # over the newest-by-name rule, so an older file name can be the active one.
    try:
        with open(os.path.join(data, "setup.json"), encoding="utf-8") as fh:
            pinned = (json.load(fh).get("calendar") or {}).get("file")
        if pinned and os.path.exists(os.path.join(data, pinned)):
            return os.path.join(data, pinned)
    except (OSError, ValueError, AttributeError):
        pass
    for folder in (data, _HERE):
        found = sorted(glob.glob(os.path.join(folder, "spawn_calendar_*.csv")))
        if found:
            return found[-1]
    return None


def load_calendar(path: str, params=None) -> Calendar:
    """Read the CSV and check it against its .meta.json.

    A hash mismatch is reported as a problem, not an exception: the calendar
    that was generated is still a valid schedule, so the controller keeps
    following it and raises the alarm (the offset is never silently changed).
    """
    problems = []
    with open(path, newline="", encoding="utf-8") as fh:
        rows = {r["date"]: r for r in csv.DictReader(fh)}
    meta_path = path[:-4] + ".meta.json" if path.endswith(".csv") else None
    meta = None
    if meta_path and os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            problems.append("calendar_meta_unreadable")
    else:
        problems.append("calendar_meta_missing")
    if meta:
        if meta.get("csv_sha256") != _sha256(path):
            problems.append("calendar_csv_hash_mismatch")
        if meta.get("cue_params_sha256") != cues.params_sha256(params):
            problems.append(
                "calendar_params_stale: cue_params.json changed after this "
                "calendar was generated; regenerate with spawn_calendar.py")
    if not rows:
        problems.append("calendar_empty")
    # Startup check of the audited cue data (scripts/data_manifest.py verify, run by the tank service's
    # command before the loop); in warn mode the controller runs on, so the mismatch becomes an alert here.
    check = os.path.join(os.path.dirname(path), "data_manifest_check.json")
    if os.path.exists(check):
        try:
            with open(check, encoding="utf-8") as fh:
                res = json.load(fh)
            if not res.get("ok", True):
                problems.append(
                    "data_manifest_mismatch: %d changed, %d missing against the audited manifest%s "
                    "(scripts/data_manifest.py verify; re-pin after a clean audit)"
                    % (len(res.get("changed", [])), len(res.get("missing", [])),
                       " (%s)" % res["error"] if res.get("error") else ""))
        except (OSError, ValueError):
            problems.append("data_manifest_check_unreadable")
    cal = Calendar(path, rows, problems)
    cal.params = params        # the planner's local time follows the params the calendar was loaded with
    return cal


# ---------------------------------------------------------------------------
# CURVES
# ---------------------------------------------------------------------------
def _shape(x: float, shape: str) -> float:
    x = min(1.0, max(0.0, x))
    if shape == "linear":
        return x
    if shape == "cosine":
        return (1 - math.cos(math.pi * x)) / 2
    return x * x * (3 - 2 * x)          # smoothstep


def day_curve(t: dt.datetime, iv: Interval | None) -> float:
    """The simulated day brightness, 0..1: a half sine across the lit window
    (0 at sunrise and sunset, 1 at the middle)."""
    if iv is None or not (iv.start <= t < iv.end):
        return 0.0
    frac = (t - iv.start).total_seconds() / (iv.end - iv.start).total_seconds()
    return round(math.sin(math.pi * frac), 4)


def curve_level(t: dt.datetime, iv: Interval | None, spec: dict) -> float:
    """0..max inside the on-window, ramping up after start and down before end.

    The window itself (on/off) is the boolean; this is only the brightness
    shape for a dimmable output. A non-dimmable plug follows the boolean.
    """
    if iv is None or not (iv.start <= t < iv.end):
        return 0.0
    shape = spec.get("shape", "smoothstep")
    up = float(spec.get("ramp_up_min", 0))
    down = float(spec.get("ramp_down_min", 0))
    level = 1.0
    since = (t - iv.start).total_seconds() / 60
    until = (iv.end - t).total_seconds() / 60
    if up > 0 and since < up:
        level = min(level, _shape(since / up, shape))
    if down > 0 and until < down:
        level = min(level, _shape(until / down, shape))
    return round(float(spec.get("max", 1.0)) * level, 4)


# ---------------------------------------------------------------------------
# PLAN
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Plan:
    t_utc: str
    local: str
    tank_date: str
    offset_days: float
    cue_date: str
    temp_daily_c: float
    temp_hourly_c: float            # what the tank should be at right now
    full_dark: bool
    any_sun: bool
    any_moon: bool
    white_on: bool
    blue_on: bool
    moon_on: bool
    watch: bool
    curtain_closed: bool
    white_level: float
    blue_level: float
    white_plus_on: bool
    white_plus_level: float         # the simulated day curve, 0..1
    moon_illum_pct: float | None
    moon_target_lux: float | None
    violations: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def tank_tz(params=None) -> ZoneInfo:
    return ZoneInfo(cues.tank_site(params)["tz"])


def plan_at(cal: Calendar, cfg: dict, when: dt.datetime, params=None):
    """Desired state at `when` (rounded down to the minute), or None when the
    calendar has no row for that local date."""
    t = when.astimezone(UTC).replace(second=0, microsecond=0)
    local = t.astimezone(tank_tz(params if params is not None else getattr(cal, "params", None)))
    row = cal.rows.get(local.date().isoformat())
    if row is None:
        return None

    lo, hi = cues.safety_band_c(params)
    daily = float(row["target_c"])
    offs = cfg["hourly_offsets_c"]
    o0, o1 = float(offs[local.hour]), float(offs[(local.hour + 1) % 24])
    hourly = daily + o0 + (o1 - o0) * local.minute / 60
    hourly = round(min(hi, max(lo, hourly)), 3)

    white = cal.interval_at("white", t)
    blue = cal.interval_at("blue", t)
    moon = cal.interval_at("moon", t) or cal.interval_at("moon2", t)
    # The lit tank is never full dark: the calendar windows and the lamp times
    # come from separate rounding and can disagree by a minute, so the lights
    # and the moon lamp take precedence.
    dark = ((cal.interval_at("astro_dark", t) is not None
             or cal.interval_at("astro_dark2", t) is not None)
            and moon is None and white is None and blue is None)

    illum = None
    target_lux = None
    if moon is not None:
        illum = float(cal.rows[moon.row_date]["moon_illum_pct"])
        mlo, mhi = cfg["moon_lux_range"]
        target_lux = round(float(mlo) + (float(mhi) - float(mlo)) * illum / 100, 3)

    day = day_curve(t, white)
    wp = cfg["white_plus"]
    plus_on = bool(wp["enabled"]) and day >= float(wp["threshold"])

    plan = Plan(
        t_utc=t.isoformat(),
        local=local.isoformat(),
        tank_date=local.date().isoformat(),
        offset_days=float(row["offset_days"] or 0),
        cue_date=row["cue_date"],
        temp_daily_c=round(daily, 3),
        temp_hourly_c=hourly,
        full_dark=dark,
        any_sun=white is not None or blue is not None,
        any_moon=moon is not None,
        white_on=white is not None,
        blue_on=blue is not None,
        moon_on=moon is not None,
        watch=cal.interval_at("watch", t) is not None,
        curtain_closed=cal.interval_at("curtain", t) is not None,
        white_level=curve_level(t, white, cfg["curves"].get("white", {})),
        blue_level=curve_level(t, blue, cfg["curves"].get("blue", {})),
        white_plus_on=plus_on,
        white_plus_level=day,
        moon_illum_pct=illum,
        moon_target_lux=target_lux,
    )
    bad = []
    if plan.full_dark and plan.any_sun:
        bad.append("sun_during_full_dark")
    if plan.full_dark and plan.any_moon:
        bad.append("moon_during_full_dark")
    if not lo <= plan.temp_hourly_c <= hi:
        bad.append("temp_outside_safety_band")
    if bad:
        object.__setattr__(plan, "violations", bad)
    return plan


_BOOLS = ("full_dark", "any_sun", "any_moon", "white_on", "white_plus_on", "blue_on",
          "moon_on", "watch", "curtain_closed")


def day_events(cal: Calendar, cfg: dict, day: dt.date, params=None):
    """Every minute at which a boolean flips during one local day, as
    (local time, field, new value) in order. For reviewing a day's plan."""
    tz = tank_tz(params if params is not None else getattr(cal, "params", None))
    start = dt.datetime.combine(day, dt.time(0), tzinfo=tz).astimezone(UTC)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0),
                              tzinfo=tz).astimezone(UTC)
    events = []
    prev = None
    t = start
    while t < end:
        p = plan_at(cal, cfg, t, params)
        if p is not None:
            cur = {k: getattr(p, k) for k in _BOOLS}
            if prev is not None:
                for k in _BOOLS:
                    if cur[k] != prev[k]:
                        events.append((p.local[11:16], k, cur[k]))
            prev = cur
        t += dt.timedelta(minutes=1)
    return events
