#!/usr/bin/env python3
"""tank_tick.py -- the minute conductor: plan, observe, compare, record.

Once a minute it
  1. asks tank_plan for the desired state (temperature, full_dark / any_sun /
     any_moon, sun curves, moon target),
  2. reads the inputs (temperature probe, lux sensor) and every plug's state,
  3. compares observed with desired and raises findings (alerts),
  4. works out what each plug WOULD be told to do (its "intent"),
  5. records all of it.

By default it switches nothing: intents are logged with executed=false.
Switching lives in tank_out.Actuator and happens only when the config sets
outputs.enabled, per channel, and (for the heater gate) after the human checks
in tank_heat.DEFAULT_HEATER are done. Every command is read back and audited in
tank_commands.jsonl. See pi-contingency-plan.md sections 3.6 and 4.3.

    python conductor.py tick  [--now ISO] [--offline] [--json]
    python conductor.py loop
    python conductor.py plan  --date YYYY-MM-DD
    python conductor.py verify-countdown --plug NAME [--seconds 90] --yes
    python conductor.py health [--plug NAME] [--json]     read-only plug check
    python conductor.py onboard [--list | --show | --reset | ...]  pick the arcs
    python conductor.py switch --plug NAME on|off [--revert-after S] --yes
                                                          manual switch, read back
    python conductor.py measure-lamp --plug NAME --yes     what a lamp does at the lux sensor

Config: tank_config.json (TANK_CONFIG, default <data>/tank_config.json). All
keys optional; see DEFAULT_CONFIG.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import sys
import time

import common
import cues
import lifeguard
import tank_arcs
import tank_cool
import tank_health
import tank_heat_log
import tank_heat
import tank_inputs
import tank_itc
import tank_itc_watch
import tank_lamp
import tank_out
import tank_plan
import tank_plugs
import tank_safe
import tank_scan
import tank_verify
import tank_vote
import tank_wedge

UTC = dt.timezone.utc
_HERE = os.path.dirname(os.path.abspath(__file__))

ONBOARD_CMD = ("docker compose run --rm -it tank python /app/scripts/conductor.py "
               "onboard")

DEFAULT_CONFIG = {
    "plan": {},                  # overrides for tank_plan.DEFAULT_PLAN_CONFIG
    "inputs": {},                # {"temp": {driver...}, "lux": {driver...}}
    "plug_array": None,          # default <data>/tuya/plug_array.json
    "extra_plugs": None,     # default <data>/extra_plugs.json (optional)
    "devices": None,             # default <data>/tuya/devices.json (local keys)
    "interval_s": 60,
    # Listen for the plugs' UDP broadcasts when the loop starts and compare each
    # plug's IP with the array (tank_scan.py). heal: use the new IP for this run.
    "plug_scan": {"enabled": True, "timeout_s": 15.0, "heal": True},
    # Everything off until a human flips it. See tank_out.py / tank_heat.py.
    "outputs": copy.deepcopy(tank_out.DEFAULT_OUTPUTS),
    # Cue-driven Inkbird T1/T2 changes (tank_itc.py). Off by default; on, it only
    # writes <data>/itc/target.json for itc_setpoints.py, which owns the phone.
    "itc_setpoints": copy.deepcopy(tank_itc.DEFAULT_ITC),
    # Does the tank heat below T1 and stop above T2 as the Inkbird is set
    # (tank_itc_watch.py)? Alerts, plus a fan stub that switches nothing.
    "itc_watch": copy.deepcopy(tank_itc_watch.DEFAULT_WATCH),
    # What the lights and the heater do to the temperature, one line a minute
    # (tank_heat_log.py); heat_report.py works on it.
    "heat_log": copy.deepcopy(tank_heat_log.DEFAULT_HEAT_LOG),
    # The independent watchdog and safe mode (lifeguard.py, its own container).
    # The controller only reads this block to validate it and to know whether a
    # lifeguard is expected; the lifeguard reads it for everything else.
    "lifeguard": copy.deepcopy(lifeguard.DEFAULT),
    "limits": {
        # Several thermometers, one decision (tank_vote.py). failover: act on a
        # healthy backup when the primary cannot be used. Off: report only.
        "redundancy": copy.deepcopy(tank_vote.DEFAULT_REDUNDANCY),
        # The lifeguard writes its status every pass; older than this and the
        # controller says the watchdog itself is down.
        "lifeguard_silent_min": 5,
        # Alarm band, 73 to 84 F (pi-contingency-plan.md 4.3). The top sits above
        # the calendar's warmest target (the 83 F cap, 28.33 C) so a tank that is
        # following the calendar never alarms; it is also the cooling cutoff's
        # ceiling (tank_cool.cutoff_c) and matches the Inkbird's high alarm. The
        # ladder above the cap: heat cut-off 83 F (outputs.heater.cut_off_c),
        # fans 83.5 F (cooling.fans.on_by_c), plugs cut and alarm 84 F.
        # Raised from 27.78, then 28.5, on 2026-10-30/31.
        "alarm_c": [22.78, 28.89],
        # Alert when the tank keeps warming after reaching the heat cut-off.
        "still_warming": copy.deepcopy(tank_heat.DEFAULT_STILL_WARMING),
        "input_max_age_s": 180,
        # The Inkbird reading arrives every 5 minutes through its app, so it gets
        # a longer allowance before it is too old to compare with the probe.
        "inkbird_max_age_s": 900,
        # Light leak in full dark. 0.2 lux sits just above what the VEML7700 read
        # through the quiet hours of 2026-11-03 (01:00 to 07:00 PST, 2,156 samples:
        # 0.144 to 0.169 lux, 40 to 47 counts). Not calibrated against a reference
        # meter; the sensor is not in a blackout enclosure, so its floor may be the
        # room, not the chip. Re-measure with the sensor covered before trusting it.
        "dark_lux_max": 0.2,
        # Consecutive full-dark minutes above dark_lux_max before light_leak alerts.
        # 1 = at once. The 0.17 to 0.43 lux "floor" of 2026-11-03 to 05 turned out to
        # be room light (a status LED, the curtain); with those fixed, lamps-off night
        # minutes read 0.04 to 0.14 lux (2026-11-14 to 25, 2,104 minutes) and real
        # leaks 0.9 to 13 lux. The tank config uses 0.6 lux and 3 minutes.
        "dark_lux_confirm_min": 1,
        "plug_offline_min": 5,
        # A plug that fails to answer but is still on the network (answers pings)
        # is a wedged plug, a different alert from "offline" (tank_wedge.py).
        "plug_wedge": copy.deepcopy(tank_wedge.DEFAULT_WEDGE),
        "calendar_low_days": 30,
        "alert_cooldown_min": 30,
        # A send that reached no channel is tried again after this many seconds
        # instead of starting the cooldown (the Telegram call can time out).
        "alert_retry_s": 120,
        # "Recovered" notice when an alert that was delivered has stayed clear
        # for recovery_confirm_min minutes (so a flapping sensor does not spam).
        "recovery_alerts": True,
        "recovery_confirm_min": 2,
        # An input timestamp this far ahead of the host clock means the two clocks
        # disagree (a skewed Pi, or a slow Windows clock under Docker).
        "clock_skew_s": 120,
        # An open alert whose sensor stays unreadable this long is dropped without a
        # recovery notice (a second-opinion probe that is simply gone).
        "unknown_expire_min": 120,
        # Finding keys whose Telegram alert is held back (the finding still shows
        # in the status and the log). A key matches itself and any "key:detail"
        # form, so "calendar" also covers "calendar:<problem>". Empty by default.
        "mute_alerts": [],
        # Off by default: with no output stage, drift just shows how the
        # existing plug schedules differ from the calendar. It is not a fault.
        "alert_on_drift": False,
        "temp_calibrated": False,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("TANK_CONFIG") or os.path.join(
        common.data_dir(), "tank_config.json")
    over = common.read_json(path, {}) if os.path.exists(path) else {}
    cfg = _merge(DEFAULT_CONFIG, over)
    cfg["plan"] = tank_plan.plan_config(cfg["plan"])      # validates
    tank_heat.heater_config(cfg["outputs"].get("heater"))  # validates
    tank_heat.still_warming_config(cfg["limits"].get("still_warming"))  # validates
    tank_cool.cooling_config(cfg["outputs"].get("cooling"))  # validates
    tank_verify.verify_config(cfg["outputs"].get("verify"))  # validates
    tank_itc.itc_config(cfg["itc_setpoints"])  # validates
    tank_itc_watch.watch_config(cfg["itc_watch"])  # validates
    tank_heat_log.heat_log_config(cfg["heat_log"])  # validates
    if not 3 <= float(cfg["plug_scan"]["timeout_s"]) <= 60:
        raise ValueError("plug_scan.timeout_s must be 3..60")
    lim = cfg["limits"]
    tank_wedge.wedge_config(lim["plug_wedge"])  # validates
    tank_vote.redundancy_config(lim["redundancy"])  # validates
    lifeguard.lifeguard_config(cfg["lifeguard"])  # validates
    if not 2 <= float(lim["lifeguard_silent_min"]) <= 120:
        raise ValueError("limits.lifeguard_silent_min must be 2..120")
    if not 30 <= float(lim["alert_retry_s"]) <= 3600:
        raise ValueError("limits.alert_retry_s must be 30..3600")
    if not 0 <= float(lim["recovery_confirm_min"]) <= 120:
        raise ValueError("limits.recovery_confirm_min must be 0..120")
    if not 30 <= float(lim["clock_skew_s"]) <= 3600:
        raise ValueError("limits.clock_skew_s must be 30..3600")
    if not 1 <= int(lim["dark_lux_confirm_min"]) <= 60:
        raise ValueError("limits.dark_lux_confirm_min must be 1..60")
    mute = cfg["limits"]["mute_alerts"]
    if not (isinstance(mute, list) and all(isinstance(k, str) and k for k in mute)):
        raise ValueError("limits.mute_alerts must be a list of finding keys")
    folder = os.path.join(common.data_dir(), "tuya")
    cfg["plug_array"] = cfg["plug_array"] or os.path.join(folder, "plug_array.json")
    cfg["devices"] = cfg["devices"] or os.path.join(folder, "devices.json")
    cfg["extra_plugs"] = cfg["extra_plugs"] or os.path.join(
        common.data_dir(), "extra_plugs.json")
    return cfg


def load_array(cfg: dict) -> list:
    """Every plug in both arrays (the Tuya plugs, then the extra-plugs file)."""
    return tank_plugs.load_plugs(cfg["plug_array"], cfg["devices"],
                                 cfg["extra_plugs"])


# ---------------------------------------------------------------------------
# SINK -- records only
# ---------------------------------------------------------------------------
class DryRunSink:
    """Writes the latest state, a compact input line every tick, and the full
    record when something changed (and hourly). No device I/O of any kind."""

    def __init__(self, folder: str):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self._last_sig = None
        self._n = 0

    def _append(self, name: str, obj: dict) -> None:
        with open(os.path.join(self.folder, name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, default=str, separators=(",", ":")) + "\n")

    def write(self, record: dict, signature, now: dt.datetime) -> bool:
        self._n += 1
        common.write_json(os.path.join(self.folder, "tank_status.json"), record)
        day = now.strftime("%Y%m%d")
        extra = {}
        if "inkbird_temp" in record["inputs"]:
            extra = {"inkbird_c": record["inputs"]["inkbird_temp"]["value"],
                     "inkbird_diff_c": record.get("inkbird_diff_c")}
        if "probe_temp" in record["inputs"]:          # the Pi probe, when temp is the Inkbird
            extra["probe_c"] = record["inputs"]["probe_temp"]["value"]
            extra["probe_diff_c"] = record.get("probe_diff_c")
        self._append(f"tank_inputs-{day}.jsonl", {
            "ts": record["ts"],
            "temp_c": record["inputs"]["temp"]["value"],
            "lux": record["inputs"]["lux"]["value"],
            **extra,
            "plugs": {n: dict({"on": p["on"], "w": p["power_w"], "ok": p["ok"],
                               "ms": p["latency_ms"]},
                              **({"level": p["level"]} if p.get("level") is not None else {}))
                      for n, p in record["plugs"].items()},
            "heater": ({k: record["heater"][k]
                        for k in ("setpoint_c", "gate_on", "reason")}
                       if record.get("heater") else None),
        })
        changed = signature != self._last_sig
        self._last_sig = signature
        if changed or self._n % 60 == 1:
            self._append("tank_events.jsonl", record)
        return changed


# ---------------------------------------------------------------------------
# THE CONDUCTOR
# ---------------------------------------------------------------------------
def _finding(key: str, level: str, message: str, alert: bool = False) -> dict:
    return {"key": key, "level": level, "message": message, "alert": alert}


class Tank:
    def __init__(self, cfg, calendar, plugs, inputs, sink,
                 notify=common.send_alert, deadman=common.ping_deadman,
                 actuator=None, health=None, arc_catalog=None,
                 data_dir=None):
        self.actuator = actuator
        self.arc_catalog = arc_catalog
        self.data_dir = data_dir
        self.health = health or tank_health.HealthTracker()
        self.cfg = cfg
        self.lim = cfg["limits"]
        self.cal = calendar
        self.plugs = plugs
        self.inputs = inputs
        self.sink = sink
        self.notify = notify
        self.deadman = deadman
        self._offline_since: dict[str, dt.datetime] = {}
        self.scan_rows: list[dict] = []          # set by startup_scan()
        self._scan_findings: list[dict] = []
        self._scan_alerted = False
        self._alert_last: dict[str, dt.datetime] = {}
        # Alerts that were delivered and have not cleared yet, so a recovery notice
        # can follow; kept on disk with the cooldowns so a restart forgets nothing.
        self._alert_active: dict[str, dict] = {}
        self._alert_retry: dict[str, dt.datetime] = {}
        self._ts_seen: dict[str, tuple] = {}
        self._leak_run = 0           # consecutive full-dark ticks above dark_lux_max
        # Alert keys whose condition cannot be judged right now (the sensor that
        # would show it is gone). They are neither "still firing" nor "recovered":
        # a clock_skew alert must not read as recovered because the Pi went away.
        self._unknown: set[str] = set()
        self.wedge = tank_wedge.WedgeWatch(self.lim.get("plug_wedge"))
        self._wedge_view: dict[str, dict] = {}
        self._state_path = (os.path.join(data_dir, "tank_alert_state.json")
                            if data_dir else None)
        self._load_alert_state()
        self.itc = tank_itc.ItcCue(cfg.get("itc_setpoints"), data_dir)
        self.itc_watch = tank_itc_watch.ItcWatch(cfg.get("itc_watch"), data_dir)
        self.heat_log = tank_heat_log.HeatLog(cfg.get("heat_log"), data_dir)
        self._rise = tank_heat.RiseWatch(
            tank_heat.still_warming_config(self.lim.get("still_warming")))

    # -- observation ------------------------------------------------------
    def _plug_reading(self, plug):
        try:
            return plug.status()
        except Exception as err:  # noqa: BLE001 - one plug must not stop a tick
            return tank_plugs.PlugReading(plug.name, False,
                                          detail=f"{type(err).__name__}"[:120])

    @staticmethod
    def _channel_state(plan, channel):
        """(desired on, desired level) for a plan channel."""
        if channel == "white":
            return plan.white_on, plan.white_level
        if channel == "white_plus":
            return plan.white_plus_on, plan.white_plus_level
        if channel == "blue":
            return plan.blue_on, plan.blue_level
        return plan.any_moon, (None if plan.moon_illum_pct is None
                               else round(plan.moon_illum_pct / 100, 3))

    def _intent(self, plug, reading, plan):
        base = {"plug": plug.name, "role": plug.role, "channel": plug.channel,
                "observed_on": reading.on, "power_w": reading.power_w,
                "desired_on": None, "desired_level": None, "executed": False}
        if plug.channel is None:
            return dict(base, action="observe_only")
        on, level = self._channel_state(plan, plug.channel)
        via = None
        also = getattr(plug, "also_follows", None)
        if also and not on:                 # main channel off: serve the second one
            on2, level2 = self._channel_state(plan, also)
            if on2:
                on, level, via = on2, level2, also
        base.update(desired_on=on, desired_level=level, via=via)
        if not reading.ok or reading.on is None:
            return dict(base, action="unknown")
        if reading.on == on:
            return dict(base, action="none")
        return dict(base, action="would_turn_on" if on else "would_turn_off")

    # -- findings ---------------------------------------------------------
    def _check_calendar(self, plan, now):
        out = [_finding("calendar:" + p.split(":")[0], "warn", p, True)
               for p in self.cal.problems]
        if plan is None:
            out.append(_finding("calendar_exhausted", "alert",
                                "no calendar row for today; regenerate "
                                "spawn_calendar.py", True))
        else:
            left = self.cal.days_left(dt.date.fromisoformat(plan.tank_date))
            if left < self.lim["calendar_low_days"]:
                out.append(_finding("calendar_low", "warn",
                                    f"calendar ends in {left} days", True))
            for v in plan.violations:
                out.append(_finding(f"plan:{v}", "alert", f"plan violation: {v}",
                                    True))
        return out

    def _check_temp(self, plan, reading, now):
        label = "" if self.lim["temp_calibrated"] else "UNCALIBRATED probe: "
        age = reading.age_s(now)
        if not reading.configured:      # bring-up: nothing to be alarmed about
            return [_finding("temp_unconfigured", "warn",
                             "no temperature probe configured yet")]
        what = f"temperature ({reading.label})" if reading.label else "temperature probe"
        if not reading.ok:
            return [_finding("temp_missing", "alert",
                             f"{what}: {reading.detail}", True)]
        if age is not None and age > reading.limit_s(self.lim["input_max_age_s"]):
            return [_finding("temp_stale", "alert",
                             f"{what} reading is {age:.0f}s old", True)]
        lo, hi = self.lim["alarm_c"]
        if reading.value > hi:
            return [_finding("temp_high", "alert",
                             f"{label}{tank_heat.deg_f(reading.value)} is above "
                             f"{tank_heat.deg_f(hi)}", True)]
        if reading.value < lo:
            return [_finding("temp_low", "alert",
                             f"{label}{tank_heat.deg_f(reading.value)} is below "
                             f"{tank_heat.deg_f(lo)}", True)]
        return []

    def _check_probe(self, reading, now):
        """The Pi probe as a second opinion (the Inkbird feed is the primary `temp`).
        Its absence is a warning in the status file, never a message: the setpoint
        watch's probe-disagreement alert covers it when it does read."""
        if reading is None or not reading.configured:
            return []
        if not reading.ok:
            return [_finding("probe_temp_missing", "warn",
                             f"Pi probe is not reading: {reading.detail}")]
        age = reading.age_s(now)
        if age is not None and age > reading.limit_s(self.lim["input_max_age_s"]):
            return [_finding("probe_temp_stale", "warn",
                             f"Pi probe reading is {age:.0f}s old")]
        return []

    def _check_lifeguard(self, now):
        """The watchdog is watched too: a lifeguard that ran here and has gone
        quiet is an alert. One that never ran is not (preflight shows that)."""
        if not self.data_dir or not self.cfg.get("lifeguard", {}).get("enabled", True):
            return [], None
        if abs((dt.datetime.now(UTC) - now).total_seconds()) > 120:
            return [], None             # a replayed tick: file ages mean nothing
        path = os.path.join(tank_safe.folder(self.data_dir), "status.json")
        try:
            age = now.timestamp() - os.path.getmtime(path)
        except OSError:
            return [], None
        view = {"age_s": round(age)}
        if age > float(self.lim["lifeguard_silent_min"]) * 60:
            return [_finding(
                "lifeguard_silent", "alert",
                f"the lifeguard (the independent watchdog) has not written its "
                f"status for {age / 60:.0f} min: check the tank-lifeguard container",
                True)], view
        return [], view

    def _check_still_warming(self, reading, now):
        """Alert when the tank is over the heat cut-off and still warming. Only
        a fresh, good reading is fed in; anything else leaves the watch alone."""
        age = reading.age_s(now)
        if not (reading.ok and reading.configured and reading.value is not None
                and (age is None
                     or age <= reading.limit_s(self.lim["input_max_age_s"]))):
            return []
        hit = self._rise.step(now, reading.value)
        if hit is None:
            return []
        label = "" if self.lim["temp_calibrated"] else "UNCALIBRATED probe: "
        above = self._rise.c["above_c"]
        return [_finding(
            "still_warming", "alert",
            f"{label}{tank_heat.deg_f(reading.value)} is over the "
            f"{tank_heat.deg_f(above)} heat cut-off and still warming "
            f"(+{tank_heat.delta_f(hit['rise_c'], 2)} in {hit['span_min']:.0f} "
            f"min, over since {hit['since'][11:16]} UTC): the heat is not stopping",
            True)]

    def _check_lux(self, plan, reading, now):
        dark = plan is not None and plan.full_dark
        if not reading.configured:
            return [_finding("lux_unconfigured", "warn",
                             "no lux sensor configured yet")]
        if not reading.ok:
            return [_finding("lux_missing", "warn",
                             f"lux sensor: {reading.detail}", dark)]
        age = reading.age_s(now)
        if age is not None and age > reading.limit_s(self.lim["input_max_age_s"]):
            return [_finding("lux_stale", "warn",
                             f"lux reading is {age:.0f}s old", dark)]
        if dark and reading.value > self.lim["dark_lux_max"]:
            self._leak_run += 1
            need = int(self.lim.get("dark_lux_confirm_min", 1))
            if self._leak_run >= need:
                return [_finding("light_leak", "alert",
                                 f"{reading.value:.2f} lux during full dark "
                                 f"(limit {self.lim['dark_lux_max']}"
                                 + (f", for {self._leak_run} min" if need > 1 else "")
                                 + ")", True)]
            return []
        self._leak_run = 0
        return []

    def _check_plugs(self, plug_reads, intents, now):
        out = []
        limit = dt.timedelta(minutes=self.lim["plug_offline_min"])
        self._wedge_view = {}
        by_name = {p.name: p for p in self.plugs}
        peers_ok = {n for n, r in plug_reads.items() if r.ok and r.configured}
        for name, r in plug_reads.items():
            if r.ok or not r.configured:        # a tbd placeholder is not offline
                self._offline_since.pop(name, None)
                continue
            since = self._offline_since.setdefault(name, now)
            if now - since >= limit:
                verdict = None
                try:
                    verdict = self.wedge.judge(by_name[name], r,
                                               len(peers_ok - {name}), since, now)
                except Exception as err:  # noqa: BLE001 - a probe must not stop a tick
                    common.log("wedge_probe_failed", plug=name, error=repr(err))
                if verdict:
                    self._wedge_view[name] = verdict
                if verdict and verdict["kind"] == "wedged":
                    down_min = (now - since).total_seconds() / 60
                    out.append(_finding(
                        f"plug_wedged:{name}", "alert",
                        tank_wedge.wedge_message(by_name[name], verdict, down_min),
                        True))
                    continue
                note = (f" [{verdict['why']}]"
                        if verdict and verdict["kind"] in ("silent", "lan") else "")
                out.append(_finding(f"plug_offline:{name}", "alert",
                                    f"plug {name} offline: {r.detail}{note}", True))
        if self.lim["alert_on_drift"]:
            for i in intents:
                if i["action"] in ("would_turn_on", "would_turn_off"):
                    out.append(_finding(f"plug_drift:{i['plug']}", "warn",
                                        f"{i['plug']} should be "
                                        f"{'on' if i['desired_on'] else 'off'}",
                                        True))
        return out

    # -- arcs -------------------------------------------------------------
    def _arcs(self):
        """(record view, findings) for the chosen light and temperature arcs.

        Read every tick, so choosing from another terminal takes effect without a
        restart. Warnings only: a tank with no choice still runs the calendar.
        """
        if self.data_dir is None:
            return None, []
        if self.arc_catalog is None:
            return ({"onboarded": False, "problem": "arc catalog missing"},
                    [_finding("arc_catalog_missing", "warn",
                              "arc_catalog.json not found; see "
                              "build_arc_catalog.py")])
        sel, problem = tank_arcs.load_selection(self.data_dir, self.arc_catalog)
        if sel is not None:
            return tank_arcs.summarize(self.arc_catalog, sel), []
        if problem:
            return ({"onboarded": False, "problem": problem},
                    [_finding("arc_selection_invalid", "warn",
                              f"arc selection unusable: {problem}. Run "
                              f"`{ONBOARD_CMD}`")])
        return ({"onboarded": False, "problem": None, "how": ONBOARD_CMD},
                [_finding("onboarding_needed", "warn",
                          "no light and temperature arc chosen yet; following "
                          f"the spawn calendar. Run `{ONBOARD_CMD}`")])

    # -- alerts -----------------------------------------------------------
    # -- alert state (cooldowns and what is still open) --------------------
    def _load_alert_state(self) -> None:
        if not self._state_path:
            return
        st = common.read_json(self._state_path, {}) or {}
        try:
            self._alert_last = {k: dt.datetime.fromisoformat(v)
                                for k, v in (st.get("last") or {}).items()}
            self._alert_active = {k: dict(v) for k, v in (st.get("active") or {}).items()}
        except (ValueError, TypeError, AttributeError):
            self._alert_last, self._alert_active = {}, {}

    def _save_alert_state(self) -> None:
        if not self._state_path:
            return
        try:
            common.write_json(self._state_path, {
                "last": {k: v.isoformat() for k, v in self._alert_last.items()},
                "active": self._alert_active})
        except OSError:
            pass

    # -- alerts -----------------------------------------------------------
    def _send_alerts(self, findings, now):
        sent = []
        unknown = set(self._unknown)
        gap = dt.timedelta(minutes=self.lim["alert_cooldown_min"])
        retry = dt.timedelta(seconds=float(self.lim.get("alert_retry_s", 120)))
        muted = set(self.lim.get("mute_alerts") or [])
        dirty = False
        current = set()
        for f in findings:
            if not f["alert"]:
                continue
            if f["key"] in muted or f["key"].split(":")[0] in muted:
                continue
            current.add(f["key"])
            act = self._alert_active.get(f["key"])
            if act and act.get("cleared_since"):       # it came back: not recovered
                act.pop("cleared_since")
                dirty = True
            last = self._alert_last.get(f["key"])
            wait_until = self._alert_retry.get(f["key"])
            if wait_until is not None and now < wait_until:
                continue
            if f.get("daily") and last is not None:
                # one a local calendar day (the container's TZ, Pacific); `now` is UTC, so the day
                # is taken after converting. A restart keeps it: `last` is on disk.
                due = last.astimezone().date() != now.astimezone().date()
            else:
                due = last is None or now - last >= gap
            if due:
                ok = self.notify(f["message"], title="Anemone tank")
                if ok is False:             # reached no channel: try again soon
                    self._alert_retry[f["key"]] = now + retry
                    common.log("alert_undelivered", key=f["key"])
                    continue
                self._alert_retry.pop(f["key"], None)
                self._alert_last[f["key"]] = now
                self._alert_active.setdefault(f["key"], {
                    "since": now.isoformat(), "message": f["message"]})
                sent.append(f["key"])
                dirty = True
        if self.lim.get("recovery_alerts", True):
            confirm = dt.timedelta(minutes=float(self.lim.get("recovery_confirm_min", 2)))
            for key in [k for k in self._alert_active
                        if k not in current and k not in unknown]:
                act = self._alert_active[key]
                if key in muted or key.split(":")[0] in muted:
                    self._alert_active.pop(key)
                    dirty = True
                    continue
                since = act.get("cleared_since")
                if since is None:
                    act["cleared_since"] = now.isoformat()
                    dirty = True
                    since = act["cleared_since"]
                if now - dt.datetime.fromisoformat(since) < confirm:
                    continue
                opened = dt.datetime.fromisoformat(act["since"])
                mins = max(1, round((now - opened).total_seconds() / 60))
                ok = self.notify(f"Recovered: {key} (open about {mins} min). Was: "
                                 f"{act.get('message', '')}"[:300], title="Anemone tank")
                if ok is False:
                    continue                # keep it open and say so next tick
                self._alert_active.pop(key)
                sent.append("recovered:" + key)
                dirty = True
        else:
            for key in [k for k in self._alert_active
                        if k not in current and k not in unknown]:
                self._alert_active.pop(key)
                dirty = True
        expire = dt.timedelta(minutes=float(self.lim.get("unknown_expire_min", 120)))
        for key in unknown:                 # cannot tell: the confirm clock restarts
            act = self._alert_active.get(key)
            if not act:
                continue
            if act.get("cleared_since"):
                act.pop("cleared_since")
                dirty = True
            # Cannot be judged for hours (a second-opinion sensor that stays gone):
            # forget it quietly rather than hold it open for ever with no recovery.
            since = act.setdefault("unknown_since", now.isoformat())
            if now - dt.datetime.fromisoformat(since) >= expire:
                self._alert_active.pop(key)
                dirty = True
        for key in [k for k in self._alert_active if k not in unknown]:
            if self._alert_active[key].pop("unknown_since", None) is not None:
                dirty = True
        if dirty:
            self._save_alert_state()
        return sent

    # -- input clocks -----------------------------------------------------
    def _check_clocks(self, reads, now):
        """The Pi sensors stamp their own readings, so two things can go wrong that
        the age test cannot see: the clocks disagree (a reading from the future has
        a negative age and never goes stale), or the stamp stops moving while the
        sensor still answers. Both are judged from this host's own wall clock."""
        out = []
        self._unknown = set()
        skew = float(self.lim.get("clock_skew_s", 120))
        for name in ("temp", "lux"):
            r = reads.get(name)
            frozen_after = (r.limit_s(self.lim["input_max_age_s"]) if r is not None
                            else float(self.lim["input_max_age_s"]))
            if r is None or not r.ok or r.ts is None:
                # Cannot judge this tick, but the timer keeps running: a sensor that
                # alternates between answering and timing out must not look new each
                # time it answers. Only a good reading with a NEW stamp resets it.
                self._unknown |= {f"clock_skew:{name}", f"{name}_frozen"}
                continue
            age = r.age_s(now)
            if age is not None and age < -skew:
                out.append(_finding(
                    f"clock_skew:{name}", "warn",
                    f"{name} reading is stamped {-age:.0f}s in the future: the "
                    "sensor's clock and this host's clock disagree (is Windows Time "
                    "running? is the sensor host's clock right, e.g. the Pi's NTP?). Staleness checks cannot be "
                    "trusted until they agree", True))
            seen = self._ts_seen.get(name)
            if seen is None or seen[0] != r.ts:
                self._ts_seen[name] = (r.ts, now)
            elif (now - seen[1]).total_seconds() > frozen_after:
                if age is not None and age > frozen_after:
                    # The ordinary stale alert already says this (the clocks agree);
                    # frozen only adds something when they do not.
                    continue
                held = (now - seen[1]).total_seconds()
                out.append(_finding(
                    f"{name}_frozen", "alert" if name == "temp" else "warn",
                    f"{name} timestamp has not moved for {held:.0f}s (last value "
                    f"{r.value:g}, stamped {r.ts.isoformat(timespec='seconds')}) but "
                    "the sensor host still answers: its logger has stopped writing. "
                    "Check that the logger is running (Pi logger: "
                    f"journalctl -u {'temp' if name == 'temp' else 'lux'}-logger; "
                    "Inkbird feed: the itc container)",
                    name == "temp"))
        return out

    # -- the tick ---------------------------------------------------------
    def startup_scan(self, scanner=tank_scan.default_scanner) -> list[dict]:
        """Listen for the plugs and check their IPs (see tank_scan.py). Never
        raises and never sends anything to a plug."""
        sc = self.cfg["plug_scan"]
        if not sc["enabled"] or not tank_scan.tuya_plugs(self.plugs):
            return []
        rows = tank_scan.scan_plugs(self.plugs, float(sc["timeout_s"]), scanner)
        healed = tank_scan.heal(self.plugs, rows) if sc["heal"] else []
        self.scan_rows = rows
        self._scan_findings = tank_scan.findings(rows, healed, _finding)
        common.log("plug_scan", plugs={r["plug"]: r["status"] for r in rows},
                   healed=healed)
        return rows

    def tick(self, when: dt.datetime) -> dict:
        now = when.astimezone(UTC).replace(microsecond=0)
        plan = tank_plan.plan_at(self.cal, self.cfg["plan"], now)
        reads = self.inputs.read_all(now)
        plug_reads = {p.name: self._plug_reading(p) for p in self.plugs}

        intents = []
        if plan is not None:
            intents = [self._intent(p, plug_reads[p.name], plan)
                       for p in self.plugs]

        for r in plug_reads.values():
            self.health.observe(now, r)
        plug_health = self.health.snapshot(now)

        findings = self._check_calendar(plan, now)
        findings += self.health.findings(plug_health)
        findings += self._check_temp(plan, reads["temp"], now)
        # Redundancy management (tank_vote.py). `control` is the reading the
        # actuator acts on: the primary itself unless failover is on and a
        # backup had to take over.
        red = tank_vote.vote(reads, now, self.lim["input_max_age_s"],
                             self.lim.get("redundancy"))
        control = red["selected"] if red["selected"] is not None else reads["temp"]
        findings += [_finding(*f) for f in red["findings"]]
        if red["failover"]:             # the alarm band follows the stand-in
            findings += [f for f in self._check_temp(plan, control, now)
                         if f["key"] in ("temp_high", "temp_low")]
        lg_findings, lg_view = self._check_lifeguard(now)
        findings += lg_findings
        findings += self._check_still_warming(reads["temp"], now)
        findings += self._check_probe(reads.get("probe_temp"), now)
        findings += self._check_lux(plan, reads["lux"], now)
        findings += self._check_clocks(reads, now)
        findings += self._check_plugs(plug_reads, intents, now)
        findings += tank_health.power_findings(self.plugs, plug_reads)
        findings += tank_health.countdown_findings(self.plugs, plug_reads)
        for f in self._scan_findings:       # alert once; keep showing until restart
            findings.append(f if not self._scan_alerted else dict(f, alert=False))
        self._scan_alerted = True
        arcs, arc_findings = self._arcs()
        findings += arc_findings

        commands, heater, cooling, verify, safe_mode = [], None, None, None, None
        if self.actuator is not None:
            res = self.actuator.apply(now, plan, plug_reads, control,
                                      intents, lux=reads["lux"])
            commands, heater = res["commands"], res["heater"]
            safe_mode = res.get("safe_mode")
            verify = res.get("verify")
            cooling = res.get("cooling")
            findings += res["findings"]
            done = {c["plug"] for c in commands if c["executed"]}
            for i in intents:
                i["executed"] = i["plug"] in done
        itc_view, itc_findings = self.itc.step(now, plan)
        findings += itc_findings
        # The setpoint watch compares the Inkbird with the Pi probe. When the Inkbird
        # feed is the primary `temp`, that is the separate `probe_temp` input.
        watch_probe = reads["probe_temp"] if "probe_temp" in reads else reads["temp"]
        watch_view, watch_findings = self.itc_watch.step(now, watch_probe)
        findings += watch_findings
        self._unknown |= self.itc_watch.unknown_keys
        sent = self._send_alerts(findings, now)

        def view(r):
            return {"ok": r.ok, "value": r.value, "detail": r.detail,
                    "age_s": r.age_s(now), "configured": r.configured}

        temp_err = None
        if plan is not None and reads["temp"].ok:
            temp_err = round(reads["temp"].value - plan.temp_hourly_c, 3)
        # Probe minus Inkbird, only when both are fresh. Logged, never acted on.
        inkbird_diff = None
        probe_diff = None
        probe = reads.get("probe_temp")
        if probe is not None and probe.ok and reads["temp"].ok:
            p_age = probe.age_s(now)
            if p_age is not None and 0 <= p_age <= probe.limit_s(self.lim["input_max_age_s"]):
                probe_diff = round(probe.value - reads["temp"].value, 3)
        ink = reads.get("inkbird_temp")
        if ink is not None and ink.ok and reads["temp"].ok:
            ink_age = ink.age_s(now)
            if ink_age is not None and 0 <= ink_age <= self.lim["inkbird_max_age_s"]:
                inkbird_diff = round(reads["temp"].value - ink.value, 3)
        record = {
            "ts": now.isoformat(),
            "plan": plan.as_dict() if plan else None,
            "inputs": {k: view(v) for k, v in reads.items()},
            "temp_error_c": temp_err,
            "inkbird_diff_c": inkbird_diff,
            "probe_diff_c": probe_diff,
            "redundancy": tank_vote.view(red),
            "safe_mode": safe_mode,
            "lifeguard": lg_view,
            "plugs": {n: {"ok": r.ok, "on": r.on, "power_w": r.power_w,
                          "level": r.level,
                          "detail": r.detail, "error": r.error,
                          "latency_ms": r.latency_ms,
                          "configured": r.configured,
                          "array": next(p.array for p in self.plugs
                                        if p.name == n),
                          "role": next(p.role for p in self.plugs if p.name == n),
                          "channel": next(p.channel for p in self.plugs
                                          if p.name == n)}
                      for n, r in plug_reads.items()},
            "plug_health": plug_health,
            "arcs": arcs,
            "intents": intents,
            "findings": findings,
            "alerts_sent": sent,
            "commands": commands,
            "heater": heater,
            "cooling": cooling,
            "verify": verify,
            "itc": itc_view,
            "itc_watch": watch_view,
            "plug_wedge": self._wedge_view,
            "executed": any(c["executed"] for c in commands),
        }
        signature = (
            tuple(sorted((k, v) for k, v in (plan.as_dict() if plan else {}).items()
                         if isinstance(v, bool))),
            tuple((i["plug"], i["action"]) for i in intents),
            tuple(sorted(f["key"] for f in findings)),
            tuple(sorted((n, r.ok) for n, r in plug_reads.items())),
            tuple(sorted((n, h["level"]) for n, h in plug_health.items())),
            (heater or {}).get("gate_on"),
            red["mode"], red["source"], bool(safe_mode),
            (arcs or {}).get("onboarded"),
            tuple(sorted((n, v["tripped"]) for n, v in
                         ((cooling or {}).get("plugs") or {}).items())),
            ((cooling or {}).get("fans") or {}).get("wanted"),
            tuple((c["plug"], c["desired_on"]) for c in commands),
            tuple(sorted((n, v["verdict"]) for n, v in
                         ((verify or {}).get("last") or {}).items())),
        )
        self.heat_log.step(now, reads, self.plugs, plug_reads, plan)
        self.sink.write(record, signature, now)
        if control.ok:              # ping only while the water can be seen
            self.deadman()
        return record


