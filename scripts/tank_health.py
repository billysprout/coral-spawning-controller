#!/usr/bin/env python3
"""tank_health.py -- read-only health and status for the plug array.

Nothing here can switch a plug. The only plug call is Plug.status(), which for
tinytuya is one data-point query. Two users:

  * HealthTracker: fed every plug reading the minute conductor already takes
    (no extra traffic). Keeps a rolling window per plug and reports a level
    (ok / degraded / down / unknown), the failure streak, success rate, query
    latency, how long it has been in its current state, the last error with a
    plain-English hint, and the plug's raw data points. Lands in
    tank_status.json under "plug_health".
  * check_plugs(): a one-shot check for `conductor.py health`. One query per
    plug, nothing else.

Health findings are warnings, never alerts: the "plug offline" alert in
tank_tick already covers a dead plug, and a health rule must never be a reason
to change what a plug does.
"""
from __future__ import annotations

import collections
import datetime as dt

HINTS = {
    "unreachable": "no answer on the LAN. The plug is unplugged or off Wi-Fi, "
                   "its IP changed (check the DHCP reservation), or another "
                   "client holds its only local connection (close the phone app)",
    "timeout": "the plug did not answer in time. Weak Wi-Fi, or another client "
               "holds its only local connection (close the phone app)",
    "bad_key_or_version": "wrong local key or protocol version. A plug re-paired "
                          "in an app gets a new key (re-run the tinytuya wizard "
                          "and refresh devices.json), or `version` in "
                          "plug_array.json is wrong",
    "no_key": "this plug's id has no local key in devices.json",
    "not_configured": "the plug needs an id and an ip in plug_array.json",
    "dp_missing": "the plug answered without the switch data point. power_dp in "
                  "plug_array.json does not match this model",
    "bad_reply": "the plug answered with something unreadable",
    "no_library": "tinytuya is not installed in this environment",
    "unknown_driver": "plug_array.json / extra_plugs.json names a driver "
                      "that does not exist (typo?). Registered: tinytuya, fake, tbd",
    "exception": "the query raised an exception",
    "error": "the plug reported an error",
}

# Tuning. Deliberately forgiving: a Wi-Fi plug misses the odd poll.
WINDOW = 60                 # readings kept per plug (an hour at one a minute)
DOWN_AFTER = 3              # consecutive failed readings
DEGRADED_BELOW = 0.9        # success rate over the window ...
DEGRADED_MIN_SAMPLES = 10   # ... once there are at least this many readings
SLOW_MS = 2000              # average query time that counts as degraded


class _Plug:
    def __init__(self):
        self.samples = collections.deque(maxlen=WINDOW)     # (ts, ok, ms)
        self.streak = 0
        self.last_ok = None
        self.last_fail = None
        self.state_since = None         # when the current ok/failing run began
        self.last_error = None
        self.last_detail = ""
        self.last_dps = None
        self.dp_keys = None             # first set of data points seen
        self.last_on = None
        self.flips = collections.deque(maxlen=120)


