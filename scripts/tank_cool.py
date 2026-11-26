#!/usr/bin/env python3
"""tank_cool.py -- over-temperature cutoff and the cooling-fan stub. Pure logic
and a fan driver registry; no plug I/O (tank_out.Actuator does the switching).

Two thresholds above a reference ("set point") temperature, n = cutoff_above_c:

    set point + n / 2   cooling fans WANTED     (fans.on_above_c, default n / 2)
    set point + n       guarded plugs CUT OFF   so the tank can cool

The cutoff exists for plugs that add heat to the tank (a lamp, a heater, a
pump). It only ever switches a plug OFF for cooling, and puts it back when the
tank has cooled:

    cut     probe fresh and temp >= min(set point + n, limits.alarm_c[1])
    hold    stays off until temp <= cutoff - resume_band_c AND it has been off
            for min_off_s (no chatter)
    resume  a plug with no lighting channel is switched back ON if it was on
            when it tripped; a plug that follows a lighting channel is handed
            back to the light plan, which switches it on only if the plan wants
            it on right now

The probe is the only input. With no fresh reading nothing new is cut (the
temperature alarm already covers a lost probe) and a plug that is already cut
stays off, up to stale_release_min, then it is released with an alert.

The absolute ceiling limits.alarm_c[1] (28.89 C, 84 F) is a backstop that does not depend
on the set point: a wrong set point can never hide a hot tank. The fans have an
absolute backstop too, fans.on_by_c (28.61 C, 83.5 F): on a day whose target is
the 83 F cap, the fans come on at 83.5 F and the plugs are cut at 84 F.

Fans
    Fans are a STUB. The driver registry below has one entry, "stub", which
    records what it would do and switches nothing. A real driver (a GPIO relay
    or MOSFET, or USB-port power through a hub that supports per-port switching)
    registers here with @register_fan and implements set_on(); nothing else
    changes. Even then it is driven only when outputs.enabled and
    outputs.cooling.fans.enabled are both true.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

DEFAULT_COOLING = {
    # The plug cutoff. Acts only when outputs.enabled is also true; either way
    # the status record shows the thresholds and what it would do.
    "enabled": False,
    # Names of the plugs (plug_array.json) the cutoff may switch off. The
    # heater-gate plug is refused: its off state blocks heat on a different rule
    # (tank_heat.decide_gate, with a dead-man) and the two must not fight.
    "plugs": [],
    # What "set point" means:
    #   "plan"  the calendar's target for this hour (plan.temp_hourly_c)
    #   "ramp"  the heater's effective set point (tank_heat.SetpointRamp); needs
    #           outputs.heater.enabled, otherwise it falls back to the plan
    #   a number  a fixed temperature in C
    "setpoint": "plan",
    "cutoff_above_c": 1.0,          # n
    # Resume once the temperature is this far below the cutoff.
    "resume_band_c": 0.5,
    "min_off_s": 300,
    # Probe lost while cut off: release after this long, with an alert.
    "stale_release_min": 60,
    "fans": {
        "enabled": False,
        "driver": "stub",
        # None: n / 2 above the set point.
        "on_above_c": None,
        "band_c": 0.2,              # fans off this far below on_above_c
        # Absolute backstop: fans are wanted by this temperature whatever the
        # set point (83.5 F), and with no set point at all. None: no cap.
        "on_by_c": 28.61,
    },
}


def cooling_config(overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_COOLING)
    over = dict(overrides or {})
    fans = over.pop("fans", None) or {}
    cfg.update(over)
    cfg["fans"].update(fans)
    if not isinstance(cfg["plugs"], list) or not all(
            isinstance(p, str) for p in cfg["plugs"]):
        raise ValueError("cooling.plugs must be a list of plug names")
    sp = cfg["setpoint"]
    if isinstance(sp, bool) or not (sp in ("plan", "ramp")
                                    or isinstance(sp, (int, float))):
        raise ValueError('cooling.setpoint must be "plan", "ramp" or a number')
    n = float(cfg["cutoff_above_c"])
    if not 0.3 <= n <= 5.0:
        raise ValueError("cooling.cutoff_above_c must be 0.3..5.0")
    band = float(cfg["resume_band_c"])
    if not 0.05 <= band < n:
        raise ValueError("cooling.resume_band_c must be 0.05 up to below "
                         "cutoff_above_c")
    if float(cfg["min_off_s"]) < 60:
        raise ValueError("cooling.min_off_s must be at least 60")
    if not 5 <= float(cfg["stale_release_min"]) <= 1440:
        raise ValueError("cooling.stale_release_min must be 5..1440")
    f = cfg["fans"]
    if f["driver"] not in FAN_DRIVERS:
        raise ValueError(f"cooling.fans.driver must be one of {sorted(FAN_DRIVERS)}")
    on = n / 2.0 if f["on_above_c"] is None else float(f["on_above_c"])
    if not 0 < on < n:
        raise ValueError("cooling.fans.on_above_c must be above 0 and below "
                         "cutoff_above_c")
    if not 0.05 <= float(f["band_c"]) < on:
        raise ValueError("cooling.fans.band_c must be 0.05 up to below on_above_c")
    f["on_above_c"] = on
    if f["on_by_c"] is not None and not 15 <= float(f["on_by_c"]) <= 40:
        raise ValueError("cooling.fans.on_by_c must be 15..40 or null")
    return cfg


def reference_c(source, plan_c, ramp_c):
    """The set point, or None when it cannot be known (no plan today)."""
    if isinstance(source, (int, float)) and not isinstance(source, bool):
        return float(source)
    if source == "ramp" and ramp_c is not None:
        return float(ramp_c)
    return None if plan_c is None else float(plan_c)


def cutoff_c(ref_c, n: float, alarm_high_c: float) -> float:
    """The temperature at which guarded plugs are cut. Never above the alarm
    ceiling, and just the ceiling when there is no set point."""
    if ref_c is None:
        return float(alarm_high_c)
    return min(float(ref_c) + float(n), float(alarm_high_c))


@dataclass(frozen=True)
class CutDecision:
    cut: bool
    reason: str


def decide_cutoff(temp_c, trusted: bool, cutoff: float, tripped: bool,
                  off_s, c: dict) -> CutDecision:
    """Should a guarded plug be cut off right now?

    off_s is how long it has been cut (None when not cut). Every rule that
    cuts comes before every rule that releases, so a bug in the release rules
    cannot leave a plug on in a hot tank.
    """
    if temp_c is None or not trusted:
        if tripped:
            if off_s is not None and off_s >= float(c["stale_release_min"]) * 60:
                return CutDecision(False, "probe_lost_release")
            return CutDecision(True, "probe_lost_hold")
        return CutDecision(False, "probe_untrusted")
    if temp_c >= cutoff:
        return CutDecision(True, "over_cutoff")
    if tripped:
        if temp_c > cutoff - float(c["resume_band_c"]):
            return CutDecision(True, "cooling_down")
        if off_s is not None and off_s < float(c["min_off_s"]):
            return CutDecision(True, "min_off")
        return CutDecision(False, "cooled")
    return CutDecision(False, "ok")


def fan_on_at_c(ref_c, f: dict):
    """The temperature the fans come on at: set point + on_above_c, but never
    above the absolute cap on_by_c. With no set point only the cap applies;
    with neither it is None."""
    options = []
    if ref_c is not None:
        options.append(float(ref_c) + float(f["on_above_c"]))
    if f.get("on_by_c") is not None:
        options.append(float(f["on_by_c"]))
    return min(options) if options else None


def decide_fans(temp_c, trusted: bool, ref_c, was_on: bool, f: dict) -> bool:
    """Fans wanted? Hysteresis: on at fan_on_at_c, off band_c lower. No fresh
    probe, or no threshold at all: keep the current state."""
    on_at = fan_on_at_c(ref_c, f)
    if temp_c is None or not trusted or on_at is None:
        return bool(was_on)
    if temp_c >= on_at:
        return True
    if temp_c <= on_at - float(f["band_c"]):
        return False
    return bool(was_on)


# ---------------------------------------------------------------------------
# FAN DRIVERS
# ---------------------------------------------------------------------------
FAN_DRIVERS: dict[str, type] = {}


class FansUnavailable(RuntimeError):
    """This driver cannot switch fans."""


def register_fan(name: str):
    def deco(cls):
        FAN_DRIVERS[name] = cls
        cls.driver = name
        return cls
    return deco


class Fan:
    driver = "base"
    actuates = False        # True for a driver that really switches something

    def __init__(self, **opts):
        self.opts = opts
        self.wanted = False

    def set_on(self, on: bool) -> None:
        raise FansUnavailable(f"{self.driver}: cannot switch fans")


@register_fan("stub")
class StubFan(Fan):
    """Placeholder: remembers what it was asked and switches nothing."""

    def set_on(self, on: bool) -> None:
        self.wanted = bool(on)


def make_fan(fans_cfg: dict) -> Fan:
    opts = {k: v for k, v in fans_cfg.items() if k != "driver"}
    return FAN_DRIVERS[fans_cfg["driver"]](**opts)
