#!/usr/bin/env python3
"""tank_out.py -- the only code that switches plugs.

Everything here is OFF until a human turns it on in tank_config.json:

    "outputs": {
      "enabled": false,                 master switch for everything below
      "channels": {"white": false, "white_plus": false, "blue": false,
                   "moon": false},
      "heater":   {"enabled": false, ...},    see tank_heat.DEFAULT_HEATER
      "cooling":  {"enabled": false, ...}     see tank_cool.DEFAULT_COOLING
    }

Rules that hold when it is on
  * Only plugs with a lighting channel, or the one plug with role "heater", are
    ever commanded. Anything else is observed only.
  * Every command is read back. A command that did not take is a finding, and
    is not recorded as the plug's known state.
  * Every command, and every refusal that matters, is appended to
    tank_commands.jsonl.
  * Manual changes (the plug's app, Google Home) are respected for
    override_hold_min, or until the plan's next transition, whichever is first.
    Two things are never respected: a lit light during full dark (the dark
    window is the point of the project), and anything that would block heat.
  * Per plug: at least min_command_interval_s between commands and at most
    max_commands_per_hour. A plug hitting the cap raises an alert and stays
    untouched until the hour rolls. Commands that turn the heater gate ON
    (the safe direction) ignore both limits.
  * Verify: see tank_verify.py. Each command that read back is then checked
    against watts and the lux sensor on the following ticks; a command whose
    effect never shows is a finding (effect_failed), and the time from cue to
    command (cue_lag_s) is recorded.
  * Heater: see tank_heat.py. The gate is held ON unless the Pi is allowed to
    block heat.
  * Cooling: see tank_cool.py. A plug named in cooling.plugs is cut OFF when the
    tank is cooling.cutoff_above_c over its set point, and the light plan does
    not touch it while it is cut. This is the one rule that may switch a plug
    with no lighting channel. Fans are a stub and switch nothing.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import common
import cues
import tank_cool
import tank_heat
import tank_plugs
import tank_safe
import tank_verify

LIGHT_CHANNELS = ("white", "white_plus", "blue", "moon")
DARK_PROTECTED = ("white", "white_plus", "blue")

DEFAULT_OUTPUTS = {
    "enabled": False,
    "channels": {"white": False, "white_plus": False, "blue": False,
                 "moon": False},
    "heater": {},                 # merged over tank_heat.DEFAULT_HEATER
    "cooling": {},                # merged over tank_cool.DEFAULT_COOLING
    "verify": {},                 # merged over tank_verify.DEFAULT_VERIFY
    # Lamp backstop (2026-11-06): every ON command to a lighting plug also arms the
    # plug's own countdown, which switches the lamp OFF after revert_after_s unless
    # the controller refreshes it (every half of that). If the PC, Docker or the
    # controller dies, the lamps go dark instead of staying lit all night.
    # plugs: null = every lighting plug whose countdown works, or a list of names.
    "lamp_deadman": {"enabled": False, "revert_after_s": 900, "plugs": None},
    # Dimmers (2026-11-08): a plug that can dim (supports_level) is driven by the
    # plan's level for its channel instead of on/off, in steps. step: the smallest
    # change of brightness worth a command (0..1); tolerance: how far the read-back
    # may sit from what was asked; override_delta: a change this big that the
    # controller did not make is a hand on the wall switch (held like a manual
    # on/off, override_hold_min); max_commands_per_hour: a 30-minute ramp is about
    # 50 steps, so this is higher than the on/off limit.
    "levels": {"step": 0.02, "tolerance": 0.015, "override_delta": 0.05,
               "max_commands_per_hour": 120},
    "override_hold_min": 30,
    "protect_dark": True,
    "min_command_interval_s": 30,
    "max_commands_per_hour": 20,
}


def _limit(reading, lim) -> float:
    """How old the temperature reading may be: its own limit (the Inkbird feed
    updates every 5 minutes), else limits.input_max_age_s."""
    own = getattr(reading, "max_age_s", None)
    return float(own) if own else float(lim["input_max_age_s"])


def _f(key, level, message, alert=False):
    return {"key": key, "level": level, "message": message, "alert": alert}


def deadman_config(over: dict | None = None) -> dict:
    """outputs.lamp_deadman merged over the default and validated."""
    cfg = dict(DEFAULT_OUTPUTS["lamp_deadman"])
    cfg.update(over or {})
    if not 120 <= float(cfg["revert_after_s"]) <= 3600:
        raise ValueError("lamp_deadman.revert_after_s must be 120..3600")
    pl = cfg["plugs"]
    if pl is not None and not (isinstance(pl, list)
                               and all(isinstance(n, str) for n in pl)):
        raise ValueError("lamp_deadman.plugs must be null or a list of plug names")
    cfg["enabled"] = bool(cfg["enabled"])
    return cfg


def levels_config(over: dict | None = None) -> dict:
    """outputs.levels merged over the default and validated."""
    cfg = dict(DEFAULT_OUTPUTS["levels"])
    cfg.update(over or {})
    if not 0.005 <= float(cfg["step"]) <= 0.25:
        raise ValueError("levels.step must be 0.005..0.25")
    if not 0.0 <= float(cfg["tolerance"]) < float(cfg["step"]):
        raise ValueError("levels.tolerance must be at least 0 and below levels.step")
    if not float(cfg["step"]) <= float(cfg["override_delta"]) <= 1.0:
        raise ValueError("levels.override_delta must be from levels.step to 1")
    if not 1 <= int(cfg["max_commands_per_hour"]) <= 1000:
        raise ValueError("levels.max_commands_per_hour must be 1..1000")
    return cfg


class Actuator:
    def __init__(self, out_cfg: dict, limits: dict, plugs, folder: str):
        self.cfg = out_cfg
        self.lim = limits
        self.plugs = {p.name: p for p in plugs}
        self.folder = folder
        self.h = tank_heat.heater_config(out_cfg.get("heater"))
        self.ramp = tank_heat.SetpointRamp(
            os.path.join(folder, "tank_setpoint.json"),
            cues.safety_band_c(), self.h)
        self._last_cmd: dict[str, bool] = {}
        self._cmd_times: dict[str, list] = {}
        self._override: dict[str, dict] = {}
        self._gate_changed = None
        # When the Pi last armed each plug's own countdown. The plug cannot be
        # asked how long is left: the KMC 30153 countdown data point holds its
        # set value until it fires, so the Pi keeps this clock itself.
        self._armed_at: dict[str, dt.datetime] = {}
        self.c = tank_cool.cooling_config(out_cfg.get("cooling"))
        self.dm = deadman_config(out_cfg.get("lamp_deadman"))
        self.lv = levels_config(out_cfg.get("levels"))
        self._last_level: dict[str, float] = {}
        self.fan = tank_cool.make_fan(self.c["fans"])
        self._fan_on = False
        self._held: set[str] = set()
        # Plugs currently cut by the over-temperature rule. Kept on disk so a
        # restart while the tank is hot cannot put them back on.
        self._trip_path = os.path.join(folder, "tank_overtemp.json")
        self._trips: dict[str, dict] = dict(
            common.read_json(self._trip_path, {}) or {})
        self.verify = tank_verify.Verifier(out_cfg.get("verify"), folder, plugs,
                                           audit=self._audit)
        self._lux = None

    # -- audit ------------------------------------------------------------
    def _audit(self, entry: dict) -> None:
        try:
            os.makedirs(self.folder, exist_ok=True)
            with open(os.path.join(self.folder, "tank_commands.jsonl"), "a",
                      encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str,
                                    separators=(",", ":")) + "\n")
        except OSError:
            pass

    # -- one command, read back -------------------------------------------
    def _command(self, now, plug, desired, reason, res, revert=None,
                 force=False, quiet=False, level=None):
        """Switch a plug and read it back. `level` (dimmers): set that brightness,
        0..1, 0 meaning off; `desired` is then level > 0."""
        name = plug.name
        times = [t for t in self._cmd_times.get(name, [])
                 if (now - t).total_seconds() < 3600]
        self._cmd_times[name] = times
        if not force:
            if times and (now - times[-1]).total_seconds() < float(
                    self.cfg["min_command_interval_s"]):
                return None
            cap = (self.lv["max_commands_per_hour"] if level is not None
                   else self.cfg["max_commands_per_hour"])
            if len(times) >= int(cap):
                res["findings"].append(_f(
                    f"command_rate_limit:{name}", "alert",
                    f"{name}: {len(times)} commands in the last hour; "
                    "holding off (is something fighting the plan?)", True))
                return None
        if not quiet:       # a bookkeeping refresh must not eat the hourly allowance
            times.append(now)
        entry = {"ts": now.isoformat(), "plug": name, "desired_on": desired,
                 "reason": reason, "revert_after_s": revert, "executed": False,
                 "verified": None, "observed_on": None,
                 "revert_remaining_s": None, "error": None,
                 "cue_lag_s": (None if level is not None
                               else self.verify.cue_lag_s(name, desired, now))}
        if level is not None:
            entry["desired_level"] = level
            entry["observed_level"] = None
        try:
            if level is None:
                plug.set_on(desired, revert_after_s=revert)
            else:
                plug.set_level(level)
            entry["executed"] = True
            back = plug.status()
            entry["verified"] = bool(back.ok and back.on == desired)
            if level is not None and entry["verified"]:
                eff = plug.effective_level(back)
                entry["observed_level"] = eff
                entry["verified"] = bool(
                    eff is not None
                    and abs(eff - level) <= float(self.lv["tolerance"]) + 1e-9)
            entry["observed_on"] = back.on
            entry["revert_remaining_s"] = back.revert_remaining_s
            if entry["verified"]:
                if revert:
                    self._armed_at[name] = now
                else:
                    self._armed_at.pop(name, None)
            if entry["verified"] and not quiet and level is None:
                self.verify.expect(now, plug, desired, reason, self._lux,
                                   entry["cue_lag_s"])
        except (tank_plugs.PlugError, tank_plugs.OutputsDisabled) as err:
            entry["error"] = str(err)[:120]
        except Exception as err:  # noqa: BLE001 - a plug must not stop the tick
            entry["error"] = type(err).__name__
        if not (quiet and entry["verified"]):
            self._audit(entry)
            res["commands"].append(entry)
        if entry["error"]:
            res["findings"].append(_f(
                f"command_failed:{name}", "alert",
                f"{name}: could not set {'on' if desired else 'off'}: "
                f"{entry['error']}", True))
        elif not entry["verified"]:
            told = ("on" if desired else "off") if level is None else f"level {level}"
            seen = (entry["observed_on"] if level is None
                    else f"level {entry['observed_level']}")
            res["findings"].append(_f(
                f"command_unverified:{name}", "alert",
                f"{name}: told {told} but it reads {seen}", True))
        else:
            self._last_cmd[name] = desired
            if level is not None:
                self._last_level[name] = level
        return entry

    # -- lamp backstop ------------------------------------------------------
    def _dm_revert(self, plug):
        """Seconds to arm on an ON command to this lamp, else None."""
        dm = self.dm
        if not (dm["enabled"] and plug.supports_revert
                and plug.channel in LIGHT_CHANNELS and plug.role != "heater"):
            return None
        if dm["plugs"] is not None and plug.name not in dm["plugs"]:
            return None
        return int(dm["revert_after_s"])

    def _dm_needs_rearm(self, now, name, reading, n) -> bool:
        armed = self._armed_at.get(name)
        if armed is None:               # unknown (a restart, or never armed): arm now
            return True
        if (now - armed).total_seconds() >= n * 0.5:
            return True
        left = reading.revert_remaining_s   # the plug disagrees: disarmed or running low
        return left is not None and (left <= 0 or left < n * 0.5)

    def _dm_fired(self, name, now, n) -> bool:
        """The plug's own countdown has probably just switched it off: it was armed
        a full countdown ago and nothing refreshed it (a manual switch-off sooner
        than that is an override, not this)."""
        armed = self._armed_at.get(name)
        return armed is not None and (now - armed).total_seconds() >= n - 45

    # -- lights -----------------------------------------------------------
    # -- dimmed lights ------------------------------------------------------
    def _target_level(self, plug, intent, reading) -> float | None:
        """The brightness to hold now, 0..1 (0 = off), or None when the plan has
        no opinion. The plan's level for the channel, capped by the plug's
        level_cap, rounded to levels.step, and off below the plug's off_below, its
        min_level (a lamp that misbehaves when dim) or (while on) the device's own
        minimum: a dimmer cannot give less than that, and asking for less would
        never read back."""
        want = intent["desired_on"]
        if want is None:
            return None
        if not want:
            return 0.0
        lvl = intent.get("desired_level")
        lvl = 1.0 if lvl is None else float(lvl)
        if getattr(plug, "fixed_level", None) is not None:
            lvl = float(plug.fixed_level)       # the plan says on; the lamp's own level
        if intent.get("via") and getattr(plug, "also_level", None) is not None:
            lvl = float(plug.also_level)        # serving its second channel: that level
        step = float(self.lv["step"])
        t = min(max(lvl, 0.0), plug.level_cap)
        t = round(round(t / step) * step, 4)
        floor = max(float(getattr(plug, "off_below", 0.01)),
                    float(reading.level_min or 0.0) - 1e-9,
                    float(getattr(plug, "min_level", 0.0)) - 1e-9)
        return 0.0 if t < floor else min(t, plug.level_cap)

    def _light_level(self, now, plan, plug, reading, intent, res):
        """Hold a dimmer at the plan's brightness. Same rules as _light (dark
        protection, hand-switch hold, command limits), on brightness: a command goes
        out when the light differs from the target by a step, or by anything when one
        of them is off."""
        name = plug.name
        if not reading.ok or reading.on is None:
            return
        target = self._target_level(plug, intent, reading)
        eff = plug.effective_level(reading)
        if target is None or eff is None:
            return
        dark_violation = (self.cfg["protect_dark"] and plan.full_dark
                          and eff > 0 and plug.channel in DARK_PROTECTED)
        if dark_violation:
            target = 0.0
        step, tol = float(self.lv["step"]), float(self.lv["tolerance"])
        diff = abs(eff - target)
        close = (diff <= tol if (eff == 0 or target == 0) else diff < step - 1e-9)
        if close:
            self._override.pop(name, None)
            last = self._last_level.get(name)
            if last is None or abs(last - eff) <= float(self.lv["override_delta"]):
                self._last_level[name] = eff
            return
        ov = self._override.get(name)
        if ov and not dark_violation:
            if now >= ov["until"]:
                self._override.pop(name)
                self._last_level.pop(name, None)        # hold over: reassert
            else:
                res["findings"].append(_f(
                    f"override:{name}", "info",
                    f"{name} left at its manual level until "
                    f"{ov['until'].isoformat()[11:16]}Z"))
                return
        last = self._last_level.get(name)       # after the hold check: it may just have been cleared
        if (last is not None and abs(eff - last) > float(self.lv["override_delta"])
                and not dark_violation and name not in self._override):
            hold = dt.timedelta(minutes=float(self.cfg["override_hold_min"]))
            self._override[name] = {"until": now + hold, "desired": target}
            self._audit({"ts": now.isoformat(), "plug": name,
                         "event": "override_detected", "observed_level": eff,
                         "last_level": last, "desired_level": target,
                         "until": (now + hold).isoformat()})
            res["findings"].append(_f(
                f"override:{name}", "info",
                f"{name} was changed by hand; leaving it for "
                f"{self.cfg['override_hold_min']} min"))
            return
        reason = "dark_protect" if dark_violation else f"plan:{plug.channel}"
        self._command(now, plug, target > 0, reason, res,
                      force=bool(dark_violation), level=target)

    # -- lights -----------------------------------------------------------
    def _light(self, now, plan, plug, reading, intent, res):
        if getattr(plug, "supports_level", False):
            return self._light_level(now, plan, plug, reading, intent, res)
        name = plug.name
        if not reading.ok or reading.on is None:
            return
        desired = intent["desired_on"]
        if desired is None:
            return
        self.verify.note_cue(name, desired, reading.on, now)
        dark_violation = (self.cfg["protect_dark"] and plan.full_dark
                          and reading.on and plug.channel in DARK_PROTECTED)
        n_dm = self._dm_revert(plug)
        if reading.on == desired:
            self._override.pop(name, None)
            self._last_cmd[name] = reading.on
            if (desired and n_dm and not dark_violation
                    and self._dm_needs_rearm(now, name, reading, n_dm)):
                self._command(now, plug, True, "rearm_deadman", res, revert=n_dm,
                              force=True, quiet=True)
            return
        if desired and n_dm and not reading.on and self._dm_fired(name, now, n_dm):
            # Not a hand switch: nothing refreshed the countdown for a whole period.
            self._armed_at.pop(name, None)
            self._last_cmd.pop(name, None)
            self._override.pop(name, None)
            self._audit({"ts": now.isoformat(), "plug": name,
                         "event": "deadman_fired", "desired_on": desired})
            res["findings"].append(_f(
                f"lamp_backstop_fired:{name}", "alert",
                f"{name}: its own countdown switched it off, so the controller "
                f"could not refresh it for {n_dm} s; switching it back on", True))
        ov = self._override.get(name)
        if ov and not dark_violation:
            if ov["desired"] != desired or now >= ov["until"]:
                self._override.pop(name)
                self._last_cmd.pop(name, None)      # hold over: reassert
            else:
                res["findings"].append(_f(
                    f"override:{name}", "info",
                    f"{name} left in its manual state until "
                    f"{ov['until'].isoformat()[11:16]}Z"))
                return
        last = self._last_cmd.get(name)
        if (last is not None and reading.on != last and not dark_violation
                and name not in self._override):
            hold = dt.timedelta(minutes=float(self.cfg["override_hold_min"]))
            self._override[name] = {"until": now + hold, "desired": desired}
            self._audit({"ts": now.isoformat(), "plug": name,
                         "event": "override_detected",
                         "observed_on": reading.on, "desired_on": desired,
                         "until": (now + hold).isoformat()})
            res["findings"].append(_f(
                f"override:{name}", "info",
                f"{name} was changed by hand; leaving it for "
                f"{self.cfg['override_hold_min']} min"))
            return
        reason = "dark_protect" if dark_violation else f"plan:{plug.channel}"
        self._command(now, plug, desired, reason, res, force=bool(dark_violation),
                      revert=n_dm if desired else None)

    def _safe_off(self, now, plug, reading, res):
        """Safe mode: switch a lit lighting plug off, whatever the plan says. No
        hand-switch hold applies (a lamp somebody turns on during safe mode goes
        off again), and the state is remembered as the controller's own so the
        plan resumes cleanly when the latch is released."""
        name = plug.name
        if not reading.ok or reading.on is None:
            return
        self._override.pop(name, None)
        dimmer = getattr(plug, "supports_level", False)
        lit = (plug.effective_level(reading) or 0) > 0 if dimmer else reading.on
        if lit:
            self._command(now, plug, False, "safe_mode", res, force=True,
                          level=0.0 if dimmer else None)
        self._last_cmd[name] = False
        if dimmer:
            self._last_level[name] = 0.0

    # -- heater gate ------------------------------------------------------
    def _heater(self, now, plan, plug, reading, temp, res):
        h = self.h
        age = temp.age_s(now)
        fresh = (temp.ok and temp.configured and temp.value is not None
                 and (age is None or age <= _limit(temp, self.lim)))
        value = temp.value if fresh else None
        setpoint = self.ramp.step(now, plan.temp_hourly_c, value)
        allow_off = bool(h["countdown_verified"] and h["inkbird_resume_verified"]
                         and self.lim["temp_calibrated"] and plug.supports_revert)
        prev = reading.on if (reading.ok and reading.on is not None) else None
        held = ((now - self._gate_changed).total_seconds()
                if self._gate_changed else None)
        cut_off = min(float(h["cut_off_c"]), float(self.lim["alarm_c"][1]))
        d = tank_heat.decide_gate(value, fresh, setpoint, prev, held, h,
                                  allow_off, cut_off,
                                  float(self.lim["alarm_c"][0]))
        res["heater"] = {
            "setpoint_c": setpoint, "plan_c": plan.temp_hourly_c,
            "cut_off_c": cut_off,
            "gate_on": d.gate_on, "reason": d.reason, "forced": d.forced,
            "allow_off": allow_off, "observed_on": reading.on,
            "inkbird_min_c": h["inkbird_min_c"],
            "inkbird_max_c": h["inkbird_max_c"],
        }
        need = setpoint + float(h["off_above_c"])
        # Only matters when the Pi may block heat; while the gate is held ON the
        # Inkbird (cue-driven T1/T2, itc_setpoints) regulates on its own.
        if allow_off and float(h["inkbird_min_c"]) < need:
            res["findings"].append(_f(
                "inkbird_window_low", "warn",
                f"the Inkbird turns heat on below "
                f"{tank_heat.deg_f(h['inkbird_min_c'])}, but "
                f"the Pi wants to hold {tank_heat.deg_f(setpoint)} (gate off above "
                f"{tank_heat.deg_f(need)}). The Pi can only block heat, so raise the "
                f"Inkbird's heat-on setting above {tank_heat.deg_f(need)} or the tank "
                "will drift down instead of being held", True))
        if not reading.ok:
            return
        revert = None if d.gate_on else int(h["revert_after_s"])
        if reading.on == d.gate_on:
            if not d.gate_on:           # keep the dead-man armed
                if self._needs_rearm(now, plug.name, reading, revert):
                    self._command(now, plug, False, "rearm_countdown", res,
                                  revert=revert, force=True, quiet=True)
            return
        entry = self._command(now, plug, d.gate_on, f"heater:{d.reason}", res,
                              revert=revert, force=(d.gate_on or d.forced))
        if entry and entry.get("verified") and prev != d.gate_on:
            self._gate_changed = now

    def _needs_rearm(self, now, name, reading, revert) -> bool:
        """Whether the gate's countdown should be refreshed now.

        Disarmed (reads 0 or nothing) means a reset plug: refresh at once. A
        plug that does count down is refreshed under half of `revert`. A plug
        whose reading never falls is refreshed by the Pi's own clock once half
        of `revert` has passed since it was armed, and at once if the Pi does
        not know when that was (a restart).
        """
        left = reading.revert_remaining_s
        if left is None or left <= 0 or left < revert * 0.5:
            return True
        armed = self._armed_at.get(name)
        return armed is None or (now - armed).total_seconds() >= revert * 0.5

    # -- over-temperature cutoff and fans ----------------------------------
    def _save_trips(self) -> None:
        try:
            common.write_json(self._trip_path, self._trips)
        except OSError:
            pass

    def _cooling(self, now, plan, plug_reads, temp, res):
        """Work out the cutoff and the fans every tick, so the status always
        shows the thresholds and what would happen. Switch only when armed."""
        c, lim = self.c, self.lim
        armed = bool(self.cfg["enabled"] and c["enabled"])
        age = temp.age_s(now)
        fresh = (temp.ok and temp.configured and temp.value is not None
                 and (age is None or age <= _limit(temp, lim)))
        value = temp.value if fresh else None
        ramp_c = self.ramp.c if self.h["enabled"] else None
        ref = tank_cool.reference_c(c["setpoint"],
                                    plan.temp_hourly_c if plan else None, ramp_c)
        alarm_high = float(lim["alarm_c"][1])
        cutoff = tank_cool.cutoff_c(ref, c["cutoff_above_c"], alarm_high)
        label = "" if lim["temp_calibrated"] else "UNCALIBRATED probe: "
        view = {"armed": armed, "setpoint_c": ref, "cutoff_c": round(cutoff, 3),
                "resume_c": round(cutoff - float(c["resume_band_c"]), 3),
                # True when the alarm ceiling, not set point + n, set the cutoff
                # (the margin over the set point is then under cutoff_above_c).
                "limited_by_alarm": ref is not None
                and cutoff < float(ref) + float(c["cutoff_above_c"]) - 1e-9,
                "probe_c": value, "plugs": {}, "fans": None}
        res["cooling"] = view

        if armed:
            for name in [n for n in self._trips if n not in c["plugs"]]:
                self._trips.pop(name)           # no longer guarded: forget it
                self._save_trips()
        for name in c["plugs"]:
            plug = self.plugs.get(name)
            if plug is None or plug.role == "heater":
                why = ("is not in the plug array" if plug is None
                       else "is the heater gate, which has its own rules")
                view["plugs"][name] = {"tripped": False, "reason": "unusable"}
                res["findings"].append(_f(
                    f"overtemp_plug:{name}", "alert",
                    f"over-temperature cutoff: {name} {why}, so it is NOT "
                    "protected", True))
                continue
            st = self._trips.get(name) if armed else None
            off_s = (None if st is None else
                     (now - dt.datetime.fromisoformat(st["since"])).total_seconds())
            d = tank_cool.decide_cutoff(value, value is not None, cutoff,
                                        st is not None, off_s, c)
            entry = view["plugs"][name] = {"tripped": st is not None,
                                           "would_cut": d.cut, "reason": d.reason}
            if not armed:
                continue
            reading = plug_reads.get(name)
            if d.cut:
                self._cut(now, plug, reading, st, value, cutoff, d, label, res)
            elif st is not None:
                self._release(now, plug, reading, st, d, res)
            entry["tripped"] = name in self._trips

        f = c["fans"]
        if f["enabled"]:
            self._fan_on = tank_cool.decide_fans(value, value is not None, ref,
                                                 self._fan_on, f)
            executed = bool(self.cfg["enabled"] and self.fan.actuates)
            if executed or not self.fan.actuates:
                before = self.fan.wanted
                self.fan.set_on(self._fan_on)
                if self.fan.wanted != before:
                    self._audit({"ts": now.isoformat(), "event": "fan",
                                 "driver": self.fan.driver,
                                 "desired_on": self.fan.wanted,
                                 "executed": executed, "probe_c": value})
            on_at = tank_cool.fan_on_at_c(ref, f)
            view["fans"] = {"driver": self.fan.driver, "wanted": self._fan_on,
                            "on_above_c": f["on_above_c"],
                            "on_by_c": f["on_by_c"],
                            "on_at_c": None if on_at is None else round(on_at, 3),
                            "executed": executed}
        else:
            view["fans"] = {"driver": self.fan.driver, "wanted": False,
                            "enabled": False}
        self._held = set(self._trips) if armed else set()

    def _cut(self, now, plug, reading, st, value, cutoff, d, label, res):
        name = plug.name
        if st is None:
            was_on = reading.on if (reading and reading.ok) else None
            self._trips[name] = {"since": now.isoformat(), "was_on": was_on,
                                 "cutoff_c": round(cutoff, 3), "peak_c": value}
            self._save_trips()
            self._audit({"ts": now.isoformat(), "plug": name,
                         "event": "overtemp_trip", "probe_c": value,
                         "cutoff_c": cutoff, "was_on": was_on,
                         "reason": d.reason})
            res["findings"].append(_f(
                f"overtemp_cut:{name}", "alert",
                f"{label}{tank_heat.deg_f(value)} is over the "
                f"{tank_heat.deg_f(cutoff)} cutoff; "
                f"{name} is being switched off so the tank can cool", True))
        elif value is not None and value > (st.get("peak_c") or value):
            st["peak_c"] = value
            self._save_trips()
        if d.reason == "probe_lost_hold":
            res["findings"].append(_f(
                f"overtemp_probe_lost:{name}", "alert",
                f"{name} is cut off for over-temperature and the probe is "
                "lost; it stays off", True))
        if reading is None or not reading.ok:
            res["findings"].append(_f(
                f"overtemp_unreachable:{name}", "alert",
                f"{name} must be cut off for over-temperature but cannot be "
                "reached; will keep trying", True))
            return
        if reading.on:
            self._command(now, plug, False, "overtemp", res, force=True)

    def _release(self, now, plug, reading, st, d, res):
        name = plug.name
        # A lighting plug goes back to the light plan, which switches it on
        # only if the plan wants it on now. Anything else is restored to the
        # state it had when it tripped.
        resume = bool(st.get("was_on")) and plug.channel is None
        if resume:
            if reading is None or not reading.ok:
                return                  # stay cut; try again next tick
            if not reading.on:
                entry = self._command(now, plug, True, "overtemp_resume", res,
                                      force=True)
                if not (entry and entry.get("verified")):
                    return              # stay cut; try again next tick
        self._trips.pop(name, None)
        self._override.pop(name, None)
        self._last_cmd.pop(name, None)
        self._save_trips()
        self._audit({"ts": now.isoformat(), "plug": name,
                     "event": "overtemp_release", "reason": d.reason,
                     "resumed": resume})
        if d.reason == "probe_lost_release":
            res["findings"].append(_f(
                f"overtemp_probe_lost:{name}", "alert",
                f"{name} was released after the probe stayed lost; the "
                "over-temperature cutoff cannot see the water", True))
        else:
            res["findings"].append(_f(
                f"overtemp_released:{name}", "info",
                f"{name} released: the tank has cooled"))

    # -- entry point ------------------------------------------------------
    def apply(self, now, plan, plug_reads, temp, intents, lux=None) -> dict:
        self._lux = lux
        # Judge earlier commands first, even if outputs have since been switched
        # off: the evidence still arrives. The view is taken at the end, so a
        # command sent this tick already shows as pending.
        judged = {"findings": []}
        self.verify.step(now, plug_reads, lux, judged)
        res = self._apply(now, plan, plug_reads, temp, intents)
        res["findings"] = judged["findings"] + res["findings"]
        res["verify"] = self.verify.view()
        return res

    def _apply(self, now, plan, plug_reads, temp, intents) -> dict:
        res = {"enabled": bool(self.cfg["enabled"]), "commands": [],
               "findings": [], "heater": None, "cooling": None}
        # Ahead of the master-switch return: the thresholds are always
        # reported, and with no plan the cutoff still has the alarm ceiling.
        self._cooling(now, plan, plug_reads, temp, res)
        if not self.cfg["enabled"] or plan is None:
            return res
        by_plug = {i["plug"]: i for i in intents}
        # Safe mode (tank_safe.py): the lifeguard has tripped. Every lighting
        # plug is held off until the latch is released; the heater rules below
        # are untouched (safe mode never blocks heat).
        latch = tank_safe.read(self.folder)
        if latch:
            res["safe_mode"] = latch
            res["findings"].append(_f(
                "safe_mode", "alert",
                f"safe mode is latched ({latch.get('reason', '?')}: "
                f"{latch.get('detail', '')}): lights are held off. Release with "
                "`lifeguard.py reset --yes` once the cause is fixed"))
        for name, plug in self.plugs.items():
            ch = plug.channel
            if name in self._held:      # cut for over-temperature: hands off
                continue
            if plug.role == "heater" or ch not in LIGHT_CHANNELS:
                continue
            if not self.cfg["channels"].get(ch, False):
                continue
            if latch:
                self._safe_off(now, plug, plug_reads[name], res)
                continue
            self._light(now, plan, plug, plug_reads[name], by_plug[name], res)
        heater = next((p for p in self.plugs.values() if p.role == "heater"), None)
        if heater is not None and self.h["enabled"]:
            self._heater(now, plan, heater, plug_reads[heater.name], temp, res)
        return res
