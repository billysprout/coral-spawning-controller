#!/usr/bin/env python3
"""lifeguard.py -- the tank's independent watchdog and safe mode.

    python lifeguard.py loop                     run forever (the compose service)
    python lifeguard.py once [--json]            one pass, print what it found
    python lifeguard.py preflight                go / no-go list of every safety layer
    python lifeguard.py ack                      stop the repeat of the open alarms
    python lifeguard.py trip --reason TEXT --yes latch safe mode by hand
    python lifeguard.py reset --yes              release safe mode

WHY IT EXISTS
The controller (tank_tick.py) checks itself: when it hangs, loses its sensors
or decides wrongly, the thing that should notice is the thing that failed.
Systems that keep something alive do not allow that. The patterns copied here:

  * Backup flight system (Space Shuttle). Four computers ran the primary
    software; a fifth ran a small, separately written program that could take
    over. The lifeguard is that fifth computer: its own container, its own
    loop, a few hundred lines, and no import of the controller's planner or
    actuator. It shares only the device drivers (tank_plugs, tank_inputs).
  * Safe mode (every deep-space probe). When the spacecraft cannot trust
    itself it stops the mission, goes to a state that cannot hurt it and waits
    for the ground. Here: every light off (lights are the tank's biggest heat
    source and darkness harms nothing), the heater left to its own thermostat,
    and a latch (tank_safe.py) that holds until the cause clears or a person
    resets it.
  * One-out-of-N trip logic (reactor protection). ANY healthy thermometer
    beyond the hard band trips, confirmed over consecutive reads. The
    controller votes its sensors for control (tank_vote.py); protection
    listens to the most alarming one.
  * Caution and warning classes (ISS). Every condition is ADVISORY, CAUTION,
    WARNING or EMERGENCY, so the page can show one word instead of a list.
  * Alarm escalation (hospital monitors). A WARNING or EMERGENCY is repeated
    every `repeat_min` until somebody acknowledges it or it clears.
  * Go / no-go poll (launch control). `preflight` lists every protective layer
    and whether it is armed, so a missing layer is seen before it is needed.
  * Cross-monitoring. The lifeguard watches the controller's heartbeat
    (tank_status.json); the controller watches the lifeguard's
    (lifeguard/status.json); each has its own off-box dead-man ping.

WHAT IT MAY DO
By default it only observes and alerts. With lifeguard.safe_mode.enabled it
may also latch safe mode. The controller obeys the latch on its next tick; the
lifeguard switches plugs itself only when the controller is silent or has not
obeyed within `direct_after_s`. It never turns a light on, and it never
switches the heater plug off. Config: the `lifeguard` block of tank_config.json.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import shutil
import sys
import time

import common
import tank_inputs
import tank_plugs
import tank_safe
import tank_vote

UTC = dt.timezone.utc
CLASSES = ("NOMINAL", "ADVISORY", "CAUTION", "WARNING", "EMERGENCY")
LIGHT_CHANNELS = ("white", "white_plus", "blue", "moon")
DEFAULT_ALARM_C = (22.78, 28.89)        # tank_tick's limits.alarm_c default (73 to 84 F)

DEFAULT = {
    "enabled": True,
    "interval_s": 60,
    # The controller writes tank_status.json every minute. Older than this and
    # it is treated as down (autoheal restarts a hung controller at about 8 min).
    "controller_silent_min": 10,
    # No healthy temperature channel for this long is a WARNING.
    "blind_min": 30,
    # The trip band: limits.alarm_c widened by hard_margin_c on both sides, or
    # an explicit [low, high] in hard_c. Outside it is an EMERGENCY.
    "hard_margin_c": 0.5,
    "hard_c": None,
    # Consecutive passes beyond the band before it counts (one bad read is not a trip).
    "confirm_reads": 2,
    "disk_free_min_mb": 500,
    # An open WARNING or EMERGENCY is sent again this often until `ack`.
    "repeat_min": 15,
    "safe_mode": {
        # Off: observe and alert only. On: trips latch safe mode.
        "enabled": False,
        # What trips it.
        "on_over_temperature": True,
        "on_controller_silent": True,
        # "auto": release once the cause has been clear for release_after_min.
        # "manual": only `lifeguard.py reset --yes` releases it.
        "latch": "auto",
        "release_after_min": 15,
        # Over-temperature releases only this far back inside the band.
        "resume_band_c": 0.5,
        # Switch the plugs directly when the controller has not put the lights
        # out this long after the latch was set (it is hung or outputs are off).
        "direct_after_s": 150,
    },
}


def _merge(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def lifeguard_config(over: dict | None = None) -> dict:
    """DEFAULT with `over` on top, validated. Raises ValueError."""
    cfg = _merge(DEFAULT, over)
    if not 15 <= float(cfg["interval_s"]) <= 600:
        raise ValueError("lifeguard.interval_s must be 15..600")
    if not 3 <= float(cfg["controller_silent_min"]) <= 120:
        raise ValueError("lifeguard.controller_silent_min must be 3..120")
    if not 5 <= float(cfg["blind_min"]) <= 720:
        raise ValueError("lifeguard.blind_min must be 5..720")
    if not 0 <= float(cfg["hard_margin_c"]) <= 3:
        raise ValueError("lifeguard.hard_margin_c must be 0..3")
    hc = cfg["hard_c"]
    if hc is not None and not (isinstance(hc, (list, tuple)) and len(hc) == 2
                               and 15 <= float(hc[0]) < float(hc[1]) <= 35):
        raise ValueError("lifeguard.hard_c must be [low, high] within 15..35 C")
    if not 1 <= int(cfg["confirm_reads"]) <= 10:
        raise ValueError("lifeguard.confirm_reads must be 1..10")
    if not 1 <= float(cfg["repeat_min"]) <= 1440:
        raise ValueError("lifeguard.repeat_min must be 1..1440")
    sm = cfg["safe_mode"]
    if sm["latch"] not in ("auto", "manual"):
        raise ValueError('lifeguard.safe_mode.latch must be "auto" or "manual"')
    if not 1 <= float(sm["release_after_min"]) <= 1440:
        raise ValueError("lifeguard.safe_mode.release_after_min must be 1..1440")
    if not 0 <= float(sm["resume_band_c"]) <= 3:
        raise ValueError("lifeguard.safe_mode.resume_band_c must be 0..3")
    if not 60 <= float(sm["direct_after_s"]) <= 3600:
        raise ValueError("lifeguard.safe_mode.direct_after_s must be 60..3600")
    return cfg


def deg_f(c: float) -> str:
    return f"{c * 9 / 5 + 32:.1f} F"


def _ago(seconds: float) -> str:
    s = float(seconds)
    if s < 120:
        return f"{s:.0f} s"
    if s < 7200:
        return f"{s / 60:.0f} min"
    if s < 172800:
        return f"{s / 3600:.0f} h"
    return f"{s / 86400:.0f} days"


def _iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _parse(text) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


class Lifeguard:
    def __init__(self, data_dir: str, config_path: str | None = None,
                 notify=common.send_alert, deadman=None, plug_loader=None,
                 env=None, disk_free_mb=None):
        self.data = data_dir
        self.cfg_path = config_path or os.environ.get("TANK_CONFIG") or os.path.join(
            data_dir, "tank_config.json")
        self.folder = tank_safe.folder(data_dir)
        self.state_path = os.path.join(self.folder, "state.json")
        self.status_path = os.path.join(self.folder, "status.json")
        self.events_path = os.path.join(self.folder, "events.jsonl")
        self.notify = notify
        self.deadman = deadman or self._ping
        self.plug_loader = plug_loader or self._load_plugs
        self.env = os.environ if env is None else env
        self.disk_free_mb = disk_free_mb or self._disk_free_mb
        self.started = dt.datetime.now(UTC)
        self._raw: dict = {}
        self._inputs = None
        self._inputs_spec = None
        st = common.read_json(self.state_path, {}) or {}
        # open: {key: {"class", "message", "since", "last_sent", "acked"}}
        self.open: dict[str, dict] = dict(st.get("open") or {})
        self.counts: dict[str, int] = dict(st.get("counts") or {})
        self.blind_since = _parse(st.get("blind_since"))
        self.clear_since = _parse(st.get("clear_since"))
        self.direct_at: dict[str, str] = dict(st.get("direct_at") or {})

    # -- plumbing -----------------------------------------------------------
    @staticmethod
    def _ping():
        """The lifeguard's own off-site dead-man (not the controller's)."""
        url = os.environ.get("LIFEGUARD_DEADMAN_URL")
        return common.ping_deadman(url) if url else None

    def _save(self) -> None:
        common.write_json(self.state_path, {
            "open": self.open, "counts": self.counts,
            "blind_since": self.blind_since and _iso(self.blind_since),
            "clear_since": self.clear_since and _iso(self.clear_since),
            "direct_at": self.direct_at})

    def _event(self, now, kind: str, **fields) -> None:
        rec = {"ts": _iso(now), "event": kind}
        rec.update(fields)
        try:
            os.makedirs(self.folder, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass
        common.log("lifeguard_" + kind, **fields)

    def _config(self) -> tuple[dict, dict, str | None]:
        """(raw tank config, lifeguard config, config error). A config that is
        missing or broken keeps the last good one, and the defaults before that:
        the watchdog must keep running when the file it shares is damaged."""
        err = None
        if os.path.exists(self.cfg_path):
            raw = common.read_json(self.cfg_path)
            if isinstance(raw, dict):
                self._raw = raw
            else:
                err = "tank_config.json cannot be read"
        try:
            lg = lifeguard_config(self._raw.get("lifeguard"))
        except (ValueError, TypeError, KeyError) as e:
            err = f"lifeguard settings are not valid ({e}); using the defaults"
            lg = lifeguard_config(None)
        return self._raw, lg, err

    def _reads(self, raw: dict, now) -> dict:
        spec = raw.get("inputs") or {}
        key = json.dumps(spec, sort_keys=True)
        if self._inputs is None or key != self._inputs_spec:
            try:
                self._inputs = tank_inputs.Inputs.from_config(spec)
            except (ValueError, TypeError):
                self._inputs = tank_inputs.Inputs.from_config({})
            self._inputs_spec = key
        return self._inputs.read_all(now)

    def _load_plugs(self, raw: dict) -> list:
        folder = os.path.join(self.data, "tuya")
        array = raw.get("plug_array") or os.path.join(folder, "plug_array.json")
        devices = raw.get("devices") or os.path.join(folder, "devices.json")
        extra = raw.get("extra_plugs") or os.path.join(self.data, "extra_plugs.json")
        if not os.path.exists(array):
            return []
        return tank_plugs.load_plugs(array, devices, extra)

    def _disk_free_mb(self) -> float | None:
        try:
            return shutil.disk_usage(self.data).free / 1e6
        except OSError:
            return None

    def _controller(self, now, lg) -> dict:
        """The controller's heartbeat: tank_status.json and what it last said."""
        p = os.path.join(self.data, "tank_status.json")
        seen = None
        try:
            seen = dt.datetime.fromtimestamp(os.path.getmtime(p), UTC)
        except OSError:
            pass
        # Silence is counted from the newer of the status file and the lifeguard's
        # own start: a status left over from before a shutdown must not raise the
        # alarm the moment both containers come back up.
        ref = max(seen, self.started) if seen else self.started
        age = max(0.0, (now - ref).total_seconds())
        rec = common.read_json(p, {}) if seen else {}
        return {"seen": seen is not None, "age_s": round(age),
                "file_age_s": round(max(0.0, (now - seen).total_seconds())) if seen else None,
                "alive": age <= float(lg["controller_silent_min"]) * 60,
                "record": rec if isinstance(rec, dict) else {}}

    # -- one pass -----------------------------------------------------------
    def step(self, now: dt.datetime | None = None, dry: bool = False) -> dict:
        """One pass. dry: look only (no latch, no switching, no alert, no file)."""
        now = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        raw, lg, cfg_err = self._config()
        sm = lg["safe_mode"]
        ctrl = self._controller(now, lg)
        reads = self._reads(raw, now)
        lim = raw.get("limits") or {}
        v = tank_vote.vote(reads, now, float(lim.get("input_max_age_s", 180)),
                           {"failover": False,
                            "miscompare_c": (lim.get("redundancy") or {}).get(
                                "miscompare_c", 1.0)})
        alarm = lim.get("alarm_c") or DEFAULT_ALARM_C
        hard = lg["hard_c"] or (float(alarm[0]) - float(lg["hard_margin_c"]),
                                float(alarm[1]) + float(lg["hard_margin_c"]))
        hard = (float(hard[0]), float(hard[1]))
        need = int(lg["confirm_reads"])

        conds: dict[str, tuple[str, str]] = {}      # key -> (class, message)

        def count(key: str, hit: bool) -> bool:
            self.counts[key] = self.counts.get(key, 0) + 1 if hit else 0
            return self.counts[key] >= need

        # 1. Water beyond the hard band: any healthy channel may trip.
        hot = count("over", v["high_c"] is not None and v["high_c"] > hard[1])
        cold = count("under", v["low_c"] is not None and v["low_c"] < hard[0])
        if hot:
            conds["over_temperature"] = ("EMERGENCY", (
                f"water is {deg_f(v['high_c'])}, above the hard limit {deg_f(hard[1])} "
                f"({self._who(reads, v, max)})"))
        if cold:
            conds["under_temperature"] = ("EMERGENCY", (
                f"water is {deg_f(v['low_c'])}, below the hard limit {deg_f(hard[0])} "
                f"({self._who(reads, v, min)}). Check the heater and its controller"))

        # 2. Nobody can see the water.
        if v["configured"] and not v["healthy"]:
            self.blind_since = self.blind_since or now
            mins = (now - self.blind_since).total_seconds() / 60
            if mins >= float(lg["blind_min"]):
                conds["blind"] = ("WARNING", (
                    f"no thermometer has given a usable reading for {mins:.0f} min: "
                    "the tank's temperature is not known"))
        else:
            self.blind_since = None
        if len(v["configured"]) >= 2 and len(v["healthy"]) == 1:
            conds["redundancy_lost"] = ("CAUTION", (
                f"only one of {len(v['configured'])} thermometers is reading "
                f"({v['healthy'][0]})"))
        if v["mode"] == "miscompare":
            conds["miscompare"] = ("CAUTION", (
                f"the two thermometers differ by {v['spread_c']:.2f} C"))
        for n in v["outvoted"]:
            conds[f"outvoted:{n}"] = ("CAUTION", f"{n} disagrees with the other two")

        # 3. The controller's heartbeat.
        if not ctrl["alive"]:
            what = ("has not written its status" if ctrl["seen"]
                    else "has never written a status")
            conds["controller_silent"] = ("WARNING", (
                f"the controller {what} for {_ago(ctrl['age_s'])} "
                "(tank_status.json). Check `docker ps` and the tank-controller log"))

        # 4. The host.
        free = self.disk_free_mb()
        if free is not None and free < float(lg["disk_free_min_mb"]):
            conds["disk_low"] = ("CAUTION", (
                f"only {free:.0f} MB free where the data lives; the controller "
                "stops when it cannot write"))
        if cfg_err:
            conds["config"] = ("CAUTION", cfg_err)

        # 5. Safe mode.
        latch = tank_safe.read(self.data)
        causes = []
        if sm["on_over_temperature"] and "over_temperature" in conds:
            causes.append("over_temperature")
        if sm["on_controller_silent"] and "controller_silent" in conds:
            causes.append("controller_silent")
        actions = []
        act = bool(lg["enabled"]) and not dry
        if act and sm["enabled"] and causes and not latch:
            latch = tank_safe.set_latch(self.data, causes[0], conds[causes[0]][1], now)
            self.clear_since = None
            self._event(now, "safe_mode_latched", reason=causes[0],
                        detail=conds[causes[0]][1])
        if latch:
            conds["safe_mode"] = ("WARNING", (
                f"safe mode is latched since {latch.get('since', '?')} "
                f"({latch.get('reason', '?')}): lights are held off"))
            if act and sm["enabled"]:
                actions = self._enforce(now, raw, sm, latch, ctrl, conds)
                self._maybe_release(now, sm, latch, causes, v, hard, ctrl)
        latch = tank_safe.read(self.data)
        if not latch:
            conds.pop("safe_mode", None)
            self.clear_since = None

        layers = self.layers(raw, lg, v, ctrl, latch, free)
        for layer in layers:
            if layer["state"] == "nogo" and layer["id"] not in ("sensors", "controller"):
                conds.setdefault(f"layer:{layer['id']}", (
                    "ADVISORY", f"{layer['name']}: {layer['detail']}"))

        sent = self._alerts(now, lg, conds) if act else []
        level = max((CLASSES.index(c) for c, _ in conds.values()), default=0)
        status = {
            "ts": _iso(now),
            "class": CLASSES[level],
            "conditions": [{"key": k, "class": c, "message": m,
                            "since": self.open.get(k, {}).get("since"),
                            "acked": bool(self.open.get(k, {}).get("acked"))}
                           for k, (c, m) in sorted(
                               conds.items(), key=lambda kv: -CLASSES.index(kv[1][0]))],
            "enabled": bool(lg["enabled"]),
            "safe_mode": {"armed": bool(sm["enabled"]), "latched": bool(latch),
                          "latch": latch, "release": sm["latch"]},
            "hard_c": [round(hard[0], 2), round(hard[1], 2)],
            "temperature": tank_vote.view(v),
            "controller": {"alive": ctrl["alive"], "age_s": ctrl["age_s"],
                           "seen": ctrl["seen"]},
            "disk_free_mb": None if free is None else round(free),
            "layers": layers,
            "actions": actions,
            "alerts_sent": sent,
        }
        if dry:
            return status
        try:
            common.write_json(self.status_path, status)
            self._save()
        except OSError as err:
            common.log("lifeguard_write_failed", error=repr(err))
        try:
            self.deadman()
        except Exception:  # noqa: BLE001 - a ping must not stop the watchdog
            pass
        return status

    @staticmethod
    def _who(reads, v, pick) -> str:
        vals = {n: reads[n].value for n in v["healthy"]}
        name = pick(vals, key=vals.get)
        return getattr(reads[name], "label", "") or name

    # -- safe mode ----------------------------------------------------------
    def _enforce(self, now, raw, sm, latch, ctrl, conds) -> list:
        """Put the lights out directly, but only when the controller is not
        doing it: it is silent, or the latch is older than direct_after_s and its
        last status still shows a light on."""
        since = _parse(latch.get("since")) or now
        waited = (now - since).total_seconds()
        plugs_seen = (ctrl["record"].get("plugs") or {}) if ctrl["alive"] else {}
        lit = [n for n, p in plugs_seen.items()
               if p.get("channel") in LIGHT_CHANNELS and p.get("on")]
        if ctrl["alive"] and (waited < float(sm["direct_after_s"]) or not lit):
            return []
        out = []
        try:
            plugs = self.plug_loader(raw)
        except Exception as err:  # noqa: BLE001
            conds["safe_mode_failed"] = ("EMERGENCY", (
                f"safe mode cannot load the plugs ({type(err).__name__}); "
                "switch the lights off by hand"))
            return [{"error": repr(err)[:120]}]
        for plug in plugs:
            light = plug.channel in LIGHT_CHANNELS
            # The heater plug is only ever switched ON, and only when the
            # controller is gone (it normally holds the gate): heat is then left
            # to the heater's own thermostat.
            heat_on = (plug.role == "heater" and not ctrl["alive"]
                       and latch.get("reason") != "over_temperature")
            if not light and not heat_on:
                continue
            last = _parse(self.direct_at.get(plug.name))
            if last and (now - last).total_seconds() < 300:
                continue
            try:
                r = plug.status()
            except Exception as err:  # noqa: BLE001
                out.append({"plug": plug.name, "error": type(err).__name__})
                continue
            if not r.ok or r.on is None:
                out.append({"plug": plug.name, "error": r.detail or "no answer"})
                continue
            want = not light            # lights off; heater gate on
            if r.on == want:
                continue
            entry = {"plug": plug.name, "desired_on": want, "verified": False}
            try:
                if light and getattr(plug, "supports_level", False):
                    plug.set_level(0.0)
                else:
                    plug.set_on(want)
                back = plug.status()
                entry["verified"] = bool(back.ok and back.on == want)
            except Exception as err:  # noqa: BLE001 - one plug must not stop the rest
                entry["error"] = str(err)[:120] or type(err).__name__
            self.direct_at[plug.name] = _iso(now)
            self._event(now, "direct_command", **entry)
            out.append(entry)
        bad = [e["plug"] for e in out if e.get("error") or e.get("verified") is False]
        if bad:
            conds["safe_mode_failed"] = ("EMERGENCY", (
                "safe mode could not switch " + ", ".join(sorted(set(bad)))
                + "; switch it by hand"))
        return out

    def _maybe_release(self, now, sm, latch, causes, v, hard, ctrl) -> None:
        if sm["latch"] != "auto" or latch.get("by") != "lifeguard":
            return                      # a hand trip is released by hand
        clear = not causes and ctrl["alive"]
        if latch.get("reason") == "over_temperature":
            clear = clear and v["high_c"] is not None and (
                v["high_c"] <= hard[1] - float(sm["resume_band_c"]))
        if not clear:
            self.clear_since = None
            return
        self.clear_since = self.clear_since or now
        if (now - self.clear_since).total_seconds() >= float(sm["release_after_min"]) * 60:
            tank_safe.clear(self.data, now, "lifeguard (cause cleared)")
            self._event(now, "safe_mode_released", reason=latch.get("reason"))
            self.clear_since = None

    # -- alerts -------------------------------------------------------------
    def _alerts(self, now, lg, conds) -> list:
        """WARNING and EMERGENCY are sent when they open, repeated every
        repeat_min until acknowledged, and followed by one notice when they
        clear. CAUTION and ADVISORY only show in the status."""
        sent = []
        repeat = float(lg["repeat_min"]) * 60
        for key, (cls, msg) in conds.items():
            o = self.open.get(key)
            if o is None:
                o = self.open[key] = {"class": cls, "message": msg, "since": _iso(now),
                                      "last_sent": None, "acked": False}
                self._event(now, "opened", key=key, cls=cls, message=msg)
            o["class"], o["message"] = cls, msg
            if CLASSES.index(cls) < CLASSES.index("WARNING") or o["acked"]:
                continue
            last = _parse(o["last_sent"])
            if last is None or (now - last).total_seconds() >= repeat:
                again = "" if last is None else " (still open; `lifeguard.py ack` stops the repeat)"
                if self.notify(f"{cls}: {msg}{again}", "Tank lifeguard"):
                    o["last_sent"] = _iso(now)
                    sent.append(key)
        for key in [k for k in self.open if k not in conds]:
            o = self.open.pop(key)
            self._event(now, "cleared", key=key, cls=o.get("class"))
            if o.get("last_sent"):
                self.notify(f"cleared: {o.get('message', key)}", "Tank lifeguard")
                sent.append(f"cleared:{key}")
        return sent

    def ack(self, now=None) -> list:
        now = now or dt.datetime.now(UTC)
        keys = [k for k, o in self.open.items() if not o.get("acked")]
        for k in keys:
            self.open[k]["acked"] = True
        self._event(now, "acknowledged", keys=keys)
        self._save()
        return keys

    # -- go / no-go ---------------------------------------------------------
    def layers(self, raw, lg, v, ctrl, latch, free) -> list:
        """Every protective layer and whether it is armed.
        state: go | caution | nogo | off (not set up; not a fault)."""
        out_cfg = raw.get("outputs") or {}
        lim = raw.get("limits") or {}
        outputs_on = bool(out_cfg.get("enabled"))
        plugs = (ctrl["record"].get("plugs") or {}) if ctrl["seen"] else {}
        rows = []

        def add(id_, name, state, detail):
            rows.append({"id": id_, "name": name, "state": state, "detail": detail})

        n, m = len(v["healthy"]), len(v["configured"])
        if m == 0:
            add("sensors", "Thermometers", "nogo", "no thermometer is set up")
        else:
            add("sensors", "Thermometers", "go" if n >= 2 else ("caution" if n == 1 else "nogo"),
                f"{n} of {m} reading" + ("" if n >= 2 else
                                         "; a second thermometer gives a backup" if m == 1 else ""))
        fo = bool((lim.get("redundancy") or {}).get("failover"))
        add("failover", "Backup thermometer takes over",
            "go" if fo and m >= 2 else "off",
            "on" if fo and m >= 2 else
            ("off (limits.redundancy.failover)" if m >= 2 else "needs a second thermometer"))
        cool = out_cfg.get("cooling") or {}
        armed = outputs_on and bool(cool.get("enabled")) and bool(cool.get("plugs"))
        add("cutoff", "Over-temperature cutoff", "go" if armed else "nogo",
            "armed on " + ", ".join(cool.get("plugs") or []) if armed else "not armed")
        dm = out_cfg.get("lamp_deadman") or {}
        add("lamp_backstop", "Lights switch themselves off if the controller dies",
            "go" if outputs_on and dm.get("enabled") else "nogo",
            "on (plug countdown)" if outputs_on and dm.get("enabled") else "off")
        heater = [nm for nm, p in plugs.items() if p.get("role") == "heater"]
        h = out_cfg.get("heater") or {}
        if heater:
            ok = bool(h.get("countdown_verified")) and bool(h.get("inkbird_resume_verified"))
            add("heater_gate", "Heater plug comes back on by itself",
                "go" if ok else "caution",
                "pull-the-plug tests done" if ok else
                "pull-the-plug tests not recorded as done; the controller will not block heat")
        else:
            add("heater_gate", "Heater plug comes back on by itself", "off",
                "no heater plug in the array")
        add("controller", "Controller heartbeat", "go" if ctrl["alive"] else "nogo",
            f"last status {_ago(ctrl['file_age_s'])} ago" if ctrl["seen"] else "no status file yet")
        add("safe_mode", "Safe mode",
            "caution" if latch else ("go" if lg["safe_mode"]["enabled"] else "off"),
            "LATCHED: lights held off" if latch else
            ("armed" if lg["safe_mode"]["enabled"] else
             "not armed (lifeguard.safe_mode.enabled); the lifeguard only alerts"))
        chans = self._channels()
        add("alerts", "Alert channels",
            "go" if len(chans) >= 2 else ("caution" if chans else "nogo"),
            (", ".join(chans) + ("" if len(chans) >= 2 else
                                 "; one channel only: add a second so one outage cannot silence an alarm"))
            if chans else "none set: nobody hears an alarm")
        dead = [k for k in ("TANK_DEADMAN_URL", "LIFEGUARD_DEADMAN_URL") if self.env.get(k)]
        add("deadman", "Off-site dead-man ping", "go" if dead else "nogo",
            f"{len(dead)} of 2 set" if dead else
            "not set: nothing tells you when this computer is off")
        if free is not None:
            low = free < float(lg["disk_free_min_mb"])
            add("disk", "Disk space", "caution" if low else "go", f"{free / 1000:.1f} GB free")
        return rows

    def _channels(self) -> list:
        e = self.env
        chans = []
        if e.get("PUSHOVER_TOKEN") and e.get("PUSHOVER_USER"):
            chans.append("pushover")
        if e.get("NTFY_TOPIC"):
            chans.append("ntfy")
        if e.get("TELEGRAM_BOT_TOKEN") and e.get("TELEGRAM_CHAT_ID"):
            chans.append("telegram")
        return chans


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
MARK = {"go": "GO     ", "caution": "CAUTION", "nogo": "NO-GO  ", "off": "off    "}