# ---------------------------------------------------------------------------
# DEAD-MAN CHECK
# ---------------------------------------------------------------------------
def verify_countdown(plug, seconds, sleep=time.sleep, say=print, poll_s=5):
    """Prove the plug turns itself back ON when its countdown expires.

    The heater gate relies on this: if the Pi dies while the gate is OFF, the
    plug restores power on its own. Whether a given plug's countdown does that
    is model-specific, so test it, don't assume it. The plug is always left ON.
    Returns a dict with ok, and either the measured time or the reason.
    """
    if not plug.supports_revert:
        return {"ok": False, "why": "plug has no revert_dp configured"}
    start = plug.status()
    if not start.ok:
        return {"ok": False, "why": f"plug not reachable: {start.detail}"}
    try:
        plug.set_on(False, revert_after_s=seconds)
        back = plug.status()
        if not (back.ok and back.on is False):
            return {"ok": False, "why": "the plug did not switch off"}
        say(f"{plug.name} is OFF with a {seconds}s countdown. Not refreshing it; "
            "watching for it to switch itself back on...")
        waited = 0
        while waited <= seconds + 30:
            sleep(poll_s)
            waited += poll_s
            r = plug.status()
            say(f"  t+{waited:>3}s  {'ON ' if r.on else 'OFF'}  "
                f"countdown remaining: {r.revert_remaining_s}")
            if r.ok and r.on:
                slack = 2 * poll_s + 10
                return {"ok": True, "came_back_after_s": waited,
                        "expected_s": seconds,
                        "timing_ok": abs(waited - seconds) <= slack}
        return {"ok": False, "why": "the plug stayed off past its countdown: "
                                    "its countdown does NOT restore power"}
    finally:
        try:
            plug.set_on(True)               # always leave the safe state
        except Exception as err:  # noqa: BLE001
            say(f"WARNING: could not switch {plug.name} back on ({err}); "
                "do it by hand now")


