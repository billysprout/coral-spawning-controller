#!/usr/bin/env python3
"""heat_equipment.py -- equipment that changes the tank's heat but is not on a plug.

The conductor sees the lamps (they are on plugs) and the heater (the Inkbird page),
but not everything. A PC fan blowing on the tank, run from a low-voltage supply,
cools the water and nothing records that it is there. This file is where such things
are written down so every heat log line and every heat-delta window can carry them,
and so nobody has to remember later which days the fan was on.

State lives in <data>/heat/equipment.json:

    {"pc_fan": [
        {"since": "2026-11-03T21:00:00+00:00", "state": "on", "voltage_v": null,
         "note": "PC fan on low voltage cooling the tank; start date not given"}
    ]}

Each entry runs from its `since` until the next one. Before the first entry the state
is "unknown", never "off": the history only says "on" or "off" when somebody said so.
`voltage_v` and `power_pct` (how hard the fan is driven: the volts, or where a speed
adjuster is set) are recorded with the entry, because the
cooling it gives depends on it; change it by adding an entry, not by editing one.
The first entry written here ("assumed": true) is a guess made when the fan was
reported; the first `set` replaces it, so give the real start with --at.

    python heat_equipment.py show
    python heat_equipment.py set pc_fan on  --voltage 5 [--at ISO] [--note TEXT]
    python heat_equipment.py set pc_fan off [--at ISO]

`--at` defaults to now. Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

UTC = dt.timezone.utc
STATES = ("on", "off")

DEFAULT = {
    "pc_fan": [{
        "since": "2026-11-03T21:00:00+00:00", "state": "on", "voltage_v": None,
        "assumed": True,          # a guess made when the fan was reported, not a record
        "note": "PC fan on low voltage cooling the tank (reported 2026-11-03; the start "
                "date and the voltage were not given, so earlier history is unknown)"}],
}


def _ts(text):
    t = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def path_for(data_dir: str) -> str:
    return os.path.join(data_dir, "heat", "equipment.json")


def load(data_dir: str, create: bool = False) -> dict:
    """{name: [entries sorted by since]}; the default when the file is missing or
    unreadable (written out when create is true)."""
    p = path_for(data_dir)
    try:
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = DEFAULT
        if create:
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                save(data_dir, raw)
            except OSError:
                pass
    out = {}
    for name, entries in (raw or {}).items():
        good = []
        for e in entries or []:
            try:
                _ts(e["since"])
                if e.get("state") in STATES:
                    good.append(e)
            except (KeyError, ValueError, TypeError):
                continue
        out[name] = sorted(good, key=lambda e: _ts(e["since"]))
    return out


def save(data_dir: str, equipment: dict) -> None:
    p = path_for(data_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(equipment, fh, indent=2)
    os.replace(tmp, p)


def entry_at(equipment: dict, name: str, t: dt.datetime):
    """The entry in force for `name` at `t`, or None (unknown)."""
    cur = None
    for e in equipment.get(name) or []:
        if _ts(e["since"]) <= t:
            cur = e
    return cur


def state_at(equipment: dict, name: str, t: dt.datetime) -> str:
    """"on", "off" or "unknown"."""
    e = entry_at(equipment, name, t)
    return e["state"] if e else "unknown"


def view_at(equipment: dict, t: dt.datetime) -> dict:
    """What a heat log line records: {name: {"state", "voltage_v"}}."""
    out = {}
    for name in equipment:
        e = entry_at(equipment, name, t)
        out[name] = {"state": e["state"] if e else "unknown",
                     "voltage_v": e.get("voltage_v") if e else None,
                     "power_pct": e.get("power_pct") if e else None}
    return out


def set_state(data_dir: str, name: str, state: str, at: dt.datetime | None = None,
              voltage_v: float | None = None, note: str = "",
              power_pct: float | None = None) -> dict:
    if state not in STATES:
        raise ValueError("state must be on or off")
    eq = load(data_dir)
    when = at or dt.datetime.now(UTC)
    # a guessed entry is dropped by the first real one; a recorded one is kept
    entries = [e for e in eq.get(name, []) if _ts(e["since"]) != when and not e.get("assumed")]
    entries.append({"since": when.isoformat(timespec="seconds"), "state": state,
                    "voltage_v": voltage_v, "power_pct": power_pct, "note": note})
    eq[name] = sorted(entries, key=lambda e: _ts(e["since"]))
    save(data_dir, eq)
    return eq


def main(argv=None) -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=os.environ.get("TANK_DATA", os.path.join(here, "data")))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    s = sub.add_parser("set")
    s.add_argument("name")
    s.add_argument("state", choices=STATES)
    s.add_argument("--voltage", type=float, default=None)
    s.add_argument("--power-pct", type=float, default=None,
                   help="where the speed adjuster is set, if there is one")
    s.add_argument("--at", default=None, help="ISO time, default now")
    s.add_argument("--note", default="")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        eq = load(a.data, create=True)
        for name, entries in eq.items():
            print(name)
            print("  before the first entry: unknown")
            for e in entries:
                print(f"  {e['since']}  {e['state']:<3}  voltage {e.get('voltage_v')}  "
                      f"power {e.get('power_pct')}%  "
                      f"{'(assumed) ' if e.get('assumed') else ''}{e.get('note', '')}")
        return 0
    when = _ts(a.at) if a.at else None
    eq = set_state(a.data, a.name, a.state, when, a.voltage, a.note, a.power_pct)
    print(f"{a.name}: {a.state} since {(when or dt.datetime.now(UTC)).isoformat(timespec='seconds')}"
          + (f" at {a.voltage} V" if a.voltage is not None else "")
          + (f" at {a.power_pct:g}% power" if a.power_pct is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
