#!/usr/bin/env python3
"""pi_log_archive.py -- move the Pi's old sensor logs to this computer.

The Pi writes 10-second lux and temperature readings to lux.jsonl and temp.jsonl
and closes them into time-stamped segments (log_segments.py). A segment that has
been closed for more than KEEP_DAYS (14) is moved here:

  * if this computer already holds the same bytes (same sha256), the Pi's copy is
    just deleted ("already at rest");
  * otherwise it is streamed over ssh, checked against the Pi's own checksum and
    size, stored gzipped under <dest>/<name>.gz with <name>.sha256, and only then
    deleted from the Pi.

A segment is never deleted from the Pi unless a verified copy is on disk here. The
Pi side only obeys four commands (pi_archive_shell.sh, the forced command of this
key): list, sum, cat and rm NAME SHA256.

    python pi_log_archive.py once      one pass, print the summary
    python pi_log_archive.py loop      a pass every PI_ARCHIVE_INTERVAL_S (6 h)

Environment: PI_ARCHIVE_HOST (pi@192.0.2.100), PI_ARCHIVE_KEY (path of the
private key), PI_ARCHIVE_KNOWN_HOSTS, PI_ARCHIVE_DEST (<data>/pi_logs),
PI_ARCHIVE_KEEP_DAYS (14), PI_ARCHIVE_INTERVAL_S.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

UTC = dt.timezone.utc
NAME_RE = re.compile(r"^(lux|temp)\.jsonl\.([0-9]+|[0-9]{8}T[0-9]{6}Z(-[0-9]+)?)$")
CHUNK = 1 << 20


class ArchiveError(Exception):
    pass


def utc_stamp(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class Pi:
    """The Pi over ssh. Tests replace this class."""

    def __init__(self, host: str, key: str, known_hosts: str, timeout: int = 120):
        self.base = ["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                     "-o", "StrictHostKeyChecking=accept-new",
                     "-o", f"UserKnownHostsFile={known_hosts}",
                     "-o", "ConnectTimeout=15", host]
        self.timeout = timeout

    def _run(self, command: str) -> str:
        try:
            p = subprocess.run(self.base + [command], capture_output=True, text=True,
                               timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ArchiveError(f"ssh {command.split()[0]}: {type(err).__name__}: {err}")
        if p.returncode != 0:
            raise ArchiveError(f"ssh {command.split()[0]} rc={p.returncode}: "
                               f"{(p.stderr or p.stdout).strip()[:160]}")
        return p.stdout

    def list(self) -> list:
        """[(name, size, mtime_epoch)] of the Pi's closed segments."""
        out = []
        for line in self._run("list").splitlines():
            parts = line.split()
            if len(parts) == 3 and NAME_RE.match(parts[0]):
                out.append((parts[0], int(parts[1]), int(parts[2])))
        return out

    def sum(self, name: str) -> str:
        return self._run(f"sum {name}").strip()

    def fetch(self, name: str, sink) -> None:
        """Stream the segment's bytes into sink(chunk)."""
        try:
            p = subprocess.Popen(self.base + [f"cat {name}"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        except OSError as err:
            raise ArchiveError(f"ssh cat: {err}")
        try:
            while True:
                chunk = p.stdout.read(CHUNK)
                if not chunk:
                    break
                sink(chunk)
            err = p.stderr.read().decode("utf-8", "replace")
            if p.wait(timeout=self.timeout) != 0:
                raise ArchiveError(f"ssh cat rc={p.returncode}: {err.strip()[:160]}")
        finally:
            if p.poll() is None:
                p.kill()

    def remove(self, name: str, sha: str) -> None:
        self._run(f"rm {name} {sha}")


def private_key_copy(key: str) -> str:
    """Path of a copy of the private key that only this user can read. ssh refuses a
    key that others can read, and a Windows bind mount shows every file as 0644 or
    0755. The copy lives in a fresh temp folder for the life of the process."""
    with open(key, "rb") as fh:
        data = fh.read()
    folder = tempfile.mkdtemp(prefix="pi-archive-")
    os.chmod(folder, 0o700)
    path = os.path.join(folder, "id")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    return path


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def archive_one(pi, dest: str, name: str, size: int) -> dict:
    """Move one segment. Returns the event record; raises ArchiveError on failure
    (nothing is deleted from the Pi in that case)."""
    remote = pi.sum(name)
    if not re.fullmatch(r"[0-9a-f]{64}", remote):
        raise ArchiveError(f"{name}: the Pi gave no checksum ({remote[:40]!r})")
    gz, shaf = os.path.join(dest, name + ".gz"), os.path.join(dest, name + ".sha256")
    if os.path.exists(gz) and _read_text(shaf) == remote:
        pi.remove(name, remote)
        return {"name": name, "action": "already_at_rest", "bytes": size, "sha256": remote}
    part = gz + ".part"
    h, n = hashlib.sha256(), 0
    try:
        with gzip.open(part, "wb", compresslevel=6) as out:
            def sink(chunk):
                nonlocal n
                h.update(chunk)
                n += len(chunk)
                out.write(chunk)
            pi.fetch(name, sink)
        if h.hexdigest() != remote or n != size:
            raise ArchiveError(f"{name}: copy does not match the Pi's ({n} of {size} bytes, "
                               f"sha {h.hexdigest()[:12]} vs {remote[:12]})")
        with open(shaf, "w", encoding="ascii") as fh:
            fh.write(remote + "\n")
        os.replace(part, gz)
    except (ArchiveError, OSError):
        for f in (part,):
            try:
                os.remove(f)
            except OSError:
                pass
        raise
    pi.remove(name, remote)
    return {"name": name, "action": "archived", "bytes": n, "sha256": remote,
            "stored": os.path.getsize(gz)}


def run_once(pi, dest: str, keep_days: float = 14.0, now: dt.datetime | None = None) -> dict:
    """One pass. Writes <dest>/archive.jsonl (one line per segment handled or
    failed) and <dest>/status.json, and returns the status. Never raises."""
    now = now or dt.datetime.now(UTC)
    status = {"ts": utc_stamp(now), "ok": False, "keep_days": keep_days, "listed": 0,
              "eligible": 0, "archived": 0, "already_at_rest": 0, "errors": [],
              "oldest_age_days": None}
    events = []
    try:
        os.makedirs(dest, exist_ok=True)
        segs = pi.list()
        status["listed"] = len(segs)
        ages = [(now.timestamp() - m) / 86400.0 for _n, _s, m in segs]
        status["oldest_age_days"] = round(max(ages), 2) if ages else None
        for (name, size, mtime), age in sorted(zip(segs, ages), key=lambda x: x[0][2]):
            if age < keep_days:
                continue
            status["eligible"] += 1
            try:
                ev = archive_one(pi, dest, name, size)
                events.append(dict(ev, ts=utc_stamp(now)))
                status["archived" if ev["action"] == "archived" else "already_at_rest"] += 1
            except (ArchiveError, OSError) as err:
                status["errors"].append(f"{name}: {err}")
                events.append({"ts": utc_stamp(now), "name": name, "action": "error",
                               "error": str(err)[:200]})
        status["ok"] = not status["errors"]
    except (ArchiveError, OSError) as err:
        status["errors"].append(str(err))
        events.append({"ts": utc_stamp(now), "action": "error", "error": str(err)[:200]})
    try:
        if events:
            with open(os.path.join(dest, "archive.jsonl"), "a", encoding="ascii") as fh:
                for ev in events:
                    fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
        tmp = os.path.join(dest, "status.json.tmp")
        with open(tmp, "w", encoding="ascii") as fh:
            json.dump(status, fh)
        os.replace(tmp, os.path.join(dest, "status.json"))
    except OSError:
        pass
    return status


def _data_dir() -> str:
    return os.environ.get("TANK_DATA", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "once"
    env = os.environ
    key = private_key_copy(env.get("PI_ARCHIVE_KEY", "/state/id_ed25519"))
    pi = Pi(env.get("PI_ARCHIVE_HOST", "pi@192.0.2.100"), key,
            env.get("PI_ARCHIVE_KNOWN_HOSTS", "/state/known_hosts"))
    dest = env.get("PI_ARCHIVE_DEST", os.path.join(_data_dir(), "pi_logs"))
    keep = float(env.get("PI_ARCHIVE_KEEP_DAYS", "14"))
    interval = max(300, int(env.get("PI_ARCHIVE_INTERVAL_S", "21600")))
    if cmd == "once":
        status = run_once(pi, dest, keep)
        print(json.dumps(status))
        return 0 if status["ok"] else 1
    if cmd == "loop":
        while True:
            started = time.monotonic()
            status = run_once(pi, dest, keep)
            print(json.dumps(status), flush=True)
            time.sleep(max(60.0, interval - (time.monotonic() - started)))
    print(f"unknown command {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