class HealthTracker:
    def __init__(self):
        self._p: dict[str, _Plug] = {}

    def observe(self, now: dt.datetime, reading) -> None:
        if not getattr(reading, "configured", True):
            return                      # a tbd placeholder has no health to track
        p = self._p.setdefault(reading.name, _Plug())
        ms = getattr(reading, "latency_ms", None)
        was_ok = p.samples[-1][1] if p.samples else None
        p.samples.append((now, bool(reading.ok), ms))
        if was_ok is None or was_ok != bool(reading.ok):
            p.state_since = now
        if reading.ok:
            p.streak = 0
            p.last_ok = now
            if p.last_on is not None and reading.on != p.last_on:
                p.flips.append(now)
            p.last_on = reading.on
            p.last_error, p.last_detail = None, ""
        else:
            p.streak += 1
            p.last_fail = now
            p.last_error = getattr(reading, "error", None) or "error"
            p.last_detail = reading.detail
        dps = getattr(reading, "dps", None)
        if dps is not None:
            p.last_dps = dict(dps)
            if p.dp_keys is None:
                p.dp_keys = sorted(dps)

    # -- reporting ---------------------------------------------------------
    @staticmethod
    def _level(p: _Plug) -> str:
        if not p.samples:
            return "unknown"
        if p.streak >= DOWN_AFTER or (p.last_ok is None and p.streak > 0):
            return "down"
        oks = [s for s in p.samples if s[1]]
        lat = [s[2] for s in oks if s[2] is not None]
        if (len(p.samples) >= DEGRADED_MIN_SAMPLES
                and len(oks) / len(p.samples) < DEGRADED_BELOW):
            return "degraded"
        if lat and sum(lat) / len(lat) > SLOW_MS:
            return "degraded"
        return "ok"

    def snapshot(self, now: dt.datetime) -> dict:
        out = {}
        for name, p in self._p.items():
            oks = [s for s in p.samples if s[1]]
            lat = [s[2] for s in oks if s[2] is not None]
            level = self._level(p)
            hour = [t for t in p.flips if (now - t).total_seconds() < 3600]
            out[name] = {
                "level": level,
                "ok": bool(p.samples and p.samples[-1][1]),
                "in_state_s": (None if p.state_since is None
                               else round((now - p.state_since).total_seconds())),
                "consecutive_failures": p.streak,
                "success_rate": (round(len(oks) / len(p.samples), 3)
                                 if p.samples else None),
                "samples": len(p.samples),
                "latency_ms": ({"last": lat[-1],
                                "avg": round(sum(lat) / len(lat)),
                                "max": max(lat)} if lat else None),
                "last_ok_age_s": (None if p.last_ok is None
                                  else round((now - p.last_ok).total_seconds())),
                "last_error": p.last_error,
                "last_detail": p.last_detail,
                "hint": HINTS.get(p.last_error) if level != "ok" or p.streak else None,
                "state_flips_1h": len(hour),
                "dps": p.last_dps,
                "dp_keys": p.dp_keys,
            }
        return out

    @staticmethod
    def findings(snap: dict) -> list:
        """Warnings only (alert False): the offline alert is tank_tick's job."""
        out = []
        for name, h in snap.items():
            if h["level"] == "down":
                out.append(_finding(
                    f"plug_down:{name}", f"{name} is not answering "
                    f"({h['last_error']}): {h['hint']}"))
            elif h["level"] == "degraded":
                rate = h["success_rate"]
                lat = (h["latency_ms"] or {}).get("avg")
                out.append(_finding(
                    f"plug_degraded:{name}", f"{name} is flaky: "
                    f"{round(rate * 100)}% of the last {h['samples']} readings "
                    f"answered, average {lat} ms"))
            if (h["dps"] is not None and h["dp_keys"] is not None
                    and sorted(h["dps"]) != h["dp_keys"]):
                out.append(_finding(
                    f"plug_dps_changed:{name}", f"{name}: its data points "
                    f"changed from {h['dp_keys']} to {sorted(h['dps'])} "
                    "(firmware update or re-pair?)"))
        return out


def _finding(key: str, message: str) -> dict:
    return {"key": key, "level": "warn", "message": message, "alert": False}


# ---------------------------------------------------------------------------
# WATTAGE SANITY (only for plugs that report watts)
# ---------------------------------------------------------------------------
def power_findings(plugs, reads: dict) -> list:
    """Compare a plug's on/off with the watts it reports. Warnings only.

    A plug that does not report watts (power_w None) is skipped: on/off is all
    it can say. Set expect_w [min, max] on a plug entry to also check the draw
    while it is ON; off_max_w (default 1.0) bounds the draw while it is OFF.
    """
    out = []
    for plug in plugs:
        r = reads.get(plug.name)
        if r is None or not r.ok or r.power_w is None or r.on is None:
            continue
        w = r.power_w
        if r.on is False and w > plug.off_max_w:
            out.append(_finding(
                f"plug_watts_while_off:{plug.name}",
                f"{plug.name} reads OFF but draws {w} W (limit {plug.off_max_w} W): "
                "stuck relay, or the plug is reporting the wrong data point"))
        elif r.on and plug.expect_w is not None:
            lo, hi = plug.expect_w
            if w < lo:
                out.append(_finding(
                    f"plug_watts_low:{plug.name}",
                    f"{plug.name} is ON but draws {w} W (expected {lo} to {hi} W): "
                    "the load may be dead or unplugged"))
            elif w > hi:
                out.append(_finding(
                    f"plug_watts_high:{plug.name}",
                    f"{plug.name} draws {w} W (expected {lo} to {hi} W): "
                    "overload or a different load than planned"))
    return out


