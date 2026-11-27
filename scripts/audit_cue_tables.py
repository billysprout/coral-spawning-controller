#!/usr/bin/env python3
"""audit_cue_tables.py -- audit the values in the locale cue tables (data/locales/<id>/cue_table_<year>.csv).

Method (see data/docs/CUE-TABLE-AUDIT.md for the reasoning):

  Tier A  invariants, every row          identities and continuity the generator must satisfy
  Tier B  independent recompute, every   sun and moon from the astral library (not dark_window.py),
          row                            sst_clim_c re-derived from the raw OISST file
  Tier C  external oracle, a sample      USNO one-day rise/set API, a stratified sample sized by a
                                         zero-defect plan; off unless --usno N is given

Tier A and B are cheap, so they are exhaustive: no sampling error. Their residuals (cue table minus
oracle, in minutes or C) are kept as numbers, not only pass/fail, so a systematic bias far inside the
tolerance still shows: per family and per locale the mean residual, its standard error and a z score
against the pooled mean. Tier C is the only costly check; generated tables fail in clusters (one locale,
one column, one regime), so its sample covers every locale first and then the edge regimes, and the
report gives the Clopper-Pearson 95% upper bound on the defect rate it supports.

    python scripts/audit_cue_tables.py                        # tiers A and B, all locales
    python scripts/audit_cue_tables.py --locales gbr,hawaii
    python scripts/audit_cue_tables.py --usno 59 --seed 1     # plus a 59-unit USNO sample (2 calls each,
                                                              # cached in data/external/usno_cache/)
    python scripts/audit_cue_tables.py --root /tmp/copy --out report.json

--root points at a repo copy (only data/locales/ and data/external/oisst_locales/ are read), so the
audit can be run against a deliberately damaged copy. Exit code 1 when any defect is found, 2 when there
is no defect but a locale's residuals are biased (|z| > 4), 0 when clean.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request

SYN = 29.530588
SUN_THR = {"sunrise": -0.833, "sunset": -0.833, "astro_dusk": -18.0, "astro_dawn": -18.0}  # degrees, geometric
SMOOTH_DAYS = 15
TOL = {  # family -> tolerance on |residual|
    "sun_min": 2.0, "astro_min": 3.0, "moon_min": 6.0, "photo_min": 2.0, "daf_d": 0.11,
    "age_step_d": 0.3, "illum_pp": 4.0, "sst_c": 0.011, "sst_step_c": 0.25, "astral_age_d": 1.0,
}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ helpers
def doy365(d):
    n = d.timetuple().tm_yday
    if d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0) and d.month > 2:
        n -= 1
    return n


def md_of(doy):
    d = dt.date(2027, 1, 1) + dt.timedelta(days=doy - 1)
    return d.strftime("%m-%d")


def hm(s):
    """'HH:MM' -> minutes, '' -> None."""
    if not s:
        return None
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def dmin(a, b):
    """Signed a - b in minutes on a 24 h clock, in [-720, 720)."""
    return (a - b + 720) % 1440 - 720


def fnum(s):
    return float(s) if s not in ("", None) else None


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def clopper_pearson_upper(k, n, conf=0.95):
    """One-sided upper bound on a binomial rate (bisection on the binomial tail)."""
    if n == 0:
        return 1.0
    if k >= n:
        return 1.0
    a = 1 - conf
    lo, hi = k / n, 1.0
    for _ in range(60):
        p = (lo + hi) / 2
        tail = sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))
        lo, hi = (p, hi) if tail > a else (lo, p)
    return hi


def zero_defect_n(p0, conf=0.95):
    """Sample size so that 0 defects in n bounds the rate below p0 at conf."""
    return math.ceil(math.log(1 - conf) / math.log(1 - p0))


class Ledger:
    """Defects plus numeric residuals per (family, locale)."""

    def __init__(self):
        self.defects, self.checked, self.res = [], {}, {}

    def check(self, fam, lid, ok, where, detail=""):
        self.checked[fam] = self.checked.get(fam, 0) + 1
        if not ok:
            self.defects.append({"family": fam, "locale": lid, "where": where, "detail": detail})

    def resid(self, fam, lid, value, where):
        tol = TOL[fam.split(":")[-1]] if fam.split(":")[-1] in TOL else None
        self.res.setdefault(fam, {}).setdefault(lid, []).append(value)
        if tol is not None:
            self.check(fam, lid, abs(value) <= tol, where, f"residual {value:+.3f} > {tol}")

    def bias(self):
        """Per family: pooled mean and sd; per locale: mean and a z score against the OTHER locales.

        The reference is robust so that one shifted locale does not move it and flag the innocent ones:
        the median of the other locales' means, and the median within-locale sd for the standard error.
        |z| > 4 is flagged, only with at least 10 residuals in the locale and 2 other locales (a tier C
        sample has a few units per locale, too few for a standard error)."""
        out = {}
        for fam, by in self.res.items():
            allv = [v for vs in by.values() for v in vs]
            if len(allv) < 3:
                continue
            mu, sd = statistics.fmean(allv), statistics.pstdev(allv)
            means = {lid: statistics.fmean(vs) for lid, vs in by.items()}
            sds = [statistics.pstdev(vs) for vs in by.values() if len(vs) > 1]
            sd_w = (statistics.median(sds) if sds else sd) or 1e-9
            loc = {}
            for lid, vs in by.items():
                others = [m for k, m in means.items() if k != lid]
                ref = statistics.median(others) if others else mu
                z = (means[lid] - ref) / (sd_w / math.sqrt(len(vs)))
                loc[lid] = {"n": len(vs), "mean": round(means[lid], 4), "z": round(z, 2),
                            "flag": len(others) >= 2 and len(vs) >= 10 and abs(z) > 4}
            p95 = sorted(abs(v) for v in allv)[int(0.95 * (len(allv) - 1))]
            out[fam] = {"n": len(allv), "mean": round(mu, 4), "sd": round(sd, 4), "abs_p95": round(p95, 4),
                        "max_abs": round(max(abs(v) for v in allv), 4),
                        "flagged_locales": sorted(k for k, v in loc.items() if v["flag"]), "by_locale": loc}
        return out


# ------------------------------------------------------------------ tier A
def tier_a(lid, rows, prof, L):
    prev = None
    season = lunar = None
    if prof.get("season_start") and prof.get("season_days"):
        s0 = int(prof["season_start"][:2]), int(prof["season_start"][3:])
        season = (doy365(dt.date(2027, *s0)) - 1, int(prof["season_days"]) - 1)
    if prof.get("daf_lo") and prof.get("daf_width"):
        lunar = (float(prof["daf_lo"]), float(prof["daf_width"]))
    for i, r in enumerate(rows):
        w = f"{lid}:{r['date']}"
        d = dt.date.fromisoformat(r["date"])
        L.check("A:date_seq", lid, prev is None or d == prev[0] + dt.timedelta(days=1), w)
        L.check("A:doy", lid, int(r["doy"]) == doy365(d) and r["month_day"] == md_of(int(r["doy"])), w)
        for c in ("sunrise", "sunset", "astro_dusk", "astro_dawn", "moonrise", "moonset"):
            v = r[c]
            ok = v == "" or (len(v) == 5 and v[2] == ":" and 0 <= hm(v) < 1440)
            L.check("A:time_format", lid, ok, w, f"{c}={v!r}")
        age, daf = fnum(r["moon_age_d"]), fnum(r["days_after_full"])
        L.resid("A:daf_d", lid, dmod(daf - (age - SYN / 2) % SYN, SYN), w)
        illum = fnum(r["moon_illum_pct"])
        L.resid("A:illum_pp", lid, illum - 50 * (1 - math.cos(2 * math.pi * age / SYN)), w)
        ss, dk = hm(r["sunset"]), hm(r["astro_dusk"])
        if ss is not None and dk is not None:
            L.check("A:dusk_after_sunset", lid, 45 <= dmin(dk, ss) <= 140, w, f"dusk-sunset {dmin(dk, ss)}")
        dn, du = hm(r["astro_dawn"]), hm(r["astro_dusk"])
        fd = fnum(r["full_dark_min"])
        if dn is not None and du is not None and fd is not None:
            span = (dn - du) % 1440
            L.check("A:full_dark_bound", lid, 0 <= fd <= span + 2, w, f"full_dark {fd} > astro night {span}")
        if r["sst_clim_c"]:
            L.check("A:sst_range", lid, 10 <= float(r["sst_clim_c"]) <= 36, w, r["sst_clim_c"])
        wa, ws, wm = r["watch"], r["watch_season"], r["watch_moon"]
        if wa != "":
            L.check("A:watch_and", lid, (wa == "True") == (ws == "True" and wm == "True"), w)
        if season and ws != "":
            x = int(r["doy"]) - 1
            exp = (x - season[0]) % 365 <= season[1]
            edge = min(abs(dmod(x - season[0], 365)), abs(dmod(season[0] + season[1] - x, 365))) <= 1
            L.check("A:watch_season", lid, (ws == "True") == exp or edge, w, f"doy {x + 1} expected {exp}")
        if lunar and wm != "":
            exp = (daf - lunar[0]) % SYN <= lunar[1] + 1e-9
            edge = min(abs(dmod(daf - lunar[0], SYN)), abs(dmod(lunar[0] + lunar[1] - daf, SYN))) <= 0.15
            L.check("A:watch_moon", lid, (wm == "True") == exp or edge, w, f"daf {daf} expected {exp}")
        if prev is not None:
            p = prev[1]
            pa = fnum(p["moon_age_d"])
            step = age - pa
            if step < -20:
                step += SYN
            L.resid("A:age_step_d", lid, step - 1.0, w)
            for c in ("sunrise", "sunset"):
                a, b = hm(r[c]), hm(p[c])
                if a is not None and b is not None:
                    L.check("A:sun_continuity", lid, abs(dmin(a, b)) <= 3, w, f"{c} jump {dmin(a, b)}")
            # photoperiod(d) = sunset(d) - sunrise on the morning of d, which is the previous row's sunrise
            # (the generator's rise/set columns come from a noon-to-noon window)
            sr, st, ph = hm(p["sunrise"]), hm(r["sunset"]), fnum(r["photoperiod_min"])
            if None not in (sr, st, ph):
                L.resid("A:photo_min", lid, ph - (st - sr) % 1440, w)
            if r["sst_clim_c"] and p["sst_clim_c"]:
                L.resid("A:sst_step_c", lid, float(r["sst_clim_c"]) - float(p["sst_clim_c"]), w)
        prev = (d, r)
    if rows and rows[0]["sst_clim_c"] and rows[-1]["sst_clim_c"]:
        L.resid("A:sst_step_c", lid, float(rows[0]["sst_clim_c"]) - float(rows[-1]["sst_clim_c"]), f"{lid}:wrap")
    sst = [float(r["sst_clim_c"]) for r in rows if r["sst_clim_c"]]
    if len(sst) == 365 and prof.get("sst_mean_c"):
        L.check("A:profile_sst", lid, abs(round(statistics.fmean(sst), 1) - float(prof["sst_mean_c"])) <= 0.051,
                f"{lid}:profile", f"mean {statistics.fmean(sst):.3f} vs {prof['sst_mean_c']}")
        lo = min(range(365), key=lambda i: sst[i])
        hi = max(range(365), key=lambda i: sst[i])
        # a flat trough or crest can move the extreme's date by days, so check the value on the profile's
        # date against the table's extreme instead of the dates themselves
        for name, idx, key in (("min", lo, "sst_min_md"), ("max", hi, "sst_max_md")):
            got = doy365(dt.date(2027, int(prof[key][:2]), int(prof[key][3:]))) - 1
            L.check("A:profile_sst", lid, abs(sst[got] - sst[idx]) <= 0.03, f"{lid}:profile",
                    f"sst_{name}_md {prof[key]} ({sst[got]}) vs {md_of(idx + 1)} ({sst[idx]})")


def dmod(x, p):
    return (x + p / 2) % p - p / 2


# ------------------------------------------------------------------ tier B
def tier_b(lid, rows, prof, root, L):
    from astral import Observer
    from astral import sun as asun
    from astral import moon as amoon
    lat, lon, tz = float(prof["ref_lat"]), float(prof["ref_lon"]), float(prof["ref_tz_h"])
    obs = Observer(latitude=lat, longitude=lon)
    tzi = dt.timezone(dt.timedelta(hours=tz))

    GAP = object()

    def safe(f, *a, **k):
        try:
            return f(*a, **k)
        except ValueError as e:
            # astral 3.2 rounds an event in the last half minute of a UTC day to hour 24 and raises:
            # that is a hole in the oracle, not a missing event
            return GAP if "must be in 0..23" in str(e) else None
        except TypeError:
            return None

    def mins(t):
        # the generator floors to the minute (strftime), so the oracle does too: a residual of 0 is agreement
        return None if t is None else t.astimezone(tzi).hour * 60 + t.astimezone(tzi).minute

    for r in rows:
        w = f"{lid}:{r['date']}"
        d = dt.date.fromisoformat(r["date"])
        n = d + dt.timedelta(days=1)
        # The generator takes the first event of each kind in the local noon-to-noon window starting on d.
        # astral searches by UTC date (its moon functions ignore tzinfo for that, and its sun functions fail
        # on an event at 23:59 UTC), so ask for every UTC date from d-1 to d+2 and keep events in the window.
        noon = dt.datetime(d.year, d.month, d.day, 12, tzinfo=tzi)
        end = noon + dt.timedelta(days=1)
        utc_days = [d + dt.timedelta(days=k) for k in (-1, 0, 1, 2)]
        kinds = (("sunset", "B:sun_min", lambda u: asun.sunset(obs, u)),
                 ("sunrise", "B:sun_min", lambda u: asun.sunrise(obs, u)),
                 ("astro_dusk", "B:astro_min", lambda u: asun.dusk(obs, u, depression=18)),
                 ("astro_dawn", "B:astro_min", lambda u: asun.dawn(obs, u, depression=18)),
                 ("moonrise", "B:moon_min", lambda u: amoon.moonrise(obs, u)),
                 ("moonset", "B:moon_min", lambda u: amoon.moonset(obs, u)))
        for c, fam, f in kinds:
            got_all = [safe(f, u) for u in utc_days]
            cands = sorted({t for t in got_all if t is not None and t is not GAP and noon <= t < end})
            got = hm(r[c])
            if not cands and GAP in got_all:
                L.checked["B:oracle_gap"] = L.checked.get("B:oracle_gap", 0) + 1
                continue
            if not cands and got is not None and c in SUN_THR:
                # astral's sun functions drop an event within seconds of 00:00 UTC (it belongs to neither
                # date). Check the table's time directly: the sun must cross the threshold within tol.
                t = noon + dt.timedelta(minutes=(got - 720) % 1440)
                e0, e1 = (asun.elevation(obs, t + dt.timedelta(minutes=k), with_refraction=False) - SUN_THR[c]
                          for k in (-tol, tol + 1))
                if e0 * e1 <= 0:
                    L.checked["B:oracle_gap"] = L.checked.get("B:oracle_gap", 0) + 1
                    continue
            v = mins(cands[0]) if cands else None
            tol = TOL[fam.split(":")[1]]
            if got is None or v is None:
                # an event within the tolerance of the window edge may fall on either side
                edge = (v is not None and abs(dmin(v, 720)) <= tol) or (got is not None and abs(dmin(got, 720)) <= tol)
                L.check(fam, lid, (got is None) == (v is None) or edge, w, f"{c} presence {r[c]!r} vs {v}")
            else:
                L.resid(fam, lid, dmin(got, v), w)
        # astral phase: 0..27.99 on a 28-day scale at 00:00 UTC of the date. The table's age is at the end of
        # the noon-to-noon window, local midnight starting d+1, which is (1 - tz/24) days after 00:00 UTC of d.
        # astral's phase is a mean-motion approximation, so this is a coarse check (tolerance 1 day).
        ph = amoon.phase(d) * SYN / 28.0
        L.resid("B:astral_age_d", lid, dmod(fnum(r["moon_age_d"]) - (ph + 1 - tz / 24.0), SYN), w)
    # sst_clim_c re-derived from the raw daily OISST file: day-of-year mean, 15-day circular running mean
    path = os.path.join(root, "data", "external", "oisst_locales", lid + ".csv")
    if os.path.exists(path) and rows and rows[0]["sst_clim_c"]:
        sums, cnt = [0.0] * 365, [0] * 365
        for x in read_csv(path):
            if x["sst_c"]:
                i = doy365(dt.date.fromisoformat(x["date"])) - 1
                sums[i] += float(x["sst_c"])
                cnt[i] += 1
        if all(cnt):
            raw = [sums[i] / cnt[i] for i in range(365)]
            h = SMOOTH_DAYS // 2
            sm = [statistics.fmean(raw[(i + k) % 365] for k in range(-h, h + 1)) for i in range(365)]
            for r in rows:
                L.resid("B:sst_c", lid, float(r["sst_clim_c"]) - sm[int(r["doy"]) - 1], f"{lid}:{r['date']}")


# ------------------------------------------------------------------ tier C
USNO = "https://aa.usno.navy.mil/api/rstt/oneday?date={d}&coords={lat:.4f},{lon:.4f}&tz={tz:g}"
# Rate limit: USNO posts none (checked 2026-11-26: https://aa.usno.navy.mil/data/api and
# https://aa.usno.navy.mil/data/api_changelog), so the default of at most 1 request per second applies.
# --pause defaults to 2 s and is never allowed below MIN_PAUSE.
MIN_PAUSE = 1.0
# After this many lookups in a row fail (each after its own backoff retries), USNO is treated as
# unreachable: tier C stops and says so, instead of retrying for hours.
STOP_AFTER_FAILS = 3


def strata_sample(tables, profiles, n, rng):
    """Stratified sample of (locale, date): one random date per locale first (a locale-wide fault is found
    with certainty), then the edge regimes (moon event within 30 min of local midnight or noon, the
    solstices), then simple random rows to reach n."""
    picks, seen = [], set()

    def add(lid, r):
        if (lid, r["date"]) not in seen and len(picks) < n:
            seen.add((lid, r["date"]))
            picks.append((lid, r))

    lids = sorted(tables)
    rng.shuffle(lids)
    for lid in lids:
        add(lid, rng.choice(tables[lid]))
    edge = []
    for lid, rows in tables.items():
        for r in rows:
            m = [hm(r[c]) for c in ("moonrise", "moonset") if r[c]]
            if any(min(abs(dmin(x, 0)), abs(dmin(x, 720))) <= 30 for x in m) or r["month_day"] in ("06-21", "12-21"):
                edge.append((lid, r))
    rng.shuffle(edge)
    for lid, r in edge[: max(0, (n - len(picks)) // 2)]:
        add(lid, r)
    allrows = [(lid, r) for lid, rows in tables.items() for r in rows]
    while len(picks) < min(n, len(allrows)):
        add(*rng.choice(allrows))
    return picks


def tier_c(tables, profiles, n, rng, L, pause=2.0, cache_dir=None, retries=5):
    """USNO has no multi-date endpoint (rstt/oneday is one date per call), so calls are spaced instead:
    `pause` seconds after every network call (at least MIN_PAUSE), exponential backoff (honouring
    Retry-After) on every failed request (any HTTP error, timeout, reset, DNS or TLS failure, bad JSON),
    and each response cached on disk so a rerun or a larger sample reuses earlier calls and an
    interrupted run resumes where it stopped. STOP_AFTER_FAILS failed lookups in a row stop tier C.
    A failed lookup is reported under failed_lookups, never as a table defect."""
    pause = max(pause, MIN_PAUSE)
    stats = {"calls": 0, "cache_hits": 0, "retries": 0, "failed_lookups": 0}

    def fetch(url):
        for k in range(retries + 1):
            wait = pause * 2 ** (k + 1)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "anemones-cue-audit/1.0"})
                with urllib.request.urlopen(req, timeout=30) as fh:
                    js = json.load(fh)
                stats["calls"] += 1
                time.sleep(pause)
                return js
            except urllib.error.HTTPError as e:
                if k == retries:
                    raise
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = max(wait, float(ra)) if ra else wait
                except ValueError:
                    pass
            except (OSError, ValueError):  # URLError, timeouts, resets, DNS/TLS, bad JSON
                if k == retries:
                    raise
            stats["retries"] += 1
            time.sleep(min(wait, 300))

    def usno(lid, d):
        p = profiles[lid]
        url = USNO.format(d=d.isoformat(), lat=float(p["ref_lat"]), lon=float(p["ref_lon"]), tz=float(p["ref_tz_h"]))
        path = os.path.join(cache_dir, f"{lid}_{d.isoformat()}.json") if cache_dir else None
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("url") == url:
                stats["cache_hits"] += 1
                js = cached["response"]
            else:
                js = None
        else:
            js = None
        if js is None:
            js = fetch(url)
            if path:
                os.makedirs(cache_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"url": url, "response": js}, fh)
        data = js["properties"]["data"]
        # sun and moon share the phen names (Rise, Set, ...), so key them by body
        ev = {}
        for k, body in (("sundata", "sun"), ("moondata", "moon")):
            for e in data.get(k, []):
                ev[f"{body}_{e['phen']}"] = hm(e["time"][:5])
        return ev

    picks = strata_sample(tables, profiles, n, rng)
    units = defects = fails_in_row = 0
    per_locale = {}
    for i, (lid, r) in enumerate(picks):
        if i and i % 25 == 0:
            print(f"  tier C: {i}/{len(picks)} units, {stats['calls']} calls, {stats['cache_hits']} cached, "
                  f"{stats['retries']} retries, {stats['failed_lookups']} failed", file=sys.stderr, flush=True)
        d = dt.date.fromisoformat(r["date"])
        try:
            a, b = usno(lid, d), usno(lid, d + dt.timedelta(days=1))
        except Exception as e:  # noqa: BLE001 - network failures are reported, not fatal
            stats["failed_lookups"] += 1
            fails_in_row += 1
            stats["last_error"] = f"{lid}:{r['date']}: {str(e)[:160]}"
            print(f"  tier C: lookup failed ({fails_in_row} in a row): {stats['last_error']}",
                  file=sys.stderr, flush=True)
            if fails_in_row >= STOP_AFTER_FAILS:
                stats["stopped"] = (f"USNO unreachable: {fails_in_row} failed lookups in a row after "
                                    f"{i + 1}/{len(picks)} units; tier C incomplete")
                print(f"  tier C STOPPED: {stats['stopped']}", file=sys.stderr, flush=True)
                break
            continue
        fails_in_row = 0
        units += 1
        per_locale[lid] = per_locale.get(lid, 0) + 1
        before = len(L.defects)
        w = f"{lid}:{r['date']}"
        # noon-to-noon window: sunset of d, sunrise of d+1; a moon event after noon on d or before noon on d+1
        for col, v in (("sunset", a.get("sun_Set")), ("sunrise", b.get("sun_Rise"))):
            if v is not None and r[col]:
                L.resid("C:sun_min", lid, dmin(hm(r[col]), v), w)
        for col, key in (("moonrise", "moon_Rise"), ("moonset", "moon_Set")):
            cand = [v for v in (a.get(key),) if v is not None and v >= 720] + \
                   [v for v in (b.get(key),) if v is not None and v < 720]
            if r[col] and cand:
                L.resid("C:moon_min", lid, dmin(hm(r[col]), cand[0]), w)
        if len(L.defects) > before:
            defects += 1
    return {"units": units, "defective_units": defects,
            "upper95_defect_rate": round(clopper_pearson_upper(defects, units), 4),
            "zero_defect_n_for_5pct": zero_defect_n(0.05), "zero_defect_n_for_1pct": zero_defect_n(0.01),
            "units_by_locale": dict(sorted(per_locale.items())), **stats}


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=REPO)
    ap.add_argument("--locales")
    ap.add_argument("--year", type=int, default=2027)
    ap.add_argument("--tiers", default="AB", help="any of A, B")
    ap.add_argument("--usno", type=int, default=0, help="tier C sample size (2 API calls each); 0 = off")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between USNO calls")
    ap.add_argument("--usno-cache", default=os.path.join(REPO, "data", "external", "usno_cache"),
                    help="folder for cached USNO responses ('' = no cache)")
    ap.add_argument("--out", help="write the JSON report here")
    ap.add_argument("--max-defects", type=int, default=40, help="defects listed in the report")
    a = ap.parse_args(argv)
    loc = os.path.join(a.root, "data", "locales")
    profiles = {r["locale_id"]: r for r in read_csv(os.path.join(loc, "locale_profiles.csv"))}
    tables = {}
    for path in sorted(glob.glob(os.path.join(loc, "*", f"cue_table_{a.year}.csv"))):
        lid = os.path.basename(os.path.dirname(path))
        if a.locales and lid not in a.locales.split(","):
            continue
        if lid in profiles:
            tables[lid] = read_csv(path)
    L = Ledger()
    t0 = time.time()
    for lid, rows in tables.items():
        if "A" in a.tiers:
            tier_a(lid, rows, profiles[lid], L)
        if "B" in a.tiers:
            tier_b(lid, rows, profiles[lid], a.root, L)
    rep = {"locales": len(tables), "rows": sum(len(v) for v in tables.values()), "tiers": a.tiers,
           "seconds_ab": round(time.time() - t0, 1)}
    if a.usno:
        rep["tier_c"] = tier_c(tables, profiles, a.usno, random.Random(a.seed), L, pause=a.pause,
                               cache_dir=a.usno_cache or None)
    by = {}
    for d in L.defects:
        by[d["family"]] = by.get(d["family"], 0) + 1
    rep["checked"] = L.checked
    rep["defects_by_family"] = by
    rep["defects"] = L.defects[: a.max_defects]
    rep["n_defects"] = len(L.defects)
    rep["bias"] = L.bias()
    js = json.dumps(rep, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(js)
    # console summary
    print(f"{rep['locales']} locales, {rep['rows']} rows, tiers {a.tiers}{'+C' if a.usno else ''}, "
          f"{rep['seconds_ab']} s; {rep['n_defects']} defects")
    for fam in sorted(L.checked):
        b = rep["bias"].get(fam)
        extra = (f"  mean {b['mean']:+.3f} sd {b['sd']:.3f} p95|r| {b['abs_p95']:.3f}"
                 + (f"  biased: {','.join(b['flagged_locales'])}" if b["flagged_locales"] else "")) if b else ""
        print(f"  {fam:<22} {L.checked[fam]:>6} checked {by.get(fam, 0):>5} bad{extra}")
    stopped = a.usno and rep["tier_c"].get("stopped")
    if a.usno:
        print("  tier C:", rep["tier_c"])
        if stopped:
            print(f"  tier C STOPPED: {stopped}")
    flagged = sorted({f"{fam}:{lid}" for fam, b in rep["bias"].items() for lid in b["flagged_locales"]})
    if flagged:
        print("  bias flags:", ", ".join(flagged))
    return 1 if L.defects else 2 if flagged else 3 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
