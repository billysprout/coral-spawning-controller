"""Safe-mode latch: one small file both the lifeguard and the controller read.

The lifeguard (scripts/lifeguard.py) sets the latch when it trips. While it is
set, the controller (tank_out.Actuator) keeps every lighting plug off and
leaves the heater rules alone; nothing else changes. The latch is a file so it
survives a restart of either side, the way a tripped breaker stays tripped.

    <data>/lifeguard/safe_mode.json     {"active": true, "reason": ..., ...}

A latch file that exists but cannot be read counts as SET (fail safe). Standard
library only, and no import of the controller: both sides must be able to load
this when the other is broken.
"""
from __future__ import annotations

import datetime as dt
import json
import os

FOLDER = "lifeguard"
LATCH = "safe_mode.json"
UTC = dt.timezone.utc


def folder(data_dir: str) -> str:
    return os.path.join(data_dir, FOLDER)


def path(data_dir: str) -> str:
    return os.path.join(folder(data_dir), LATCH)


def read(data_dir: str | None) -> dict | None:
    """The latch when it is set, else None."""
    if not data_dir:
        return None
    p = path(data_dir)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return {"active": True, "reason": "unreadable",
                "detail": "the safe-mode latch file exists but cannot be read"}
    if not isinstance(obj, dict) or not obj.get("active"):
        return None
    return obj


def _write(p: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, p)


def set_latch(data_dir: str, reason: str, detail: str, now: dt.datetime,
              by: str = "lifeguard") -> dict:
    """Set the latch (keeps the first trip's time if it is already set)."""
    old = read(data_dir)
    obj = {"active": True, "reason": reason, "detail": detail,
           "since": (old or {}).get("since") or now.astimezone(UTC).isoformat(),
           "by": by}
    _write(path(data_dir), obj)
    return obj


def clear(data_dir: str, now: dt.datetime, by: str) -> bool:
    """Release the latch. True when there was one."""
    p = path(data_dir)
    was = read(data_dir) is not None
    if os.path.exists(p):
        _write(p, {"active": False, "released": now.astimezone(UTC).isoformat(),
                   "released_by": by})
    return was
