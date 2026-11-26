#!/usr/bin/env python3
"""tank_inputs.py -- minute-precise sensor inputs (temperature probe, lux).

Each input is a Source with one method, read(now) -> Reading. A source never
raises: a failure comes back as Reading(ok=False, detail=...), so one dead
sensor cannot stop a tick.

Drivers
    ds18b20_sysfs   1-Wire probe on the Pi, read from /sys/bus/w1/devices.
                    Rejects a failed CRC, the 85.0 power-on value and -127.
    jsonl           the newest line of a JSON-lines file that another process
                    keeps appending to. This is the bridge for hardware that
                    cannot be reached from the container (an I2C lux sensor on
                    the Pi, reef-pi, anything): a small writer on the Pi emits
                    {"ts": "2026-11-02T09:00:00+00:00", "lux": 0.21} and the
                    conductor reads it (scripts/lux_logger.py is that writer).
    http_json       the same line served over HTTP: GET <url> returns one JSON
                    object {"ts": ..., "<field>": number}. For a sensor on
                    another machine than the conductor: scripts/lux_logger.py
                    --serve-port 8099 serves its latest reading at /lux. The
                    timestamp is the sensor's own, so the staleness rule
                    works exactly as for the file; an unreachable host, a
                    non-200 answer or a slow reply (timeout_s, default 4) is a
                    failed reading, never a stale one passed off as fresh.

An optional third input, "inkbird_temp", is the Inkbird ITC-306A read through its
app (one {"ts", "temp_c"} line every 5 minutes, or a {"temp_c": null, "error":
...} line when a read failed). It is only compared with "temp" and logged; no
cue, alert or output uses it.

Spec examples (tank_config.json "inputs"):
    {"temp": {"driver": "ds18b20_sysfs", "offset_c": 0.0},
     "lux":  {"driver": "jsonl", "path": "/data/sensors/lux.jsonl",
              "field": "lux"}}
    {"lux": {"driver": "http_json", "url": "http://192.0.2.100:8099/lux",
             "field": "lux"}}

Standard library only.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import glob
import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

UTC = dt.timezone.utc

# The inputs that are water temperatures, and the range a water temperature
# can plausibly have in this tank (C). Anything else is a sensor or OCR fault.
TEMP_INPUTS = ("temp", "inkbird_temp", "probe_temp")
DEFAULT_RANGE_C = (5.0, 45.0)


@dataclass
class Reading:
    name: str
    ok: bool
    value: float | None = None
    ts: dt.datetime | None = None       # when the value was measured (UTC)
    detail: str = ""
    configured: bool = True             # False: no source chosen yet
    # Set from the input's spec (max_age_s, label), not by the source itself: how old
    # a reading may be before it is stale (a feed that updates every 5 minutes needs
    # more than the 3 minutes a Pi sensor does) and what to call it in an alert.
    max_age_s: float | None = None
    label: str = ""

    def age_s(self, now: dt.datetime) -> float | None:
        if self.ts is None:
            return None
        return (now - self.ts).total_seconds()

    def limit_s(self, default: float) -> float:
        """The staleness limit for this reading: its own, else `default`."""
        return float(self.max_age_s) if self.max_age_s else float(default)


class Source:
    def __init__(self, name: str, **opts):
        self.name = name
        self.opts = opts
        self.offset = float(opts.get("offset_c", 0.0))
        # An exact 0.000 from a DS18B20 is its all-zero scratchpad (a floating or
        # unpowered 1-Wire bus), which passes the sensor's CRC. It is not a
        # tank temperature. On by default for the two temperature inputs.
        self.reject_zero = bool(opts.get("reject_zero", name in ("temp", "inkbird_temp",
                                                                 "probe_temp")))
        self.max_age_s = opts.get("max_age_s")
        self.label = str(opts.get("label", ""))
        # Plausibility (temperature inputs only). A value outside range_c is not a
        # water temperature; max_step_c / step_window_s / match_keys drive the
        # slip and spike checks in JsonlSource (see _screen).
        is_temp = name in TEMP_INPUTS
        rng = opts.get("range_c", list(DEFAULT_RANGE_C) if is_temp else None)
        self.range_c = None if rng is None else (float(rng[0]), float(rng[1]))
        self.max_step_c = float(opts.get("max_step_c", 0.8 if is_temp else 0))
        self.step_window_s = float(opts.get("step_window_s", 600))
        self.match_keys = tuple(opts.get("match_keys", ("t1_f", "t2_f")))

    def read(self, now: dt.datetime) -> Reading:
        raise NotImplementedError

    def in_range(self, r: "Reading") -> "Reading":
        """A good reading outside range_c becomes a failed one."""
        if r.ok and self.range_c is not None and not (
                self.range_c[0] <= r.value <= self.range_c[1]):
            return Reading(self.name, False,
                           detail=(f"{r.value:g} is outside the plausible "
                                   f"{self.range_c[0]:g} to {self.range_c[1]:g} range"))
        return r


class NoSource(Source):
    def read(self, now):
        return Reading(self.name, False, detail="no source configured",
                       configured=False)


class Ds18b20Sysfs(Source):
    """One DS18B20 under /sys/bus/w1/devices/28-*/w1_slave."""

    def read(self, now):
        base = self.opts.get("path", "/sys/bus/w1/devices")
        dev = self.opts.get("device")
        paths = ([os.path.join(base, dev, "w1_slave")] if dev
                 else sorted(glob.glob(os.path.join(base, "28-*", "w1_slave"))))
        if not paths:
            return Reading(self.name, False, detail="no DS18B20 found")
        try:
            with open(paths[0], encoding="ascii") as fh:
                lines = fh.read().strip().splitlines()
        except OSError as err:
            return Reading(self.name, False, detail=f"read failed: {err}"[:120])
        if len(lines) < 2 or not lines[0].endswith("YES"):
            return Reading(self.name, False, detail="CRC check failed")
        if "t=" not in lines[1]:
            return Reading(self.name, False, detail="no t= field")
        try:
            raw = int(lines[1].split("t=")[1]) / 1000.0
        except ValueError:
            return Reading(self.name, False, detail="unparseable t=")
        # 85.0 is the sensor's power-on value and -127 is a bus fault. Both
        # look like data and are not.
        if raw == 85.0 or raw <= -126.0:
            return Reading(self.name, False, detail=f"bogus value {raw}")
        if raw == 0.0 and self.reject_zero:
            return Reading(self.name, False, detail=ZERO_DETAIL)
        return Reading(self.name, True, round(raw + self.offset, 3), now)


ZERO_DETAIL = ("exactly 0.0 is an all-zero sensor read (floating or unpowered "
               "1-Wire bus), not a temperature")


def reading_from_object(name, obj, field, offset, reject_zero=False):
    """Reading from one {"ts": ISO8601 with zone, "<field>": number} object."""
    try:
        value = float(obj[field])
        ts = dt.datetime.fromisoformat(str(obj["ts"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        # A writer that failed says so in an "error" key (value null).
        if obj.get(field) is None and obj.get("error"):
            return Reading(name, False, detail=f"sensor: {obj['error']}"[:120])
        return Reading(name, False, detail=f"line lacks ts/{field}")
    if ts.tzinfo is None:
        return Reading(name, False, detail="ts has no timezone")
    if not math.isfinite(value):
        return Reading(name, False, detail="non-finite value")
    if reject_zero and value == 0.0:
        return Reading(name, False, detail=ZERO_DETAIL)
    return Reading(name, True, round(value + offset, 4), ts.astimezone(UTC))


class JsonlSource(Source):
    """Newest line of a JSON-lines file: {"ts": ISO8601, "<field>": number}."""

    def read(self, now):
        path = self.opts.get("path")
        field = self.opts.get("field", self.name)
        if not path:
            return Reading(self.name, False, detail="path is required")
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 4096))
                tail = fh.read().decode("utf-8", "replace").strip().splitlines()
        except OSError as err:
            return Reading(self.name, False, detail=f"read failed: {err}"[:120])
        failure = None
        good = []                       # [(Reading, object)] newest first
        for line in reversed(tail):
            try:
                obj = json.loads(line)
            except ValueError:
                continue                # a half-written last line
            r = self.in_range(reading_from_object(self.name, obj, field,
                                                  self.offset, self.reject_zero))
            if not self.opts.get("skip_errors"):
                return r
            # A feed that writes an error line for a failed poll (the Inkbird reader
            # does) must not read as "missing" on one bad poll: use the newest good
            # reading, unless it is already too old and a newer failure explains why.
            if not r.ok:
                if not good:
                    failure = failure or r
                continue
            good.append((r, obj))
        if good:
            r = self._screen(good, field)
            limit = self.max_age_s
            if (r.ok and failure is not None and limit
                    and (now - r.ts).total_seconds() > float(limit)):
                return failure
            return r
        if failure is not None:
            return failure
        return Reading(self.name, False, detail="file has no readable line")

    def _screen(self, good, field):
        """Keep an OCR slip or a one-off spike from reading as the water.

        good: readings newest first. Two checks, both off when max_step_c is 0.
          slip   the newest value equals the line's own T1 or T2 (to 0.1 F) and is
                 more than max_step_c away from the newest value that does not:
                 the reader took a setpoint for the temperature (2026-11-03: 76
                 minutes of T2 = 77.6 F while the tank was 26.7 C). The last
                 distinct reading is held, so it ages out into a stale alert.
          spike  the newest value is more than max_step_c away from the one before
                 it, which is under step_window_s older, and nothing has confirmed
                 it yet: the earlier reading is held (it ages out like any other)
                 until a second reading agrees. A real step is seen one poll late.
        """
        r0, o0 = good[0]
        step = self.max_step_c
        if step <= 0:
            return r0

        def matches(r, obj):
            for key in self.match_keys:
                try:
                    f = float(obj.get(key))
                except (TypeError, ValueError):
                    continue
                if abs((r.value - self.offset) * 1.8 + 32 - f) <= 0.06:
                    return True
            return False

        if matches(r0, o0):
            base = next((r for r, o in good[1:] if not matches(r, o)), None)
            if base is not None and abs(r0.value - base.value) > step:
                return dataclasses.replace(
                    base,
                    detail=(f"holding {base.value:g} C: the newer {r0.value:g} C "
                            "equals the device's own T1/T2 and is "
                            f"{abs(r0.value - base.value):.2f} C away, taken as a "
                            "setpoint misread"))
        if len(good) >= 2:
            r1 = good[1][0]
            gap = (r0.ts - r1.ts).total_seconds()
            if 0 < gap <= self.step_window_s and abs(r0.value - r1.value) > step:
                return dataclasses.replace(
                    r1, detail=(f"holding {r1.value:g} C: the newer {r0.value:g} C "
                                "jumped and is not confirmed yet"))
        return r0


class HttpJsonSource(Source):
    """GET a URL that returns {"ts": ISO8601, "<field>": number}."""

    def read(self, now):
        url = self.opts.get("url")
        field = self.opts.get("field", self.name)
        if not url:
            return Reading(self.name, False, detail="url is required")
        if not str(url).startswith(("http://", "https://")):
            return Reading(self.name, False, detail="url must be http or https")
        timeout = float(self.opts.get("timeout_s", 4))
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return Reading(self.name, False, detail=f"http {resp.status}")
                body = resp.read(65536)
        except urllib.error.HTTPError as err:
            err.close()
            return Reading(self.name, False, detail=f"http {err.code}")
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            return Reading(self.name, False,
                           detail=f"unreachable: {getattr(err, 'reason', err)}"[:120])
        try:
            obj = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return Reading(self.name, False, detail="reply is not JSON")
        if not isinstance(obj, dict):
            return Reading(self.name, False, detail="reply is not an object")
        return self.in_range(reading_from_object(self.name, obj, field, self.offset,
                                                 self.reject_zero))


SOURCES = {"ds18b20_sysfs": Ds18b20Sysfs, "jsonl": JsonlSource,
           "http_json": HttpJsonSource, "none": NoSource}


def build_source(name: str, spec: dict | None) -> Source:
    spec = dict(spec or {})
    cls = SOURCES.get(spec.pop("driver", "none"))
    if cls is None:
        raise ValueError(f"{name}: unknown input driver")
    return cls(name, **spec)


class Inputs:
    def __init__(self, sources: dict[str, Source]):
        self.sources = sources

    @classmethod
    def from_config(cls, spec: dict | None) -> "Inputs":
        spec = spec or {}
        names = ["temp", "lux"]
        for extra in ("inkbird_temp", "probe_temp"):
            if spec.get(extra):         # optional second thermometer, never the alarm
                names.append(extra)
        return cls({n: build_source(n, spec.get(n)) for n in names})

    def read_all(self, now: dt.datetime) -> dict[str, Reading]:
        out = {}
        for name, src in self.sources.items():
            try:
                r = src.read(now)
            except Exception as err:  # noqa: BLE001 - sources must not raise
                r = Reading(name, False, detail=f"{type(err).__name__}: {err}"[:120])
            r.max_age_s, r.label = src.max_age_s, src.label
            out[name] = r
        return out
