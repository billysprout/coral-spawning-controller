#!/usr/bin/env python3
"""track_core.py -- data shaping for the tracking page (track_web.py).

Read-only on the data dir except for its own cache. Answers "where is the tank
on its cue table, and what did the water and the lamps actually do":

    summary(data)          header: calendar, setup basis, cue date, countdowns
    year(data)             one row per calendar day: plan + daily actuals
    recent(data, days)     5-minute buckets: plan vs actual, lamps, events

Sources (all append-only JSONL, never loaded whole into the page):
    Inkbird   tank_config inputs.temp when it is a jsonl driver, else
              sensors/itc306a_docker.jsonl (temp_c)
    Pi probe  tank_inputs-YYYYMMDD.jsonl probe_c (the controller's value,
              probe offset already applied); also lux and per-plug on/off
    lamps     heat/heat_log-YYYYMMDD.jsonl `lights` (role-keyed: white,
              white_plus, blue, moon); tank_inputs plug states mapped to roles
              are the fallback for days without a heat log
    events    tank_commands.jsonl (commands, verify verdicts, overrides,
              over-temperature trips and releases)

Every source file is reduced to 5-minute buckets (count/sum/min/max for
numbers, true/total counts for on/off) and per-local-day aggregates. Both are
cached in <cache>/ (default <data>/track/, or TRACK_CACHE) keyed by the source
file's size and mtime; a file that only grew is read from where the cache
stopped. The cache can be deleted at any time.

Standard library plus cues.py and tank_plan.py.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import cues  # noqa: E402
import tank_plan  # noqa: E402

UTC = dt.timezone.utc
BUCKET_S = 300
CACHE_VERSION = 1
LIT_LUX = 20.0          # lux at the sensor counted as "lit" (moon lamp adds ~8)
LAMP_ROLES = ("white", "white_plus", "blue", "moon")
DAY_LAMPS = ("white", "white_plus", "blue")
RECENT_DAYS = (1, 3, 7)
MAX_EVENTS = 3000

_lock = threading.RLock()


class TrackError(Exception):
    pass


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def data_dir() -> str:
    return os.environ.get("TANK_DATA") or os.path.join(os.path.dirname(_HERE), "data")


def cache_dir(data: str) -> str:
    return os.environ.get("TRACK_CACHE") or os.path.join(data, "track")


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _epoch(ts) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.timestamp()


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _add_num(b, key, v):
    if v is None:
        return
    s = b.get(key)
    if s is None:
        b[key] = [1, v, v, v]
    else:
        s[0] += 1
        s[1] += v
        if v < s[2]:
            s[2] = v
        if v > s[3]:
            s[3] = v


def _add_flag(b, key, v):
    if v is None:
        return
    s = b.get(key)
    if s is None:
        b[key] = [1 if v else 0, 1]
    else:
        s[0] += 1 if v else 0
        s[1] += 1


def _merge_stat(into, key, s):
    cur = into.get(key)
    if cur is None:
        into[key] = list(s)
    elif len(s) == 4:
        cur[0] += s[0]
        cur[1] += s[1]
        cur[2] = min(cur[2], s[2])
        cur[3] = max(cur[3], s[3])
    else:
        cur[0] += s[0]
        cur[1] += s[1]


def _r(v, n=3):
    return None if v is None else round(v, n)


# ---------------------------------------------------------------------------
# extractors: one JSON record -> (epoch, {field: value}) contributions
# ---------------------------------------------------------------------------
def _x_temp(rec, b, roles):
    _add_num(b, "ink", _num(rec.get("temp_c")))


def _x_inputs(rec, b, roles):
    _add_num(b, "probe", _num(rec.get("probe_c")))
    _add_num(b, "ctl", _num(rec.get("temp_c")))
    lux = _num(rec.get("lux"))
    _add_num(b, "lux", lux)
    if lux is not None:
        _add_flag(b, "luxlit", lux >= LIT_LUX)
    for name, p in (rec.get("plugs") or {}).items():
        if isinstance(p, dict) and isinstance(p.get("on"), bool):
            _add_flag(b, "p:" + name, p["on"])


def _x_heat(rec, b, roles):
    lights = rec.get("lights") or {}
    seen = []
    for role in LAMP_ROLES:
        v = lights.get(role)
        if isinstance(v, bool):
            _add_flag(b, "L:" + role, v)
            if role in DAY_LAMPS:
                seen.append(v)
    if seen:
        _add_flag(b, "L:any", any(seen))
    for name, p in (rec.get("plugs") or {}).items():
        if isinstance(p, dict) and p.get("role"):
            roles[name] = p["role"]


EXTRACT = {"temp": _x_temp, "inputs": _x_inputs, "heat": _x_heat}


# ---------------------------------------------------------------------------
# cache: per source file, 5-min buckets + per-local-day aggregates
# ---------------------------------------------------------------------------
_daily_mem: dict = {}                    # path -> (key, days, roles)
_bucket_mem: "OrderedDict[str, dict]" = OrderedDict()
_BUCKET_MEM_MAX = 24
_warned = set()


def _cache_paths(cdir, kind, path):
    base = f"{kind}-{os.path.basename(path)}"
    return os.path.join(cdir, base + ".b5.json"), os.path.join(cdir, base + ".day.json")


def _write_cache(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as err:
        if path not in _warned:
            _warned.add(path)
            sys.stderr.write(f"track cache not written ({err}); keeping it in memory\n")


def _src_key(st):
    return [CACHE_VERSION, LIT_LUX, st.st_size, st.st_mtime_ns]


def _daily_from_buckets(buckets: dict, tz) -> dict:
    days: dict = {}
    for t, b in buckets.items():
        d = dt.datetime.fromtimestamp(int(t), tz).date().isoformat()
        dd = days.setdefault(d, {})
        for k, s in b.items():
            if k.startswith("p:"):
                continue
            if len(s) == 4:
                _merge_stat(dd, k, s)
            elif s[1]:
                # minutes lit = bucket length x the share of samples that were on
                _merge_stat(dd, k, [BUCKET_S / 60 * s[0] / s[1], BUCKET_S / 60])
    return days


def _load_buckets(cdir, kind, path, st, tz):
    """Buckets for one source file, updated incrementally. Returns the cache
    record {key, offset, buckets, roles}. Caller holds _lock."""
    bpath, dpath = _cache_paths(cdir, kind, path)
    rec = _bucket_mem.get(path)
    if rec is None:
        rec = _read_json(bpath)
    want = _src_key(st)
    if rec and rec.get("key") == want:
        _remember(path, rec)
        return rec, False
    with open(path, "rb") as fh:
        head = fh.read(128).hex()
        # incremental only when the file just grew: same version, same first
        # bytes, not shorter than what was read before
        ok = (rec and (rec.get("key") or [None, None])[:2] == want[:2]
              and rec.get("head") == head and rec.get("src") == path
              and st.st_size >= rec.get("offset", 0))
        if not ok:
            rec = {"key": None, "offset": 0, "buckets": {}, "roles": {}, "src": path,
                   "head": head}
        fh.seek(rec["offset"])
        chunk = fh.read()
    buckets, roles = rec["buckets"], rec["roles"]
    fn = EXTRACT[kind]
    end = chunk.rfind(b"\n")
    if end >= 0:
        for line in chunk[:end].split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            t = _epoch(r.get("ts"))
            if t is None:
                continue
            key = str(int(t // BUCKET_S * BUCKET_S))
            fn(r, buckets.setdefault(key, {}), roles)
        rec["offset"] += end + 1
    rec["key"] = want
    _remember(path, rec)
    _write_cache(bpath, rec)
    days = _daily_from_buckets(buckets, tz)
    _daily_mem[path] = (want, days, dict(roles))
    _write_cache(dpath, {"key": want, "days": days, "roles": roles})
    return rec, True


def _remember(path, rec):
    _bucket_mem[path] = rec
    _bucket_mem.move_to_end(path)
    while len(_bucket_mem) > _BUCKET_MEM_MAX:
        _bucket_mem.popitem(last=False)


def daily_for(cdir, kind, path, tz):
    """(days, roles) for a source file, from the cache when it is current."""
    try:
        st = os.stat(path)
    except OSError:
        return {}, {}
    want = _src_key(st)
    with _lock:
        mem = _daily_mem.get(path)
        if mem and mem[0] == want:
            return mem[1], mem[2]
        _, dpath = _cache_paths(cdir, kind, path)
        disk = _read_json(dpath)
        if disk and disk.get("key") == want:
            _daily_mem[path] = (want, disk.get("days") or {}, disk.get("roles") or {})
            return _daily_mem[path][1], _daily_mem[path][2]
        _load_buckets(cdir, kind, path, st, tz)
        mem = _daily_mem[path]
        return mem[1], mem[2]


def buckets_for(cdir, kind, path, tz):
    try:
        st = os.stat(path)
    except OSError:
        return {}, {}
    with _lock:
        rec, _ = _load_buckets(cdir, kind, path, st, tz)
        return rec["buckets"], rec["roles"]


# ---------------------------------------------------------------------------
# context: calendar, params, plan config, sources (reloaded on change)
# ---------------------------------------------------------------------------
_ctx_mem: dict = {}


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _config_path(data):
    return os.environ.get("TANK_CONFIG") or os.path.join(data, "tank_config.json")


def _map_data_path(data, p):
    """A config path written for the container (/data/...) on this data dir."""
    if not p:
        return None
    if p.startswith("/data/"):
        return os.path.join(data, *p[len("/data/"):].split("/"))
    return p if os.path.isabs(p) else os.path.join(data, p)


def context(data: str) -> dict:
    os.environ["TANK_DATA"] = data         # find_calendar / active_params_path read it
    cal_path = tank_plan.find_calendar()
    ppath = os.path.abspath(cues.active_params_path())
    cfg_path = _config_path(data)
    setup_path = os.path.join(data, "setup.json")
    key = (data, cal_path, _mtime(cal_path) if cal_path else None, ppath, _mtime(ppath),
           _mtime(cfg_path), _mtime(setup_path))
    with _lock:
        ctx = _ctx_mem.get(data)
        if ctx and ctx["key"] == key:
            return ctx
        cues._cache.pop(ppath, None)
        params = cues.load_params(ppath)
        notes = []
        cfg_raw = _read_json(cfg_path, {}) or {}
        try:
            plan_cfg = tank_plan.plan_config(cfg_raw.get("plan"))
        except ValueError as err:
            plan_cfg = tank_plan.plan_config()
            notes.append(f"tank_config plan section invalid ({err}); using defaults")
        cal = tank_plan.load_calendar(cal_path, params) if cal_path else None
        temp_in = (cfg_raw.get("inputs") or {}).get("temp") or {}
        ink = None
        if temp_in.get("driver") == "jsonl":
            ink = _map_data_path(data, temp_in.get("path"))
        if not ink or not os.path.exists(ink):
            ink = os.path.join(data, "sensors", "itc306a_docker.jsonl")
        ctx = {"key": key, "data": data, "cal_path": cal_path, "cal": cal,
               "params": params, "params_path": ppath, "plan_cfg": plan_cfg,
               "tz": tank_plan.tank_tz(params), "ink_path": ink,
               "ink_label": temp_in.get("label") or "Inkbird",
               "setup": _read_json(setup_path), "notes": notes,
               "cache": cache_dir(data)}
        _ctx_mem[data] = ctx
        return ctx


def _dated_files(data, pattern, regex):
    out = {}
    for p in glob.glob(os.path.join(data, pattern)):
        m = re.search(regex, os.path.basename(p))
        if m:
            out[m.group(1)] = p
    return out


def _inputs_files(data):
    return _dated_files(data, "tank_inputs-*.jsonl", r"tank_inputs-(\d{8})\.jsonl$")


def _heat_files(data):
    return _dated_files(os.path.join(data, "heat"), "heat_log-*.jsonl",
                        r"heat_log-(\d{8})\.jsonl$")


def _ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# summary (header)
# ---------------------------------------------------------------------------
def _now():
    return dt.datetime.now(UTC)


def summary(data: str) -> dict:
    ctx = context(data)
    cal, tz, params = ctx["cal"], ctx["tz"], ctx["params"]
    now = _now()
    today = now.astimezone(tz).date()
    out = {"now_utc": now.isoformat(timespec="seconds"),
           "now_local": now.astimezone(tz).isoformat(timespec="seconds"),
           "tz": str(tz), "today": today.isoformat(), "data_dir": data,
           "notes": list(ctx["notes"]), "params_file": os.path.basename(ctx["params_path"]),
           "sources": {"inkbird": os.path.relpath(ctx["ink_path"], data).replace(os.sep, "/"),
                       "inkbird_label": ctx["ink_label"],
                       "probe": "tank_inputs-*.jsonl probe_c",
                       "lamps": "heat/heat_log-*.jsonl lights (fallback: tank_inputs plugs)",
                       "lit_lux": LIT_LUX},
           "sandbox": os.path.exists(os.path.join(data, "SANDBOX"))}
    st = ctx["setup"] or {}
    calst = st.get("calendar") if isinstance(st, dict) else None
    if calst and calst.get("basis"):
        b = calst["basis"]
        out["setup"] = {"file": calst.get("file"), "locale": b.get("locale"),
                        "locale_name": b.get("locale_name"), "temp": b.get("temp"),
                        "genus": b.get("genus"), "species": b.get("species"),
                        "window": b.get("window"), "start": b.get("start"),
                        "start_mode": b.get("start_mode"), "start_value": b.get("start_value"),
                        "offset_days": b.get("offset_days"), "readiness": b.get("readiness"),
                        "caveats": b.get("caveats"), "confirmed_utc": calst.get("confirmed_utc")}
    else:
        out["setup"] = None
        out["setup_note"] = "calendar not set up via setup page"
    if cal is None:
        out["calendar"] = None
        out["notes"].append("no spawn calendar found in the data dir")
        return out
    out["calendar"] = {"file": os.path.basename(cal.path),
                       "first": cal.first_date.isoformat() if cal.first_date else None,
                       "last": cal.last_date.isoformat() if cal.last_date else None,
                       "days": len(cal.rows), "problems": cal.problems,
                       "pinned": bool(calst and calst.get("file") == os.path.basename(cal.path))}
    row = cal.rows.get(today.isoformat())
    start = None
    if out["setup"] and out["setup"].get("start"):
        try:
            start = dt.date.fromisoformat(out["setup"]["start"])
        except ValueError:
            start = None
    start = start or cal.first_date
    out["programme_start"] = start.isoformat() if start else None
    out["days_into_programme"] = (today - start).days if start else None
    out["days_to_calendar_end"] = cal.days_left(today)
    try:
        out["params_anchor"] = cues.anchor_date(params).isoformat()
    except (KeyError, IndexError, ValueError):
        out["params_anchor"] = None
    if row is None:
        out["today_row"] = None
        out["notes"].append(f"today ({today}) is outside the calendar")
    else:
        out["today_row"] = {"cue_date": row.get("cue_date"),
                            "offset_days": _num_s(row.get("offset_days")),
                            "target_c": _num_s(row.get("target_c")),
                            "raw_c": _num_s(row.get("raw_c")),
                            "clamped": row.get("clamped") == "true",
                            "photoperiod_min": _num_s(row.get("photoperiod_min")),
                            "moon_illum_pct": _num_s(row.get("moon_illum_pct")),
                            "watch_season": row.get("watch_season") == "true",
                            "watch": row.get("watch") == "true"}
    # next watch night (0 = tonight, unless tonight's window is already over)
    nxt, season = None, None
    for d in sorted(cal.rows):
        if d < today.isoformat():
            continue
        r = cal.rows[d]
        if season is None and r.get("watch_season") == "true":
            season = d
        if r.get("watch") == "true":
            end = tank_plan._parse_utc(r.get("watch_end_utc"))
            if d == today.isoformat() and end is not None and end <= now:
                continue
            nxt = d
            break
    if nxt:
        r = cal.rows[nxt]
        out["next_watch"] = {"date": nxt, "days": (dt.date.fromisoformat(nxt) - today).days,
                             "start_local": r.get("watch_start_local"),
                             "end_local": r.get("watch_end_local"),
                             "cue_date": r.get("cue_date"),
                             "days_after_full": _num_s(r.get("days_after_full"))}
    else:
        out["next_watch"] = None
    out["next_watch_season"] = ({"date": season,
                                 "days": (dt.date.fromisoformat(season) - today).days}
                                if season else None)
    p = tank_plan.plan_at(cal, ctx["plan_cfg"], now, params)
    out["plan_now"] = p.as_dict() if p else None
    out["status"] = _status_brief(data, now)
    out["life"] = _life_brief(data, now)
    return out


def _num_s(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _status_brief(data, now):
    s = _read_json(os.path.join(data, "tank_status.json"))
    if not isinstance(s, dict):
        return None
    t = _epoch(s.get("ts"))
    inp = s.get("inputs") or {}
    plugs = {n: {"on": p.get("on"), "ok": p.get("ok"), "role": p.get("role"),
                 "channel": p.get("channel"), "level": p.get("level")}
             for n, p in (s.get("plugs") or {}).items() if isinstance(p, dict)}
    cool = s.get("cooling") or {}
    return {"ts": s.get("ts"), "age_s": None if t is None else int(now.timestamp() - t),
            "temp_c": (inp.get("temp") or {}).get("value"),
            "probe_c": (inp.get("probe_temp") or {}).get("value"),
            "lux": (inp.get("lux") or {}).get("value"),
            "temp_error_c": s.get("temp_error_c"), "plugs": plugs,
            "cutoff_c": cool.get("cutoff_c"), "cooling_armed": cool.get("armed"),
            "tripped": sorted(n for n, v in (cool.get("plugs") or {}).items()
                              if isinstance(v, dict) and v.get("tripped")),
            "redundancy": s.get("redundancy"), "safe_mode": s.get("safe_mode"),
            "findings": [{"level": f.get("level"), "message": f.get("message")}
                         for f in (s.get("findings") or [])[:12] if isinstance(f, dict)]}


def _life_brief(data, now):
    """The lifeguard's last status (lifeguard.py), or None when it never ran here."""
    path = os.path.join(data, "lifeguard", "status.json")
    s = _read_json(path)
    if not isinstance(s, dict):
        return None
    t = _epoch(s.get("ts"))
    return {"age_s": None if t is None else int(now.timestamp() - t),
            "class": s.get("class"), "enabled": s.get("enabled"),
            "conditions": [c for c in (s.get("conditions") or []) if isinstance(c, dict)][:20],
            "layers": [x for x in (s.get("layers") or []) if isinstance(x, dict)],
            "safe_mode": s.get("safe_mode") or {}, "hard_c": s.get("hard_c"),
            "temperature": s.get("temperature") or {},
            "controller": s.get("controller") or {}}


