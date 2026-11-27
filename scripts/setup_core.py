#!/usr/bin/env python3
"""setup_core.py -- the onboarding flow behind setup_web.py.

Three steps, each resumable, each recorded in <data>/setup.json:

  1. CALENDAR  pick a locale (Okinawa, or one of the data/locales cue tables),
               a temperature curve for it, the tank's own site, and a start
               point (lead time to the first watch season, a cue date, or a raw
               offset). Preview the generated calendar, then confirm. Confirming
               writes <data>/cue_params.json, <data>/spawn_calendar_<start>_<locale>.csv
               (+ .meta.json), pins it in setup.json and records the arcs.
  2. DEVICES   Tuya local keys (cloud fetch, like the tinytuya wizard), a LAN
               scan, plug roles, a read-only health check, and each plug's
               power-on state (read; written only after a per-plug yes). The
               Inkbird VM: read-only checks of its feed plus a hand checklist.
  3. DECISIONS every fail-safe and setting the owner chose, shown with its
               current value and why it is there; each must be confirmed (or
               changed) by a person. Nothing is assumed confirmed.

Nothing in this module runs on its own: setup_web.py calls it. Writes are
atomic and every file it replaces is backed up beside itself first.

STAGING. With SETUP_STAGING=1 (the compose `setup` service) any write to a
real plug is refused; only fake drivers can be written. Tuya cloud reads and
the passive LAN scan are allowed (they send nothing to a plug).

Standard library, plus tinytuya for the cloud fetch, scan and plug writes.
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import glob
import hashlib
import json
import math
import os
import shutil
import subprocess
import time

import cues
import spawn_calendar
import tank_arcs

UTC = dt.timezone.utc
_HERE = os.path.dirname(os.path.abspath(__file__))
BASE_PARAMS = os.path.join(_HERE, "cue_params.json")      # Okinawa, shipped


def base_params_path(data: str) -> str:
    """The Okinawa reference parameters: shipped beside this module, else
    installed from the cue-table repo into <data>/reference/ by cue_import.py."""
    if os.path.exists(BASE_PARAMS):
        return BASE_PARAMS
    p = os.path.join(data, "reference", "cue_params.json")
    if not os.path.exists(p):
        raise SetupError("no cue tables installed: run `python scripts/cue_import.py fetch` "
                         "(or sh scripts/start.sh), then reload this page")
    return p
DEFAULT_DAYS = 730
MAX_DAYS = 1100
LIGHT_ROLES = ("white", "white_plus", "blue", "moon")
ROLES = LIGHT_ROLES + ("heater", "unassigned")


class SetupError(ValueError):
    """Something the person has to fix; the message says what."""


def staging() -> bool:
    return os.environ.get("SETUP_STAGING", "") == "1"


def now_utc() -> str:
    return dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# FILES
# ---------------------------------------------------------------------------
def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, obj, sort_keys=False) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=2, sort_keys=sort_keys)
        fh.write("\n")
    os.replace(tmp, path)


def backup(path: str) -> str | None:
    """Copy path to path.bak-setup-<UTC stamp> (once per second at most)."""
    if not os.path.exists(path):
        return None
    dst = f"{path}.bak-setup-{dt.datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    if not os.path.exists(dst):
        shutil.copy2(path, dst)
    return dst


def state_path(data: str) -> str:
    return os.path.join(data, "setup.json")


def load_state(data: str) -> dict:
    st = _read_json(state_path(data), {}) or {}
    st.setdefault("version", 1)
    st.setdefault("decisions", {})
    st.setdefault("itc", {"checklist": {}})
    st.setdefault("devices", {})
    return st


def save_state(data: str, st: dict) -> None:
    st["updated_utc"] = now_utc()
    _write_json(state_path(data), st)


def _sha(obj_bytes: bytes) -> str:
    return hashlib.sha256(obj_bytes).hexdigest()


# ---------------------------------------------------------------------------
# STEP 1: LOCALES AND TEMPERATURE CURVES
# ---------------------------------------------------------------------------
def _locales_dir(data: str) -> str:
    return os.path.join(data, "locales")


def _float(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _table_rows(data: str, rel: str) -> list[dict]:
    """A locale cue table, from the data dir (rel is repo-relative 'data/...')."""
    path = os.path.join(data, rel[5:] if rel.startswith("data/") else rel)
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _ladder_rel(table_rel: str) -> str:
    """The locale's temperature ladder (temp_logger_pair.py) beside its cue table."""
    return table_rel.rsplit("/", 1)[0] + "/temp_ladder_2027.csv"


def _ladder_rows(data: str, table_rel: str) -> list[dict]:
    """Ladder rows, or [] when the locale has no logger that passed pairing."""
    try:
        return _table_rows(data, _ladder_rel(table_rel))
    except OSError:
        return []


def _column_series(rows, col) -> dict[int, float]:
    out = {}
    for r in rows:
        v = _float(r.get(col))
        doy = _float(r.get("doy"))
        if v is not None and doy is not None:
            out[int(doy)] = v
    return out