def switch_plug(plug, on, revert_after_s=None, say=print):
    """Manual switch through the same Plug.set_on the actuator uses, read back.

    Returns {ok, before, after, ...}. Never retries and never leaves a countdown
    armed by accident: switching ON clears it (Plug.set_on does that).
    """
    before = plug.status()
    if not before.ok:
        return {"ok": False, "why": f"plug not reachable: {before.detail}"}
    say(f"{plug.name} reads {'ON' if before.on else 'OFF'}, countdown "
        f"{before.revert_remaining_s}s; sending {'ON' if on else 'OFF'}"
        + (f" with a {revert_after_s}s revert" if revert_after_s else ""))
    try:
        plug.set_on(on, revert_after_s=revert_after_s)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "why": f"{type(err).__name__}: {err}"[:120]}
    after = plug.status()
    return {"ok": bool(after.ok and after.on == on), "asked_on": on,
            "before_on": before.on, "after_on": after.on,
            "countdown_s": after.revert_remaining_s, "dps": after.dps,
            "latency_ms": after.latency_ms}


# ---------------------------------------------------------------------------
# WIRING AND CLI
# ---------------------------------------------------------------------------
def build_tank(cfg: dict, hardware: bool = True) -> Tank:
    cal_path = tank_plan.find_calendar()
    if not cal_path:
        common.die("no spawn_calendar_*.csv found (set TANK_CALENDAR)")
    cal = tank_plan.load_calendar(cal_path)
    plugs = []
    if hardware and os.path.exists(cfg["plug_array"]):
        plugs = load_array(cfg)
    inputs = (tank_inputs.Inputs.from_config(cfg["inputs"]) if hardware
              else tank_inputs.Inputs.from_config({}))
    actuator = tank_out.Actuator(cfg["outputs"], cfg["limits"], plugs,
                                 common.data_dir())
    cat_path = tank_arcs.find_catalog(common.data_dir(), _HERE)
    try:
        catalog = tank_arcs.load_catalog(cat_path) if cat_path else None
    except tank_arcs.ArcError as err:
        common.log("arc_catalog_unusable", error=str(err))
        catalog = None
    return Tank(cfg, cal, plugs, inputs, DryRunSink(common.data_dir()),
                actuator=actuator, arc_catalog=catalog,
                data_dir=common.data_dir())


