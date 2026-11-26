#!/usr/bin/env python3
"""cues.py -- single reader of cue_params.json (read-only, stdlib only).

Everything that needs the seasonal temperature curve, the stepped season
shift, the safety band or the site definitions goes through here, so
conductor.py, dark_window.py and spawn_calendar.py cannot drift apart.

Temperature model
    raw_c(date)    = SST fit evaluated at (day-of-year(date) - offset(date))
                     fit = mean + sum(cos_k*cos(k*w) + sin_k*sin(k*w)),
                     w = 2*pi*doy/365.25, doy may be fractional/negative
    clamped(date)  = raw_c clamped to safety_band_c
    target_c(date) = clamped series with a day-to-day step limit
                     (max_step_c_per_day). The limit makes the value depend on
                     the previous day, so the series is walked forward from a
                     deterministic anchor: the `from` date of the FIRST
                     offset_schedule entry (the start of the programme). On the
                     anchor day the target is the clamped raw value; each later
                     day moves toward its clamped raw value by at most the step
                     limit. Dates before the anchor get the clamped raw value
                     (no limit; there is no history to limit against).

Season shift
    offset_days(date) comes from `offset_schedule`: a list of {from,
    offset_days, ramp_days?}. From `from` the offset moves linearly from its
    previous value to offset_days over ramp_days (0 or missing = a step).
    Offsets are fractional during a ramp: temperature uses the fractional
    value; cue_date (used for the sun) rounds to a whole day.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PARAMS_PATH = os.path.join(_HERE, "cue_params.json")


class Params(dict):
    """cue_params.json contents plus its path, hash and the walked series."""

    path: str
    sha256: str

    def __init__(self, data, path):
        super().__init__(data)
        self.path = path
        with open(path, "rb") as fh:
            self.sha256 = hashlib.sha256(fh.read()).hexdigest()
        self._series = []          # clamped+limited target_c from the anchor on
        self._schedule = None


_cache = {}


def active_params_path() -> str:
    """TANK_CUE_PARAMS, else <data>/cue_params.json when the setup page has
    written one (the tank's own locale and start point), else the Okinawa file
    shipped beside this module."""
    env = os.environ.get("TANK_CUE_PARAMS")
    if env:
        return env
    data = os.environ.get("TANK_DATA", os.path.join(os.path.dirname(_HERE), "data"))
    local = os.path.join(data, "cue_params.json")
    return local if os.path.exists(local) else reference_params_path()


def reference_params_path() -> str:
    """The Okinawa reference parameters: shipped beside this module in the
    research repo, else installed from the cue-table repo by cue_import.py at
    <data>/reference/cue_params.json (the public controller ships none). The
    repo's own data/reference is the fallback, so a run with TANK_DATA pointed
    elsewhere (the test service uses /tmp/nodata) still finds the installed copy."""
    if os.path.exists(DEFAULT_PARAMS_PATH):
        return DEFAULT_PARAMS_PATH
    repo_data = os.path.join(os.path.dirname(_HERE), "data")
    data = os.environ.get("TANK_DATA", repo_data)
    for d in (data, repo_data):
        p = os.path.join(d, "reference", "cue_params.json")
        if os.path.exists(p):
            return p
    return os.path.join(data, "reference", "cue_params.json")


def load_params(path=None) -> Params:
    path = os.path.abspath(path or active_params_path())
    if path not in _cache:
        with open(path, encoding="utf-8") as fh:
            _cache[path] = Params(json.load(fh), path)
    return _cache[path]


def _p(p):
    return p if p is not None else load_params()


def params_sha256(p=None) -> str:
    return _p(p).sha256


def safety_band_c(p=None):
    lo, hi = _p(p)["safety_band_c"]
    return float(lo), float(hi)


def season_gate_c(p=None) -> float:
    return float(_p(p)["season_gate_c"])


def _md(s):
    m, d = s.split("-")
    return int(m), int(d)


def in_watch_season(cue: dt.date, p=None) -> bool:
    """True when the cue date's month-day is inside the watch season
    (watch.season_start .. season_end inclusive, year-independent). A season
    whose start is after its end wraps the year end."""
    w = _p(p)["watch"]
    a, b, x = _md(w["season_start"]), _md(w["season_end"]), (cue.month, cue.day)
    return (a <= x <= b) if a <= b else (x >= a or x <= b)


def is_watch_night(date: dt.date, days_after_full: float, p=None) -> bool:
    """Watch rule: cue date inside the watch season AND days_after_full in
    [days_after_full_min, days_after_full_max]. No temperature gate. A moon
    window whose min is above its max wraps past full moon (a locale window
    such as 27.8 to 6.8 days after full)."""
    p = _p(p)
    w = p["watch"]
    lo, hi = float(w["days_after_full_min"]), float(w["days_after_full_max"])
    moon_ok = (lo <= days_after_full <= hi if lo <= hi
               else days_after_full >= lo or days_after_full <= hi)
    return in_watch_season(cue_date(date, p), p) and moon_ok


def max_step_c(p=None) -> float:
    return float(_p(p)["max_step_c_per_day"])


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# --- sites --------------------------------------------------------------
def reference_site(p=None):
    """dark_window.Site for the reference (Okinawa) site."""
    import dark_window
    s = _p(p)["reference_site"]
    return dark_window.Site(float(s["lat"]), float(s["lon"]),
                            float(s["tz_offset_h"]))


def tank_site(p=None) -> dict:
    return dict(_p(p)["tank_site"])


# --- SST fit ------------------------------------------------------------
def fit_c(doy: float, p=None) -> float:
    fit = _p(p)["sst_fit"]
    w0 = 2 * math.pi * doy / float(fit["period_days"])
    v = float(fit["mean"])
    for h in fit["harmonics"]:
        k = h["k"]
        v += h["cos"] * math.cos(k * w0) + h["sin"] * math.sin(k * w0)
    return v


# --- season offset --------------------------------------------------------
def _schedule(p):
    if p._schedule is None:
        ents = []
        for e in p["offset_schedule"]:
            ents.append((dt.date.fromisoformat(e["from"]),
                         float(e["offset_days"]), float(e.get("ramp_days", 0))))
        ents.sort(key=lambda e: e[0])
        p._schedule = ents
    return p._schedule


def anchor_date(p=None) -> dt.date:
    """Start of the programme: `from` of the first offset_schedule entry."""
    return _schedule(_p(p))[0][0]


def _offset(date, ents):
    idx = -1
    for i, (frm, _, _) in enumerate(ents):
        if frm <= date:
            idx = i
    if idx < 0:
        return ents[0][1]
    frm, off, ramp = ents[idx]
    if idx == 0 or ramp <= 0:
        return off
    prev = _offset(frm - dt.timedelta(days=1), ents[:idx])
    frac = min(1.0, (date - frm).days / ramp)
    return prev + (off - prev) * frac


def offset_days(date: dt.date, p=None) -> float:
    return _offset(date, _schedule(_p(p)))


def cue_date(date: dt.date, p=None) -> dt.date:
    """Reference date whose sun is used for `date` (whole-day offset)."""
    return date - dt.timedelta(days=int(round(offset_days(date, p))))


# --- temperature ----------------------------------------------------------
def raw_c(date: dt.date, p=None) -> float:
    p = _p(p)
    return fit_c(date.timetuple().tm_yday - offset_days(date, p), p)


def clamp_c(c: float, p=None) -> float:
    lo, hi = safety_band_c(p)
    return min(hi, max(lo, c))


def target_c(date: dt.date, p=None) -> float:
    p = _p(p)
    anchor = anchor_date(p)
    if date < anchor:
        return clamp_c(raw_c(date, p), p)
    n = (date - anchor).days
    ser = p._series
    step = max_step_c(p)
    while len(ser) <= n:
        d = anchor + dt.timedelta(days=len(ser))
        want = clamp_c(raw_c(d, p), p)
        if not ser:
            ser.append(want)
        else:
            prev = ser[-1]
            ser.append(prev + max(-step, min(step, want - prev)))
    return ser[n]


def target_detail(date: dt.date, p=None) -> dict:
    """target/raw in C and F, the clamped flag and the whole-day cue date."""
    p = _p(p)
    t = target_c(date, p)
    r = raw_c(date, p)
    return {"target_c": t, "raw_c": r, "clamped": clamp_c(r, p) != r,
            "offset_days": offset_days(date, p), "cue_date": cue_date(date, p)}
