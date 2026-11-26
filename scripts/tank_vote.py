"""Temperature redundancy management: several thermometers, one decision.

The tank has up to three temperature channels (inputs.temp, the primary;
inputs.probe_temp and inputs.inkbird_temp, the second opinions). Until
2026-11-15 only the primary was ever acted on: when it went blind, the
over-temperature cutoff and the heater gate went blind with it, even with a
healthy probe in the water.

This module is the selection logic flight computers and reactor protection
systems use for redundant sensors:

  3 healthy channels   mid-value select. The middle reading is used, so one
                       channel failing high or low cannot move the answer; a
                       channel far from the middle is reported as outvoted.
  2 healthy channels   they are compared. Agreeing: the primary's value is
                       used (no change from single-sensor behaviour).
                       Disagreeing: nobody can say which is wrong, so the
                       primary is still used and the miscompare is reported.
  1 healthy channel    that channel. With `failover` on this is where a backup
                       takes over from a dead primary.
  0                    blind.

It also gives the highest and lowest healthy reading. A protection trip should
use those ("one out of N": any healthy channel may trip), which is what the
lifeguard does.

`failover` is off by default: with it off, `selected` is always the primary
reading itself and the controller behaves exactly as before; the vote is only
reported. Standard library only.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

CHANNELS = ("temp", "probe_temp", "inkbird_temp")

DEFAULT_REDUNDANCY = {
    # Act on a healthy backup channel when the primary cannot be used.
    "failover": False,
    # Healthy channels further apart than this disagree (C). The Pi probe and
    # the Inkbird sit within about 0.3 C of each other once the probe's offset
    # is applied; 1.0 C is far outside that.
    "miscompare_c": 1.0,
}


def redundancy_config(over: dict | None = None) -> dict:
    cfg = dict(DEFAULT_REDUNDANCY)
    cfg.update(over or {})
    if not isinstance(cfg["failover"], bool):
        raise ValueError("limits.redundancy.failover must be true or false")
    if not 0.1 <= float(cfg["miscompare_c"]) <= 5.0:
        raise ValueError("limits.redundancy.miscompare_c must be 0.1..5")
    return cfg


def _healthy(reading, now: dt.datetime, default_age_s: float) -> bool:
    if reading is None or not reading.configured or not reading.ok:
        return False
    if reading.value is None:
        return False
    age = reading.age_s(now)
    if age is None:
        return True                 # a source with no timestamp was read just now
    return age <= reading.limit_s(default_age_s)


def _label(name: str, reading) -> str:
    return getattr(reading, "label", "") or name


def vote(reads: dict, now: dt.datetime, default_age_s: float = 180.0,
         cfg: dict | None = None) -> dict:
    """Vote the temperature channels in `reads` ({name: Reading}).

    Returns a dict:
      selected   the Reading the controller should act on
      source     the name of the channel `selected` came from
      mode       "triple" | "dual" | "miscompare" | "single" | "blind"
      failover   True when `selected` is not the primary
      configured / healthy   channel names
      high_c / low_c         highest and lowest healthy value (None when blind)
      spread_c               high minus low
      outvoted               channels far from the voted value
      findings               [(key, level, message, alert)]
    """
    cfg = redundancy_config(cfg)
    limit = float(cfg["miscompare_c"])
    chans = {n: reads[n] for n in CHANNELS
             if n in reads and getattr(reads[n], "configured", True)}
    primary = reads.get("temp")
    healthy = [n for n in chans if _healthy(chans[n], now, default_age_s)]
    values = {n: float(chans[n].value) for n in healthy}
    out = {"selected": primary, "source": "temp", "mode": "blind", "failover": False,
           "configured": list(chans), "healthy": healthy,
           "high_c": max(values.values()) if values else None,
           "low_c": min(values.values()) if values else None,
           "spread_c": (round(max(values.values()) - min(values.values()), 3)
                        if values else None),
           "outvoted": [], "voted_c": None, "findings": []}
    if not healthy:
        return out

    # The voted value: the middle of three, else the first healthy channel in
    # priority order (the primary when it is healthy).
    if len(healthy) >= 3:
        ranked = sorted(healthy, key=lambda n: values[n])
        mid = ranked[len(ranked) // 2]
        out["mode"] = "triple"
        out["voted_c"] = values[mid]
        out["outvoted"] = [n for n in healthy if abs(values[n] - values[mid]) > limit]
        pick = mid if "temp" in out["outvoted"] or "temp" not in healthy else "temp"
    elif len(healthy) == 2:
        pick = healthy[0]
        out["voted_c"] = values[pick]
        out["mode"] = "dual" if out["spread_c"] <= limit else "miscompare"
    else:
        pick = healthy[0]
        out["voted_c"] = values[pick]
        out["mode"] = "single"

    for n in out["outvoted"]:
        out["findings"].append((
            f"temp_outvoted:{n}", "warn",
            f"{_label(n, chans[n])} reads {values[n]:.2f} C, more than {limit:g} C "
            f"from the other two channels ({out['voted_c']:.2f} C): it is outvoted",
            False))
    if out["mode"] == "miscompare":
        a, b = healthy
        out["findings"].append((
            "temp_miscompare", "warn",
            f"{_label(a, chans[a])} ({values[a]:.2f} C) and {_label(b, chans[b])} "
            f"({values[b]:.2f} C) differ by {out['spread_c']:.2f} C, more than "
            f"{limit:g} C. With two channels nobody can say which is wrong; check "
            "both against a hand thermometer", False))
    if len(chans) >= 2 and len(healthy) == 1:
        down = [_label(n, chans[n]) for n in chans if n not in healthy]
        out["findings"].append((
            "temp_redundancy_lost", "warn",
            f"only {_label(healthy[0], chans[healthy[0]])} is reading; "
            f"{', '.join(down)} cannot be used. One more failure and the tank's "
            "temperature is not known", False))

    if pick != "temp":
        src = chans[pick]
        why = "is outvoted" if "temp" in healthy else "cannot be used"
        if cfg["failover"]:
            out["selected"] = dataclasses.replace(
                src, name="temp", label=f"{_label(pick, src)}, standing in for the primary")
            out["source"] = pick
            out["failover"] = True
            out["findings"].append((
                "temp_failover", "alert",
                f"the primary temperature {why}; the controller is acting "
                f"on {_label(pick, src)} ({values[pick]:.2f} C) until it returns", True))
        else:
            out["findings"].append((
                "temp_backup_idle", "warn",
                f"the primary temperature {why} and {_label(pick, src)} is "
                f"reading ({values[pick]:.2f} C), but sensor failover is off "
                "(limits.redundancy.failover), so the cutoff and the heater gate "
                "are not acting on it", False))
    return out


def view(v: dict) -> dict:
    """The vote without the Reading object, for the status file."""
    return {k: v[k] for k in ("source", "mode", "failover", "configured", "healthy",
                              "voted_c", "high_c", "low_c", "spread_c", "outvoted")}