def onboard_command(args) -> int:
    """Pick the light and temperature arcs. Nothing here touches a plug."""
    data = common.data_dir()
    cat_path = tank_arcs.find_catalog(data, _HERE)
    if not cat_path:
        common.die("no arc_catalog.json (build it with build_arc_catalog.py)")
    try:
        cat = tank_arcs.load_catalog(cat_path)
        near = tank_arcs.parse_near(args.near)
        if args.reset:
            print("choice cleared" if tank_arcs.clear_selection(data)
                  else "nothing was chosen")
            return 0
        sel, problem = tank_arcs.load_selection(data, cat)
        if args.show:
            if sel is None:
                print(problem or "no arcs chosen yet")
                return 1
            s = tank_arcs.summarize(cat, sel)
            print(f"temperature: {s['temp']['name']} [{s['temp']['id']}]")
            print(f"light:       {s['light']['name']} [{s['light']['id']}]")
            print(f"offset:      {s['offset_days']:+g} days   (chosen "
                  f"{s['chosen_utc']})")
            print(s["note"])
            return 0
        if args.list:
            kinds = [args.kind] if args.kind else list(tank_arcs.KINDS)
            for k in kinds:
                arcs = tank_arcs.sort_arcs(tank_arcs.arcs_of(cat, k),
                                           args.sort, near)
                print(f"\n=== {k} arcs, sorted by {args.sort} ===\n")
                print(tank_arcs.format_list(arcs, near))
            return 0
        if args.temp_arc or args.light_arc:
            done = tank_arcs.save_selection(data, cat, {
                "temp_arc": args.temp_arc, "light_arc": args.light_arc,
                "offset_days": args.offset_days})
            print(f"saved: temperature {done['temp_arc']}, light "
                  f"{done['light_arc']}, offset {done['offset_days']:+g} days")
            return 0
        if not sys.stdin.isatty():
            print("onboarding is interactive and this is not a terminal. Run:\n"
                  f"  {ONBOARD_CMD}\nor pass --temp-arc ID --light-arc ID "
                  "[--offset-days N]; --list shows the ids.")
            return 2
        if sel is not None:
            again = input(f"Already chosen ({sel['temp_arc']} + "
                          f"{sel['light_arc']}, {sel['offset_days']:+g} d). "
                          "Choose again? [y/N] ")
            if not again.strip().lower().startswith("y"):
                return 0
        done = tank_arcs.onboard(cat, data, sort=args.sort, near=near)
        print(f"\nsaved: temperature {done['temp_arc']}, light "
              f"{done['light_arc']}, offset {done['offset_days']:+g} days.\n"
              "The conductor picks this up on its next tick. It is recorded in "
              "tank_status.json; the plan itself still follows the spawn "
              "calendar until the arc calculation is built.")
        return 0
    except tank_arcs.ArcError as err:
        common.die(str(err))
    return 0