def list_locales(data: str) -> list[dict]:
    """Every locale the flow can offer, with its readiness and the temperature
    curves it can play. 'ready' means the sky table, the window and at least one
    temperature curve are present; 'half-ready' carries the reasons."""
    base = cues.load_params(base_params_path(data))
    oki_w = base["watch"]
    out = [{
        "id": "okinawa",
        "name": "Ryukyu Islands (Okinawa), JAZA S. gigantea",
        "ref": dict(base["reference_site"]),
        "grade": "anemone",
        "focus": "S. gigantea (JAZA tank, 4 spawns 2020-2022)",
        "season_start": oki_w["season_start"], "season_end": oki_w["season_end"],
        "daf_lo": oki_w["days_after_full_min"], "daf_hi": oki_w["days_after_full_max"],
        "records": 4, "species_basis": "gigantea",
        "temps": [{"id": "jaza", "label": "JAZA tank curve (Figure 4 fit)",
                   "level": "0 (tank record)", "ready": True}],
        "genera": [],
        "readiness": "ready",
        "caveats": ["Only four spawn events; dates +/- 3 days."],
        "table": None,
    }]
    prof = os.path.join(_locales_dir(data), "locale_profiles.csv")
    if not os.path.exists(prof):
        return out
    genera = {}
    gw = os.path.join(_locales_dir(data), "locale_genus_windows.csv")
    if os.path.exists(gw):
        with open(gw, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                genera.setdefault(r["locale_id"], []).append({
                    "genus": r["genus"], "grade": r["grade"], "focus": r["focus"],
                    "n_nights": int(float(r["n_nights"] or 0)),
                    "season_start": r["season_start"], "season_end": r["season_end"],
                    "daf_lo": _float(r["daf_lo"]), "daf_hi": _float(r["daf_hi"])})
    species = load_species(data)
    out[0]["species"] = [JAZA_SPECIES(base)] + species.get("okinawa", [])
    with open(prof, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["locale_id"] == "okinawa" or not r.get("table"):
                continue
            caveats, temps = [], []
            try:
                rows = _table_rows(data, r["table"])
            except OSError:
                rows = []
                caveats.append("cue table file missing")
            reef = _column_series(rows, "temp_clim_c")
            sst = _column_series(rows, "sst_clim_c")
            logger = _column_series(_ladder_rows(data, r["table"]), "temp_l1_c")
            if len(reef) >= 300:
                temps.append({"id": "locale_reef", "label": "reef water, in situ climatology",
                              "level": "0 (in situ)", "ready": True})
            if len(logger) >= 300:
                temps.append({"id": "locale_logger",
                              "label": "reef logger, in situ (passed the satellite pairing check)",
                              "level": "1 (logger)", "ready": True})
            if len(sst) >= 300:
                temps.append({"id": "locale_sst", "label": "satellite SST (OISST, shape and timing)",
                              "level": "4 (satellite)", "ready": True})
            temps.append({"id": "jaza_aligned",
                          "label": "JAZA gigantea curve, moved so its spawn month meets this locale's season",
                          "level": "0, shifted", "ready": True})
            readiness = "ready"
            if r["grade"] in ("thin", "usable"):
                readiness = "half-ready"
                caveats.append(f"{r['grade']} evidence ({r['n_nights']} nights)")
            if r["focus"] == "broad":
                good = [x for x in species.get(r["locale_id"], [])
                        if x["n_nights"] >= 10 and x["focus"] == "focused"]
                if good and r["grade"] == "rich":
                    readiness = "ready-by-species"
                    caveats.append("broad pooled window: aim at a species (" + ", ".join(
                        x["species"] for x in good[:3]) + (" ..." if len(good) > 3 else "") + ")")
                else:
                    readiness = "half-ready"
                    caveats.append("broad window: mixes species; pick a species or genus row if one fits")
            if not reef and not logger:
                caveats.append("no in situ temperature: satellite or shifted JAZA only")
            if not rows:
                readiness = "not-ready"
            caveats.append("window is from coral and other records, not S. gigantea")
            out.append({
                "id": r["locale_id"], "name": r["name"],
                "ref": {"lat": float(r["ref_lat"]), "lon": float(r["ref_lon"]),
                        "tz_offset_h": float(r["ref_tz_h"]), "name": r["name"]},
                "grade": r["grade"], "focus": r["focus"],
                "season_start": r["season_start"], "season_end": r["season_end"],
                "daf_lo": _float(r["daf_lo"]), "daf_hi": _float(r["daf_hi"]),
                "records": int(float(r["records"] or 0)), "species_basis": "coral/other",
                "temps": temps, "genera": genera.get(r["locale_id"], []),
                "species": species.get(r["locale_id"], []),
                "readiness": readiness, "caveats": caveats, "table": r["table"],
            })
    for loc in out:
        loc["max_lead_days"] = max_lead(loc["season_start"], loc["season_end"])
        for g in loc["genera"]:
            g["max_lead_days"] = max_lead(g["season_start"], g["season_end"])
    order = {"ready": 0, "ready-by-species": 1, "half-ready": 2, "not-ready": 3}
    return out[:1] + sorted(out[1:], key=lambda l: (order[l["readiness"]], -l["records"]))


def JAZA_SPECIES(base) -> dict:
    """The owner's own target: S. gigantea from the JAZA tank record, with the
    watch window cue_params.json already carries (spawns Jun 2 - Jul 2 +/- 3 d,
    6 to 9 days after full)."""
    w = base["watch"]
    return {"species": "Stichodactyla gigantea (JAZA)", "genus": "Stichodactyla",
            "records": 4, "n_nights": 4, "years": "2020-2022", "grade": "anemone",
            "focus": "focused", "season_start": w["season_start"], "season_end": w["season_end"],
            "daf_lo": w["days_after_full_min"], "daf_hi": w["days_after_full_max"],
            "padded": True, "note": w.get("season_source", "")}


def load_species(data: str) -> dict:
    """{locale_id: [species window rows]} from locale_species_windows.csv,
    most nights first."""
    path = os.path.join(_locales_dir(data), "locale_species_windows.csv")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.setdefault(r["locale_id"], []).append({
                "species": r["species"], "genus": r["genus"],
                "records": int(r["records"] or 0), "n_nights": int(r["n_nights"] or 0),
                "years": r["years"], "grade": r["grade"], "focus": r["focus"],
                "season_start": r["season_start"], "season_end": r["season_end"],
                "season_peak": r["season_peak"],
                "daf_lo": _float(r["daf_lo"]), "daf_hi": _float(r["daf_hi"]),
                "min_p50": r["min_p50"],
                "season_days": int(float(r["season_days"] or 0)),
                "wide": float(r["season_days"] or 0) > WIDE_SEASON_DAYS})
    return out


WIDE_SEASON_DAYS = 30   # a species season longer than this on record is flagged on the page


def pad_window(w: dict, season_days: float, moon_days: float) -> dict:
    """Widen a window by season_days each side (month-day, wraps the year) and
    moon_days each side (days after full, wraps the cycle). JAZA's own watch
    window is its spawn dates +/- 3 days."""
    out = dict(w)
    if season_days:
        y = 2027
        a = dt.date(y, *map(int, w["season_start"].split("-"))) - dt.timedelta(days=season_days)
        b = dt.date(y, *map(int, w["season_end"].split("-"))) + dt.timedelta(days=season_days)
        if season_days * 2 + season_days_of(w) >= 365:
            raise SetupError("padding would cover the whole year")
        out["season_start"], out["season_end"] = a.strftime("%m-%d"), b.strftime("%m-%d")
    if moon_days:
        syn = 29.530588
        width = (float(w["daf_hi"]) - float(w["daf_lo"])) % syn
        if width + 2 * moon_days >= syn:
            raise SetupError("moon padding would cover the whole cycle")
        out["daf_lo"] = round((float(w["daf_lo"]) - moon_days) % syn, 1)
        out["daf_hi"] = round((float(w["daf_hi"]) + moon_days) % syn, 1)
    return out


def season_days_of(w) -> int:
    return season_days(w["season_start"], w["season_end"])


def _solve(a, b):
    """Gaussian elimination with partial pivoting (small dense systems)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            raise SetupError("temperature fit is singular")
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_harmonics(series: dict[int, float], k_max: int = 2, period=365.25) -> dict:
    """Least-squares mean + k_max harmonics, in cue_params sst_fit form."""
    if len(series) < 300:
        raise SetupError("need at least 300 days of temperature to fit a curve")
    cols = []
    for doy, v in sorted(series.items()):
        w = 2 * math.pi * doy / period
        x = [1.0]
        for k in range(1, k_max + 1):
            x += [math.cos(k * w), math.sin(k * w)]
        cols.append((x, v))
    n = 1 + 2 * k_max
    ata = [[sum(x[i] * x[j] for x, _ in cols) for j in range(n)] for i in range(n)]
    atb = [sum(x[i] * v for x, v in cols) for i in range(n)]
    coef = _solve(ata, atb)
    harm = [{"k": k, "cos": round(coef[2 * k - 1], 6), "sin": round(coef[2 * k], 6)}
            for k in range(1, k_max + 1)]
    fit = {"mean": round(coef[0], 6), "harmonics": harm, "period_days": period, "unit": "C"}
    res = [v - _eval_fit(fit, doy) for doy, v in series.items()]
    fit["rmse_c"] = round(math.sqrt(sum(r * r for r in res) / len(res)), 4)
    return fit


def _eval_fit(fit, doy):
    w = 2 * math.pi * doy / float(fit["period_days"])
    return float(fit["mean"]) + sum(h["cos"] * math.cos(h["k"] * w) + h["sin"] * math.sin(h["k"] * w)
                                    for h in fit["harmonics"])


def shift_fit(fit: dict, days: float) -> dict:
    """The same curve, later by `days` (new(doy) = old(doy - days))."""
    out = copy.deepcopy(fit)
    for h in out["harmonics"]:
        phi = 2 * math.pi * h["k"] * days / float(fit["period_days"])
        a, b = h["cos"], h["sin"]
        h["cos"] = round(a * math.cos(phi) - b * math.sin(phi), 6)
        h["sin"] = round(a * math.sin(phi) + b * math.cos(phi), 6)
    return out


def _md_doy(md: str) -> int:
    m, d = (int(x) for x in md.split("-"))
    return dt.date(2027, m, d).timetuple().tm_yday


def season_days(start_md, end_md) -> int:
    a, b = _md_doy(start_md), _md_doy(end_md)
    return (b - a) % 365 + 1


def max_lead(start_md, end_md) -> int:
    """The longest lead that starts the tank outside the watch season, so the
    first watch season comes after nearly a full year of cues."""
    return max(0, 365 - season_days(start_md, end_md))


def _season_mid_doy(start_md, end_md) -> float:
    a, b = _md_doy(start_md), _md_doy(end_md)
    if b < a:
        b += 365
    return ((a + b) / 2.0) % 365


def locale_by_id(data, locale_id) -> dict:
    for loc in list_locales(data):
        if loc["id"] == locale_id:
            return loc
    raise SetupError(f"no locale {locale_id!r}")


# ---------------------------------------------------------------------------
# STEP 1: DRAFT PARAMS AND THE START POINT
# ---------------------------------------------------------------------------
def _closest(md: str, near: dt.date) -> dt.date:
    m, d = (int(x) for x in md.split("-"))
    best = None
    for y in (near.year - 1, near.year, near.year + 1):
        try:
            c = dt.date(y, m, d)
        except ValueError:          # 02-29
            c = dt.date(y, m, 28)
        if best is None or abs((c - near).days) < abs((best - near).days):
            best = c
    return best


def start_offset(mode: str, start: dt.date, value, season_start: str) -> dict:
    """offset_days for a start point. Modes:
      lead      value = days from the start date to the first night of the
                watch season (the cue date on start+lead is season_start)
      cue_date  value = the cue date (YYYY-MM-DD or MM-DD) played on the start date
      offset    value = offset_days itself (cue date = tank date - offset)
    The cue date's year is the one nearest the tank date: only month-day
    matters to the sun and the temperature curve, and the moon is never shifted.
    """
    if mode == "lead":
        lead = int(value)
        if not 0 <= lead <= 365:
            # the cue year repeats: a lead of 365 + n is the same calendar as n
            raise SetupError("lead must be 0..365 days (the cue year repeats)")
        d = start + dt.timedelta(days=lead)
        off = (d - _closest(season_start, d)).days
    elif mode == "cue_date":
        text = str(value).strip()
        md = text[5:] if len(text) == 10 else text
        off = (start - _closest(md, start)).days
    elif mode == "offset":
        off = int(round(float(value)))
    else:
        raise SetupError("start mode must be lead, cue_date or offset")
    if abs(off) > tank_arcs.MAX_OFFSET_DAYS:
        raise SetupError(f"offset {off} is outside +/-{tank_arcs.MAX_OFFSET_DAYS} days")
    return {"offset_days": off, "cue_date_at_start": (start - dt.timedelta(days=off)).isoformat()}


def build_params(data: str, choice: dict) -> tuple[dict, dict]:
    """(cue_params dict, basis) for a choice:
      locale, temp, genus (optional), start, start_mode, start_value,
      tank_site {lat, lon, tz} (optional; default the shipped one), days."""
    base = copy.deepcopy(dict(cues.load_params(base_params_path(data))))
    loc = locale_by_id(data, choice.get("locale") or "okinawa")
    temp = choice.get("temp") or loc["temps"][0]["id"]
    if temp not in {t["id"] for t in loc["temps"]}:
        raise SetupError(f"{loc['id']} has no temperature curve {temp!r}")
    try:
        start = dt.date.fromisoformat(choice.get("start") or dt.date.today().isoformat())
    except ValueError:
        raise SetupError("start must be YYYY-MM-DD") from None
    window = {"season_start": loc["season_start"], "season_end": loc["season_end"],
              "daf_lo": loc["daf_lo"], "daf_hi": loc["daf_hi"], "from": "locale pooled"}
    g = choice.get("genus")
    if g:
        row = next((x for x in loc["genera"] if x["genus"] == g), None)
        if row is None:
            raise SetupError(f"{loc['id']} has no genus row {g!r}")
        window = {**{k: row[k] for k in ("season_start", "season_end", "daf_lo", "daf_hi")},
                  "from": f"genus {g}"}
    sp = choice.get("species")
    if sp:
        row = next((x for x in loc.get("species", []) if x["species"] == sp), None)
        if row is None:
            raise SetupError(f"{loc['id']} has no species row {sp!r}")
        window = {**{k: row[k] for k in ("season_start", "season_end", "daf_lo", "daf_hi")},
                  "from": f"species {sp} ({row['n_nights']} nights, {row['grade']})"}
    pad_s = float(choice.get("pad_season_days") or 0)
    pad_m = float(choice.get("pad_moon_days") or 0)
    if not (0 <= pad_s <= 60 and 0 <= pad_m <= 7):
        raise SetupError("padding must be 0..60 days (season) and 0..7 days (moon)")
    if window["daf_lo"] is None or window["daf_hi"] is None:
        raise SetupError("this window has no moon range")
    if pad_s or pad_m:
        window = {**pad_window(window, int(pad_s), pad_m),
                  "from": window["from"] + f", padded +/-{pad_s:g} d season, +/-{pad_m:g} d moon"}
    p = base
    p["reference_site"] = {k: loc["ref"][k] for k in ("lat", "lon", "tz_offset_h", "name")}
    if loc["id"] != "okinawa" or g or (sp and not sp.endswith("(JAZA)")) or pad_s or pad_m:
        p["watch"] = {
            **p["watch"],
            "season_start": window["season_start"], "season_end": window["season_end"],
            "days_after_full_min": round(float(window["daf_lo"]), 1),
            "days_after_full_max": round(float(window["daf_hi"]), 1),
            "season_source": f"{loc['name']}: {window['from']} window from data/locales "
                             f"({loc['grade']}, {loc['records']} records)",
        }
    if temp == "jaza":
        pass                                     # base sst_fit is the JAZA fit
    elif temp == "jaza_aligned":
        shift = (_season_mid_doy(window["season_start"], window["season_end"])
                 - _season_mid_doy(base["watch"]["season_start"], base["watch"]["season_end"]))
        p["sst_fit"] = {**shift_fit(base["sst_fit"], shift),
                        "source": f"JAZA Figure 4 fit shifted {shift:+.0f} d to the "
                                  f"{loc['id']} season"}
    elif temp == "locale_logger":
        fit = fit_harmonics(_column_series(_ladder_rows(data, loc["table"]), "temp_l1_c"))
        fit["source"] = f"{_ladder_rel(loc['table'])} temp_l1_c (2 harmonics)"
        p["sst_fit"] = fit
    else:
        col = "temp_clim_c" if temp == "locale_reef" else "sst_clim_c"
        fit = fit_harmonics(_column_series(_table_rows(data, loc["table"]), col))
        fit["source"] = f"{loc['table']} {col} (2 harmonics)"
        p["sst_fit"] = fit
    ts = choice.get("tank_site") or {}
    if ts:
        try:
            p["tank_site"] = {"lat": float(ts["lat"]), "lon": float(ts["lon"]),
                              "tz": str(ts["tz"]), "name": str(ts.get("name") or "tank")}
        except (KeyError, TypeError, ValueError):
            raise SetupError("tank site needs lat, lon and tz") from None
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(p["tank_site"]["tz"])
        except Exception:  # noqa: BLE001
            raise SetupError(f"unknown time zone {p['tank_site']['tz']!r}") from None
    band = choice.get("safety_band_c")
    if band:
        try:
            lo, hi = float(band[0]), float(band[1])
        except (TypeError, ValueError, IndexError):
            raise SetupError("safety band needs two numbers") from None
        if not 20.0 <= lo < hi <= 31.0:
            raise SetupError("safety band must be inside 20..31 C with low below high")
        p["safety_band_c"] = [lo, hi]
    step = choice.get("max_step_c_per_day")
    if step not in (None, ""):
        step = float(step)
        if not 0.01 <= step <= 0.5:
            raise SetupError("max change per day must be 0.01..0.5 C")
        p["max_step_c_per_day"] = step
    so = start_offset(choice.get("start_mode") or "lead", start,
                      choice.get("start_value", 365), p["watch"]["season_start"])
    p["offset_schedule"] = [{"from": start.isoformat(), "offset_days": so["offset_days"]}]
    days = int(choice.get("days") or DEFAULT_DAYS)
    if not 30 <= days <= MAX_DAYS:
        raise SetupError(f"days must be 30..{MAX_DAYS}")
    basis = {"locale": loc["id"], "locale_name": loc["name"], "temp": temp,
             "genus": g or None, "species": sp or None, "window": window, "start": start.isoformat(),
             "start_mode": choice.get("start_mode") or "lead",
             "start_value": choice.get("start_value", 365), "days": days,
             "readiness": loc["readiness"], "caveats": loc["caveats"],
             "max_lead_days": max_lead(window["season_start"], window["season_end"]), **so}
    return p, basis


def current_limits() -> dict:
    """The active params' tank site, band and step: the form's defaults."""
    p = cues.load_params(cues.active_params_path())
    return {**dict(p["tank_site"]), "safety_band_c": list(p["safety_band_c"]),
            "max_step_c_per_day": p["max_step_c_per_day"],
            "noon": solar_noon_check(p["tank_site"], dt.date.today())}


def _draft_params_file(data: str, params: dict) -> str:
    """Write params to a content-named draft file (cues caches by path)."""
    raw = (json.dumps(params, indent=2, sort_keys=True) + "\n").encode()
    folder = os.path.join(data, "setup", "drafts")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"cue_params-{_sha(raw)[:12]}.json")
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(raw)
    return path


NOON_WARN_H = 1.5     # tank solar noon this far from 12:00 local: warn in the preview
NOON_REFUSE_H = 3.0   # this far: refuse to apply (the longitude cannot be right)


def solar_noon_check(site: dict, day: dt.date) -> dict:
    """Where the tank's solar noon falls on its own clock. Lamp and moon times are
    placed on the tank's solar noon, which only the longitude sets; the time zone
    only turns them into clock times. A longitude that does not belong to the time
    zone (the Okinawa reference default with a US zone, say) shifts the whole day."""
    from zoneinfo import ZoneInfo
    import dark_window
    t = dark_window.solar_noon_utc(day, float(site["lon"])).astimezone(ZoneInfo(site["tz"]))
    std = ZoneInfo(site["tz"]).utcoffset(dt.datetime(day.year, 1, 15)) or dt.timedelta(0)
    std2 = ZoneInfo(site["tz"]).utcoffset(dt.datetime(day.year, 7, 15)) or dt.timedelta(0)
    off_h = min(std, std2).total_seconds() / 3600          # standard (non-summer) offset
    hours = t.hour + t.minute / 60
    return {"noon_local": t.strftime("%H:%M"), "off_h": round(hours - 12.0, 2),
            "suggested_lon": round(off_h * 15.0, 1)}


def _noon_message(site: dict, chk: dict) -> str:
    return (f"with longitude {float(site['lon']):g} the tank's midday falls at {chk['noon_local']} on "
            f"{site['tz']} clocks, so lights would run {abs(chk['off_h']):.1f} h "
            f"{'late' if chk['off_h'] > 0 else 'early'}; a longitude near {chk['suggested_lon']:g} "
            "fits this time zone (set your tank's own, east positive)")


def preview(data: str, choice: dict) -> dict:
    """Generate the calendar for a choice (nothing outside setup/drafts is
    written) and summarise it for a person to check."""
    params, basis = build_params(data, choice)
    ppath = _draft_params_file(data, params)
    p = cues.load_params(ppath)
    start = dt.date.fromisoformat(basis["start"])
    t0 = time.monotonic()
    rows = spawn_calendar.build_calendar(start, basis["days"], p)
    temps = [float(r["target_c"]) for r in rows]
    watch = [r for r in rows if r["watch"] == "true"]
    first_watch = watch[0]["date"] if watch else None
    clamped = sum(r["clamped"] == "true" for r in rows)
    steps = [abs(temps[i] - temps[i - 1]) for i in range(1, len(temps))]
    warnings = []
    if first_watch:
        lead = (dt.date.fromisoformat(first_watch) - start).days
        if lead < 0.8 * basis["max_lead_days"]:
            warnings.append(f"first watch night is {lead} days after the start (a repeating "
                            f"year allows up to about {basis['max_lead_days']}): treat it as a "
                            "baseline, not a test of the table")
    else:
        warnings.append("no watch night in this calendar")
    if basis["start_mode"] == "lead" and int(basis["start_value"]) > basis["max_lead_days"]:
        warnings.append(f"a lead over {basis['max_lead_days']} days starts the tank inside "
                        "the watch season (the cue year repeats)")
    if clamped:
        warnings.append(f"{clamped} days are clamped to the {p['safety_band_c']} C band")
    if basis["readiness"] != "ready":
        warnings.append(f"locale is {basis['readiness']}: " + "; ".join(basis["caveats"]))
    noon = solar_noon_check(p["tank_site"], start)
    if abs(noon["off_h"]) >= NOON_WARN_H:
        warnings.insert(0, _noon_message(p["tank_site"], noon))
    last = rows[-1]["date"]
    nights = []
    for season in _group_watch(watch):
        nights.append({"from": season[0]["date"], "to": season[-1]["date"],
                       "cue_from": season[0]["cue_date"], "n": len(season),
                       "temp_c": season[0]["target_c"]})
    series = [{"d": r["date"], "t": float(r["target_c"]), "raw": float(r["raw_c"]),
               "pp": int(r["photoperiod_min"]), "w": r["watch"] == "true",
               "ws": r["watch_season"] == "true", "moon": float(r["moon_illum_pct"]),
               "dk": int(float(r.get("dark_min") or 0))}
              for r in rows]
    return {
        "basis": basis, "params_draft": os.path.basename(ppath),
        "params": params,
        "summary": {
            "first_date": rows[0]["date"], "last_date": last, "rows": len(rows),
            "temp_min_c": min(temps), "temp_max_c": max(temps), "clamped_days": clamped,
            "max_step_c": round(max(steps) if steps else 0, 3),
            "photoperiod_min": [min(s["pp"] for s in series), max(s["pp"] for s in series)],
            "watch_nights": len(watch), "first_watch": first_watch,
            "cue_date_at_start": rows[0]["cue_date"], "offset_days": basis["offset_days"],
            "build_s": round(time.monotonic() - t0, 1),
            "max_lead_days": basis["max_lead_days"],
        },
        "watch_seasons": nights, "warnings": warnings,
        "first_rows": [{k: r[k] for k in ("date", "cue_date", "target_c", "white_on_local",
                                          "white_off_local", "moon_on_local", "moon_off_local",
                                          "days_after_full", "watch")} for r in rows[:7]],
        "series": series,
    }


def _group_watch(watch_rows):
    groups, cur, prev = [], [], None
    for r in watch_rows:
        d = dt.date.fromisoformat(r["date"])
        if prev is not None and (d - prev).days > 3:
            groups.append(cur)
            cur = []
        cur.append(r)
        prev = d
    if cur:
        groups.append(cur)
    return groups


def apply_calendar(data: str, choice: dict, confirm: bool) -> dict:
    """Write the active params and calendar and pin them. confirm must be True."""
    if confirm is not True:
        raise SetupError("confirm the preview first")
    params, basis = build_params(data, choice)
    ppath = _draft_params_file(data, params)
    p = cues.load_params(ppath)
    start = dt.date.fromisoformat(basis["start"])
    noon = solar_noon_check(p["tank_site"], start)
    if abs(noon["off_h"]) >= NOON_REFUSE_H:
        raise SetupError("tank site: " + _noon_message(p["tank_site"], noon))
    rows = spawn_calendar.build_calendar(start, basis["days"], p)
    name = f"spawn_calendar_{start.isoformat()}_{basis['locale']}"
    if basis["temp"] not in ("jaza",):
        name += f"_{basis['temp']}"
    aim = basis.get("species") or basis.get("genus")
    if aim and not aim.endswith("(JAZA)"):
        name += "_" + "".join(c if c.isalnum() else "-" for c in aim.lower()).strip("-")[:32]
    csv_path = os.path.join(data, name + ".csv")
    for path in (csv_path, csv_path[:-4] + ".meta.json",
                 os.path.join(data, "cue_params.json")):
        backup(path)
    active = os.path.join(data, "cue_params.json")
    shutil.copyfile(ppath, active)                 # same bytes, same sha
    sha = spawn_calendar.write_csv(rows, csv_path)
    spawn_calendar.write_meta(csv_path, csv_path, sha, p, start, basis["days"])
    st = load_state(data)
    prev = st.get("calendar")
    st["calendar"] = {"file": os.path.basename(csv_path), "params": "cue_params.json",
                      "params_sha256": p.sha256, "csv_sha256": sha,
                      "basis": basis, "confirmed_utc": now_utc(),
                      "previous": (prev or {}).get("file")}
    st.setdefault("history", []).append({"ts": now_utc(), "what": "calendar",
                                         "file": os.path.basename(csv_path)})
    save_state(data, st)
    arcs = _record_arcs(data, basis)
    return {"ok": True, "file": os.path.basename(csv_path), "arcs": arcs,
            "note": "the conductor follows this calendar from its next tick "
                    "(setup.json pins it; cue_params.json is the active params)"}


def _record_arcs(data, basis):
    """Keep arc_selection.json (tank_status 'arcs') in line with the choice."""
    cat_path = tank_arcs.find_catalog(data, _HERE)
    if not cat_path:
        return None
    try:
        cat = tank_arcs.load_catalog(cat_path)
    except tank_arcs.ArcError:
        return None
    loc = basis["locale"]
    if loc == "okinawa":
        sel = {"temp_arc": "spawn-calendar-temp", "light_arc": "spawn-calendar-light"}
    else:
        t = {"locale_reef": f"locale-{loc}-reef-temp", "locale_sst": f"locale-{loc}-sst",
             "locale_logger": f"locale-{loc}-logger-temp",
             "jaza_aligned": "okinawa-jaza-composite"}[basis["temp"]]
        sel = {"temp_arc": t, "light_arc": f"locale-{loc}-sky"}
    sel["offset_days"] = basis["offset_days"]
    try:
        return tank_arcs.save_selection(data, cat, sel)
    except tank_arcs.ArcError as err:
        return {"error": str(err)}


# ---------------------------------------------------------------------------
# STEP 2: PLUGS
# ---------------------------------------------------------------------------
def tuya_dir(data):
    return os.path.join(data, "tuya")


def _devices(data) -> list:
    d = _read_json(os.path.join(tuya_dir(data), "devices.json"), [])
    return d if isinstance(d, list) else []


def _array_path(data):
    return os.path.join(tuya_dir(data), "plug_array.json")


def _code_dp(mapping: dict, code: str):
    for dp, m in (mapping or {}).items():
        if isinstance(m, dict) and m.get("code") == code:
            return int(dp)
    return None


def device_summary(data) -> list:
    """devices.json without its keys, plus what the flow infers per device."""
    out = []
    for d in _devices(data):
        m = d.get("mapping") or {}
        cat = d.get("category")
        out.append({
            "id": d.get("id"), "name": d.get("name"), "category": cat,
            "product": d.get("product_name"), "model": d.get("model"),
            "cloud_ip": d.get("ip") or None, "version": d.get("version"),
            "has_key": bool(d.get("key")), "sub": bool(d.get("sub")),
            "driver": ("tinytuya_dimmer" if cat == "tgkg" else
                       "tinytuya" if cat in ("cz", "pc", "kg") else None),
            "power_dp": _code_dp(m, "switch_1") or _code_dp(m, "switch_led_1"),
            "revert_dp": _code_dp(m, "countdown_1"),
            "relay_status_dp": _code_dp(m, "relay_status"),
            "codes": sorted({v.get("code") for v in m.values() if isinstance(v, dict)}),
        })
    return out


def plugs_state(data) -> dict:
    arr = _read_json(_array_path(data), {"plugs": []}) or {"plugs": []}
    st = load_state(data)
    return {"devices": device_summary(data), "array": arr.get("plugs", []),
            "devices_file": os.path.exists(os.path.join(tuya_dir(data), "devices.json")),
            "cloud_config": os.path.exists(os.path.join(tuya_dir(data), "tinytuya.json")),
            "staging": staging(), "record": st.get("devices", {}),
            "functions": [{"id": k, "label": v[0], "what": v[1]} for k, v in FUNCTIONS.items()]}


def cloud_fetch(data, region, api_key, api_secret, device_id, remember=False,
                cloud_cls=None) -> dict:
    """Pull every device and its local key from the Tuya IoT cloud (what the
    tinytuya wizard does) into tuya/devices.json. The secret is stored in
    tuya/tinytuya.json only when remember is true. Returns a keyless summary."""
    if not (region and api_key and api_secret and device_id):
        raise SetupError("region, API key, API secret and one device id are all needed")
    if cloud_cls is None:
        import tinytuya
        cloud_cls = tinytuya.Cloud
    c = cloud_cls(apiRegion=region, apiKey=api_key, apiSecret=api_secret,
                  apiDeviceID=device_id)
    if getattr(c, "error", None):
        raise SetupError(f"Tuya cloud refused the sign-in: {_scrub(c.error, api_key, api_secret)}")
    devs = c.getdevices(verbose=False, include_map=True)
    if not isinstance(devs, list):
        raise SetupError(f"Tuya cloud returned no device list: {_scrub(devs, api_key, api_secret)}")
    path = os.path.join(tuya_dir(data), "devices.json")
    backup(path)
    _write_json(path, devs)
    if remember:
        _write_json(os.path.join(tuya_dir(data), "tinytuya.json"),
                    {"apiKey": api_key, "apiSecret": api_secret,
                     "apiRegion": region, "apiDeviceID": device_id})
    st = load_state(data)
    st["devices"]["cloud_fetch_utc"] = now_utc()
    st["devices"]["count"] = len(devs)
    save_state(data, st)
    return {"ok": True, "count": len(devs), "devices": device_summary(data)}


def _scrub(obj, *secrets) -> str:
    text = str(obj)[:300]
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def scan(data, timeout_s=8.0, scanner=None) -> dict:
    """Passive UDP listen for the devices' broadcasts (sends nothing)."""
    import tank_scan
    ids = [d["id"] for d in _devices(data) if d.get("id")]
    if not ids:
        raise SetupError("no devices yet: fetch them from the Tuya cloud first")
    scanner = scanner or tank_scan.default_scanner
    try:
        found = scanner(ids, max(3.0, min(30.0, float(timeout_s))))
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "why": f"{type(err).__name__}: {err}"[:200], "found": {}}
    st = load_state(data)
    st["devices"]["scan_utc"] = now_utc()
    st["devices"]["scan_found"] = found
    save_state(data, st)
    blind = not found
    return {"ok": True, "found": found, "blind": blind,
            "note": ("nothing heard: broadcasts do not cross a Docker bridge or "
                     "Docker Desktop's VM; run the scan on the host or type the IPs "
                     "(a DHCP reservation per plug is the real fix)") if blind else None}


def suggest_array(data) -> list:
    """Array rows for every device, keeping what plug_array.json already says
    (name, role, options) and filling the rest from the mapping and the scan."""
    arr = (_read_json(_array_path(data), {}) or {}).get("plugs", [])
    by = {(e.get("id"), e.get("gang")): e for e in arr}
    found = load_state(data).get("devices", {}).get("scan_found", {}) or {}
    out = []
    for d in device_summary(data):
        if not d["driver"] or d["sub"]:
            continue
        gangs = (1, 2) if d["driver"] == "tinytuya_dimmer" else (None,)
        for g in gangs:
            cur = by.get((d["id"], g))
            row = dict(cur) if cur else {
                "name": (d["name"] or d["id"][-6:]).strip()[:24] + (f"-g{g}" if g else ""),
                "driver": d["driver"], "role": "unassigned", "id": d["id"],
                "version": float(d["version"] or (3.5 if g else 3.3)),
            }
            if g:
                row["gang"] = g
            else:
                row.setdefault("power_dp", d["power_dp"] or 1)
                if d["revert_dp"] is not None:
                    row.setdefault("revert_dp", d["revert_dp"])
            row["ip"] = found.get(d["id"]) or row.get("ip") or d["cloud_ip"]
            row["_device_name"] = d["name"]
            row["_existing"] = bool(cur)
            out.append(row)
    # keep array entries that have no device (fake, tbd)
    ids = {d["id"] for d in device_summary(data)}
    for e in arr:
        if e.get("id") not in ids:
            out.append({**e, "_existing": True, "_device_name": None})
    return out


def save_array(data, rows: list, ack_role_changes: bool = False) -> dict:
    """Validate and write tuya/plug_array.json. A role that differs from the
    current file needs ack_role_changes (each role change should be backed by
    notes and a lux step from measure-lamp, not a guess)."""
    import tank_plugs
    if not isinstance(rows, list) or not rows:
        raise SetupError("no plugs to save")
    cur = {e.get("name"): e for e in
           (_read_json(_array_path(data), {}) or {}).get("plugs", [])}
    clean, names, changes = [], set(), []
    for r in rows:
        r = {k: v for k, v in r.items() if not k.startswith("_")}
        if r.get("role") not in ROLES:
            raise SetupError(f"{r.get('name')}: role must be one of {ROLES}")
        if not r.get("name") or r["name"] in names:
            raise SetupError(f"plug name missing or repeated: {r.get('name')!r}")
        names.add(r["name"])
        old = cur.get(r["name"])
        if old and old.get("role") != r["role"]:
            changes.append(f"{r['name']}: {old.get('role')} -> {r['role']}")
        clean.append(r)
    singles = [r["role"] for r in clean if r["role"] in ("heater",)]
    if len(singles) > 1:
        raise SetupError("only one plug can be the heater gate")
    if changes and not ack_role_changes:
        return {"ok": False, "needs_ack": changes,
                "why": "role changes need a tick to confirm you checked them "
                       "(notes plus a lux step with measure-lamp)"}
    path = _array_path(data)
    tmp = path + ".check"
    _write_json(tmp, {"plugs": clean})
    try:
        tank_plugs.load_plugs(tmp, os.path.join(tuya_dir(data), "devices.json"))
    except (ValueError, TypeError) as err:
        os.remove(tmp)
        raise SetupError(f"plug_array would not load: {err}") from None
    os.remove(tmp)
    bak = backup(path)
    _write_json(path, {"plugs": clean})
    st = load_state(data)
    st["devices"]["array_saved_utc"] = now_utc()
    st["devices"]["role_changes"] = changes
    save_state(data, st)
    return {"ok": True, "plugs": len(clean), "role_changes": changes,
            "backup": os.path.basename(bak) if bak else None}


def _load(data):
    import tank_plugs
    return tank_plugs.load_plugs(_array_path(data),
                                 os.path.join(tuya_dir(data), "devices.json"),
                                 os.path.join(data, "extra_plugs.json"))


def health(data) -> list:
    """Read-only: one status query per plug (tank_health.check_plugs)."""
    import tank_health
    rows = tank_health.check_plugs(_load(data))
    for r in rows:
        r.pop("dps", None)
    return rows


# Power-on state ---------------------------------------------------------
POWER_ON_WANT = {"heater": "power_on", **{r: "power_off" for r in LIGHT_ROLES}}


def _fake_state_path(data):
    return os.path.join(data, "setup", "fake_plug_state.json")


def _relay_dp(data, plug):
    """The power-on (relay_status) data point from the device's mapping. A fake
    plug that carries a real device id answers like that model would."""
    dev_id = getattr(plug, "dev_id", None) or plug.opts.get("id")
    dev = next((d for d in device_summary(data) if d["id"] == dev_id), None)
    if dev is not None:
        return dev["relay_status_dp"]
    if getattr(plug, "driver", "") in ("fake", "fake_dimmer"):
        return int(plug.opts.get("relay_status_dp", 38))
    return None


def power_on_read(data) -> list:
    """Each plug's power-on (relay_status) setting, read only."""
    fake = _read_json(_fake_state_path(data), {}) or {}
    out = []
    for plug in _load(data):
        dp = _relay_dp(data, plug)
        want = POWER_ON_WANT.get(plug.role)
        row = {"name": plug.name, "role": plug.role, "driver": plug.driver,
               "dp": dp, "want": want, "value": None, "ok": False}
        if dp is None:
            row["why"] = ("this model has no power-on setting the local API can "
                          "reach; set it in Smart Life, or note that it cannot be set")
        elif plug.driver in ("fake", "fake_dimmer"):
            row.update(ok=True, value=fake.get(plug.name, plug.opts.get("relay_status", "last")))
        else:
            r = plug.status()
            if r.ok:
                row.update(ok=True, value=(r.dps or {}).get(str(dp)))
            else:
                row["why"] = r.detail or r.error
        row["matches"] = row["value"] == want if want else None
        out.append(row)
    return out


def power_on_set(data, name: str, value: str, confirm: str) -> dict:
    """Write one plug's power-on setting, then read it back. confirm must be
    the plug's name (the per-plug yes). Real plugs are refused in staging."""
    if value not in ("power_off", "power_on", "last"):
        raise SetupError("value must be power_off, power_on or last")
    if confirm != name:
        raise SetupError("type the plug's name to confirm this write")
    plug = next((p for p in _load(data) if p.name == name), None)
    if plug is None:
        raise SetupError(f"no plug {name!r}")
    dp = _relay_dp(data, plug)
    if dp is None:
        raise SetupError(f"{name} has no power-on setting on the local API")
    if plug.driver in ("fake", "fake_dimmer"):
        st = _read_json(_fake_state_path(data), {}) or {}
        before = st.get(name)
        st[name] = value
        _write_json(_fake_state_path(data), st)
        after = value
    else:
        if staging():
            raise SetupError("staging: writes to real plugs are refused (SETUP_STAGING=1)")
        before = (plug.status().dps or {}).get(str(dp))
        import tinytuya
        reply = plug._device(tinytuya).set_value(int(dp), value)
        if isinstance(reply, dict) and (reply.get("Error") or reply.get("Err")):
            raise SetupError(f"{name}: {reply.get('Error')} (Err {reply.get('Err')})")
        after = (plug.status().dps or {}).get(str(dp))
    ok = after == value
    st = load_state(data)
    st["devices"].setdefault("power_on", {})[name] = {
        "value": after, "asked": value, "before": before, "ok": ok, "ts": now_utc()}
    save_state(data, st)
    return {"ok": ok, "name": name, "before": before, "after": after}


# ---------------------------------------------------------------------------
# STEP 2: INKBIRD VM (read-only checks plus a hand checklist)
# ---------------------------------------------------------------------------
# The Inkbird walkthrough: (id, title, [steps]). The ids are what setup.json
# records, so keep them stable.
ITC_STEPS = [
    ("phone_first", "Pair the ITC-306A with the Inkbird app on your phone", [
        "Install the INKBIRD app (publisher INKBIRD TECH) on your phone and make an Inkbird account.",
        "Add the ITC-306A in the app by following its add-device steps (the controller joins 2.4 GHz Wi-Fi only).",
        "Check the app shows the live temperature, T1 and T2. The emulator signs in to this same account later, so it sees this device without pairing it again.",
        "In your router, give the ITC-306A a fixed address (DHCP reservation) by its MAC. Look it up by MAC, never by an old IP.",
    ]),
    ("stack_up", "Start the emulator stack", [
        "Docker Desktop needs at least 12 GB of memory (Settings, Resources) and must start when you sign in to Windows.",
        "Run: docker compose --profile android up -d kvm-enable android-inkbird itc  (always name the services).",
        "First boot takes about 5 to 7 minutes. docker ps shows anemones-android as healthy when Android has finished booting.",
        "Never rename or copy the anemones-android container: compose would recreate it. The sign-ins live in its volumes.",
    ]),
    ("play_signin", "Sign in to Google Play inside the emulator", [
        "Open http://127.0.0.1:6081 in a browser on this computer and click Connect. You see an Android phone screen.",
        "Do not close the emulator window in that view: closing it quits the emulator, and the container restarts.",
        "Open the Play Store and sign in. A spare Google account is fine.",
        "Search INKBIRD and install the app from INKBIRD TECH.",
    ]),
    ("app_signin", "Sign in to the Inkbird app in the emulator", [
        "Open the INKBIRD app and sign in with the same account as your phone.",
        "Tap the ITC-306A so its page shows the current temperature, T1 and T2.",
    ]),
    ("adb_allowed", "Let the reader control the emulator", [
        "When the itc service first connects, Android asks 'Allow USB debugging?'.",
        "Tick 'Always allow from this computer' and tap Allow. The key is kept in a volume, so this is a one-off.",
    ]),
    ("device_page", "Leave the app on the device page", [
        "The reader reads the temperature off this screen every 5 minutes, so leave the ITC-306A page open.",
        "If it was closed, the service brings the app back by itself; if readings stop, open the page again in noVNC.",
    ]),
    ("first_read", "Check a first good reading", [
        "The status box above shows 'feed fresh and reading: yes' with a temperature, T1 and T2 that match the device.",
        "If not: check docker logs itc-service for the last error.",
    ]),
    ("writes_ok", "Decide on Inkbird writes", [
        "The controller can move T1/T2 with the cue table (itc_setpoints in Safety and settings). It taps the real app.",
        "To stop all writes at once, create the file data/itc/HALT (reads go on). Delete it to resume.",
    ]),
]
ITC_CHECKLIST = [(i, t) for i, t, _ in ITC_STEPS]


def _host_path(data, p):
    """A /data/... path from tank_config, mapped to this data dir."""
    if p and p.startswith("/data/"):
        return os.path.join(data, p[6:])
    return p


def itc_status(data, docker=shutil.which("docker")) -> dict:
    cfg = _read_json(os.path.join(data, "tank_config.json"), {}) or {}
    feed = _host_path(data, ((cfg.get("inputs") or {}).get("temp") or {}).get("path")
                      or "/data/sensors/itc306a_docker.jsonl")
    out = {"feed": os.path.relpath(feed, data) if feed else None, "feed_ok": False}
    now = time.time()
    if feed and os.path.exists(feed):
        out["feed_age_s"] = round(now - os.path.getmtime(feed))
        last = None
        with open(feed, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 4096))
            for line in fh.read().splitlines():
                try:
                    last = json.loads(line)
                except ValueError:
                    pass
        out["last"] = last
        try:                    # age of the newest reading, not of the file
            ts = dt.datetime.fromisoformat(str(last["ts"]).replace("Z", "+00:00"))
            out["reading_age_s"] = round(now - ts.timestamp())
        except (TypeError, KeyError, ValueError):
            out["reading_age_s"] = None
        out["feed_ok"] = bool(last and last.get("temp_c") is not None
                              and out["reading_age_s"] is not None
                              and out["reading_age_s"] < 900)
    svc = _read_json(os.path.join(data, "itc", "service.json"))
    if svc:
        out["service"] = svc
        out["service_age_s"] = round(now - os.path.getmtime(os.path.join(data, "itc", "service.json")))
    out["halt_file"] = os.path.exists(os.path.join(data, "itc", "HALT"))
    out["containers"] = None
    if docker:
        try:
            res = subprocess.run([docker, "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
                                 capture_output=True, text=True, timeout=10)
            rows = dict(l.split("\t", 1) for l in res.stdout.splitlines() if "\t" in l)
            out["containers"] = {n: rows.get(n, "absent")
                                 for n in ("kvm-enable", "anemones-android", "itc-service")}
        except (OSError, subprocess.SubprocessError):
            pass
    st = load_state(data)
    out["checklist"] = [{"id": k, "text": t, "steps": steps, "done": st["itc"]["checklist"].get(k)}
                        for k, t, steps in ITC_STEPS]
    return out


HEATER_STEPS = [
    ("docker", "Docker Desktop is installed and starts with the computer", [
        "This page is running inside Docker, so Docker itself works.",
        "Make sure it comes back after a restart: Docker Desktop, Settings, General, tick 'Start Docker Desktop when you sign in to your computer'. On Linux: sudo systemctl enable docker.",
        "Memory: Docker's default is plenty. The whole controller uses about 200 MB.",
        "Install links, if you are setting up another computer: Windows and Mac https://www.docker.com/products/docker-desktop/ , Linux https://docs.docker.com/engine/install/",
    ]),
    ("thermostat", "The heater runs on its own thermostat", [
        "Plug the heater into a standalone heater controller with its own probe in the tank (any aquarium heater controller with a high-temperature cutoff).",
        "Set its heat-on point to your tank's normal temperature, and its high alarm or cutoff a couple of degrees above.",
        "Never plug the heater straight into a smart plug the controller switches. The controller may only block heat (the heater gate below), never be the only thing that stops it.",
    ]),
    ("thermometer", "The controller can read the water temperature", [
        "Pick one: a DS18B20 probe on a Raspberry Pi or any Linux board (driver ds18b20_sysfs), a program that writes the reading to a file (jsonl), or a device that answers a web address with the reading (http_json). docs/QUICKSTART.md, 'Temperature and light sources', has the details.",
        "Set it in step 5, 'Where the controller reads the water temperature'.",
        "This step ticks itself when a fresh reading arrives: run sh scripts/start.sh check after you set it.",
    ]),
    ("gate", "Optional: a heater gate plug", [
        "A smart plug in front of the heater thermostat lets the controller block heat when the water is too warm. It must come back ON after a power cut.",
        "Give that plug the heater job in step 2, then follow docs/TURNING-OUTPUTS-ON.md step 5: the countdown test, the thermostat power-return test and the pull-the-plug test.",
        "Skip this if you do not want the controller to touch the heater at all.",
    ]),
    ("check", "Run the health check", [
        "In the controller folder run: sh scripts/start.sh check",
        "It checks the containers, the cue tables, the thermometer reading, the plugs (reading only) and the alerts, and reports here. This step ticks itself when it passes.",
    ]),
]


def heater_status(data) -> dict:
    """Step 3 without the Inkbird emulator. Items tick themselves from what the
    helper's health check reported; the rest are ticked by hand."""
    st = load_state(data)
    done = st.setdefault("heater", {})
    chk = (host_status(data).get("check") or {})
    auto = {"docker": True,
            "thermometer": bool(chk.get("temp_ok")),
            "check": bool(chk.get("ok"))}
    return {"mode": "inkbird" if inkbird_in_use(data) else "generic",
            "last_check": chk or None,
            "steps": [{"id": k, "title": t, "steps": sx,
                       "done": done.get(k) or (chk.get("checked_utc") if auto.get(k) and k != "docker" else None)
                       or ("auto" if k == "docker" else None),
                       "auto": k in auto}
                      for k, t, sx in HEATER_STEPS]}


def heater_check(data, item: str, done: bool) -> dict:
    if item not in {k for k, _, _ in HEATER_STEPS}:
        raise SetupError(f"no step {item!r}")
    st = load_state(data)
    st.setdefault("heater", {})[item] = now_utc() if done else None
    save_state(data, st)
    return {"ok": True}


def itc_check(data, item: str, done: bool) -> dict:
    if item not in dict(ITC_CHECKLIST):
        raise SetupError(f"no checklist item {item!r}")
    st = load_state(data)
    st["itc"]["checklist"][item] = now_utc() if done else None
    save_state(data, st)
    return {"ok": True}



# ---------------------------------------------------------------------------
# TELEGRAM ALERTS
# ---------------------------------------------------------------------------
TG_STEPS = [
    ("install", "Install Telegram on your phone", [
        "iPhone: https://apps.apple.com/app/telegram-messenger/id686449807",
        "Android: https://play.google.com/store/apps/details?id=org.telegram.messenger",
        "Computer (optional): https://desktop.telegram.org/",
        "Open it and sign up with your phone number.",
    ]),
    ("bot", "Make a bot", [
        "In Telegram, open a chat with @BotFather (the one with the blue check mark): https://t.me/BotFather",
        "Send /newbot, give it a name (e.g. Reef tank) and a username ending in bot.",
        "BotFather replies with a token like 123456:ABC-DEF... Keep it to yourself: it works like a password.",
        "Send your new bot one message (e.g. hi) from the phone that should get the alerts.",
    ]),
    ("helper", "Run the alert helper", [
        "In the controller folder run: sh scripts/start.sh telegram",
        "It asks for the token (typing is hidden), finds your chat from the message you sent, writes both into .env, restarts the controller and sends a test alert.",
        "This step ticks itself when the test alert went out. The token never passes through this page.",
    ]),
    ("deadman", "Add a 'computer is off' ping (recommended)", [
        "An alert cannot say the computer is dead. A free service can: make a check at https://healthchecks.io (period 5 min, grace 10 min).",
        "The helper asks for its ping URL at the end (or run sh scripts/start.sh telegram again). healthchecks.io then messages you when the pings stop.",
    ]),
    ("feed", "Optional: the daily feed bot", [
        "A noon summary on a second bot (token in .env as reeflive_token, chat TELEGRAM_FEED_CHAT_ID).",
        "Keep it separate: a chatty feed must not bury real alerts.",
    ]),
]


def telegram_status(data) -> dict:
    import common
    st = load_state(data)
    hs = host_status(data)
    tg, dm = hs.get("telegram") or {}, hs.get("deadman") or {}
    auto = {"helper": tg.get("test_delivered") and tg.get("configured"),
            "bot": tg.get("configured"), "install": tg.get("configured"),
            "deadman": dm.get("configured")}
    return {
        "token_set": bool(os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("configured")),
        "chat_set": bool(os.environ.get("TELEGRAM_CHAT_ID") or tg.get("configured")),
        "deadman_set": bool(os.environ.get("TANK_DEADMAN_URL") or dm.get("configured")),
        "helper": {"telegram": tg or None, "deadman": dm or None},
        "feed_set": bool(os.environ.get("TELEGRAM_FEED_BOT_TOKEN") or os.environ.get("reeflive_token")),
        "channels": common.alert_channels(),
        "note": ("this page never sees the token: sh scripts/start.sh telegram sets it up and reports "
                 "here what it did") if staging() else None,
        "steps": [{"id": k, "title": t, "steps": sx,
                   "done": st.setdefault("telegram", {}).get(k) or auto.get(k)} for k, t, sx in TG_STEPS],
        "last_test": st.get("telegram", {}).get("last_test"),
    }


def telegram_check(data, item, done) -> dict:
    if item not in {k for k, _, _ in TG_STEPS}:
        raise SetupError(f"no step {item!r}")
    st = load_state(data)
    st.setdefault("telegram", {})[item] = now_utc() if done else None
    save_state(data, st)
    return {"ok": True}


def telegram_chats(urlopen=None) -> list:
    """Chats that have written to the bot (getUpdates). The token never leaves
    this process."""
    import urllib.request
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SetupError("TELEGRAM_BOT_TOKEN is not set in this container's environment")
    urlopen = urlopen or urllib.request.urlopen
    try:
        with urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10) as r:
            body = json.loads(r.read())
    except Exception as err:  # noqa: BLE001
        raise SetupError(f"Telegram did not answer: {_scrub(err, token)}") from None
    chats = {}
    for u in body.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        c = msg.get("chat") or {}
        if c.get("id") is not None:
            chats[c["id"]] = {"id": c["id"], "type": c.get("type"),
                              "name": c.get("title") or " ".join(
                                  x for x in (c.get("first_name"), c.get("last_name")) if x)}
    return list(chats.values())