def countdown_findings(plugs, reads: dict) -> list:
    """Warn when a plug that must come back ON by itself is OFF with its
    countdown disarmed. Warnings only; this never sends a command.

    The countdown data point holds the armed length and drops to 0 when it
    fires or is cleared (it does not tick down), so OFF with 0 means nothing
    will turn the plug back on. A power cut with the plug's power-on state set
    to OFF, or a reset, does exactly this. Only plugs marked revert_to_on (the
    heater role by default) are checked: for a light, OFF with no countdown is
    normal.
    """
    out = []
    for plug in plugs:
        r = reads.get(plug.name)
        if (r is None or not r.ok or not getattr(plug, "revert_to_on", False)
                or not plug.supports_revert or r.on is not False
                or r.revert_remaining_s is None):
            continue
        if r.revert_remaining_s <= 0:
            out.append(_finding(
                f"countdown_not_armed:{plug.name}",
                f"{plug.name} is OFF with no countdown armed, so it will not "
                "turn itself back on. The plug was probably reset or lost power. "
                "With outputs enabled the heater loop re-arms it on its next tick; "
                "otherwise switch it ON or re-arm it by hand"))
    return out


# ---------------------------------------------------------------------------
# ONE-SHOT CHECK (conductor.py health)
# ---------------------------------------------------------------------------
def check_plugs(plugs, now: dt.datetime | None = None) -> list:
    """One status() per plug. Read-only. Returns a row per plug."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = []
    for plug in plugs:
        try:
            r = plug.status()
        except Exception as err:  # noqa: BLE001 - report it, keep going
            from tank_plugs import PlugReading
            r = PlugReading(plug.name, False, detail=type(err).__name__[:120],
                            error="exception")
        rows.append({
            "name": plug.name, "driver": plug.driver, "role": plug.role,
            "array": getattr(plug, "array", "plugs"), "configured": r.configured,
            "channel": plug.channel, "ip": getattr(plug, "ip", None),
            "version": getattr(plug, "version", None),
            "key_present": (None if not hasattr(plug, "_key")
                            else bool(plug._key)),       # never the key itself
            "can_revert": plug.supports_revert,
            "ok": r.ok, "on": r.on, "power_w": r.power_w,
            "countdown_s": r.revert_remaining_s, "latency_ms": r.latency_ms,
            "error": r.error, "detail": r.detail, "dps": r.dps,
            "hint": None if r.ok else HINTS.get(r.error or "error"),
            "warnings": [f["message"] for f in
                         countdown_findings([plug], {plug.name: r})],
        })
    return rows


def format_report(rows: list) -> str:
    lines = []
    for r in rows:
        state = ("ON" if r["on"] else "OFF") if r["ok"] else (
            "NOT SET" if not r["configured"] else "NO ANSWER")
        head = (f"{r['name']:<12} {state:<9} role={r['role']}"
                f"{'/' + r['channel'] if r['channel'] else ''}"
                f"  {r['driver']}"
                f"{'' if r['array'] == 'plugs' else '[' + r['array'] + ']'}"
                f" {r['ip'] or '-'}")
        if r["ok"]:
            bits = [f"{r['latency_ms']} ms" if r["latency_ms"] is not None else None,
                    f"{r['power_w']} W" if r["power_w"] is not None else None,
                    f"countdown {r['countdown_s']:g}s"
                    if r["countdown_s"] is not None else
                    ("no countdown dp" if not r["can_revert"] else None)]
            head += "  " + ", ".join(b for b in bits if b)
        lines.append(head)
        if r["dps"] is not None:
            lines.append(f"    data points: {r['dps']}")
        if not r["ok"] and not r["configured"]:
            lines.append("    placeholder: driver not selected yet (tbd)")
        elif not r["ok"]:
            lines.append(f"    {r['error'] or 'error'}: {r['detail']}")
            if r["hint"]:
                lines.append(f"    hint: {r['hint']}")
        for w in r.get("warnings", []):
            lines.append(f"    WARNING: {w}")
        if r["key_present"] is False:
            lines.append("    local key: MISSING from devices.json")
    return "\n".join(lines)
