#!/usr/bin/env python3
"""tank_heat_log.py -- the granular record of what heats the tank.

Every conductor tick (a minute) one line goes to <data>/heat/heat_log-YYYYMMDD.jsonl:
the temperature the conductor is using, the Pi probe beside it, which lights are on
(by the plugs' own state, per channel), the lux sensor, the plan's lamp flags, the
Inkbird's T1/T2 and whether the heater was on. Nothing here decides or switches
anything; it is the data the heat table will be worked out from (heat_report.py).

    {"ts": "2026-11-03T20:00:00+00:00", "temp_c": 26.3, "temp_ts": "...", "temp_age_s": 120,
     "probe_c": null, "lux": 1206.0,
     "lights": {"white": true, "white_plus": true, "blue": true, "moon": null},
     "plugs": {"plug-10": {"on": true, "role": "white"}, ...},
     "plan": {"white_on": true, "white_plus_on": true, "blue_on": true, "moon_on": false,
              "full_dark": false},
     "band_f": [77.1, 77.6], "heating": false, "heating_ts": "...",
     "equipment": {"pc_fan": {"state": "on", "voltage_v": null}}}

"heating" is whether the Inkbird page showed "Heating" at its last read (every 5
minutes, from the itc service's feed); null when the feed has no such field.

Once an hour (chart_every_min) the report and chart are rebuilt in <data>/heat/.
A failure to log or to chart never touches the tick.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os

import heat_equipment

DEFAULT_HEAT_LOG = {
    "enabled": True,
    "chart_every_min": 60,
    "feed": None,                 # default <data>/sensors/itc306a_docker.jsonl
}


def heat_log_config(overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_HEAT_LOG)
    cfg.update(overrides or {})
    if not 5 <= float(cfg["chart_every_min"]) <= 1440:
        raise ValueError("heat_log.chart_every_min must be 5..1440")
    return cfg


def _tail_lines(path: str, nbytes: int = 12000) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - nbytes))
            return fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def latest_feed_line(path: str | None) -> dict | None:
    """The newest good Inkbird feed line (temp_c not null), or None."""
    if not path:
        return None
    for line in reversed(_tail_lines(path)):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("temp_c") is not None:
            return r
    return None


class HeatLog:
    def __init__(self, cfg: dict | None, data_dir: str | None, builder=None):
        self.cfg = heat_log_config(cfg)
        self.data = data_dir
        self._builder = builder
        self._last_chart: dt.datetime | None = None

    def _equipment(self, now) -> dict:
        """Equipment not on a plug (the PC fan), as data/heat/equipment.json says; read
        again whenever the file changes."""
        path = heat_equipment.path_for(self.data)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if getattr(self, "_eq_mtime", "unset") != mtime:
            self._eq_mtime = mtime
            self._eq = heat_equipment.load(self.data, create=True)
        return heat_equipment.view_at(self._eq, now)

    def _feed_path(self):
        if self.cfg["feed"]:
            return self.cfg["feed"]
        return os.path.join(self.data, "sensors", "itc306a_docker.jsonl") if self.data else None

    def row(self, now, reads, plugs, plug_reads, plan) -> dict:
        def val(r):
            return None if r is None or not r.ok else r.value
        temp = reads.get("temp")
        lights = {"white": None, "white_plus": None, "blue": None, "moon": None}
        plug_view = {}
        for p in plugs:
            r = plug_reads.get(p.name)
            on = None if r is None or not r.ok else r.on
            plug_view[p.name] = {"on": on, "role": p.role}
            if r is not None and r.ok and getattr(r, "level", None) is not None:
                plug_view[p.name]["level"] = r.level      # dimmers: the setting, 0..1
            ch = p.channel
            if ch in lights:
                lights[ch] = on
        feed = latest_feed_line(self._feed_path())
        band = None
        heating = None
        heating_ts = None
        if feed:
            if feed.get("t1_f") is not None and feed.get("t2_f") is not None:
                band = [feed["t1_f"], feed["t2_f"]]
            if feed.get("heating") is not None:
                heating, heating_ts = bool(feed["heating"]), feed.get("ts")
        plan_view = None
        if plan is not None:
            plan_view = {k: getattr(plan, k, None) for k in
                         ("white_on", "white_plus_on", "blue_on", "moon_on", "full_dark")}
        return {
            "ts": now.isoformat(timespec="seconds"),
            "temp_c": val(temp),
            "temp_ts": temp.ts.isoformat(timespec="seconds") if temp is not None and temp.ts else None,
            "temp_age_s": None if temp is None or temp.ts is None else round(temp.age_s(now)),
            "probe_c": val(reads.get("probe_temp")),
            "lux": val(reads.get("lux")),
            "lights": lights, "plugs": plug_view, "plan": plan_view,
            "band_f": band, "heating": heating, "heating_ts": heating_ts,
            "equipment": self._equipment(now),
        }

    def step(self, now, reads, plugs, plug_reads, plan):
        """Append this tick's line; maybe rebuild the chart. Never raises."""
        if not self.cfg["enabled"] or not self.data:
            return None
        try:
            row = self.row(now, reads, plugs, plug_reads, plan)
            folder = os.path.join(self.data, "heat")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"heat_log-{now:%Y%m%d}.jsonl")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        except Exception:  # noqa: BLE001 - logging must not touch the tick
            return None
        every = dt.timedelta(minutes=float(self.cfg["chart_every_min"]))
        if self._last_chart is None or now - self._last_chart >= every:
            self._last_chart = now
            try:
                build = self._builder
                if build is None:
                    import heat_report
                    build = heat_report.build
                build(self.data, os.path.join(self.data, "heat"), None, now)
            except Exception:  # noqa: BLE001
                pass
        return row
