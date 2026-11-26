#!/usr/bin/env python3
"""tank_verify.py -- did the world do what the cue said? (closes the loop)

The conductor's loop is cue -> action -> verify:

    cue      the plan says a channel should now be ON or OFF (tank_plan)
    action   tank_out switches the plug and reads the plug back (existing)
    verify   THIS MODULE: over the next ticks, check the effect with evidence
             that does not come from the plug's own on/off flag

A plug that reads back ON proves the relay moved, not that a lamp lit. So each
command that read back correctly leaves an expectation on disk
(tank_verify.json), and every tick compares it with what can be measured:

    watts   the plug's own draw. ON should draw more than off_max_w (and sit in
            expect_w when the plug has one); OFF should draw off_max_w or less.
            Used whenever the plug reports watts. Judged on the next tick.
    lux     the lux sensor against its reading just before the command. Used for
            a channel only when outputs.verify.channels.<ch>.min_lux_change is
            set (a lamp the sensor cannot see must not be judged by it). Judged
            from settle_s after the command until window_s.
    plug    the plug still reads what we commanded. A plug that changed on its
            own is reported as plug_changed (manual changes are handled by
            tank_out's override rule, not here).

Verdicts (one per command, written to tank_commands.jsonl as event "verify"):

    confirmed      some evidence agreed and none disagreed
    failed         evidence disagreed (warn finding effect_failed:<plug>, alert)
    plug_only      no usable evidence before window_s: only the plug's read-back
                   backs the command. Normal when no sensor or watts exist
    plug_changed   the plug stopped matching the command before it was judged
    superseded     a newer command to the same plug replaced this expectation

Also measured here: cue_lag_s, how long the plan and the plug disagreed before
the command went out (a tick or two when healthy).

Nothing here sends a command or changes a plan. Standard library only.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os

import common

CHANNELS = ("white", "white_plus", "blue", "moon")

DEFAULT_VERIFY = {
    "enabled": True,
    "settle_s": 45,            # lux is not judged before this
    "window_s": 300,           # lux evidence that never shows by now is a failure
    # Lamps commanded within this many seconds of each other cannot be told apart by
    # one lux sensor (a lamp switching off while a brighter one switches on reads as
    # a rise): lux is not used as evidence for any of them, the plug read-back and
    # watts still are. Happens at every plan transition that changes two channels.
    "confound_s": 90,
    # Per channel: min_lux_change is the rise (or fall) the lamp must make at the
    # sensor. Instead of that number, `lamp_lux` may hold what the lamp was MEASURED
    # to add at the sensor's own position (conductor.py measure-lamp), and the
    # threshold is `fraction` of it, so a lamp running below full still passes. An
    # explicit min_lux_change wins. `position` is a free-text note of where the
    # measurement was made: redo both when the sensor moves (glass-position
    # readings do not apply to a sensor that is not at the glass).
    "channels": {c: {"min_lux_change": None, "lamp_lux": None, "fraction": 0.4,
                     "position": None} for c in CHANNELS},
}


def verify_config(overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_VERIFY)
    over = dict(overrides or {})
    chans = over.pop("channels", {}) or {}
    cfg.update(over)
    for ch, vals in chans.items():
        if ch not in CHANNELS:
            raise ValueError(f"verify.channels: unknown channel {ch!r}")
        cfg["channels"][ch].update(vals or {})
    if not 5 <= float(cfg["settle_s"]) <= 1800:
        raise ValueError("verify.settle_s must be 5..1800")
    if not 0 <= float(cfg["confound_s"]) <= 600:
        raise ValueError("verify.confound_s must be 0..600")
    if not float(cfg["settle_s"]) < float(cfg["window_s"]) <= 3600:
        raise ValueError("verify.window_s must exceed settle_s and be at most 3600")
    for ch, vals in cfg["channels"].items():
        v = vals.get("min_lux_change")
        if v is not None and float(v) <= 0:
            raise ValueError(f"verify.channels.{ch}.min_lux_change must be > 0")
        v = vals.get("lamp_lux")
        if v is not None and float(v) <= 0:
            raise ValueError(f"verify.channels.{ch}.lamp_lux must be > 0")
        if not 0.05 <= float(vals.get("fraction", 0.4)) <= 1.0:
            raise ValueError(f"verify.channels.{ch}.fraction must be 0.05..1")
    return cfg


def effective_min_lux(cfg: dict, ch: str):
    """The lux change a lamp command on `ch` must make at the sensor, or None
    (lux is then not used as evidence): the explicit min_lux_change, else
    fraction x lamp_lux when a measured value is configured."""
    vals = (cfg.get("channels") or {}).get(ch) or {}
    if vals.get("min_lux_change") is not None:
        return float(vals["min_lux_change"])
    if vals.get("lamp_lux") is not None:
        return float(vals.get("fraction", 0.4)) * float(vals["lamp_lux"])
    return None


def _iso(t):
    return t.isoformat() if t else None


def _parse(s):
    return dt.datetime.fromisoformat(s) if s else None


class Verifier:
    """Holds the pending expectations (persisted) and judges them each tick."""

    def __init__(self, cfg: dict | None, folder: str, plugs, audit=None):
        self.cfg = verify_config(cfg)
        self.plugs = {p.name: p for p in plugs}
        self.path = os.path.join(folder, "tank_verify.json")
        self.audit = audit or (lambda entry: None)
        state = common.read_json(self.path, {}) or {}
        self.pending: dict[str, dict] = dict(state.get("pending") or {})
        self.last: dict[str, dict] = dict(state.get("last") or {})
        self._cue: dict[str, dict] = {}         # in memory: plan vs plug disagreement

    # -- persistence ------------------------------------------------------
    def _save(self):
        try:
            common.write_json(self.path, {"pending": self.pending,
                                          "last": self.last})
        except OSError:
            pass

    # -- cue timing ---------------------------------------------------------
    def note_cue(self, name, desired_on, observed_on, now):
        """Call every tick with the plan's wish and the plug's state. Starts a
        clock when they first disagree; clears it when they agree."""
        if observed_on is None or desired_on is None or observed_on == desired_on:
            self._cue.pop(name, None)
            return
        cur = self._cue.get(name)
        if cur is None or cur["desired"] != desired_on:
            self._cue[name] = {"desired": desired_on, "since": now}

    def cue_lag_s(self, name, desired_on, now):
        cur = self._cue.get(name)
        if cur is None or cur["desired"] != desired_on:
            return 0.0
        return round((now - cur["since"]).total_seconds(), 1)

    # -- expectations -------------------------------------------------------
    def expect(self, now, plug, desired_on, reason, lux, cue_lag_s=None):
        """Record what should now be true. Call only after a read-back that
        matched. A newer command replaces an unjudged older one."""
        if not self.cfg["enabled"]:
            return
        old = self.pending.pop(plug.name, None)
        if old:
            self._finish(now, plug.name, old, "superseded", [], quiet=True)
        before = None
        if lux is not None and lux.ok and lux.configured and lux.value is not None:
            before = float(lux.value)
        self.pending[plug.name] = {
            "plug": plug.name, "channel": plug.channel, "desired_on": bool(desired_on),
            "reason": reason, "at": _iso(now), "lux_before": before,
            "cue_lag_s": cue_lag_s, "evidence": []}
        self._save()

    # -- judging ------------------------------------------------------------
    def _confounded(self, exp) -> bool:
        """Another lamp's command is pending within confound_s of this one."""
        gap = float(self.cfg.get("confound_s", 90))
        at = _parse(exp["at"])
        for name, other in self.pending.items():
            if name == exp["plug"] or other is exp:
                continue
            if abs((_parse(other["at"]) - at).total_seconds()) <= gap:
                return True
        return False

    def _evidence(self, now, exp, plug, reading, lux):
        """New evidence items: {"kind", "agrees": bool, "detail"}, plus whether
        the lux judgement is final (its window has closed)."""
        out = []
        want = exp["desired_on"]
        if reading is not None and reading.ok and reading.on is not None \
                and reading.on != want:
            out.append({"kind": "plug", "agrees": False,
                        "detail": f"plug now reads {'ON' if reading.on else 'OFF'}"})
            return out
        if reading is not None and reading.ok and reading.power_w is not None:
            w = reading.power_w
            lo_hi = getattr(plug, "expect_w", None)
            off_max = getattr(plug, "off_max_w", 1.0)
            if want:
                ok = (lo_hi[0] <= w <= lo_hi[1]) if lo_hi else w > off_max
                need = (f"expected {lo_hi[0]}-{lo_hi[1]} W" if lo_hi
                        else f"expected more than {off_max} W")
            else:
                ok = w <= off_max
                need = f"expected {off_max} W or less"
            out.append({"kind": "watts", "agrees": ok,
                        "detail": f"{w} W, {need}"})
        ch = exp.get("channel")
        change = effective_min_lux(self.cfg, ch) if ch in CHANNELS else None
        if change is not None and self._confounded(exp):
            change = None
        age = (now - _parse(exp["at"])).total_seconds()
        if (change is not None and exp.get("lux_before") is not None
                and age >= float(self.cfg["settle_s"]) and lux is not None
                and lux.ok and lux.configured and lux.value is not None):
            delta = float(lux.value) - exp["lux_before"]
            agrees = delta >= float(change) if want else -delta >= float(change)
            if agrees or age >= float(self.cfg["window_s"]):
                out.append({"kind": "lux", "agrees": agrees,
                            "detail": f"lux {exp['lux_before']:.3g} -> "
                                      f"{float(lux.value):.3g} (needs a "
                                      f"{'rise' if want else 'fall'} of "
                                      f"{float(change):.3g})"})
        return out

    def step(self, now, plug_reads, lux, res) -> dict:
        """Judge every pending expectation. Adds findings to res; returns the
        view that goes into the status record."""
        if not self.cfg["enabled"]:
            return self.view()
        changed = False
        for name in list(self.pending):
            exp = self.pending[name]
            plug = self.plugs.get(name)
            if plug is None:
                self.pending.pop(name)
                changed = True
                continue
            new = self._evidence(now, exp, plug, plug_reads.get(name), lux)
            for item in new:
                if not any(e["kind"] == item["kind"] for e in exp["evidence"]):
                    exp["evidence"].append(item)
                    changed = True
            ev = exp["evidence"]
            age = (now - _parse(exp["at"])).total_seconds()
            verdict = None
            if any(e["kind"] == "plug" and not e["agrees"] for e in ev):
                verdict = "plug_changed"
            elif any(not e["agrees"] for e in ev):
                verdict = "failed"
            elif ev and any(e["kind"] == "lux" for e in ev):
                verdict = "confirmed"          # lux, the slow one, is the last word
            elif age >= float(self.cfg["window_s"]):
                verdict = "confirmed" if ev else "plug_only"
            if verdict:
                self.pending.pop(name)
                self._finish(now, name, exp, verdict, ev, res=res)
                changed = True
        if changed:
            self._save()
        return self.view()

    def view(self) -> dict:
        """What the status record shows: open expectations and the last verdicts."""
        if not self.cfg["enabled"]:
            return {"enabled": False, "pending": [], "last": self.last}
        return {"enabled": True,
                "pending": [{"plug": n, "desired_on": e["desired_on"],
                             "since": e["at"]} for n, e in self.pending.items()],
                "last": self.last}

    def _finish(self, now, name, exp, verdict, evidence, res=None, quiet=False):
        summary = {"verdict": verdict, "desired_on": exp["desired_on"],
                   "commanded_at": exp["at"], "judged_at": _iso(now),
                   "cue_lag_s": exp.get("cue_lag_s"), "evidence": evidence}
        self.last[name] = summary
        if not quiet:
            self.audit(dict(summary, ts=now.isoformat(), plug=name, event="verify"))
        if verdict == "failed" and res is not None:
            bad = "; ".join(f"{e['kind']}: {e['detail']}" for e in evidence
                            if not e["agrees"])
            res["findings"].append({
                "key": f"effect_failed:{name}", "level": "warn", "alert": True,
                "message": (f"{name} was switched {'ON' if exp['desired_on'] else 'OFF'}"
                            f" and read back correctly, but the effect was not seen "
                            f"({bad}). A lamp may be unplugged, burnt out, or not "
                            "visible to the sensor")})