# ---------------------------------------------------------------------------
# year view
# ---------------------------------------------------------------------------
def _day_stats(dd, key):
    s = dd.get(key)
    if not s or not s[0]:
        return None
    return [_r(s[1] / s[0]), _r(s[2]), _r(s[3]), s[0]]


def year(data: str) -> dict:
    ctx = context(data)
    cal, tz, cdir = ctx["cal"], ctx["tz"], ctx["cache"]
    if cal is None:
        raise TrackError("no spawn calendar found in the data dir")
    t0 = time.monotonic()
    dates = sorted(cal.rows)
    lo = cal.first_date - dt.timedelta(days=1)
    hi = cal.last_date + dt.timedelta(days=1)
    actual: dict = {}

    def merge(days):
        for d, dd in days.items():
            if dates[0] <= d <= dates[-1]:
                tgt = actual.setdefault(d, {})
                for k, s in dd.items():
                    _merge_stat(tgt, k, s)

    ink_days, _ = daily_for(cdir, "temp", ctx["ink_path"], tz)
    merge(ink_days)
    for ymd, path in sorted(_inputs_files(data).items()):
        if _ymd(lo) <= ymd <= _ymd(hi):
            merge(daily_for(cdir, "inputs", path, tz)[0])
    for ymd, path in sorted(_heat_files(data).items()):
        if _ymd(lo) <= ymd <= _ymd(hi):
            merge(daily_for(cdir, "heat", path, tz)[0])

    cols = {k: [] for k in ("date", "cue_date", "offset_days", "target_c", "raw_c",
                            "clamped", "photoperiod_min", "moon_illum_pct",
                            "days_after_full", "watch_season", "watch",
                            "ink", "probe", "lit_lux_min", "lux_cov_min",
                            "lit_plug_min", "plug_cov_min")}
    for d in dates:
        r = cal.rows[d]
        a = actual.get(d, {})
        cols["date"].append(d)
        cols["cue_date"].append(r.get("cue_date"))
        cols["offset_days"].append(_num_s(r.get("offset_days")))
        cols["target_c"].append(_num_s(r.get("target_c")))
        cols["raw_c"].append(_num_s(r.get("raw_c")))
        cols["clamped"].append(r.get("clamped") == "true")
        cols["photoperiod_min"].append(_num_s(r.get("photoperiod_min")))
        cols["moon_illum_pct"].append(_num_s(r.get("moon_illum_pct")))
        cols["days_after_full"].append(_num_s(r.get("days_after_full")))
        cols["watch_season"].append(r.get("watch_season") == "true")
        cols["watch"].append(r.get("watch") == "true")
        cols["ink"].append(_day_stats(a, "ink"))
        cols["probe"].append(_day_stats(a, "probe"))
        lx, pl = a.get("luxlit"), a.get("L:any")
        cols["lit_lux_min"].append(_r(lx[0], 0) if lx else None)
        cols["lux_cov_min"].append(_r(lx[1], 0) if lx else None)
        cols["lit_plug_min"].append(_r(pl[0], 0) if pl else None)
        cols["plug_cov_min"].append(_r(pl[1], 0) if pl else None)
    band = cues.safety_band_c(ctx["params"])
    return {"today": _now().astimezone(tz).date().isoformat(), "rows": cols,
            "safety_band_c": list(band), "lit_lux": LIT_LUX,
            "calendar": os.path.basename(cal.path),
            "elapsed_ms": int((time.monotonic() - t0) * 1000)}


