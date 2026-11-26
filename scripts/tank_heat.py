#!/usr/bin/env python3
"""tank_heat.py -- heater gate decisions. Pure logic; no I/O except the ramp's
state file.

Arrangement (pi-contingency-plan.md 4.3): a Pi-controlled smart plug feeds the
Inkbird, and the Inkbird feeds the heater.

    wall -> [smart plug = the GATE] -> Inkbird -> heater

With the gate ON the Inkbird runs the heater by its own settings. With the
gate OFF the heater cannot run at all. The Inkbird has two numbers, T1 (heat
turns ON below it) and T2 (heat turns OFF above it).

    ceiling   always enforced: the Inkbird stops the heater at T2 whatever the
              Pi does.
    floor     enforced whenever the gate is ON. The Pi may switch it OFF only
              when the tank is well above the hard floor, the probe is trusted,
              and a plug-side countdown will switch it back ON if the Pi stops
              refreshing it. Every other case holds the gate ON, which is the
              same as having no Pi at all.

THE PI CAN ONLY BLOCK HEAT, NEVER ADD IT. So for the Pi to hold the tank at its
setpoint, the Inkbird must already be asking for heat there: T1 has to sit ABOVE
the Pi's setpoint (plus its off band), not at the all-year lowest temperature.
With T1 at the lowest temperature the Inkbird would not heat at 25 C at all and
the Pi could not make the tank any warmer than the room. T1 and T2 therefore
track the season, a little above the setpoint; the wide safe range (73 to 83 F)
lives in the Inkbird's alarms and in the hard floor below, not in T1/T2.

The Pi therefore only ever makes the tank cooler or steadier than the Inkbird
alone would. It can never push the temperature above T2.
"""
from __future__ import annotations

import collections
import copy
import datetime as dt
import json
import os
from dataclasses import dataclass

DEFAULT_HEATER = {
    # Master switch for gate control. Off: the heater plug is observed only.
    "enabled": False,
    # The human sets these two after doing the pull-the-plug tests (see
    # pi-contingency-plan.md 4.3). The Pi will not command the gate OFF until
    # both are true, the probe is calibrated, and the plug has a revert_dp.
    "countdown_verified": False,
    "inkbird_resume_verified": False,
    # Seconds until the plug switches itself back on if not re-armed. The Pi
    # re-arms it every tick while the gate is OFF.
    "revert_after_s": 600,
    # Gate ON below setpoint - on_below_c, OFF above setpoint + off_above_c.
    "on_below_c": 0.15,
    "off_above_c": 0.15,
    "min_dwell_s": 120,
    # Never block heat within this margin of the hard floor (limits.alarm_c[0],
    # 73 F). A backstop for a probe or rule that is wrong while the gate is off.
    "floor_margin_c": 0.5,
    # What the Inkbird is set to: T1 = heat ON below it, T2 = heat OFF above it
    # (76.0 / 76.5 F on 2026-10-25). Keep in step with the device; the Pi cannot
    # read it. T1 must sit above the setpoint for the Pi to be able to regulate
    # (see the module docstring); the conductor warns when it does not.
    "inkbird_min_c": 24.44,
    "inkbird_max_c": 24.72,
    # The effective setpoint walks toward the plan at this rate, starting from
    # the measured tank temperature (the calendar starts at 26.17 C, not at the
    # tank's temperature).
    "ramp_c_per_day": 0.1,
    "ramp_max_age_s": 3600,
    # Absolute heat cut-off: at or above this the gate is forced OFF (still only
    # when off is allowed, as for every gate-off). 83 F; the Inkbird's own T2
    # cut-off is the first line, this is the Pi's backstop for it. Used as
    # min(cut_off_c, limits.alarm_c[1]).
    "cut_off_c": 28.33,
}

# Heat that does not stop: once the tank is at or above `above_c` (the heat
# cut-off) it should stop warming. If it is still rising after the grace period,
# something other than the heater is adding heat, or the heater is not being
# stopped. Alert-only; it needs no output to be enabled.
DEFAULT_STILL_WARMING = {
    "enabled": True,
    "above_c": 28.33,       # 83 F
    "grace_min": 5,         # allowed to coast this long after reaching it
    "window_min": 30,       # compare with the oldest reading in this window
    "rise_c": 0.15,         # a rise this big inside the window is "warming"
}


def deg_f(c: float, places: int = 1) -> str:
    """A Celsius reading as text in Fahrenheit, for alerts (config stays in C)."""
    return f"{float(c) * 9.0 / 5.0 + 32.0:.{places}f} F"


def delta_f(dc: float, places: int = 1) -> str:
    """A Celsius difference as Fahrenheit degrees (no +32 offset)."""
    return f"{float(dc) * 9.0 / 5.0:.{places}f} F"


def still_warming_config(overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_STILL_WARMING)
    cfg.update(overrides or {})
    if not 15 <= float(cfg["above_c"]) <= 40:
        raise ValueError("still_warming.above_c must be 15..40")
    if not 1 <= float(cfg["grace_min"]) <= 120:
        raise ValueError("still_warming.grace_min must be 1..120")
    if not float(cfg["grace_min"]) <= float(cfg["window_min"]) <= 240:
        raise ValueError("still_warming.window_min must be grace_min..240")
    if not 0.1 <= float(cfg["rise_c"]) <= 2.0:
        raise ValueError("still_warming.rise_c must be 0.1..2")
    return cfg