def telegram_test(data) -> dict:
    import common
    if "telegram" not in common.alert_channels():
        raise SetupError("Telegram is not configured here (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
    ok = common.send_alert("Test alert from the setup page. If you can read this, alerts work.",
                           title="Anemone tank (test)")
    st = load_state(data)
    st.setdefault("telegram", {})["last_test"] = {"ts": now_utc(), "delivered": ok}
    save_state(data, st)
    return {"ok": ok}

# ---------------------------------------------------------------------------
# STEP 3: DECISIONS
# ---------------------------------------------------------------------------
# (id, group, label, where, type, why). where: "config:<dotted path>",
# "params:<dotted path>" (read-only here: change it in step 1),
# "plug:<role>:<field>", or "check" (a fact about the hardware, yes/no).
DECISIONS = [
    # --- fail-safes ---
    ("alarm_band", "Fail-safes", "Alarm band (C)", "config:limits.alarm_c", "pair",
     "73 to 84 F. Top sits above the calendar's warmest target so a tank on plan never alarms."),
    ("cooling_on", "Fail-safes", "Over-temperature cutoff on", "config:outputs.cooling.enabled", "bool",
     "Cuts the listed plugs when the tank runs hot (tank_cool.py)."),
    ("cooling_setpoint", "Fail-safes", "Cutoff set point (C)", "config:outputs.cooling.setpoint", "num_or_text",
     "A fixed temperature works best: 'plan' follows the daily target and can cut every warm afternoon, because lamps hold a tank a degree or two above plan."),
    ("cooling_above", "Fail-safes", "Cut at set point + (C)", "config:outputs.cooling.cutoff_above_c", "num",
     "With 28.0 this cuts at 28.6 C (83.5 F), resumes 0.5 C below."),
    ("cooling_plugs", "Fail-safes", "Plugs the cutoff may cut", "config:outputs.cooling.plugs", "list",
     "The plugs that add the most heat, usually the brightest lamps."),
    ("lamp_deadman", "Fail-safes", "Lamp backstop on", "config:outputs.lamp_deadman.enabled", "bool",
     "Each lamp ON arms the plug's own countdown, so a dead controller cannot leave lamps on. Dimmers are not covered."),
    ("lamp_deadman_s", "Fail-safes", "Lamp backstop countdown (s)", "config:outputs.lamp_deadman.revert_after_s", "num",
     "900 s: the plug turns itself off 15 min after the last refresh."),
    ("protect_dark", "Fail-safes", "Protect full dark", "config:outputs.protect_dark", "bool",
     "Refuses any lamp ON during the plan's full-dark window, whatever asked for it."),
    ("rate_interval", "Fail-safes", "Minimum seconds between commands", "config:outputs.min_command_interval_s", "num",
     "Per plug; stops a flapping loop from hammering a relay."),
    ("rate_hour", "Fail-safes", "Max commands per hour", "config:outputs.max_commands_per_hour", "num",
     "Per plug."),
    ("heater_gate", "Fail-safes", "Heater gate plug in use", "config:outputs.heater.enabled", "bool",
     "Off: the Inkbird alone runs the heater. On needs countdown_verified and inkbird_resume_verified."),
    ("autoheal", "Fail-safes", "Autoheal sidecar restarts a hung controller", "check", "check",
     "compose service tank-autoheal, label anemones.autoheal on tank only."),
    ("inkbird_alarm", "Fail-safes", "Inkbird's own high/low alarm set on the device", "check", "check",
     "Your heater thermostat's own high-temperature alarm or cutoff, set on the device itself. It works even when this computer is off."),
    ("gfci", "Fail-safes", "GFCI and drip loops on every tank cord", "check", "check",
     "Hardware fail-safe; nothing in software replaces it."),
    ("pi_case", "Fail-safes", "Sensor board (Raspberry Pi or similar, if you use one) in a case, away from salt spray", "check", "check",
     "A bare board above the water dies of salt spray. Skip if you have no sensor board."),
    ("dhcp", "Fail-safes", "DHCP reservations for every plug and the Pi", "check", "check",
     "The controller reaches plugs by IP; Docker Desktop cannot hear the drift scan."),
    # --- alerts ---
    ("alerts_channel", "Alerts", "Telegram alert channel configured", "env:TELEGRAM_BOT_TOKEN", "env",
     "Reuses the ocbridge bot: rotating that token also breaks tank alerts."),
    ("deadman", "Alerts", "Off-box dead-man URL configured", "env:TANK_DEADMAN_URL", "env",
     "Silence from the controller raises the alarm somewhere else."),
    ("lifeguard_deadman", "Alerts", "Lifeguard's own dead-man URL configured", "env:LIFEGUARD_DEADMAN_URL", "env",
     "A second off-site check, so you also hear when the watchdog itself stops."),
    ("cal_low", "Alerts", "Warn this many days before the calendar ends", "config:limits.calendar_low_days", "num",
     "Default 30."),
    # --- sensors ---
    ("temp_source", "Sensors", "Temperature source (driver)", "config:inputs.temp.driver", "text",
     "Inkbird feed (jsonl from the emulator) or a probe on a Pi (ds18b20_sysfs, jsonl, http_json); none = no temperature feed yet (a warning, and the over-temperature cutoff cannot act)."),
    ("probe_offset", "Sensors", "Pi probe offset (C)", "config:inputs.probe_temp.offset_c", "num",
     "Only if you use a second probe: its difference from a trusted thermometer."),
    ("temp_calibrated", "Sensors", "Temperature counts as calibrated", "config:limits.temp_calibrated", "bool",
     "No written calibration record for the Inkbird yet."),
    ("dark_lux", "Sensors", "Light-leak limit in full dark (lux)", "config:limits.dark_lux_max", "num",
     "0.6 lux in the example config. With a VEML7700 the quiet floor wandered 0.17 to 0.43 lux; not checked against a reference meter. Ignored if you have no lux sensor."),
    ("dark_lux_min", "Sensors", "Minutes over the limit before light_leak alerts", "config:limits.dark_lux_confirm_min", "num",
     "3 minutes, so a run of minutes means a lamp, not the room."),
    # --- outputs and cues ---
    ("outputs_on", "Outputs", "Outputs enabled (the controller switches plugs)", "config:outputs.enabled", "bool",
     "On since 2026-11-03."),
    ("ch_white", "Outputs", "White channel switched", "config:outputs.channels.white", "bool", ""),
    ("ch_white_plus", "Outputs", "White-plus channel switched", "config:outputs.channels.white_plus", "bool", ""),
    ("ch_blue", "Outputs", "Blue channel switched", "config:outputs.channels.blue", "bool",
     "Blue follows white (plugs are on/off)."),
    ("ch_moon", "Outputs", "Moon channel switched", "config:outputs.channels.moon", "bool",
     "Interim lamp on the dimmer is far too bright (8.4 lux at 1 percent vs 0.1-0.3 target)."),
    ("white_plus", "Cues", "White-plus threshold (fraction of day curve)", "config:plan.white_plus.threshold", "num",
     "Above this fraction of the day's light curve the extra-bright lamp joins in (0.75: only around midday)."),
    ("dark_source", "Cues", "Full-dark definition", "config:plan.dark_source", "text",
     "astro: sun below -18 deg and moon down."),
    ("moon_lux", "Cues", "Moon target at the sensor (lux)", "config:plan.moon_lux_range", "pair",
     "0.1 to 0.3 lux. Moonlight much brighter than a real moon may hold spawning back instead of cueing it."),
    ("hourly", "Cues", "Hourly temperature offsets (C, 24 values)", "config:plan.hourly_offsets_c", "list",
     "Flat zeros: the water target holds steady through the day. No measured daily reef pattern has been found yet (an open quest)."),
    ("itc_cue", "Cues", "Cue-driven Inkbird T1/T2 writes", "config:itc_setpoints.enabled", "bool",
     "Moves the Inkbird's set points with the cue table, through its app in the emulator."),
    # --- life support: redundancy, the independent watchdog, safe mode ---
    ("sensor_failover", "Life support", "Backup thermometer takes over", "config:limits.redundancy.failover", "bool",
     "When the main thermometer cannot be read, the cutoff, the heater gate and the alarm act on the second one "
     "instead of going blind. Needs a second thermometer (probe_temp)."),
    ("lifeguard_on", "Life support", "Lifeguard watchdog on", "config:lifeguard.enabled", "bool",
     "A separate container that watches the controller's heartbeat and reads the thermometers itself, "
     "and alerts on its own when either goes quiet or the water passes the hard limits."),
    ("safe_mode", "Life support", "Safe mode armed", "config:lifeguard.safe_mode.enabled", "bool",
     "Lets the lifeguard put every light out when the water passes the hard limit or the controller stays "
     "silent. The heater is left to its own thermostat. Off: the lifeguard only alerts."),
    ("lifeguard_margin", "Life support", "Hard limits: alarm band plus (C)", "config:lifeguard.hard_margin_c", "num",
     "The lifeguard's emergency limits are the alarm band widened by this much on both sides."),
    ("lifeguard_silent", "Life support", "Controller counts as down after (min)", "config:lifeguard.controller_silent_min", "num",
     "No status from the controller for this long is a warning (autoheal restarts a hung controller at about 8 min)."),
    ("safe_release", "Life support", "Safe mode release", "config:lifeguard.safe_mode.latch", "text",
     "auto: released once the cause has been clear for a while. manual: only `lifeguard.py reset --yes`."),
    ("itc_bounds", "Cues", "Inkbird T1/T2 bounds (F)", "config:itc_setpoints.bounds_f", "pair", ""),
    ("itc_interval", "Cues", "Min seconds between Inkbird writes", "config:itc_setpoints.min_interval_s", "num", ""),
    ("itc_watch", "Cues", "Inkbird heating watch alerts", "config:itc_watch.enabled", "bool", ""),
    # --- from step 1 (read-only here) ---
    ("band", "Calendar (step 1)", "Safety band for targets (C)", "params:safety_band_c", "pair",
     "23 to 29 C; every target is clamped into it."),
    ("step", "Calendar (step 1)", "Max change per day (C)", "params:max_step_c_per_day", "num", "0.1 C per day."),
    ("watch_moon", "Calendar (step 1)", "Watch nights: days after full", "params:watch", "watch",
     "JAZA: 6 to 9 days after full moon."),
]


# What each plug function is, in plain words (the Plugs tab and the per-function
# fail-safes use these).
FUNCTIONS = {
    "white": ("Main white light", "the daylight lamp; on from sunrise to sunset"),
    "white_plus": ("High-power white (midday)", "a strong lamp that only joins around midday; "
                   "also the biggest heat source, so the over-temperature cutoff can cut it"),
    "blue": ("Blue / actinic light", "follows the white light (on/off plugs cannot dim)"),
    "moon": ("Moon light (dimmer only)", "a very dim lamp on a Tuya dimmer, on while the moon is up"),
    "heater": ("Heater gate", "a plug in front of the Inkbird heater controller; it must come "
               "back ON by itself, or the heater loses power"),
    "unassigned": ("Not used / watch only", "read but never switched"),
}

# Life support: the only rows asked up front, in plain words (label, why).
# Everything else keeps its default under "Other settings".
UPFRONT = {
    "temp_source": ("Where the controller reads the water temperature",
                    "Without a thermometer the controller cannot see the water, and the too-hot cutoff below cannot work."),
    "alarm_band": ("Send an alert if the water goes outside this range",
                   "Pick a range a little wider than your target, so a normal day never sets it off."),
    "cooling_on": ("Switch the lamps off if the water gets too hot",
                   "Lamps heat the water. If it gets too warm, the controller turns them off until it cools down."),
    "cooling_setpoint": ("The temperature that counts as too hot",
                         "The lamps go off a little above this and come back on once the water is below it again."),
    "lamp_deadman": ("Lamps switch themselves off if this computer stops",
                     "Each lamp plug runs its own timer. If the controller stops refreshing it, the lamp turns off by itself, so it cannot stay on all night."),
    "inkbird_alarm": ("Your heater thermostat has its own high-temperature cutoff or alarm",
                      "Set on the thermostat itself. It is the protection that still works when this computer, the network or the controller is down."),
    "gfci": ("Every tank power cord has a drip loop and a safety switch (GFCI/RCD)",
             "Water and mains power: a drip loop keeps water off the socket and a GFCI or RCD cuts the power if something leaks."),
    "alerts_channel": ("Phone alerts are set up",
                       "Without them nobody hears about a problem until they look. Step 4 sets them up."),
    "deadman": ("Tell me if this computer goes off",
                "An alert cannot say the computer is dead. A free outside service (healthchecks.io) can: it messages you when the pings stop."),
    "safe_mode": ("Turn the lamps off by themselves in an emergency",
                  "If the water passes the hard limits or the controller goes quiet, the watchdog turns every lamp off and leaves the heater to its own thermostat."),
}
CRITICAL = set(UPFRONT)
# Inkbird rows: shown only on a tank that reads an Inkbird through the emulator.
INKBIRD_ONLY = {"itc_cue", "itc_bounds", "itc_interval", "itc_watch"}


def inkbird_in_use(data: str) -> bool:
    """True on a tank that reads the Inkbird ITC-306A through the Android
    emulator (the first tank). The public release does not ship that reader."""
    cfg = _read_json(os.path.join(data, "tank_config.json"), {}) or {}
    t = (cfg.get("inputs") or {}).get("temp") or {}
    return ("itc306a" in str(t.get("path", "")) or str(t.get("label", "")).lower() == "inkbird"
            or os.path.exists(os.path.join(data, "itc", "service.json")))


def host_status(data: str) -> dict:
    """What the host-side helper (sh scripts/start.sh telegram | golive | check)
    last did, written to <data>/host_status.json. It never holds a secret."""
    return _read_json(os.path.join(data, "host_status.json"), {}) or {}


def _plug_decisions(arr, cfg) -> list:
    """Per-plug fail-safes, by the plug's function. 7-tuples: the DECISIONS
    fields plus tier."""
    out = []
    cut = set(((cfg.get("outputs") or {}).get("cooling") or {}).get("plugs") or [])
    for e in arr:
        n, role = e.get("name"), e.get("role", "unassigned")
        label = FUNCTIONS.get(role, (role, ""))[0]
        grp = f"{label}: {n}"
        hot = "function"
        if role in ("white", "white_plus", "blue"):
            out += [
                (f"{n}.power_on", grp, f"{n} comes back OFF after a power cut", f"pcheck:{n}", "check",
                 "Set with the power-on control in this card where the model allows; otherwise in "
                 "Smart Life. A lamp that comes back on at night breaks the dark cue and adds heat.", hot),
                (f"{n}.revert_dp", grp, f"{n} countdown data point (lamp backstop)", f"pfield:{n}:revert_dp",
                 "num", "The plug's own timer that switches it off if the controller stops refreshing it. "
                 "Without it the backstop cannot protect this lamp.", hot),
                (f"{n}.in_cutoff", grp, f"{n} is cut by the over-temperature cutoff", f"derived:{n}:cutoff",
                 "bool", "Change the list under Critical life support (Plugs the cutoff may cut).",
                 "function"),
                (f"{n}.expect_w", grp, f"{n} expected draw while on (W: min, max)", f"pfield:{n}:expect_w",
                 "pair", "Optional: a lamp drawing outside this range raises a warning (dead lamp or "
                 "wrong load). Only for plugs that report watts.", "function"),
            ]
        elif role == "moon":
            out += [
                (f"{n}.fixed_level", grp, f"{n} fixed brightness (0-1)", f"pfield:{n}:fixed_level", "num",
                 "The moon lamp runs at this level whenever the plan has the moon up.", "function"),
                (f"{n}.level_cap", grp, f"{n} brightness ceiling (0-1)", f"pfield:{n}:level_cap", "num",
                 "Nothing can set it brighter than this.", "function"),
                (f"{n}.no_backstop", grp, f"Accept: {n} has no backstop", f"pcheck:{n}", "check",
                 "Dimmer countdowns are untested, so if the controller dies with the moon on, it stays on.",
                 "function"),
            ]
        elif role == "heater":
            out += [
                (f"{n}.power_on", grp, f"{n} comes back ON after a power cut", f"pcheck:{n}", "check",
                 "Set relay_status to power_on in this card, or in Smart Life. If it comes back off, "
                 "the heater has no power until someone notices.", "critical"),
                (f"{n}.revert_dp", grp, f"{n} countdown data point", f"pfield:{n}:revert_dp", "num",
                 "When the controller turns the gate off it arms this timer, so the plug turns itself "
                 "back on if the controller dies.", "critical"),
                (f"{n}.countdown_verified", grp, f"{n} countdown proven to switch it back ON",
                 "config:outputs.heater.countdown_verified", "bool",
                 "Run conductor.py verify-countdown --plug NAME --yes (the plug loses power for up to "
                 "the countdown length). Leave false until it passes.", "critical"),
                (f"{n}.inkbird_resume", grp, "Inkbird resumes heating after its power returns",
                 "config:outputs.heater.inkbird_resume_verified", "bool",
                 "Unplug and replug the Inkbird; check it heats again without a button press.",
                 "critical"),
            ]
    return out


def _dig(obj, dotted):
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None, False
        obj = obj[part]
    return obj, True


def _set(obj, dotted, value):
    parts = dotted.split(".")
    for part in parts[:-1]:
        obj = obj.setdefault(part, {})
    obj[parts[-1]] = value


def _defaults():
    import tank_tick
    return copy.deepcopy(tank_tick.DEFAULT_CONFIG)


def decisions(data) -> list:
    """Every decision with its current value, where it lives and whether a
    person has confirmed it (and whether it changed since)."""
    cfg = _read_json(os.path.join(data, "tank_config.json"), {}) or {}
    params = dict(cues.load_params(cues.active_params_path()))
    arr = (_read_json(_array_path(data), {}) or {}).get("plugs", [])
    dflt = _defaults()
    st = load_state(data)["decisions"]
    out = []
    ink = inkbird_in_use(data)
    hs = host_status(data)
    specs = [(*d, "critical" if d[0] in CRITICAL else "other") for d in DECISIONS
             if ink or d[0] not in INKBIRD_ONLY]
    specs += _plug_decisions(arr, cfg)
    cut = set(((cfg.get("outputs") or {}).get("cooling") or {}).get("plugs") or [])
    for did, group, label, where, typ, why, tier in specs:
        kind, _, path = where.partition(":")
        value, source = None, None
        if kind == "pfield":
            pn, _, field = path.partition(":")
            hit = [e for e in arr if e.get("name") == pn]
            value = hit[0].get(field) if hit else None
            source = f"plug_array.json ({pn})"
        elif kind == "derived":
            value = path.partition(":")[0] in cut
            source = "tank_config.json outputs.cooling.plugs"
        if kind == "config":
            value, found = _dig(cfg, path)
            source = "tank_config.json"
            if not found:
                value, found = _dig(dflt, path)
                source = "built-in default (not in tank_config.json)" if found else "not set"
        elif kind == "params":
            value, _ = _dig(params, path)
            if typ == "watch" and value:
                value = [value.get("days_after_full_min"), value.get("days_after_full_max"),
                         f"{value.get('season_start')}..{value.get('season_end')}"]
            source = os.path.basename(cues.active_params_path())
        elif kind == "plug":
            role, _, field = path.partition(":")
            hit = [e for e in arr if e.get("role") == role]
            value = hit[0].get(field) if hit else None
            source = f"plug_array.json ({hit[0]['name']})" if hit else f"no plug with role {role}"
        elif kind == "env":
            helper = {"TELEGRAM_BOT_TOKEN": (hs.get("telegram") or {}).get("configured"),
                      "TANK_DEADMAN_URL": (hs.get("deadman") or {}).get("configured")}.get(path)
            value = bool(os.environ.get(path) or helper)
            source = "environment (.env)" + (" (set by sh scripts/start.sh)" if helper and not os.environ.get(path) else "")
        rec = st.get(did)
        stale = bool(rec) and kind not in ("check", "pcheck") and rec.get("value") != value
        if kind in ("check", "pcheck") and rec and rec.get("value") is not True:
            rec = {**rec, "confirmed_utc": None}       # "not yet" is not a yes
        tech = label
        if did in UPFRONT:
            label, why = UPFRONT[did]
        out.append({"id": did, "group": group, "label": label, "where": where, "type": typ,
                    "why": why, "value": value, "source": source, "tier": tier, "tech_label": tech,
                    "plug": path.partition(":")[0] if kind in ("pfield", "pcheck", "derived") else None,
                    "editable": kind in ("config", "plug", "pfield"),
                    "confirmed": rec and not stale and rec.get("confirmed_utc"),
                    "confirmed_value": rec and rec.get("value"),
                    "changed_since_confirm": stale})
    return out


def preflight(data) -> dict:
    """The lifeguard's go / no-go list for this data folder, worked out now. A
    dry pass: nothing is latched, switched, alerted or written."""
    import lifeguard
    lg = lifeguard.Lifeguard(data, os.path.join(data, "tank_config.json"))
    st = lg.step(dry=True)
    return {"class": st["class"], "layers": st["layers"], "safe_mode": st["safe_mode"],
            "hard_c": st["hard_c"]}


def _coerce(typ, raw):
    if typ == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if typ == "num":
        v = float(raw)
        return int(v) if v.is_integer() and "." not in str(raw) else v
    if typ == "num_or_text":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return str(raw).strip()
    if typ in ("pair", "list"):
        if isinstance(raw, str):
            raw = json.loads(raw) if raw.strip().startswith("[") else [
                x.strip() for x in raw.split(",") if x.strip()]
        if not isinstance(raw, list):
            raise SetupError("expected a list")
        vals = []
        for x in raw:
            try:
                vals.append(float(x))
            except (TypeError, ValueError):
                vals.append(str(x))
        if typ == "pair" and len(vals) != 2:
            raise SetupError("expected two values")
        return vals
    return str(raw).strip()


def confirm_decision(data, did: str, value=None, change: bool = False,
                     answer: bool | None = None) -> dict:
    """Record a person's confirmation. With change, write the new value first
    (tank_config.json is validated by tank_tick.load_config before it is
    replaced). For a 'check', answer is yes/no and is what gets recorded."""
    row = next((d for d in decisions(data) if d["id"] == did), None)
    if row is None:
        raise SetupError(f"no decision {did!r}")
    kind, _, path = row["where"].partition(":")
    new = row["value"]
    if change:
        if not row["editable"]:
            raise SetupError(f"{row['label']} is not changed here"
                             + (" (step 1 sets it and regenerates the calendar)"
                                if kind == "params" else ""))
        new = _coerce(row["type"], value)
        if kind == "config":
            _write_config(data, path, new)
        else:
            key, _, field = path.partition(":")
            arr = _read_json(_array_path(data), {}) or {}
            by = "name" if kind == "pfield" else "role"
            hit = [e for e in arr.get("plugs", []) if e.get(by) == key]
            if not hit:
                raise SetupError(f"no plug with {by} {key}")
            hit[0][field] = new
            res = save_array(data, arr["plugs"])
            if not res.get("ok"):
                raise SetupError(res.get("why") or "plug array not saved")
    if kind in ("check", "pcheck"):
        if answer is None:
            raise SetupError("answer yes or no")
        new = bool(answer)
    st = load_state(data)
    st["decisions"][did] = {"value": new, "confirmed_utc": now_utc(),
                            "changed": bool(change), "was": row["value"] if change else None}
    save_state(data, st)
    return {"ok": True, "id": did, "value": new}


def _write_config(data, path, value):
    import tank_tick
    cfg_path = os.path.join(data, "tank_config.json")
    cfg = _read_json(cfg_path, {}) or {}
    _set(cfg, path, value)
    tmp = cfg_path + ".check"
    _write_json(tmp, cfg)
    try:
        old = os.environ.get("TANK_DATA")
        os.environ["TANK_DATA"] = data
        tank_tick.load_config(tmp)
    except (ValueError, TypeError, KeyError) as err:
        os.remove(tmp)
        raise SetupError(f"tank_config.json would be invalid: {err}") from None
    finally:
        if old is None:
            os.environ.pop("TANK_DATA", None)
        else:
            os.environ["TANK_DATA"] = old
    os.remove(tmp)
    backup(cfg_path)
    _write_json(cfg_path, cfg)


# ---------------------------------------------------------------------------
# OVERVIEW
# ---------------------------------------------------------------------------
def followed_today(data) -> dict | None:
    """The calendar the conductor follows now (setup.json pin, else the newest
    by name: tank_plan.find_calendar) and its row for today's tank date, so the
    page can say how far a new calendar's day 1 is from today's target. Read
    only; None when there is no calendar or no row for today."""
    import tank_plan                     # lazy: only the overview needs it
    os.environ["TANK_DATA"] = data
    path = tank_plan.find_calendar()
    if not path or not os.path.exists(path):
        return None
    try:
        tz = tank_plan.tank_tz(cues.load_params(cues.active_params_path()))
        today = dt.datetime.now(UTC).astimezone(tz).date().isoformat()
    except Exception:  # noqa: BLE001 - fall back to the host's date
        today = dt.date.today().isoformat()
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            row = next((r for r in csv.DictReader(fh) if r.get("date") == today), None)
    except OSError:
        return None
    out = {"file": os.path.basename(path), "date": today}
    if row:
        out.update({"cue_date": row.get("cue_date"), "target_c": _float(row.get("target_c")),
                    "watch_season": row.get("watch_season") == "true"})
    return out


def overview(data) -> dict:
    st = load_state(data)
    dec = decisions(data)
    cal = st.get("calendar")
    plugs = (_read_json(_array_path(data), {}) or {}).get("plugs", [])
    itc = st["itc"]["checklist"]
    return {
        "data": data, "staging": staging(),
        "calendar": cal, "active_params": os.path.basename(cues.active_params_path()),
        "calendars": sorted(os.path.basename(p) for p in glob.glob(os.path.join(data, "spawn_calendar_*.csv"))),
        "today": followed_today(data),
        "plugs": {"count": len(plugs), "roles": {p["name"]: p.get("role") for p in plugs},
                  "saved": st["devices"].get("array_saved_utc")},
        "itc": ({"done": sum(1 for v in itc.values() if v), "of": len(ITC_CHECKLIST), "mode": "inkbird"}
                if inkbird_in_use(data) else
                {"done": sum(1 for x in heater_status(data)["steps"] if x["done"]), "of": len(HEATER_STEPS),
                 "mode": "generic"}),
        "decisions": {"confirmed": sum(1 for d in dec if d["confirmed"]), "of": len(dec),
                      "changed_since": sum(1 for d in dec if d["changed_since_confirm"]),
                      "critical": {"confirmed": sum(1 for d in dec if d["tier"] == "critical" and d["confirmed"]),
                                   "of": sum(1 for d in dec if d["tier"] == "critical")}},
        "telegram": {"done": sum(1 for v in st.get("telegram", {}).values() if isinstance(v, str)),
                     "of": len(TG_STEPS)},
    }


# ---------------------------------------------------------------------------
# STAGING SEED
# ---------------------------------------------------------------------------
SEED_FILES = ["tank_config.json", "arc_catalog.json", "arc_selection.json",
              "spawn_calendar_2026-11-01.csv", "spawn_calendar_2026-11-01.meta.json",
              "itc/service.json"]


def seed_staging(src: str, dst: str) -> list:
    """Copy an allowlist of the live data into a staging data dir, with every
    plug turned into a fake (no keys, no IPs that a write could reach)."""
    if os.path.abspath(src) == os.path.abspath(dst):
        raise SetupError("staging must be a different folder")
    done = []
    for rel in SEED_FILES:
        s = os.path.join(src, rel)
        if not os.path.exists(s) and rel == "tank_config.json":
            s = os.path.join(_HERE, "tank_config.example.json")   # fresh clone: no live config yet
        if os.path.exists(s):
            os.makedirs(os.path.dirname(os.path.join(dst, rel)), exist_ok=True)
            shutil.copy2(s, os.path.join(dst, rel))
            done.append(rel)
    loc = os.path.join(src, "locales")
    if os.path.isdir(loc):
        shutil.copytree(loc, os.path.join(dst, "locales"), dirs_exist_ok=True)
        done.append("locales/")
    ref = os.path.join(src, "reference")      # imported Okinawa reference params (cue_import.py)
    if os.path.isdir(ref):
        shutil.copytree(ref, os.path.join(dst, "reference"), dirs_exist_ok=True)
        done.append("reference/")
    feed = os.path.join(src, "sensors", "itc306a_docker.jsonl")
    if os.path.exists(feed):
        os.makedirs(os.path.join(dst, "sensors"), exist_ok=True)
        with open(feed, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 20000))
            tail = fh.read().split(b"\n", 1)[-1]
        with open(os.path.join(dst, "sensors", "itc306a_docker.jsonl"), "wb") as fh:
            fh.write(tail)
        done.append("sensors/itc306a_docker.jsonl (tail)")
    arr = _read_json(os.path.join(src, "tuya", "plug_array.json"), {}) or {}
    if not arr.get("plugs"):   # fresh clone: data/tuya is gitignored, use the shipped example
        arr = _read_json(os.path.join(_HERE, "plug_array.example.json"), {}) or {}
    fake = []
    for e in arr.get("plugs", []):
        f = {k: v for k, v in e.items() if k not in ("ip", "version", "power_dp",
                                                      "revert_dp")}
        f["driver"] = "fake_dimmer" if e.get("driver") == "tinytuya_dimmer" else "fake"
        f["on"] = False
        fake.append(f)
    os.makedirs(os.path.join(dst, "tuya"), exist_ok=True)
    _write_json(os.path.join(dst, "tuya", "plug_array.json"), {"plugs": fake})
    devs = _read_json(os.path.join(src, "tuya", "devices.json"), [])
    if isinstance(devs, list) and devs:
        _write_json(os.path.join(dst, "tuya", "devices.json"),
                    [{k: v for k, v in d.items() if k != "key"} for d in devs])
        done.append(f"tuya/devices.json ({len(devs)} devices, local keys removed)")
    done.append(f"tuya/plug_array.json ({len(fake)} plugs, all fake)")
    return done


