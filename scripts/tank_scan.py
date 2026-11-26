#!/usr/bin/env python3
"""tank_scan.py -- find the plugs on the LAN and check their IPs for drift.

The conductor reaches each Tuya plug at the `ip` written in plug_array.json and
has no discovery of its own, so a plug that comes back from a power cut on a new
DHCP address simply drops off (a DHCP reservation prevents that; this catches it
when there is none or it was missed). Every Tuya plug broadcasts its device id
and current IP a few times a minute, so a short listen is enough to compare:

    ok         the plug is at the IP in the array
    drifted    the plug answers from another IP (found = the new one)
    not_seen   no broadcast from that id in the listening window
    skipped    not a Tuya plug with an id and an IP (fake, tbd)

Read-only: the scan listens for UDP broadcasts (poll=False), needs no local key
and sends nothing to a plug. What happens next is the caller's choice:

    heal()       point the running plug objects at the new IP, for this run only
    write_ips()  rewrite the ip lines in plug_array.json (the `scan --write` command)

Nothing here decides anything about lights or heat. Broadcasts do not cross a
router or a Docker bridge network, so a scan from there sees nothing; that is
reported once as "scan_blind", not as every plug missing.

Standard library plus tinytuya (imported only when a real scan runs).
"""
from __future__ import annotations

import json
import os


def default_scanner(wanted_ids, timeout_s):
    """{device id: ip} from tinytuya's passive listener. Stops early once every
    wanted id has been heard, so a healthy array takes about 5 s."""
    import tinytuya.scanner as scanner
    found = scanner.devices(verbose=False, scantime=timeout_s, color=False,
                            poll=False, byID=True, assume_yes=True,
                            wantids=list(wanted_ids))
    return {i: d.get("ip") for i, d in (found or {}).items() if d.get("ip")}


def tuya_plugs(plugs) -> list:
    """The plugs a scan can say something about: an id and an IP on the object."""
    return [p for p in plugs if getattr(p, "dev_id", None) and getattr(p, "ip", None)]


def compare(plugs, found: dict) -> list[dict]:
    """One row per plug: name, id, array_ip, found_ip, status."""
    rows = []
    for p in plugs:
        dev_id, ip = getattr(p, "dev_id", None), getattr(p, "ip", None)
        if not (dev_id and ip):
            rows.append({"plug": p.name, "id": dev_id, "array_ip": ip,
                         "found_ip": None, "status": "skipped"})
            continue
        seen = found.get(dev_id)
        status = "not_seen" if seen is None else ("ok" if seen == ip else "drifted")
        rows.append({"plug": p.name, "id": dev_id, "array_ip": ip,
                     "found_ip": seen, "status": status})
    return rows


def scan_plugs(plugs, timeout_s: float = 15.0, scanner=default_scanner) -> list[dict]:
    """Listen once and compare. A scan that raises is reported, never raised."""
    targets = tuya_plugs(plugs)
    if not targets:
        return compare(plugs, {})
    try:
        found = scanner([p.dev_id for p in targets], timeout_s)
    except Exception as err:  # noqa: BLE001 - a bad scan must not stop the conductor
        rows = compare(plugs, {})
        for r in rows:
            if r["status"] == "not_seen":
                r["status"] = "scan_failed"
                r["detail"] = f"{type(err).__name__}: {err}"[:120]
        return rows
    return compare(plugs, found)


def heal(plugs, rows: list[dict]) -> list[str]:
    """Point drifted plugs at the IP they answered from, in memory only.
    Returns the names changed. The array file is not touched."""
    by_name = {p.name: p for p in plugs}
    changed = []
    for r in rows:
        if r["status"] == "drifted" and r["plug"] in by_name:
            by_name[r["plug"]].ip = r["found_ip"]
            changed.append(r["plug"])
    return changed


def findings(rows: list[dict], healed=(), finding=None) -> list[dict]:
    """Conductor findings for a scan. `finding(key, level, message, alert)` builds
    them (tank_tick._finding); drift alerts, a missing plug only warns because the
    offline alert already covers it."""
    finding = finding or (lambda k, lvl, msg, alert=False:
                          {"key": k, "level": lvl, "message": msg, "alert": alert})
    scanned = [r for r in rows if r["status"] != "skipped"]
    if scanned and all(r["status"] in ("not_seen", "scan_failed") for r in scanned):
        why = next((r["detail"] for r in scanned if r.get("detail")), None)
        return [finding("scan_blind", "warn",
                        "plug IP scan heard no plug at all"
                        + (f" ({why})" if why else "")
                        + "; broadcasts may not reach this host (Docker bridge "
                          "network, another subnet, a firewall on UDP 6666/6667), "
                          "so IP drift is not being checked")]
    out = []
    for r in scanned:
        if r["status"] == "drifted":
            action = ("using the new address until restart; " if r["plug"] in healed
                      else "")
            out.append(finding(
                f"plug_ip_drift:{r['plug']}", "warn",
                f"{r['plug']} is at {r['found_ip']}, not {r['array_ip']} in "
                f"plug_array.json: {action}give it a DHCP reservation and run "
                f"`conductor.py scan --write`", True))
        elif r["status"] == "not_seen":
            out.append(finding(
                f"plug_not_seen:{r['plug']}", "warn",
                f"{r['plug']} was not heard on the LAN during the scan "
                f"(expected at {r['array_ip']})"))
    return out


def write_ips(array_path: str, rows: list[dict]) -> list[str]:
    """Rewrite `ip` for drifted plugs in plug_array.json, keeping the file's own
    line endings. Returns the names changed. Plugs not in this file are skipped."""
    with open(array_path, "rb") as fh:
        raw = fh.read()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    spec = json.loads(raw.decode("utf-8"))
    moved = {r["plug"]: r["found_ip"] for r in rows if r["status"] == "drifted"}
    changed = []
    for entry in spec.get("plugs", []):
        if entry.get("name") in moved:
            entry["ip"] = moved[entry["name"]]
            changed.append(entry["name"])
    if changed:
        text = json.dumps(spec, indent=2).replace("\n", nl) + nl
        tmp = array_path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(text.encode("utf-8"))
        os.replace(tmp, array_path)
    return changed


def format_report(rows: list[dict]) -> str:
    lines = [f"{'plug':10s} {'status':12s} {'array ip':16s} found ip"]
    for r in rows:
        lines.append(f"{r['plug']:10s} {r['status']:12s} {str(r['array_ip'] or '-'):16s} "
                     f"{r['found_ip'] or '-'}")
    return "\n".join(lines)