def _parse_now(text: str | None) -> dt.datetime:
    if not text:
        return dt.datetime.now(UTC)
    t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _summary(rec: dict) -> str:
    p = rec["plan"]
    if p is None:
        return f"{rec['ts']} NO PLAN (calendar has no row)"
    flags = " ".join(k for k in ("full_dark", "any_sun", "any_moon", "watch")
                     if p[k]) or "-"
    t = rec["inputs"]["temp"]["value"]
    lux = rec["inputs"]["lux"]["value"]
    acts = ", ".join(f"{i['plug']}:{i['action']}" for i in rec["intents"]
                     if i["action"] not in ("none", "observe_only")) or "none"
    heat = rec.get("heater")
    heat_s = (f" | heater gate={'ON' if heat['gate_on'] else 'OFF'}"
              f" ({heat['reason']}) setpoint={heat['setpoint_c']}"
              if heat else "")
    cool = rec.get("cooling")
    cool_s = ""
    if cool:
        cut = [n for n, v in cool["plugs"].items() if v.get("tripped")]
        cool_s = (f" | cutoff {cool['cutoff_c']}C"
                  f"{' CUT: ' + ','.join(cut) if cut else ''}"
                  f"{' fans' if (cool.get('fans') or {}).get('wanted') else ''}")
    arcs = rec.get("arcs")
    arc_s = ""
    if arcs is not None:
        arc_s = (f" | arcs {arcs['temp']['id']} + {arcs['light']['id']} "
                 f"{arcs['offset_days']:+g}d (recorded only)"
                 if arcs.get("onboarded") else " | arcs: not chosen")
    cmds = ", ".join(f"{c['plug']}->{'on' if c['desired_on'] else 'off'}"
                     f"{'' if c['verified'] else '(UNVERIFIED)'}"
                     for c in rec.get("commands", [])) or "none"
    return (f"{p['local'][:16]} plan[{flags}] T={p['temp_hourly_c']} "
            f"(daily {p['temp_daily_c']}) white={p['white_level']} "
            f"blue={p['blue_level']} moon_lux={p['moon_target_lux']} | "
            f"probe={t} lux={lux} | intents: {acts} | commands: {cmds}"
            f"{heat_s}{cool_s}{arc_s} | findings: {len(rec['findings'])}")