def sandbox_config(dst: str) -> list:
    """Point a seeded copy at the dummy feed (sandbox_feed.py) so the sandbox
    conductor can run its full loop on fake plugs: inputs read the dummy jsonl
    files, the boot-time plug scan is off (fakes have no IP), and the Inkbird
    write path and watch are off (no emulator in the sandbox)."""
    path = os.path.join(dst, "tank_config.json")
    cfg = _read_json(path, {}) or {}
    feed = "/data/sensors/sandbox_{}.jsonl"
    cfg["inputs"] = {
        "temp": {"driver": "jsonl", "path": feed.format("temp"), "field": "temp_c",
                 "max_age_s": 300, "skip_errors": True, "label": "sandbox Inkbird"},
        "probe_temp": {"driver": "jsonl", "path": feed.format("probe"), "field": "temp_c",
                       "max_age_s": 300, "label": "sandbox probe"},
        "lux": {"driver": "jsonl", "path": feed.format("lux"), "field": "lux",
                "max_age_s": 300},
    }
    cfg["plug_scan"] = {"enabled": False, "timeout_s": 15.0, "heal": False}
    cfg.setdefault("itc_setpoints", {})["enabled"] = False
    cfg.setdefault("itc_watch", {})["enabled"] = False
    notes = ["tank_config.json: inputs -> sandbox feed, plug scan off, Inkbird writes off"]
    # Sandbox only: the fakes must be switched, or there is nothing to watch. Roles are
    # given to fake plugs that have none; outputs go on for the lighting channels.
    pa = os.path.join(dst, "tuya", "plug_array.json")
    arr = _read_json(pa, {}) or {}
    plugs = arr.get("plugs", [])
    if plugs and not any(e.get("role") in LIGHT_ROLES for e in plugs):
        free = [e for e in plugs if e.get("role") == "unassigned"]
        for e, role in zip(free, LIGHT_ROLES):
            e["role"] = role
        _write_json(pa, arr)
        notes.append("tuya/plug_array.json: fake roles " + ", ".join(
            f"{e['name']}={e['role']}" for e in free[:len(LIGHT_ROLES)]))
    out = cfg.setdefault("outputs", {})
    out["enabled"] = True
    ch = out.setdefault("channels", {})
    for r in LIGHT_ROLES:
        ch[r] = True
    notes.append("tank_config.json: outputs on for the fake lamps (sandbox only)")
    _write_json(path, cfg)
    open(os.path.join(dst, "SANDBOX"), "w").write(
        "sandbox data: fake plugs and dummy sensors (sandbox_feed.py)\n")
    return notes