def heater_config(overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_HEATER)
    cfg.update(overrides or {})
    if not 60 <= float(cfg["revert_after_s"]) <= 3600:
        raise ValueError("heater.revert_after_s must be 60..3600")
    for k in ("on_below_c", "off_above_c"):
        if not 0.05 <= float(cfg[k]) <= 1.0:
            raise ValueError(f"heater.{k} must be 0.05..1.0")
    if float(cfg["min_dwell_s"]) < 30:
        raise ValueError("heater.min_dwell_s must be at least 30")
    if float(cfg["floor_margin_c"]) < 0:
        raise ValueError("heater.floor_margin_c cannot be negative")
    if not float(cfg["inkbird_min_c"]) < float(cfg["inkbird_max_c"]):
        raise ValueError("heater.inkbird_min_c must be below inkbird_max_c")
    if not 0 < float(cfg["ramp_c_per_day"]) <= 1.0:
        raise ValueError("heater.ramp_c_per_day must be 0..1")
    if not 15 <= float(cfg["cut_off_c"]) <= 40:
        raise ValueError("heater.cut_off_c must be 15..40")
    return cfg


@dataclass(frozen=True)
class Decision:
    gate_on: bool
    reason: str
    forced: bool            # True: safety rule, bypasses dwell and rate limits


def decide_gate(temp_c, trusted: bool, setpoint_c: float, prev_on,
                held_s, h: dict, allow_off: bool,
                cut_off_c: float, floor_c: float) -> Decision:
    """Should the gate be ON (heat allowed) right now?

    cut_off_c is the effective heat cut-off: the caller passes
    min(heater.cut_off_c, limits.alarm_c[1]).

    Order matters: every safety rule that holds the gate ON comes first, so a
    bug in the control rules cannot cut heat when the probe is in doubt.
    """
    prev = True if prev_on is None else bool(prev_on)
    if temp_c is None or not trusted:
        return Decision(True, "probe_untrusted", True)
    if temp_c <= float(floor_c) + float(h["floor_margin_c"]):
        return Decision(True, "near_floor", True)
    if not allow_off:
        return Decision(True, "off_not_allowed", True)
    if temp_c >= cut_off_c:
        return Decision(False, "over_cut_off", True)
    if temp_c < setpoint_c - float(h["on_below_c"]):
        want, why = True, "below_setpoint"
    elif temp_c > setpoint_c + float(h["off_above_c"]):
        want, why = False, "above_setpoint"
    else:
        want, why = prev, "within_band"
    if want != prev and held_s is not None and held_s < float(h["min_dwell_s"]):
        return Decision(prev, "dwell", False)
    return Decision(want, why, False)


class RiseWatch:
    """Notices a tank that keeps warming after it has reached the heat cut-off.

    An episode starts when a fresh reading is at or above `above_c` and ends
    when a reading falls below it. Once the episode is `grace_min` old, compare
    the newest reading with the oldest one still inside the last `window_min`
    minutes of the episode: a rise of `rise_c` or more means the heat is not
    stopping. One threshold over a window that grows to `window_min` catches a
    fast climb within minutes and a slow one (0.3 C/h) within the half hour,
    while probe noise (one or two 0.0625 C steps) stays under it. Readings that
    are missing or stale are not fed in, so they neither start nor end an
    episode.
    """

    def __init__(self, cfg: dict):
        self.c = cfg
        self.start = None
        self.hist: collections.deque = collections.deque()

    def step(self, now: dt.datetime, value) -> dict | None:
        c = self.c
        if not c["enabled"] or value is None:
            return None
        if value < float(c["above_c"]):
            self.start = None
            self.hist.clear()
            return None
        if self.start is None:
            self.start = now
        self.hist.append((now, value))
        horizon = now - dt.timedelta(minutes=float(c["window_min"]))
        while len(self.hist) > 1 and self.hist[0][0] < horizon:
            self.hist.popleft()
        if (now - self.start).total_seconds() < float(c["grace_min"]) * 60:
            return None
        t0, v0 = self.hist[0]
        rise = value - v0
        if rise >= float(c["rise_c"]):
            return {"rise_c": round(rise, 3),
                    "span_min": round((now - t0).total_seconds() / 60.0, 1),
                    "since": self.start.isoformat()}
        return None


class SetpointRamp:
    """The setpoint the gate actually chases: it starts at the tank's measured
    temperature and walks toward the plan no faster than ramp_c_per_day.

    State survives restarts in a small JSON file. If the file is missing or
    older than ramp_max_age_s the ramp re-starts from the measured temperature,
    because the tank may have drifted while the conductor was down.
    """

    def __init__(self, path: str | None, band: tuple, h: dict):
        self.path = path
        self.lo, self.hi = band
        self.rate = float(h["ramp_c_per_day"])
        self.max_age = float(h["ramp_max_age_s"])
        self.c = None
        self.ts = None
        self._load()

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.c = float(data["c"])
            self.ts = dt.datetime.fromisoformat(data["ts"])
        except (OSError, ValueError, KeyError, TypeError):
            self.c = self.ts = None

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"c": round(self.c, 4), "ts": self.ts.isoformat()}, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _clamp(self, c):
        return min(self.hi, max(self.lo, c))

    def step(self, now: dt.datetime, target_c: float, temp_c) -> float:
        stale = (self.c is None or self.ts is None
                 or (now - self.ts).total_seconds() > self.max_age)
        if stale:
            self.c = self._clamp(temp_c if temp_c is not None else target_c)
        else:
            days = max(0.0, (now - self.ts).total_seconds()) / 86400.0
            room = self.rate * days
            delta = self._clamp(target_c) - self.c
            self.c += max(-room, min(room, delta))
        self.ts = now
        self._save()
        return round(self.c, 4)
