#!/usr/bin/env python3
"""sandbox_feed.py -- dummy temperature and lux for the sandbox stack.

The sandbox conductor (compose profile `sandbox`) drives fake plugs only. This
loop stands in for the Inkbird, the Pi probe and the Pi lux sensor so the
conductor sees a tank that answers its own lamps:

    lux   = room floor + the lamp_lux of every lit lamp (outputs.verify), plus a
            small moon value when the moon lamp is on
    temp  = the plan's hourly target + 0.4 C per lit lamp, eased toward that
            each step, plus a little noise

It reads <data>/tank_status.json (the conductor's own record of each plug) and
appends one line per step to <data>/sensors/sandbox_{temp,probe,lux}.jsonl.
It talks to no device and no network. Each file is cut to its last 2,000
lines when it passes 1 MB.

    python sandbox_feed.py [--every-s 30] [--once]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import time

ROOM_LUX = 0.15
MOON_LUX = 0.2
LAMP_HEAT_C = 0.4
EASE = 0.15
MAX_BYTES = 1_000_000


def data_dir() -> str:
    return os.environ.get("TANK_DATA", "/data")


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _append(path, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj) + "\n")
    if os.path.getsize(path) > MAX_BYTES:
        with open(path, encoding="utf-8") as fh:
            keep = fh.readlines()[-2000:]
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(keep)


def step(data: str, temp_c: float | None, rng=random) -> float:
    status = _read(os.path.join(data, "tank_status.json"), {}) or {}
    cfg = _read(os.path.join(data, "tank_config.json"), {}) or {}
    verify = ((cfg.get("outputs") or {}).get("verify") or {}).get("channels") or {}
    lux, lit = ROOM_LUX, 0
    for p in (status.get("plugs") or {}).values():
        if not p.get("on"):
            continue
        ch = p.get("channel")
        if ch == "moon":
            lux += MOON_LUX
            continue
        lit += 1
        lux += float((verify.get(ch) or {}).get("lamp_lux") or 50.0)
    plan = status.get("plan") or {}
    base = plan.get("temp_hourly_c") or plan.get("temp_daily_c") or 26.0
    want = float(base) + LAMP_HEAT_C * lit
    temp_c = want if temp_c is None else temp_c + EASE * (want - temp_c)
    t = round(temp_c + rng.uniform(-0.03, 0.03), 3)
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    sens = os.path.join(data, "sensors")
    t_f = t * 9 / 5 + 32
    _append(os.path.join(sens, "sandbox_temp.jsonl"),
            {"ts": ts, "temp_c": t, "t1_f": round(t_f - 0.3, 1),
             "t2_f": round(t_f + 0.2, 1), "heating": False, "read": "sandbox"})
    _append(os.path.join(sens, "sandbox_probe.jsonl"),
            {"ts": ts, "temp_c": round(t + rng.uniform(-0.05, 0.05), 3)})
    _append(os.path.join(sens, "sandbox_lux.jsonl"),
            {"ts": ts, "lux": round(lux + rng.uniform(0, 0.02), 3)})
    return temp_c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sandbox_feed.py")
    ap.add_argument("--every-s", type=float, default=30.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    data, temp = data_dir(), None
    while True:
        temp = step(data, temp)
        with open(os.path.join(data, "sandbox_feed_heartbeat.json"), "w") as fh:
            fh.write(json.dumps({"ts": time.time(), "temp_c": temp}))
        if a.once:
            return 0
        time.sleep(a.every_s)


if __name__ == "__main__":
    raise SystemExit(main())