SCENARIO_FILE = os.path.join(_HERE, "sandbox_scenarios.json")
FAKE_STATE_KEYS = ("ok", "stuck", "fail_set", "on", "power_w", "expect_w",
                   "revert_s", "off_s", "level")


def list_scenarios() -> dict:
    return {k: v.get("desc", "") for k, v in
            (_read_json(SCENARIO_FILE, {}) or {}).get("scenarios", {}).items()}


def apply_scenario(dst: str, name: str) -> list:
    """Reset every fake in <dst>/tuya/plug_array.json to a clean state, then
    apply one scenario. Refuses unless <dst> is a sandbox copy (SANDBOX file)
    and every plug in it is a fake."""
    if not os.path.exists(os.path.join(dst, "SANDBOX")):
        raise SetupError(f"{dst} is not a sandbox copy (seed it with --sandbox)")
    sc = (_read_json(SCENARIO_FILE, {}) or {}).get("scenarios", {}).get(name)
    if sc is None:
        raise SetupError(f"no scenario {name!r}; have {sorted(list_scenarios())}")
    path = os.path.join(dst, "tuya", "plug_array.json")
    arr = _read_json(path, {}) or {}
    plugs = []
    for e in arr.get("plugs", []):
        if e.get("driver") not in ("fake", "fake_dimmer"):
            raise SetupError(f"{e.get('name')} is not a fake plug; refusing")
        if e.get("_scenario_added"):
            continue
        clean = {k: v for k, v in e.items() if k not in FAKE_STATE_KEYS}
        clean["on"] = False
        plugs.append(clean)
    done = []
    for p in plugs:
        extra = (sc.get("roles") or {}).get(p.get("role"))
        if extra:
            p.update(extra)
            done.append(f"{p['name']} ({p['role']}): {extra}")
    for add in sc.get("add") or []:
        plugs.append({**add, "_scenario_added": True})
        done.append(f"added {add['name']}: {add}")
    _write_json(path, {"plugs": plugs})
    st = load_state(dst)
    st["sandbox_scenario"] = {"name": name, "ts": now_utc()}
    save_state(dst, st)
    return done or ["all fakes reset to healthy"]
