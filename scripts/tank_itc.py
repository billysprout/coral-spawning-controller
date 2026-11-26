#!/usr/bin/env python3
"""tank_itc.py -- cue side of the Inkbird ITC-306A setpoint changes.

The plan's temperature cue (tank_plan.Plan.temp_hourly_c, what the tank should be
at right now) decides where the Inkbird's two settings should sit:

    T1 (floor, heat on below it)    = target - below_f
    T2 (ceiling, heat off above it) = target + above_f      (Fahrenheit, as the app)

When the wanted band has moved far enough from what is already on the device, this
writes a request file, <data>/itc/target.json. It touches no phone. The process
that owns the Android phone, itc_setpoints.py, applies the request and records
what happened (<data>/itc/applied.json, result.json), which this reads back so
the tank status shows requested / applied / pending.

    {"seq": 4, "ts": "...", "floor_f": 76.0, "ceil_f": 76.5, "target_c": 24.58,
     "cue": {"tank_date": "...", "local": "...", "temp_hourly_c": 24.58}}

Rules
  * Off unless itc_setpoints.enabled. Nothing is written otherwise.
  * Never ask for a band outside itc_setpoints.bounds_f (default 73 to 83 F, the
    Inkbird's own alarm band), and never closer than below_f + above_f apart.
  * A new request only when the wanted band differs from the applied one by
    min_change_f or more, and at most every min_interval_s. A request that has
    been waiting that long is written again with a fresh time, because the
    applier rejects a stale one.
  * A request still unapplied after pending_alert_min minutes, or a result that
    failed for good, is a finding.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os

import itc_setpoints

UTC = dt.timezone.utc

DEFAULT_ITC = {
    "enabled": False,
    "below_f": 0.2,
    "above_f": 0.3,
    "bounds_f": list(itc_setpoints.DEFAULTS["bounds_f"]),
    "min_change_f": 0.5,
    "min_interval_s": 3600,
    "pending_alert_min": 45,
}


def itc_config(overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_ITC)
    cfg.update(overrides or {})
    for k in ("below_f", "above_f"):
        if not 0.1 <= float(cfg[k]) <= 5.0:
            raise ValueError(f"itc_setpoints.{k} must be 0.1..5")
    lo, hi = cfg["bounds_f"]
    if not 30 <= float(lo) < float(hi) <= 120:
        raise ValueError("itc_setpoints.bounds_f must be two temperatures 30..120")
    if not 0.1 <= float(cfg["min_change_f"]) <= 5.0:
        raise ValueError("itc_setpoints.min_change_f must be 0.1..5")
    if not 60 <= float(cfg["min_interval_s"]) <= 86400:
        raise ValueError("itc_setpoints.min_interval_s must be 60..86400")
    if float(cfg["pending_alert_min"]) < 5:
        raise ValueError("itc_setpoints.pending_alert_min must be at least 5")
    return cfg


def band_for(target_c: float, cfg: dict) -> tuple[float, float]:
    """(floor_f, ceil_f) in tenths of a degree F for a target in C, inside the
    bounds and at least below_f + above_f apart."""
    return itc_setpoints.band_f(itc_setpoints.c_to_f(target_c), cfg["below_f"],
                                cfg["above_f"], cfg["bounds_f"])


def wants_request(want: tuple[float, float], applied: dict | None,
                  request: dict | None, now: dt.datetime, cfg: dict) -> str | None:
    """Why a new request should be written now, or None."""
    step = float(cfg["min_change_f"])
    if applied is not None and (abs(want[0] - float(applied["floor_f"])) < step
                                and abs(want[1] - float(applied["ceil_f"])) < step):
        return None
    if request is None:
        return "first_request"
    age = (now - dt.datetime.fromisoformat(request["ts"])).total_seconds()
    if age < float(cfg["min_interval_s"]):
        return None
    differs = (abs(want[0] - float(request["floor_f"])) >= step
               or abs(want[1] - float(request["ceil_f"])) >= step)
    return "cue_moved" if differs else "refresh_unapplied"


def _read(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


class ItcCue:
    def __init__(self, cfg: dict | None, folder: str | None):
        self.cfg = itc_config(cfg)
        self.folder = os.path.join(folder, "itc") if folder else None

    def _write(self, req: dict) -> None:
        os.makedirs(self.folder, exist_ok=True)
        path = os.path.join(self.folder, "target.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(req, fh, indent=2)
        os.replace(tmp, path)

    def step(self, now: dt.datetime, plan) -> tuple[dict, list[dict]]:
        """(view for the status record, findings)."""
        c = self.cfg
        if not c["enabled"]:
            return {"enabled": False}, []
        if self.folder is None or plan is None:
            return {"enabled": True, "idle": "no data folder" if plan else "no plan"}, []
        hold = _read(os.path.join(self.folder, "hold.json"))
        if hold:                    # a reversal asked the cue side to stay quiet
            try:
                if dt.datetime.fromisoformat(hold["until"]) > now:
                    return {"enabled": True, "hold": hold.get("reason"),
                            "until": hold["until"]}, []
            except (KeyError, ValueError, TypeError):
                pass
        want = band_for(plan.temp_hourly_c, c)
        request = _read(os.path.join(self.folder, "target.json"))
        applied = _read(os.path.join(self.folder, "applied.json"))
        result = _read(os.path.join(self.folder, "result.json"))
        view = {"enabled": True, "want_f": list(want),
                "requested": request and {k: request.get(k) for k in
                                          ("seq", "ts", "floor_f", "ceil_f")},
                "applied": applied and {k: applied.get(k) for k in
                                        ("seq", "ts", "floor_f", "ceil_f")},
                "last_result": result and {k: result.get(k) for k in
                                           ("ts", "seq", "status", "retryable")},
                "wrote": None}
        findings = []
        try:
            why = wants_request(want, applied, request, now, c)
        except (ValueError, KeyError, TypeError):      # a damaged file: start over
            why = "request_unreadable"
        if why:
            seq = int((request or {}).get("seq", 0) or 0)
            seq = max(seq, int((applied or {}).get("seq", 0) or 0)) + 1
            req = {"seq": seq, "ts": now.isoformat(timespec="seconds"),
                   "floor_f": want[0], "ceil_f": want[1],
                   "target_c": round(plan.temp_hourly_c, 3),
                   "cue": {"tank_date": plan.tank_date, "local": plan.local,
                           "temp_hourly_c": plan.temp_hourly_c}}
            try:
                self._write(req)
                view["requested"] = {k: req[k] for k in ("seq", "ts", "floor_f", "ceil_f")}
                view["wrote"] = why
            except OSError as err:
                findings.append({"key": "itc_request_unwritable", "level": "warn",
                                 "message": f"cannot write the Inkbird setpoint "
                                            f"request: {err}", "alert": False})
        req_view = view["requested"]
        pending = bool(req_view) and (applied is None
                                      or int(applied.get("seq", 0)) < int(req_view["seq"]))
        view["pending"] = pending
        if pending:
            age_min = (now - dt.datetime.fromisoformat(req_view["ts"])).total_seconds() / 60
            if age_min >= float(c["pending_alert_min"]):
                findings.append({
                    "key": "itc_setpoint_pending", "level": "warn", "alert": True,
                    "message": f"Inkbird setpoints {req_view['floor_f']}/"
                               f"{req_view['ceil_f']} F requested {age_min:.0f} min "
                               "ago and not applied (is itc_setpoints.py running?)"})
        if (result and result.get("status") not in itc_setpoints.OK_STATUSES
                and not result.get("retryable") and pending):
            findings.append({
                "key": "itc_setpoint_failed", "level": "warn", "alert": True,
                "message": f"Inkbird setpoint change failed for good: "
                           f"{result.get('status')}: {result.get('reason')}"})
        return view, findings
