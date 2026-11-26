#!/usr/bin/env python3
"""tank_itc_watch.py -- does the tank do what the Inkbird's T1/T2 say it should?

The Inkbird ITC-306A heats only when its own reading is BELOW T1 and stops when
it is ABOVE T2; between the two it keeps whatever it was doing. So the only
statements this can check are the two edges:

    not heating     the Inkbird has read below T1 (by `below_t1_margin_f`) for
                    the whole `window_min`, and the tank has risen less than
                    `min_rise_f` over it. A dead heater, a switched-off unit, a
                    wrong T1, a probe out of the water.
    not stopping    the Inkbird has read above T2 (by `above_t2_margin_f`) for
                    the whole `window_min` and is not falling. The heater is
                    stuck on, or something else (lights, the room) is adding
                    heat. Either way the counter-measure is cooling, so this
                    asks the fan.

Inputs are the Inkbird feed (data/sensors/itc306a_docker.jsonl: one line every
5 minutes, with t1_f and t2_f read off the device page when the poller could see
them) and the Pi probe. The band comes from the newest feed line that carries it,
else from data/itc/applied.json (what itc_setpoints.py last wrote). The feed is
read afresh every tick, so a restart loses nothing.

Alert-only unless itc_watch.reversal.enabled (off by default). Reversal is the
one write this can cause: on a fault it asks itc_setpoints.py, through the same
request file the cue side uses (data/itc/target.json), to put the Inkbird back to

    * the band it held before the last change, when the fault shows up within
      revert_window_h of that change and the device is still at the changed band
      ("undo_last_change"; the previous band is recorded in applied.json); else
    * for not stopping only, a low fallback band ("fallback_low"), so the
      Inkbird is not asking for heat at all.

A heater that is dead (not heating, no recent change) is alert-only: no setpoint
fixes it. A reversal is limited to one per cooldown_h and max_per_day, never
while a request is still unapplied, and writes a hold (data/itc/hold.json) so the
cue side does not immediately put the band back. The reversal band is only as
good as the evidence: the previous band was never proven good, only the one the
device held before; the alert says so.

Alert-only apart from the fan, and the fan is a STUB (tank_cool.StubFan): it
remembers what it was asked and switches nothing; a real driver registered with
tank_cool.register_fan can be named in itc_watch.fan.driver later. Nothing here
writes to the Inkbird or to a plug.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os

import itc_setpoints
import tank_cool

UTC = dt.timezone.utc

DEFAULT_WATCH = {
    "enabled": False,
    "feed": None,                    # default <data>/sensors/itc306a_docker.jsonl
    "max_age_s": 900,                # a reading older than this is not current
    "blind_alert_min": 20,           # no current reading for this long: alert
    "window_min": 45,                # how long a condition must hold
    "min_readings": 4,               # and how many readings must show it
    "below_t1_margin_f": 0.2,        # not heating: below T1 by at least this
    "min_rise_f": 0.2,               # ... and risen less than this over the window
    "above_t2_margin_f": 0.5,        # not stopping: above T2 by at least this
    "fall_tolerance_f": 0.0,         # ... and not lower than at the window's start
    # Until the lamp heat is part of the temperature cue the tank runs above T2 by design, so the
    # not-stopping alert is sent once a calendar day, unless the tank is at the alarm level (alarm_high_f),
    # where it goes back to the normal repeat (alert_cooldown_min).
    "not_stopping_once_a_day": True,
    "alarm_high_f": 83.0,
    "probe_diff_max_c": 0.8,         # probe against Inkbird, both current
    "fan": {"driver": "stub", "off_at_t2_f": 0.0},
    "reversal": {
        "enabled": False,
        "fallback_f": [76.0, 77.4],  # the device's settings before 2026-11-02
        "bounds_f": [73.0, 83.0],
        "revert_window_h": 6,        # a fault this soon after a change undoes it
        "cooldown_h": 3,             # at most one reversal per this
        "max_per_day": 2,
        "hold_h": 6,                 # the cue side stays quiet this long after
        "pending_alert_min": 30,     # a reversal still unapplied after this alerts
    },
}


def watch_config(overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_WATCH)
    over = dict(overrides or {})
    cfg["fan"].update(over.pop("fan", None) or {})
    cfg["reversal"].update(over.pop("reversal", None) or {})
    cfg.update(over)
    if not 300 <= float(cfg["max_age_s"]) <= 7200:
        raise ValueError("itc_watch.max_age_s must be 300..7200")
    if not 10 <= float(cfg["window_min"]) <= 360:
        raise ValueError("itc_watch.window_min must be 10..360")
    if not 2 <= int(cfg["min_readings"]) <= 100:
        raise ValueError("itc_watch.min_readings must be 2..100")
    for k in ("below_t1_margin_f", "min_rise_f", "above_t2_margin_f"):
        if not 0.0 <= float(cfg[k]) <= 5.0:
            raise ValueError(f"itc_watch.{k} must be 0..5")
    if not 75 <= float(cfg["alarm_high_f"]) <= 100:
        raise ValueError("itc_watch.alarm_high_f must be 75..100")
    if not 0.1 <= float(cfg["probe_diff_max_c"]) <= 5.0:
        raise ValueError("itc_watch.probe_diff_max_c must be 0.1..5")
    if float(cfg["blind_alert_min"]) < 5:
        raise ValueError("itc_watch.blind_alert_min must be at least 5")
    if cfg["fan"].get("driver") not in tank_cool.FAN_DRIVERS:
        raise ValueError("itc_watch.fan.driver is not a registered fan driver")
    rv = cfg["reversal"]
    lo, hi = float(rv["bounds_f"][0]), float(rv["bounds_f"][1])
    ff, fc = float(rv["fallback_f"][0]), float(rv["fallback_f"][1])
    if not (30 <= lo < hi <= 120 and lo <= ff < fc <= hi):
        raise ValueError("itc_watch.reversal.fallback_f must be floor < ceiling "
                         "inside bounds_f")
    for k, lim in (("revert_window_h", (0.5, 72)), ("cooldown_h", (0.5, 72)),
                   ("hold_h", (0.5, 72)), ("pending_alert_min", (5, 720))):
        if not lim[0] <= float(rv[k]) <= lim[1]:
            raise ValueError(f"itc_watch.reversal.{k} must be {lim[0]}..{lim[1]}")
    if not 1 <= int(rv["max_per_day"]) <= 10:
        raise ValueError("itc_watch.reversal.max_per_day must be 1..10")
    return cfg


# ---------------------------------------------------------------------------
# PURE PARTS (unit tested)
# ---------------------------------------------------------------------------
def parse_feed(lines, now: dt.datetime) -> list[dict]:
    """Readings, oldest first: ts, temp_f, and t1_f/t2_f when present. Failed
    reads (temp_c null) and damaged lines are skipped."""
    out = []
    for line in lines:
        try:
            r = json.loads(line)
            if r.get("temp_c") is None:
                continue
            ts = dt.datetime.fromisoformat(r["ts"])
            if ts > now + dt.timedelta(minutes=2):
                continue
            out.append({"ts": ts, "temp_f": itc_setpoints.c_to_f(float(r["temp_c"])),
                        "t1_f": r.get("t1_f"), "t2_f": r.get("t2_f"),
                        "heating": r.get("heating")})
        except (ValueError, KeyError, TypeError):
            continue
    out.sort(key=lambda r: r["ts"])
    return out


def band_from(readings, applied: dict | None, now: dt.datetime, max_age_s: float):
    """(t1_f, t2_f, source) from the newest current reading that carries both,
    else from applied.json, else None."""
    for r in reversed(readings):
        if (now - r["ts"]).total_seconds() > max_age_s:
            break
        if r["t1_f"] is not None and r["t2_f"] is not None:
            return float(r["t1_f"]), float(r["t2_f"]), "device page"
    if applied:
        try:
            return float(applied["floor_f"]), float(applied["ceil_f"]), "applied.json"
        except (KeyError, TypeError, ValueError):
            pass
    return None


def window(readings, now: dt.datetime, minutes: float):
    """Readings from the last `minutes`, plus whether they really span it (the
    oldest is within one reading interval of the window's start)."""
    start = now - dt.timedelta(minutes=minutes)
    inside = [r for r in readings if r["ts"] >= start]
    return inside


def judge(readings, band, now: dt.datetime, c: dict) -> dict:
    """The two edge checks. Returns {"not_heating": dict|None,
    "not_stopping": dict|None, "latest": reading|None, "n": int}."""
    out = {"not_heating": None, "not_stopping": None, "latest": None, "n": 0}
    if not readings or band is None:
        return out
    t1, t2, _src = band
    latest = readings[-1]
    out["latest"] = latest
    if (now - latest["ts"]).total_seconds() > float(c["max_age_s"]):
        return out
    win = window(readings, now, c["window_min"])
    out["n"] = len(win)
    # the readings must cover the window, not just bunch at its end
    span_min = ((win[-1]["ts"] - win[0]["ts"]).total_seconds() / 60.0) if win else 0.0
    if len(win) < int(c["min_readings"]) or span_min < 0.75 * float(c["window_min"]):
        return out
    temps = [r["temp_f"] for r in win]
    rise = temps[-1] - temps[0]
    if all(t < t1 - float(c["below_t1_margin_f"]) for t in temps) \
            and rise < float(c["min_rise_f"]):
        out["not_heating"] = {"t1_f": t1, "latest_f": round(temps[-1], 2),
                              "rise_f": round(rise, 2), "span_min": round(span_min)}
    if all(t > t2 + float(c["above_t2_margin_f"]) for t in temps) \
            and rise >= -float(c["fall_tolerance_f"]):
        # The device's own "Heating" label, read off the page. When every reading in
        # the window says it was NOT heating, the heater is not what keeps the tank
        # above T2 (lamps or the room are): that is expected until the lamp heat is
        # part of the temperature cue, so it is not a fault. Unknown (None) counts as
        # "might be the heater".
        idle = all(r.get("heating") is False for r in win)
        out["not_stopping"] = {"t2_f": t2, "latest_f": round(temps[-1], 2),
                               "rise_f": round(rise, 2), "span_min": round(span_min),
                               "heater_idle": idle}
    return out


def fan_decision(was_on: bool, latest_f, band, not_stopping: bool, c: dict) -> bool:
    """Fan on when the tank is not stopping; off again once it is back at or
    below T2 (plus fan.off_at_t2_f). Unknown: keep the last state."""
    if not_stopping:
        return True
    if latest_f is None or band is None:
        return was_on
    if latest_f <= band[1] + float(c["fan"].get("off_at_t2_f", 0.0)):
        return False
    return was_on


def reversal_target(fault: str, band, applied: dict | None, now: dt.datetime,
                    rc: dict) -> dict | None:
    """Where a reversal would put the Inkbird, or None. `band` is the device's
    current (t1, t2, source); `applied` is applied.json."""
    if fault not in ("not_heating", "not_stopping") or band is None:
        return None

    def differs(a, b):
        return abs(a[0] - b[0]) > 0.05 or abs(a[1] - b[1]) > 0.05
    cur = (band[0], band[1])
    try:
        prev = applied.get("previous") if applied else None
        if (applied or {}).get("cue", {}) and (applied["cue"] or {}).get("reversal"):
            prev = None                 # never undo a reversal: that restores the fault
        changed = dt.datetime.fromisoformat(applied["changed_ts"]) if prev else None
        if prev and changed is not None:
            age_h = (now - changed).total_seconds() / 3600.0
            at_applied = not differs(cur, (float(applied["floor_f"]),
                                           float(applied["ceil_f"])))
            tgt = (float(prev["floor_f"]), float(prev["ceil_f"]))
            if (0 <= age_h <= float(rc["revert_window_h"]) and at_applied
                    and differs(cur, tgt)):
                return {"floor_f": tgt[0], "ceil_f": tgt[1],
                        "reason": "undo_last_change"}
    except (KeyError, TypeError, ValueError):
        pass
    if fault == "not_stopping":
        tgt = (float(rc["fallback_f"][0]), float(rc["fallback_f"][1]))
        if differs(cur, tgt):
            return {"floor_f": tgt[0], "ceil_f": tgt[1], "reason": "fallback_low"}
    return None


def reversal_allowed(history: list[str], now: dt.datetime, rc: dict) -> bool:
    """One per cooldown_h and at most max_per_day in the last 24 h."""
    times = []
    for t in history:
        try:
            times.append(dt.datetime.fromisoformat(t))
        except ValueError:
            continue
    if any((now - t).total_seconds() < float(rc["cooldown_h"]) * 3600 for t in times):
        return False
    return sum((now - t).total_seconds() < 86400 for t in times) < int(rc["max_per_day"])


# ---------------------------------------------------------------------------
class ItcWatch:
    def __init__(self, cfg: dict | None, folder: str | None):
        self.cfg = watch_config(cfg)
        self.data = folder
        self.fan = tank_cool.make_fan(self.cfg["fan"])
        self._fan_on = False
        self._blind_since: dt.datetime | None = None
        self._last_sig = None
        # Finding keys this step could not judge (no current Inkbird reading, no
        # probe): the alert layer must not announce them as recovered.
        self.unknown_keys: set[str] = set()

    def _feed_path(self) -> str | None:
        if self.cfg["feed"]:
            return self.cfg["feed"]
        return os.path.join(self.data, "sensors", "itc306a_docker.jsonl") \
            if self.data else None

    @staticmethod
    def _tail(path: str, n: int = 400) -> list[str]:
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 60000))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        return lines[-n:]

    def _applied(self) -> dict | None:
        if not self.data:
            return None
        try:
            with open(os.path.join(self.data, "itc", "applied.json"),
                      encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _json(self, name: str):
        if not self.data:
            return None
        try:
            with open(os.path.join(self.data, "itc", name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _write_json(self, name: str, obj) -> None:
        d = os.path.join(self.data, "itc")
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
        os.replace(tmp, os.path.join(d, name))

    def _reverse(self, now, fault, band, applied, detail):
        """The reversal step for one fault. Returns (view, findings)."""
        rc = self.cfg["reversal"]
        view = {"enabled": True, "fault": fault, "wrote": None, "why_not": None}
        req = self._json("target.json")
        applied_seq = int((applied or {}).get("seq", 0) or 0)
        if req and int(req.get("seq", 0)) > applied_seq:
            view["why_not"] = "a request is still unapplied"
            return view, []
        state = self._json("reversal.json") or {"history": []}
        if not reversal_allowed(state.get("history", []), now, rc):
            view["why_not"] = "cooldown or daily limit"
            return view, []
        tgt = reversal_target(fault, band, applied, now, rc)
        if tgt is None:
            view["why_not"] = "no band to go back to (alert only)"
            return view, []
        lo, hi = float(rc["bounds_f"][0]), float(rc["bounds_f"][1])
        if not lo <= tgt["floor_f"] < tgt["ceil_f"] <= hi:
            view["why_not"] = "target outside bounds"
            return view, []
        seq = max(int((req or {}).get("seq", 0) or 0), applied_seq) + 1
        try:
            self._write_json("target.json", {
                "seq": seq, "ts": now.isoformat(timespec="seconds"),
                "floor_f": tgt["floor_f"], "ceil_f": tgt["ceil_f"],
                "cue": {"reversal": tgt["reason"], "fault": fault,
                        "from_f": [band[0], band[1]], "detail": detail}})
            state.setdefault("history", []).append(now.isoformat(timespec="seconds"))
            state["history"] = state["history"][-20:]
            self._write_json("reversal.json", state)
            self._write_json("hold.json", {
                "until": (now + dt.timedelta(hours=float(rc["hold_h"]))).isoformat(
                    timespec="seconds"),
                "reason": f"reversal:{tgt['reason']}", "seq": seq})
        except OSError as err:
            view["why_not"] = f"cannot write the request: {err}"
            return view, []
        self._log({"ts": now.isoformat(), "event": "reversal", "seq": seq,
                   "fault": fault, **tgt, "from_f": [band[0], band[1]]})
        view.update(wrote=tgt["reason"], seq=seq, to_f=[tgt["floor_f"], tgt["ceil_f"]])
        caveat = (" (the band the device held before the last change; not proven good)"
                  if tgt["reason"] == "undo_last_change" else
                  " (low fallback band, so the Inkbird stops asking for heat)")
        return view, [{
            "key": "itc_reversal", "level": "alert", "alert": True,
            "message": f"{fault.replace('_', ' ')}: asking the Inkbird to go back from "
                       f"{band[0]}/{band[1]} F to {tgt['floor_f']}/{tgt['ceil_f']} F"
                       f"{caveat}; the cue side is on hold for {rc['hold_h']} h"}]

    def _log(self, rec: dict) -> None:
        if not self.data:
            return
        try:
            os.makedirs(os.path.join(self.data, "itc"), exist_ok=True)
            with open(os.path.join(self.data, "itc", "watch.jsonl"), "a",
                      encoding="utf-8") as fh:
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def step(self, now: dt.datetime, probe) -> tuple[dict, list[dict]]:
        """(view, findings). `probe` is the Pi probe's tank_inputs.Reading."""
        c = self.cfg
        self.unknown_keys = set()
        if not c["enabled"]:
            return {"enabled": False}, []
        path = self._feed_path()
        readings = parse_feed(self._tail(path), now) if path else []
        band = band_from(readings, self._applied(), now, c["max_age_s"])
        latest = readings[-1] if readings else None
        age_s = (now - latest["ts"]).total_seconds() if latest else None
        current = age_s is not None and age_s <= float(c["max_age_s"])
        findings = []

        if not current:
            self.unknown_keys |= {"itc_not_heating", "itc_not_stopping",
                                  "itc_probe_disagree", "itc_reversal_pending"}
            self._blind_since = self._blind_since or now
            blind_min = (now - self._blind_since).total_seconds() / 60.0
            findings.append({
                "key": "itc_watch_blind", "level": "warn",
                "alert": blind_min >= float(c["blind_alert_min"]),
                "message": "no current Inkbird reading "
                           + ("(feed empty)" if age_s is None else f"(newest is {age_s / 60:.0f} min old)")
                           + ": cannot check that the tank heats and stops as instructed"})
        else:
            self._blind_since = None

        verdict = judge(readings, band, now, c) if current else \
            {"not_heating": None, "not_stopping": None, "latest": latest, "n": 0}
        nh, ns = verdict["not_heating"], verdict["not_stopping"]
        if nh:
            findings.append({
                "key": "itc_not_heating", "level": "alert", "alert": True,
                "message": f"Inkbird reads {nh['latest_f']} F, below T1 {nh['t1_f']} F for "
                           f"{nh['span_min']} min, and has risen only {nh['rise_f']} F: the "
                           "tank is not heating as instructed (heater, power, T1 or probe?)"})
        if ns:
            at_alarm = float(ns["latest_f"]) >= float(c["alarm_high_f"])
            daily = bool(c["not_stopping_once_a_day"]) and not at_alarm
            quiet = bool(ns.get("heater_idle")) and not at_alarm
            findings.append({
                "key": "itc_not_stopping", "level": "info" if quiet else "alert",
                "alert": not quiet,
                # "daily": the sender keeps it to one a calendar day; at the alarm level it is off
                "daily": daily,
                "message": f"Inkbird reads {ns['latest_f']} F, above T2 {ns['t2_f']} F for "
                           f"{ns['span_min']} min and is not falling: the tank is not "
                           "stopping as instructed ("
                           + ("the Inkbird says it is NOT heating, so this is the lamps "
                              "or the room, not a stuck heater"
                              if ns.get("heater_idle")
                              else "heater stuck on, or lights/room")
                           + "); cooling fan requested (stub)"
                           + (f"; sent once a day while below {c['alarm_high_f']:g} F"
                              if daily else f"; AT THE ALARM LEVEL ({c['alarm_high_f']:g} F)")})

        diff = None
        age_fn = getattr(probe, "age_s", None)
        probe_age = age_fn(now) if callable(age_fn) else None
        # A stale probe reading is not a measurement of now: it cannot disagree.
        probe_fresh = (probe is not None and probe.ok and probe.value is not None
                       and (probe_age is None or abs(probe_age) <= float(c["max_age_s"])))
        if not probe_fresh:
            self.unknown_keys.add("itc_probe_disagree")
        if current and probe_fresh:
            diff = round(probe.value - itc_setpoints.f_to_c(latest["temp_f"]), 3)
            if abs(diff) > float(c["probe_diff_max_c"]):
                findings.append({
                    "key": "itc_probe_disagree", "level": "warn", "alert": True,
                    "message": f"Pi probe and Inkbird differ by {diff * 9.0 / 5.0:+.1f} F "
                               f"(limit {float(c['probe_diff_max_c']) * 9.0 / 5.0:.1f} F): "
                               "one of them is not reading "
                               "the water, or the Inkbird read is wrong"})

        latest_f = latest["temp_f"] if current else None
        want = fan_decision(self._fan_on, latest_f, band, bool(ns), c)
        if want != self._fan_on:
            self._log({"ts": now.isoformat(), "event": "fan", "driver": self.fan.driver,
                       "desired_on": want, "executed": False,
                       "latest_f": None if latest_f is None else round(latest_f, 2),
                       "t2_f": None if band is None else band[1]})
        self._fan_on = want
        self.fan.set_on(want)

        reversal_view = None
        if c["reversal"]["enabled"]:
            applied = self._applied()
            # A tank above T2 with the heater idle has nothing for a setpoint
            # reversal to fix, so only a (possibly) heating Inkbird counts as a fault.
            ns_fault = ns and not ns.get("heater_idle")
            fault = "not_stopping" if ns_fault else ("not_heating" if nh else None)
            if fault and current:
                reversal_view, rfind = self._reverse(now, fault, band, applied,
                                                     (ns if ns_fault else None) or nh)
                findings += rfind
            else:
                reversal_view = {"enabled": True, "fault": None, "wrote": None}
            req = self._json("target.json")
            if (req and (req.get("cue") or {}).get("reversal")
                    and int(req.get("seq", 0)) > int((applied or {}).get("seq", 0) or 0)):
                age_min = (now - dt.datetime.fromisoformat(req["ts"])).total_seconds() / 60
                if age_min >= float(c["reversal"]["pending_alert_min"]):
                    findings.append({
                        "key": "itc_reversal_pending", "level": "alert", "alert": True,
                        "message": f"reversal to {req['floor_f']}/{req['ceil_f']} F "
                                   f"requested {age_min:.0f} min ago and not applied "
                                   "(is itc_setpoints.py running?)"})

        sig = (bool(nh), bool(ns), want)
        if sig != self._last_sig:
            self._last_sig = sig
            self._log({"ts": now.isoformat(), "event": "state", "not_heating": nh,
                       "not_stopping": ns, "fan_wanted": want,
                       "band": band and {"t1_f": band[0], "t2_f": band[1],
                                         "source": band[2]}})
        view = {"enabled": True, "current": current,
                "age_s": None if age_s is None else round(age_s),
                "latest_f": None if latest_f is None else round(latest_f, 2),
                "band": band and {"t1_f": band[0], "t2_f": band[1], "source": band[2]},
                "readings_in_window": verdict["n"],
                "not_heating": nh, "not_stopping": ns, "probe_diff_c": diff,
                "fan": {"driver": self.fan.driver, "wanted": want, "executed": False},
                "reversal": reversal_view}
        return view, findings
