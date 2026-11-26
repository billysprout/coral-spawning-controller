#!/usr/bin/env python3
"""tank_wedge.py -- tell a WEDGED Tuya plug from a plug that is simply gone.

A KMC plug (plug-8) has twice stopped taking local control while still on the
network: its control port (TCP 6668) times out and the status query fails with
Err 905, but it still answers some pings and is still on its Wi-Fi (SmartLife, by
the cloud, kept working). Only unplugging it for a few seconds cleared it. That
needs a different alert from "plug offline", which could equally be a power cut,
a Wi-Fi drop or the router.

The trigger, all four at once:

  1. response shape   the plug's status query came back ok=False with an error
                      class "unreachable" (Err 901 or 905) or "timeout" (Err 902),
                      for at least min_down_min minutes. A bad key (914), a
                      missing data point or a garbled reply is not this.
  2. not the LAN      at least one other plug answered in the same tick, so this
                      PC, the router and the Wi-Fi are working.
  3. no control port  a TCP connect to the plug's control port (6668) times out
                      or is refused (it is not accepting a connection).
  4. alive anyway     at least one of the ping_count ICMP echoes was answered.

No echo at all is "silent": unplugged, off Wi-Fi or the router lost it, which is
the existing plug_offline alert, with the probe result appended. Every answer
missing and no peer reachable is "lan": the network, not the plug.

The probes are read-only (a ping and a TCP connect that sends nothing) and run
at most every probe_every_s per plug, once the plug has been down long enough.
The probe functions are injectable for tests. Standard library only.
"""
from __future__ import annotations

import copy
import datetime as dt
import os
import re
import socket
import subprocess

DEFAULT_WEDGE = {
    "enabled": True,
    "min_down_min": 5,          # down this long before it is judged (matches plug_offline_min)
    "probe_every_s": 300,       # at most one probe per plug per this
    "ping_count": 4,
    "tcp_port": 6668,           # the Tuya local control port
    "tcp_timeout_s": 3.0,
}

WEDGE_ERRORS = ("unreachable", "timeout")


def wedge_config(overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_WEDGE)
    cfg.update(overrides or {})
    if not 1 <= float(cfg["min_down_min"]) <= 240:
        raise ValueError("plug_wedge.min_down_min must be 1..240")
    if not 30 <= float(cfg["probe_every_s"]) <= 3600:
        raise ValueError("plug_wedge.probe_every_s must be 30..3600")
    if not 1 <= int(cfg["ping_count"]) <= 10:
        raise ValueError("plug_wedge.ping_count must be 1..10")
    if not 1 <= int(cfg["tcp_port"]) <= 65535:
        raise ValueError("plug_wedge.tcp_port must be a port")
    if not 0.5 <= float(cfg["tcp_timeout_s"]) <= 15:
        raise ValueError("plug_wedge.tcp_timeout_s must be 0.5..15")
    return cfg


# ---------------------------------------------------------------------------
# PROBES (replaced in tests)
# ---------------------------------------------------------------------------
def icmp_probe(ip: str, count: int = 4) -> int | None:
    """How many of `count` echoes were answered, or None when there is no ping
    program to ask."""
    if os.name == "nt":
        cmd = ["ping", "-n", str(count), "-w", "1000", ip]
    else:
        cmd = ["ping", "-c", str(count), "-W", "1", ip]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=count * 2 + 6)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = p.stdout.decode("utf-8", "replace")
    m = re.search(r"(\d+)\s+(?:packets\s+)?received", out)      # Linux
    if m:
        return int(m.group(1))
    m = re.search(r"Received\s*=\s*(\d+)", out)                 # Windows
    if m:
        # A "Destination host unreachable" reply from this very PC counts as a
        # received packet on Windows; count only real replies from the target.
        return len(re.findall(rf"Reply from {re.escape(ip)}:.*TTL=", out))
    return None


def tcp_probe(ip: str, port: int, timeout_s: float) -> str:
    """"open", "refused", "timeout" or "error". Sends nothing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect((ip, port))
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except (socket.timeout, TimeoutError):
        return "timeout"
    except OSError:
        return "error"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# JUDGEMENT (pure; unit tested)
# ---------------------------------------------------------------------------
def classify(error: str | None, peers_ok: int, tcp: str | None,
             echoes: int | None, count: int) -> dict:
    """{"kind": wedged|silent|lan|other|unknown, "why": text}."""
    if error not in WEDGE_ERRORS:
        return {"kind": "other", "why": f"error class {error!r} is not a network failure"}
    if peers_ok < 1:
        return {"kind": "lan",
                "why": "no other plug answered either: the network, this PC or the "
                       "router, not this plug"}
    if tcp == "open":
        return {"kind": "other", "why": "its control port accepts connections"}
    if echoes is None:
        return {"kind": "unknown", "why": "no ping program here to look for it"}
    if echoes <= 0:
        return {"kind": "silent",
                "why": f"no ping reply (0/{count}) and no control port: unplugged, "
                       "off Wi-Fi or lost by the router"}
    return {"kind": "wedged",
            "why": f"answers {echoes}/{count} pings but control port {tcp}: its "
                   "network stack is wedged"}


class WedgeWatch:
    """Per-plug probing and the cached verdict. One instance per conductor."""

    def __init__(self, cfg: dict | None, icmp=icmp_probe, tcp=tcp_probe):
        self.cfg = wedge_config(cfg)
        self._icmp, self._tcp = icmp, tcp
        self._cache: dict[str, tuple[dt.datetime, dict]] = {}

    def judge(self, plug, reading, peers_ok: int, down_since: dt.datetime | None,
              now: dt.datetime) -> dict | None:
        """The verdict for a plug that has been failing, or None when this
        should not (yet) be judged. Probes at most every probe_every_s."""
        c = self.cfg
        if not c["enabled"] or reading.ok or down_since is None:
            self._cache.pop(plug.name, None)
            return None
        if (now - down_since).total_seconds() < float(c["min_down_min"]) * 60:
            return None
        ip = getattr(plug, "ip", None)
        if not ip:
            return None
        hit = self._cache.get(plug.name)
        if hit and (now - hit[0]).total_seconds() < float(c["probe_every_s"]):
            return dict(hit[1])
        error = getattr(reading, "error", None)
        if error not in WEDGE_ERRORS:                    # nothing to probe for
            verdict = classify(error, peers_ok, None, None, int(c["ping_count"]))
            self._cache[plug.name] = (now, verdict)
            return verdict
        tcp = self._tcp(ip, int(c["tcp_port"]), float(c["tcp_timeout_s"]))
        echoes = self._icmp(ip, int(c["ping_count"]))
        verdict = classify(error, peers_ok, tcp, echoes, int(c["ping_count"]))
        verdict.update(tcp=tcp, echoes=echoes, ip=ip, probed=now.isoformat())
        self._cache[plug.name] = (now, verdict)
        return verdict


def wedge_message(plug, verdict: dict, down_min: float) -> str:
    what = f"{plug.name} ({plug.role})" if getattr(plug, "role", None) else plug.name
    return (f"{what} is WEDGED, not gone: down {down_min:.0f} min, {verdict['why']} "
            f"(TCP {verdict.get('tcp')} on {verdict.get('ip')}). Unplug it for about "
            "10 seconds and plug it back in; SmartLife may still work because it "
            "uses the cloud.")