# ---------------------------------------------------------------------------
# recent view
# ---------------------------------------------------------------------------
def _role_map(data, roles_seen):
    """plug name -> role: roles recorded in heat logs (time-correct), then the
    current status and plug_array for names the heat logs never saw."""
    out = {}
    arr = _read_json(os.path.join(data, "tuya", "plug_array.json"), {}) or {}
    for p in arr.get("plugs") or []:
        if isinstance(p, dict) and p.get("name") and p.get("role"):
            out[p["name"]] = p["role"]
    s = _read_json(os.path.join(data, "tank_status.json"), {}) or {}
    for n, p in (s.get("plugs") or {}).items():
        if isinstance(p, dict) and p.get("role"):
            out[n] = p["role"]
    out.update(roles_seen)
    return out


def _events(data, t0, t1):
    path = os.path.join(data, "tank_commands.jsonl")
    out = []
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return out
    with fh:
        for line in fh:
            if '"ts"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = _epoch(r.get("ts")) if isinstance(r, dict) else None
            if t is None or not t0 <= t <= t1:
                continue
            ev = r.get("event")
            reason = r.get("reason")
            if ev is None:
                kind = "command"
                if reason in ("overtemp", "overtemp_resume"):
                    kind = "cutoff"
                elif reason == "dark_protect":
                    kind = "dark_protect"
            elif ev in ("overtemp_trip", "overtemp_release"):
                kind = "cutoff"
            else:
                kind = ev
            e = {"t": int(t), "kind": kind, "plug": r.get("plug")}
            for k in ("event", "reason", "desired_on", "executed", "verified", "observed_on",
                      "error", "verdict", "probe_c", "cutoff_c", "desired_level",
                      "observed_level", "was_on", "resumed"):
                if r.get(k) is not None:
                    e[k] = r[k]
            out.append(e)
    return out[-MAX_EVENTS:]