def format_preflight(status: dict) -> str:
    lines = [f"Life support: {status['class']}"]
    for layer in status["layers"]:
        lines.append(f"  {MARK[layer['state']]}  {layer['name']}: {layer['detail']}")
    for c in status["conditions"]:
        lines.append(f"  [{c['class']}] {c['message']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lifeguard.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("loop")
    once = sub.add_parser("once")
    once.add_argument("--json", action="store_true")
    sub.add_parser("preflight")
    sub.add_parser("ack")
    trip = sub.add_parser("trip")
    trip.add_argument("--reason", required=True)
    trip.add_argument("--yes", action="store_true")
    reset = sub.add_parser("reset")
    reset.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)

    common.load_env()
    data = common.data_dir()
    now = dt.datetime.now(UTC)

    if args.cmd == "trip":
        if not args.yes:
            print("this latches safe mode: every light is held off until `reset`. Add --yes")
            return 2
        tank_safe.set_latch(data, "hand", args.reason, now, by="hand")
        print("safe mode latched; release with `lifeguard.py reset --yes`")
        return 0
    if args.cmd == "reset":
        if not args.yes:
            print("this releases safe mode: the lights follow the plan again. Add --yes")
            return 2
        was = tank_safe.clear(data, now, "hand")
        print("safe mode released" if was else "safe mode was not latched")
        return 0

    lg = Lifeguard(data)
    if args.cmd == "ack":
        keys = lg.ack(now)
        print("acknowledged: " + (", ".join(keys) if keys else "nothing open"))
        return 0
    if args.cmd == "once":
        status = lg.step(now)
        print(json.dumps(status, indent=2) if args.json else format_preflight(status))
        return 0
    if args.cmd == "preflight":
        status = lg.step(now, dry=True)             # a look, not an alarm
        print(format_preflight(status))
        return 1 if any(x["state"] == "nogo" for x in status["layers"]) else 0

    stop = common.StopFlag()
    common.log("lifeguard_start", data=data)
    while not stop.stopped:
        t0 = time.monotonic()
        interval = 60.0
        try:
            status = lg.step()
            interval = float(lifeguard_config(lg._raw.get("lifeguard"))["interval_s"])
            common.log("lifeguard_pass", cls=status["class"],
                       conditions=[c["key"] for c in status["conditions"]])
        except Exception as err:  # noqa: BLE001 - the watchdog itself must not die
            common.log("lifeguard_error", error=repr(err))
        if stop.sleep(max(1.0, interval - (time.monotonic() - t0))):
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
