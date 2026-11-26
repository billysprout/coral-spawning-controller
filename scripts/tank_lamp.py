#!/usr/bin/env python3
"""tank_lamp.py -- measure what a lamp does at the lux sensor, to set min_lux_change.

tank_verify.py confirms a lamp command from the lux sensor only for channels
that have outputs.verify.channels.<ch>.min_lux_change set: the rise (or fall)
in lux the lamp must make at the sensor. That number depends on the lamp, the
sensor's position and the room, so it is measured, not guessed:

    python conductor.py measure-lamp --plug plug-8 --yes

For each cycle the tool switches the plug OFF, waits settle_s for the lamp and
the sensor to settle, reads the lux sensor for sample_s, then does the same with
the plug ON. It uses only readings the sensor stamped AFTER the settle time, so
the previous state cannot leak in, and it puts the plug back as it found it
(even if something fails midway).

From the readings it reports, per state, the mean and the scatter, and the
change the lamp makes (on minus off). It then suggests a threshold:

    suggested = max(5 x noise, 0.5 lux, 40% of the change)

where noise is the larger scatter of the two states. 40% of the change means a
lamp running well below full still passes; 5 x noise and the 0.5 lux floor mean
ordinary flicker and the sensor's step size cannot. If the change is too small
to put a threshold above those floors (at least twice the floor), the tool says
the lamp is NOT visible at this position and suggests nothing: move the sensor
or leave lux verification off for that channel (the watts check still applies).
Several cycles also show whether the readings are stable: cycles whose changes
disagree by more than 30% raise a warning (daylight, someone switching a room
light).

Nothing here changes the config; it prints the snippet to paste. Standard library
only.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics
import time

UTC = dt.timezone.utc
FLOOR_LUX = 0.5            # about four steps of a BH1750 at its finest setting
NOISE_FACTOR = 5.0
CHANGE_FRACTION = 0.4
MIN_SAMPLES = 3


class LampError(Exception):
    pass


def sig2(x: float) -> float:
    """Round to two significant figures."""
    if x == 0 or not math.isfinite(x):
        return x
    r = round(x, 1 - int(math.floor(math.log10(abs(x)))))
    return int(r) if r == int(r) else r


def summarize(values: list) -> dict:
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return {"n": len(values), "mean": statistics.fmean(values), "sd": sd,
            "min": min(values), "max": max(values)}


def suggest(off: list, on: list, cycle_changes: list | None = None) -> dict:
    """The threshold from pooled readings of each state. Pure arithmetic."""
    a, b = summarize(off), summarize(on)
    change = b["mean"] - a["mean"]
    noise = max(a["sd"], b["sd"])
    floor = max(NOISE_FACTOR * noise, FLOOR_LUX)
    out = {"off": a, "on": b, "change_lux": change, "noise_lux": noise,
           "floor_lux": floor, "suggested_min_lux_change": None,
           "usable": False, "warnings": []}
    if change >= 2 * floor:
        out["suggested_min_lux_change"] = sig2(max(floor, CHANGE_FRACTION * change))
        out["usable"] = True
    elif change <= 0:
        out["warnings"].append(
            "the lamp made the reading no brighter (or darker): the sensor does not "
            "see this lamp from where it is")
    else:
        out["warnings"].append(
            f"the change ({change:.2f} lux) is under twice the noise floor "
            f"({floor:.2f} lux): too small to verify the lamp from here")
    if cycle_changes and len(cycle_changes) > 1 and out["usable"]:
        lo, hi = min(cycle_changes), max(cycle_changes)
        if hi > 0 and (hi - lo) / hi > 0.3:
            out["warnings"].append(
                "the cycles disagree by more than 30% "
                f"({', '.join(f'{c:.2f}' for c in cycle_changes)} lux): something else "
                "is changing the light (daylight, a room light); repeat in steady light")
    return out


def _switch(plug, on: bool):
    plug.set_on(on)
    back = plug.status()
    if not (back.ok and back.on == on):
        raise LampError(f"{plug.name} did not go {'ON' if on else 'OFF'} "
                        f"(reads {back.on if back.ok else 'no answer'})")


def _collect(lux_source, after: dt.datetime, until_s: float, poll_s: float,
             now_fn, sleep, max_age_s: float) -> list:
    """Readings stamped at or after `after`, distinct, until the window closes."""
    seen, values = set(), []
    end = now_fn() + dt.timedelta(seconds=until_s)
    while True:
        now = now_fn()
        r = lux_source.read(now)
        if (r.ok and r.ts is not None and r.ts >= after and r.ts not in seen
                and (now - r.ts).total_seconds() <= max_age_s):
            seen.add(r.ts)
            values.append(float(r.value))
        if now >= end:
            return values
        sleep(poll_s)


def measure(plug, lux_source, settle_s=20.0, sample_s=40.0, cycles=2, poll_s=3.0,
            now_fn=lambda: dt.datetime.now(UTC), sleep=time.sleep, say=print,
            max_age_s=180.0) -> dict:
    """Run the cycles and return the report (see suggest()). The plug is put back
    in the state it was found in, always."""
    start = plug.status()
    if not start.ok or start.on is None:
        raise LampError(f"{plug.name} is not reachable: {start.detail}")
    first = lux_source.read(now_fn())
    if not first.ok:
        raise LampError(f"lux sensor not readable: {first.detail}")
    started_on = bool(start.on)
    off_all, on_all, changes = [], [], []
    try:
        for i in range(1, cycles + 1):
            got = {}
            for state in (False, True):
                _switch(plug, state)
                t0 = now_fn()
                say(f"cycle {i}/{cycles}: lamp {'ON ' if state else 'OFF'}, settling "
                    f"{settle_s:.0f}s then reading {sample_s:.0f}s")
                sleep(settle_s)
                after = t0 + dt.timedelta(seconds=settle_s)
                vals = _collect(lux_source, after, sample_s, poll_s, now_fn, sleep,
                                max_age_s)
                if len(vals) < MIN_SAMPLES:
                    raise LampError(
                        f"only {len(vals)} new lux readings in {sample_s:.0f}s with the "
                        f"lamp {'ON' if state else 'OFF'} (need {MIN_SAMPLES}); is the "
                        "lux logger running, or lengthen --sample-s")
                got[state] = vals
                say(f"    {len(vals)} readings, mean {statistics.fmean(vals):.2f} lux")
            off_all += got[False]
            on_all += got[True]
            changes.append(statistics.fmean(got[True]) - statistics.fmean(got[False]))
    finally:
        try:
            _switch(plug, started_on)           # leave it as we found it
        except Exception as err:  # noqa: BLE001
            say(f"WARNING: could not put {plug.name} back "
                f"{'ON' if started_on else 'OFF'} ({err}); do it by hand now")
    report = suggest(off_all, on_all, changes)
    report.update(plug=plug.name, cycles=cycles, cycle_changes=changes,
                  settle_s=settle_s, sample_s=sample_s, restored_on=started_on)
    return report


def format_report(rep: dict, channel: str | None) -> str:
    ch = channel or "<channel>"
    a, b = rep["off"], rep["on"]
    lines = [
        f"{rep['plug']}: lamp OFF  mean {a['mean']:.2f} lux, scatter {a['sd']:.2f}  ({a['n']} readings)",
        f"{rep['plug']}: lamp ON   mean {b['mean']:.2f} lux, scatter {b['sd']:.2f}  ({b['n']} readings)",
        f"change at the sensor: {rep['change_lux']:+.2f} lux "
        f"(per cycle: {', '.join(f'{c:+.2f}' for c in rep['cycle_changes'])}); "
        f"noise floor {rep['floor_lux']:.2f} lux"]
    for w in rep["warnings"]:
        lines.append(f"WARNING: {w}")
    if rep["usable"]:
        v = rep["suggested_min_lux_change"]
        lines += ["", f"Suggested threshold: {v} lux. Put this in data/tank_config.json:",
        '  ' + json.dumps({"outputs": {"verify": {"channels": {ch: {"min_lux_change": v}}}}})]
    else:
        lines += ["", "No threshold suggested: leave min_lux_change unset for this "
                      "channel (the watts check still applies)."]
    lines.append(f"{rep['plug']} was put back {'ON' if rep['restored_on'] else 'OFF'}.")
    return "\n".join(lines)