def recent(data: str, days: int) -> dict:
    if days not in RECENT_DAYS:
        raise TrackError(f"days must be one of {RECENT_DAYS}")
    ctx = context(data)
    cal, tz, cdir, cfg, params = ctx["cal"], ctx["tz"], ctx["cache"], ctx["plan_cfg"], ctx["params"]
    t_start = time.monotonic()
    now = _now().timestamp()
    t1 = int(now // BUCKET_S * BUCKET_S)
    t0 = t1 - days * 86400 + BUCKET_S
    d0 = dt.datetime.fromtimestamp(t0, UTC).date()
    d1 = dt.datetime.fromtimestamp(t1, UTC).date()
    want = {_ymd(d0 + dt.timedelta(days=i)) for i in range((d1 - d0).days + 1)}

    ink, _ = buckets_for(cdir, "temp", ctx["ink_path"], tz)
    inp, heat, roles_seen = {}, {}, {}
    for ymd, path in _inputs_files(data).items():
        if ymd in want:
            b, _ = buckets_for(cdir, "inputs", path, tz)
            inp.update(b)
    for ymd, path in sorted(_heat_files(data).items()):
        if ymd in want:
            b, roles = buckets_for(cdir, "heat", path, tz)
            heat.update(b)
            roles_seen.update(roles)
    roles = _role_map(data, roles_seen)

    keys = ("t", "plan_c", "plan_day_c", "ink_c", "probe_c", "probe_min", "probe_max",
            "lux", "lux_max", "full_dark", "any_sun", "any_moon", "watch",
            *(f"plan_{r}" for r in LAMP_ROLES), *(f"lamp_{r}" for r in LAMP_ROLES))
    cols = {k: [] for k in keys}

    def mean(b, k):
        s = b.get(k) if b else None
        return _r(s[1] / s[0]) if s and s[0] else None

    for t in range(t0, t1 + 1, BUCKET_S):
        k = str(t)
        cols["t"].append(t)
        p = tank_plan.plan_at(cal, cfg, dt.datetime.fromtimestamp(t, UTC), params) if cal else None
        cols["plan_c"].append(p.temp_hourly_c if p else None)
        cols["plan_day_c"].append(p.temp_daily_c if p else None)
        for f in ("full_dark", "any_sun", "any_moon", "watch"):
            cols[f].append(None if p is None else int(getattr(p, f)))
        for r in LAMP_ROLES:
            cols[f"plan_{r}"].append(None if p is None else int(getattr(p, f"{r}_on")))
        cols["ink_c"].append(mean(ink.get(k), "ink"))
        ib = inp.get(k)
        cols["probe_c"].append(mean(ib, "probe"))
        ps = ib.get("probe") if ib else None
        cols["probe_min"].append(_r(ps[2]) if ps else None)
        cols["probe_max"].append(_r(ps[3]) if ps else None)
        cols["lux"].append(mean(ib, "lux"))
        ls = ib.get("lux") if ib else None
        cols["lux_max"].append(_r(ls[3], 2) if ls else None)
        hb = heat.get(k)
        for r in LAMP_ROLES:
            s = hb.get("L:" + r) if hb else None
            v = _r(s[0] / s[1], 2) if s and s[1] else None
            if v is None and ib:
                fr = [ps_[0] / ps_[1] for n, ps_ in ib.items()
                      if n.startswith("p:") and ps_[1] and roles.get(n[2:]) == r]
                v = _r(max(fr), 2) if fr else None
            cols[f"lamp_{r}"].append(v)
    return {"days": days, "bucket_s": BUCKET_S, "t0": t0, "t1": t1, "tz": str(tz),
            "cols": cols, "events": _events(data, t0, t1 + BUCKET_S),
            "roles": {n: r for n, r in sorted(roles.items())},
            "safety_band_c": list(cues.safety_band_c(params)),
            "lit_lux": LIT_LUX, "ink_label": ctx["ink_label"],
            "elapsed_ms": int((time.monotonic() - t_start) * 1000)}
