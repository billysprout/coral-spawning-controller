#!/usr/bin/env python3
"""dark_window.py -- per-date dark windows for the reef site (read-only).

Site: representative Okinawa coordinates from the build plan (section 07):
26.2 N, 127.7 E, UTC+9 (no DST). All cues follow this site, not the tank.

"Dark" means the sun is below -18 deg (astronomical twilight) AND the moon is
below its rising horizon. Each row is the night that begins on its date: the
window runs local noon -> next local noon, so a night is never split by
midnight. Sunset, dusk, moonrise and moonset are the first such event in that
window; a morning moonset/sunrise belongs to the night it ends. Output per dark block:

    pre-moonrise   : astronomical dusk -> moonrise
    post-moonset   : moonset -> astronomical dawn
    between        : moonset -> moonrise (moon down inside the night)
    moon-down      : astronomical dusk -> dawn with no moon above the horizon

Method: NOAA low-precision solar position; Meeus ch. 47 truncated lunar
position (main terms, roughly 0.1-0.2 deg). Event times are linearly
interpolated between 1-minute samples. Accuracy is minutes, not seconds. The
moon's apparent-horizon threshold uses the standard 0.7275*pi - 0.5667 deg.

Usage:
    python dark_window.py --start 2026-01-01 --days 365 --out data/dark_windows.csv
    python dark_window.py --start 2026-06-01 --days 1 --md
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import math
import os
import sys

LAT_DEG = 26.2
LON_DEG = 127.7
TZ_OFFSET_H = 9.0
Site = collections.namedtuple("Site", "lat lon tz_offset_h")
OKINAWA = Site(LAT_DEG, LON_DEG, TZ_OFFSET_H)   # default reference site

SUN_RISE_THR = -0.8333          # geometric sunrise/sunset
ASTRO_THR = -18.0               # astronomical twilight
MOON_EDGE_MARGIN_MIN = 2.0      # block boundary matched to a moon event
SAMPLE_MIN = 1.0

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root; this file lives in scripts/
DEFAULT_OUT = os.path.join(_ROOT, "data", "dark_windows.csv")


def _rad(d: float) -> float:
    return math.radians(d)


def jd_from_utc(t: dt.datetime) -> float:
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp() / 86400.0 + 2440587.5


def sun_position(jd: float):
    """Return (ra_rad, dec_rad, ecliptic_longitude_deg)."""
    T = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + 36000.76983 * T + 0.0003032 * T * T) % 360
    M = _rad((357.52911 + 35999.05029 * T - 0.0001537 * T * T) % 360)
    C = ((1.914602 - 0.004817 * T - 0.000014 * T * T) * math.sin(M)
         + (0.019993 - 0.000101 * T) * math.sin(2 * M)
         + 0.000289 * math.sin(3 * M))
    true_long = L0 + C
    omega = _rad(125.04 - 1934.136 * T)
    lam = _rad(true_long - 0.00569 - 0.00478 * math.sin(omega))
    eps0 = 23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60
    eps = _rad(eps0 + 0.00256 * math.cos(omega))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    return ra, dec, math.degrees(lam) % 360, eps


# Meeus ch. 47 main terms: (D, M, M', F) multipliers, sum_l (1e-6 deg), sum_b
# (1e-6 deg), sum_r (m), with E for terms containing M.
_LR = [
    (0, 0, 1, 0, 6288774, 0), (2, 0, -1, 0, 1274027, 0), (2, 0, 0, 0, 658314, 0),
    (0, 0, 2, 0, 213618, 0), (0, 1, 0, 0, -185116, 0), (0, 0, 0, 2, -114332, 0),
    (2, 0, -2, 0, 58793, 0), (2, -1, -1, 0, 57066, 0), (2, 0, 1, 0, 53322, 0),
    (2, -1, 0, 0, 45758, 0), (0, 1, -1, 0, -40923, 0), (1, 0, 0, 0, -34720, 0),
    (0, 1, 1, 0, -30383, 0), (2, 0, 0, -2, 15327, 0), (0, 0, 1, 2, 12529, 0),
    (0, 0, 1, -2, -10176, 0), (4, 0, -1, 0, 10, 0),
]
_B = [
    (0, 0, 0, 1, 5128122), (0, 0, 1, 1, 280602), (0, 0, 1, -1, 277693),
    (2, 0, 0, -1, 173237), (2, 0, -1, 1, 55413), (2, 0, -1, -1, 46271),
    (2, 0, 0, 1, 32573), (0, 0, 2, 1, 17198), (2, 0, 1, -1, 9266),
    (0, 0, 2, -1, 8822), (2, -1, 0, -1, 8216), (2, 0, -2, -1, 4324),
    (2, 0, 1, 1, 4200), (2, 1, 0, -1, -3359),
]
_R = [
    (0, 0, 1, 0, -20905355), (2, 0, -1, 0, -3699111), (2, 0, 0, 0, -2955968),
    (0, 0, 2, 0, -569925), (0, 1, 0, 0, 48888), (0, 0, 0, 2, -3149),
]


def moon_position(jd: float):
    """Return (ra_rad, dec_rad, ecliptic_longitude_deg, distance_km)."""
    T = (jd - 2451545.0) / 36525.0
    Lp = 218.3164477 + 481267.88123421 * T
    D = _rad((297.8501921 + 445267.1114034 * T) % 360)
    M = _rad((357.5291092 + 35999.0502909 * T) % 360)
    Mp = _rad((134.9633964 + 477198.8675055 * T) % 360)
    F = _rad((93.2720950 + 483202.0175233 * T) % 360)
    E = 1 - 0.002516 * T - 0.0000074 * T * T

    def e_factor(m_coeff: int) -> float:
        return E ** abs(m_coeff)

    sl = 0.0
    for d, m, mp, f, l, _ in _LR:
        arg = d * D + m * M + mp * Mp + f * F
        sl += e_factor(m) * l * math.sin(arg)
    sb = 0.0
    for d, m, mp, f, b in _B:
        arg = d * D + m * M + mp * Mp + f * F
        sb += e_factor(m) * b * math.sin(arg)
    sr = 0.0
    for d, m, mp, f, r in _R:
        arg = d * D + m * M + mp * Mp + f * F
        sr += e_factor(m) * r * math.cos(arg)

    lam_deg = (Lp + sl / 1e6) % 360
    beta = _rad(sb / 1e6)
    dist_km = 385000.56 + sr / 1000.0

    _, _, _, eps = sun_position(jd)
    lam = _rad(lam_deg)
    ra = math.atan2(math.sin(lam) * math.cos(eps) - math.tan(beta) * math.sin(eps),
                    math.cos(lam))
    dec = math.asin(math.sin(beta) * math.cos(eps)
                    + math.cos(beta) * math.sin(eps) * math.sin(lam))
    return ra, dec, lam_deg, dist_km


def equation_of_time_min(date: dt.date) -> float:
    """NOAA equation of time (minutes, + when the sun runs ahead of the clock)
    at 12:00 UTC on `date`."""
    doy = date.timetuple().tm_yday
    g = 2 * math.pi / 365.0 * (doy - 1)
    return 229.18 * (0.000075 + 0.001868 * math.cos(g)
                     - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g)
                     - 0.040849 * math.sin(2 * g))


def solar_noon_utc(date: dt.date, lon_deg: float) -> dt.datetime:
    """UTC instant of local solar noon at lon_deg (east positive) for the
    calendar date `date`: that date's 00:00 UTC + 720 - 4*lon - EoT minutes.
    Valid for any longitude whose noon falls on `date` in UTC (both sites here
    do)."""
    minutes = 720.0 - 4.0 * lon_deg - equation_of_time_min(date)
    base = dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc)
    return base + dt.timedelta(minutes=minutes)


def _altitude_deg(jd: float, ra: float, dec: float, site: Site = OKINAWA) -> float:
    T = (jd - 2451545.0) / 36525.0
    gmst = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
            + 0.000387933 * T * T - T ** 3 / 38710000.0) % 360
    H = _rad(gmst + site.lon - math.degrees(ra))
    phi = _rad(site.lat)
    sin_alt = (math.sin(phi) * math.sin(dec)
               + math.cos(phi) * math.cos(dec) * math.cos(H))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))


def sun_altitude(jd: float, site: Site = OKINAWA) -> float:
    ra, dec, _, _ = sun_position(jd)
    return _altitude_deg(jd, ra, dec, site)


def moon_altitude_and_h0(jd: float, site: Site = OKINAWA):
    ra, dec, _, dist = moon_position(jd)
    parallax = math.degrees(math.asin(6378.14 / dist))
    h0 = 0.7275 * parallax - 0.5667
    return _altitude_deg(jd, ra, dec, site), h0


def moon_phase(jd: float):
    """Return (illumination_fraction, age_days_since_new_moon)."""
    _, _, moon_lam, _ = moon_position(jd)
    _, _, sun_lam, _ = sun_position(jd)
    elong = (moon_lam - sun_lam) % 360
    illum = (1 - math.cos(_rad(elong))) / 2
    age = elong / 360.0 * 29.530588
    return illum, age


def _crossings(samples, thr_key):
    """samples: list of (t_utc, value - threshold). Return [(t, 'rise'|'set')]."""
    out = []
    for (t0, f0), (t1, f1) in zip(samples, samples[1:]):
        if f0 == 0 or (f0 < 0) == (f1 < 0):
            continue
        frac = f0 / (f0 - f1)
        t = t0 + (t1 - t0) * frac
        out.append((t, "rise" if f1 > 0 else "set"))
    return out


def day_events(local_noon_utc: dt.datetime, site: Site = OKINAWA,
               sun_events: bool = True, moon_events: bool = True):
    """Scan 24 h from `local_noon_utc` (1-min samples) at `site`.

    `local_noon_utc` is only the window start (any instant works; the CLI uses
    local clock noon). With sun_events/moon_events False the matching body is
    skipped (its event lists are empty and there are no dark blocks).
    """
    n = int(24 * 60 / SAMPLE_MIN) + 1
    sun, moon = [], []
    for i in range(n):
        t = local_noon_utc + dt.timedelta(minutes=i * SAMPLE_MIN)
        jd = jd_from_utc(t)
        if sun_events:
            sun.append((t, sun_altitude(jd, site)))
        if moon_events:
            ma, h0 = moon_altitude_and_h0(jd, site)
            moon.append((t, ma, h0))
    if not (sun_events and moon_events):
        def _x(series, thr, kind):
            return [t for t, k in _crossings([(t, v - thr) for t, v in series], "")
                    if k == kind]
        ms = [(t, m - h0) for t, m, h0 in moon]
        return {
            "sunrise": _x(sun, SUN_RISE_THR, "rise"),
            "sunset": _x(sun, SUN_RISE_THR, "set"),
            "dawn": _x(sun, ASTRO_THR, "rise"),
            "dusk": _x(sun, ASTRO_THR, "set"),
            "moonrise": [t for t, k in _crossings(ms, "") if k == "rise"],
            "moonset": [t for t, k in _crossings(ms, "") if k == "set"],
            "blocks": [],
        }

    sunrise = [t for t, k in _crossings([(t, s - SUN_RISE_THR) for t, s in sun], "")
               if k == "rise"]
    sunset = [t for t, k in _crossings([(t, s - SUN_RISE_THR) for t, s in sun], "")
              if k == "set"]
    dawn = [t for t, k in _crossings([(t, s - ASTRO_THR) for t, s in sun], "")
            if k == "rise"]
    dusk = [t for t, k in _crossings([(t, s - ASTRO_THR) for t, s in sun], "")
            if k == "set"]
    moon_series = [(t, m - h0) for t, m, h0 in moon]
    moonrise = [t for t, k in _crossings(moon_series, "") if k == "rise"]
    moonset = [t for t, k in _crossings(moon_series, "") if k == "set"]

    # Dark blocks: sun below -18 AND moon below its horizon threshold.
    dark = []
    for (t, s), (_, m, h0) in zip(sun, moon):
        dark.append(s < ASTRO_THR and m < h0)
    blocks = []
    start = None
    for i, is_dark in enumerate(dark):
        t = sun[i][0]
        if is_dark and start is None:
            start = t
        elif not is_dark and start is not None:
            blocks.append((start, t))
            start = None
    if start is not None:
        blocks.append((start, sun[-1][0]))

    return {
        "sunrise": sunrise, "sunset": sunset, "dawn": dawn, "dusk": dusk,
        "moonrise": moonrise, "moonset": moonset, "blocks": blocks,
    }


def _near(t, events, minutes=MOON_EDGE_MARGIN_MIN):
    for e in events:
        if abs((e - t).total_seconds()) <= minutes * 60:
            return e
    return None


def classify_block(start, end, ev):
    moonset_hit = _near(start, ev["moonset"])
    moonrise_hit = _near(end, ev["moonrise"])
    start_by = "moonset" if moonset_hit else "astro_dusk"
    end_by = "moonrise" if moonrise_hit else "astro_dawn"
    if start_by == "astro_dusk" and end_by == "moonrise":
        label = "pre-moonrise"
    elif start_by == "moonset" and end_by == "astro_dawn":
        label = "post-moonset"
    elif start_by == "moonset" and end_by == "moonrise":
        label = "between-moon"
    else:
        label = "moon-down"
    return label


def local_str(t, with_date=False, tz_offset_h: float = TZ_OFFSET_H):
    if t is None:
        return ""
    lt = t + dt.timedelta(hours=tz_offset_h)
    fmt = "%Y-%m-%d %H:%M" if with_date else "%H:%M"
    return lt.strftime(fmt)


def first(events):
    return events[0] if events else None


def build_row(date: dt.date, sst_target: float, season_gate: bool, max_blocks=3,
              site: Site = OKINAWA):
    # Local noon on `date` is 12:00 - tz_offset UTC.
    local_noon = dt.datetime(date.year, date.month, date.day, 12, 0,
                             tzinfo=dt.timezone.utc) - dt.timedelta(
                                 hours=site.tz_offset_h)
    ev = day_events(local_noon, site)
    local_str_ = lambda t: local_str(t, tz_offset_h=site.tz_offset_h)  # noqa: E731
    mid = local_noon + dt.timedelta(hours=12)
    illum, age = moon_phase(jd_from_utc(mid))

    row = {
        "date": date.isoformat(),
        "sunrise_local": local_str_(first(ev["sunrise"])),
        "sunset_local": local_str_(first(ev["sunset"])),
        "astro_dusk_local": local_str_(first(ev["dusk"])),
        "astro_dawn_local": local_str_(first(ev["dawn"])),
        "moonrise_local": local_str_(first(ev["moonrise"])),
        "moonset_local": local_str_(first(ev["moonset"])),
        "moon_illum_pct": round(illum * 100, 1),
        "moon_age_d": round(age, 1),
        "target_sst_c": round(sst_target, 1),
        "season_gate_ok": season_gate,
        "sst_in_band": _sst_band()[0] <= sst_target <= _sst_band()[1],
    }
    blocks = ev["blocks"][:max_blocks]
    for i in range(max_blocks):
        k = f"dark{i + 1}"
        if i < len(blocks):
            s, e = blocks[i]
            row[f"{k}_start"] = local_str_(s)
            row[f"{k}_end"] = local_str_(e)
            row[f"{k}_min"] = round((e - s).total_seconds() / 60)
            row[f"{k}_label"] = classify_block(s, e, ev)
        else:
            row[f"{k}_start"] = row[f"{k}_end"] = ""
            row[f"{k}_min"] = ""
            row[f"{k}_label"] = ""
    return row


def columns(max_blocks=3):
    base = ["date", "sunrise_local", "sunset_local", "astro_dusk_local",
            "astro_dawn_local", "moonrise_local", "moonset_local",
            "moon_illum_pct", "moon_age_d", "target_sst_c", "season_gate_ok",
            "sst_in_band"]
    for i in range(max_blocks):
        k = f"dark{i + 1}"
        base += [f"{k}_start", f"{k}_end", f"{k}_min", f"{k}_label"]
    return base


def _sst_band():
    import cues
    return cues.safety_band_c()


def sst_for(date: dt.date):
    """Seasonal SST target from cues.py (single source: cue_params.json)."""
    import cues
    sst = cues.target_c(date)
    return sst, sst >= cues.season_gate_c()


def build_table(start: dt.date, days: int):
    rows = []
    for k in range(days):
        d = start + dt.timedelta(days=k)
        sst, gate = sst_for(d)
        rows.append(build_row(d, sst, gate))
    return rows


def write_csv(rows, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns())
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_md(rows):
    cols = ["date", "sunset_local", "astro_dusk_local", "astro_dawn_local",
            "moonrise_local", "moonset_local", "moon_illum_pct",
            "dark1_start", "dark1_end", "dark1_min", "dark1_label",
            "dark2_start", "dark2_end", "dark2_min", "dark2_label",
            "target_sst_c"]
    out = ["| " + " | ".join(cols) + " |",
           "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", default=dt.date.today().isoformat(),
                    help="first local date, YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default=DEFAULT_OUT, help="CSV path")
    ap.add_argument("--md", action="store_true", help="print a markdown table")
    ns = ap.parse_args(argv)

    start = dt.date.fromisoformat(ns.start)
    rows = build_table(start, ns.days)
    write_csv(rows, ns.out)
    print(f"wrote {len(rows)} rows to {ns.out}", file=sys.stderr)
    if ns.md:
        print(write_md(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