COMMANDS = ("tick", "loop", "plan", "verify-countdown", "health", "onboard",
            "switch", "measure-lamp", "scan")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    ap = argparse.ArgumentParser(prog="conductor.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        s = sub.add_parser(name)
        s.add_argument("--config")
    vc = sub.choices["verify-countdown"]
    vc.add_argument("--plug", required=True)
    vc.add_argument("--seconds", type=int, default=90)
    vc.add_argument("--yes", action="store_true",
                    help="actually switch the plug off for the test")
    sw = sub.choices["switch"]
    sw.add_argument("--plug", required=True)
    sw.add_argument("state", choices=["on", "off"])
    sw.add_argument("--revert-after", type=int, default=None,
                    help="seconds; with OFF, arms the plug's own countdown")
    sw.add_argument("--yes", action="store_true",
                    help="actually send the command")
    ml = sub.choices["measure-lamp"]
    ml.add_argument("--plug", required=True)
    ml.add_argument("--settle-s", type=float, default=20.0,
                    help="wait after each switch before reading (default 20)")
    ml.add_argument("--sample-s", type=float, default=40.0,
                    help="how long to read in each state (default 40)")
    ml.add_argument("--cycles", type=int, default=2, help="off/on cycles (default 2)")
    ml.add_argument("--even-if-dark", action="store_true",
                    help="allow it during the plan's full-dark window (it lights the lamp)")
    ml.add_argument("--json", action="store_true")
    ml.add_argument("--yes", action="store_true", help="actually switch the plug")
    sc = sub.choices["scan"]
    sc.add_argument("--timeout", type=float, default=None,
                    help="seconds to listen (default plug_scan.timeout_s)")
    sc.add_argument("--write", action="store_true",
                    help="rewrite drifted ip lines in plug_array.json")
    sc.add_argument("--json", action="store_true")
    hc = sub.choices["health"]
    hc.add_argument("--plug", help="check only this plug")
    hc.add_argument("--json", action="store_true")
    ob = sub.choices["onboard"]
    ob.add_argument("--list", action="store_true", help="show the arcs and exit")
    ob.add_argument("--show", action="store_true", help="show the current choice")
    ob.add_argument("--reset", action="store_true", help="forget the choice")
    ob.add_argument("--kind", choices=tank_arcs.KINDS, help="with --list")
    ob.add_argument("--sort", choices=tank_arcs.SORTS, default="name")
    ob.add_argument("--near", help="LAT,LON: sort by distance from here")
    ob.add_argument("--temp-arc")
    ob.add_argument("--light-arc")
    ob.add_argument("--offset-days", type=float, default=0.0)
    sub.choices["tick"].add_argument("--now")
    sub.choices["tick"].add_argument("--offline", action="store_true",
                                     help="no plugs, no sensors")
    sub.choices["tick"].add_argument("--json", action="store_true")
    sub.choices["plan"].add_argument("--date", required=True)
    args = ap.parse_args(argv)

    common.load_env()
    cfg = load_config(args.config)

    if args.cmd == "onboard":
        return onboard_command(args)

    if args.cmd == "plan":
        cal_path = tank_plan.find_calendar()
        if not cal_path:
            common.die("no spawn_calendar_*.csv found (set TANK_CALENDAR)")
        cal = tank_plan.load_calendar(cal_path)
        day = dt.date.fromisoformat(args.date)
        row = cal.rows.get(day.isoformat())
        if row is None:
            common.die(f"{day} is outside the calendar "
                       f"({cal.first_date} to {cal.last_date})")
        print(f"{day}  daily target {row['target_c']} C "
              f"({row['target_f']} F)  offset {row['offset_days']} d  "
              f"cue date {row['cue_date']}")
        for p in cal.problems:
            print(f"  PROBLEM: {p}")
        for when, name, value in tank_plan.day_events(cal, cfg["plan"], day):
            print(f"  {when}  {name:15s} -> {value}")
        return 0

    if args.cmd == "health":
        # Read-only: one status query per plug. No calendar, no sensors, no
        # commands, and the local keys are never printed.
        if not os.path.exists(cfg["plug_array"]):
            common.die(f"no plug array at {cfg['plug_array']}")
        plugs = load_array(cfg)
        if args.plug:
            plugs = [p for p in plugs if p.name == args.plug]
            if not plugs:
                common.die(f"no plug named {args.plug}")
        rows = tank_health.check_plugs(plugs)
        print(json.dumps(rows, indent=2, default=str) if args.json
              else tank_health.format_report(rows))
        return 0 if all(r["ok"] or not r["configured"] for r in rows) else 1

    if args.cmd == "scan":
        # Passive: listens for the plugs' broadcasts, sends nothing to a plug.
        if not os.path.exists(cfg["plug_array"]):
            common.die(f"no plug array at {cfg['plug_array']}")
        plugs = load_array(cfg)
        timeout = args.timeout or float(cfg["plug_scan"]["timeout_s"])
        if not 3 <= timeout <= 60:
            common.die("--timeout must be 3..60")
        rows = tank_scan.scan_plugs(plugs, timeout)
        print(json.dumps(rows, indent=2) if args.json else tank_scan.format_report(rows))
        for f in tank_scan.findings(rows):
            print(f"  [{f['level']}] {f['message']}")
        drifted = [r for r in rows if r["status"] == "drifted"]
        if args.write and drifted:
            done = tank_scan.write_ips(cfg["plug_array"], rows)
            print(f"updated {cfg['plug_array']}: {', '.join(done) or 'nothing'}")
            skipped = [r["plug"] for r in drifted if r["plug"] not in done]
            if skipped:
                print(f"not in that file (edit by hand): {', '.join(skipped)}")
        elif drifted:
            print("Rerun with --write to update plug_array.json.")
        return 1 if drifted or any(r["status"] in ("not_seen", "scan_failed")
                                   for r in rows) else 0

    if args.cmd == "switch":
        if not os.path.exists(cfg["plug_array"]):
            common.die(f"no plug array at {cfg['plug_array']}")
        plugs = {p.name: p for p in load_array(cfg)}
        if args.plug not in plugs:
            common.die(f"no plug named {args.plug}; have {sorted(plugs)}")
        if args.revert_after is not None and (
                args.state != "off" or not 5 <= args.revert_after <= 3600):
            common.die("--revert-after is for OFF only, 5..3600 seconds")
        if not args.yes:
            print(f"This would switch {args.plug} {args.state.upper()}. "
                  "Rerun with --yes.")
            return 0
        res = switch_plug(plugs[args.plug], args.state == "on", args.revert_after)
        print(json.dumps(res, default=str))
        return 0 if res["ok"] else 1

    if args.cmd == "measure-lamp":
        if not 0 <= args.settle_s <= 600 or not 10 <= args.sample_s <= 600                 or not 1 <= args.cycles <= 6:
            common.die("--settle-s 0..600, --sample-s 10..600, --cycles 1..6")
        if not os.path.exists(cfg["plug_array"]):
            common.die(f"no plug array at {cfg['plug_array']}")
        plugs = {p.name: p for p in load_array(cfg)}
        if args.plug not in plugs:
            common.die(f"no plug named {args.plug}; have {sorted(plugs)}")
        plug = plugs[args.plug]
        mins = args.cycles * 2 * (args.settle_s + args.sample_s) / 60
        if not args.yes:
            print(f"This would switch {args.plug} OFF and ON {args.cycles} times "
                  f"(about {mins:.0f} min) while reading the lux sensor, then put it "
                  "back as it was. The lamp will light. Rerun with --yes.")
            return 0
        cal_path = tank_plan.find_calendar()
        if cal_path and not args.even_if_dark:
            plan = tank_plan.plan_at(tank_plan.load_calendar(cal_path), cfg["plan"],
                                     dt.datetime.now(UTC))
            if plan is not None and plan.full_dark:
                common.die("the plan says full dark right now and this would light "
                           "the lamp; run it by day, or pass --even-if-dark")
        lux = tank_inputs.Inputs.from_config(cfg["inputs"]).sources["lux"]
        try:
            rep = tank_lamp.measure(plug, lux, args.settle_s, args.sample_s,
                                    args.cycles,
                                    max_age_s=cfg["limits"]["input_max_age_s"])
        except tank_lamp.LampError as err:
            common.die(str(err))
        print(json.dumps(rep, default=str) if args.json
              else tank_lamp.format_report(rep, plug.channel))
        return 0 if rep["usable"] else 3

    if args.cmd == "verify-countdown":
        if not 20 <= args.seconds <= 600:
            common.die("--seconds must be 20..600")
        if not os.path.exists(cfg["plug_array"]):
            common.die(f"no plug array at {cfg['plug_array']}")
        plugs = {p.name: p for p in load_array(cfg)}
        if args.plug not in plugs:
            common.die(f"no plug named {args.plug}; have {sorted(plugs)}")
        if not args.yes:
            print(f"This would switch {args.plug} OFF for up to {args.seconds}s "
                  "and check that it turns itself back on. Anything plugged "
                  "into it loses power meanwhile. Rerun with --yes.")
            return 0
        res = verify_countdown(plugs[args.plug], args.seconds)
        print(json.dumps(res))
        if res["ok"] and res["timing_ok"]:
            print("PASS. Set outputs.heater.countdown_verified to true once you "
                  "have also done the Inkbird power-return check "
                  "(pi-contingency-plan.md 4.3).")
            return 0
        print("NOT VERIFIED. Leave countdown_verified false; the Pi will then "
              "never switch the heater gate off.")
        return 1

    tank = build_tank(cfg, hardware=not getattr(args, "offline", False))
    if args.cmd == "tick":
        rec = tank.tick(_parse_now(args.now))
        print(json.dumps(rec, indent=2, default=str) if args.json
              else _summary(rec))
        for f in rec["findings"]:
            print(f"  [{f['level']}] {f['message']}")
        return 0

    stop = common.StopFlag()
    interval = float(cfg["interval_s"])
    tank.startup_scan()
    common.log("tank_loop_start", plugs=[p.name for p in tank.plugs],
               calendar=tank.cal.path, interval_s=interval)
    if tank.arc_catalog is not None:
        sel, _ = tank_arcs.load_selection(common.data_dir(), tank.arc_catalog)
        if sel is None:
            common.log("onboarding_needed",
                       note="no light and temperature arc chosen yet; "
                            "following the spawn calendar",
                       run=ONBOARD_CMD)
    while not stop.stopped:
        try:
            tank.tick(dt.datetime.now(UTC))
        except Exception as err:  # noqa: BLE001 - the loop must survive a bad tick
            common.log("tick_failed", error=repr(err))
        if stop.sleep(interval - (time.time() % interval) + 0.5):
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
