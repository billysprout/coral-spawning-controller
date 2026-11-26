"""common.py -- small shared helpers for the long-running services.

Used by the tank controller, the feed and the setup page. Standard library only.

  * load_env        read the gitignored .env into os.environ (never overrides)
  * log             one JSON object per line on stdout, flushed
  * write_json      atomic JSON file write (temp file + replace)
  * StopFlag        SIGINT/SIGTERM -> a flag the main loop polls
  * send_alert      Pushover, ntfy and/or Telegram; reports whether anything was sent
"""
from __future__ import annotations

import datetime
import json
import os
import re
import signal
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root; this file lives in scripts/
ENV_FILE = os.path.join(ROOT, ".env")


def load_env(path: str = ENV_FILE) -> None:
    """Fill os.environ from a .env file. Existing variables win."""
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return
    with fh:
        for line in fh:
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if not m:
                continue
            name, value = m.group(1), m.group(2).strip().strip('"').strip("'")
            if value and name not in os.environ:
                os.environ[name] = value


def data_dir() -> str:
    return os.environ.get("TANK_DATA", os.path.join(ROOT, "data"))


def now_iso() -> str:
    """Local time WITH its UTC offset, so it is unambiguous across DST."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def local_stamp(now: datetime.datetime | None = None) -> tuple[str, int]:
    """(naive local ISO string, UTC offset in minutes) for one instant.

    The naive local string keeps the CSV readable and compatible with older
    readers. The offset disambiguates the hour that repeats when DST ends: the
    same local time appears twice, with offsets that differ by 60.
    """
    aware = (now or datetime.datetime.now()).astimezone()
    offset = int(aware.utcoffset().total_seconds() // 60)
    return aware.replace(tzinfo=None).isoformat(timespec="seconds"), offset


def log(event: str, **fields) -> None:
    """Structured log line. Never raises: logging must not kill a service."""
    rec = {"ts": now_iso(), "event": event}
    rec.update(fields)
    try:
        print(json.dumps(rec, default=str), flush=True)
    except (OSError, ValueError):
        pass


def write_json(path: str, obj) -> None:
    """Atomically replace path with obj as JSON."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


class StopFlag:
    """Set by SIGINT/SIGTERM. sleep() returns early once the flag is set."""

    def __init__(self) -> None:
        self.stopped = False
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, self._handle)
                except (ValueError, OSError):  # not the main thread
                    pass

    def _handle(self, signum, _frame) -> None:
        self.stopped = True

    def sleep(self, seconds: float) -> bool:
        """Sleep up to `seconds`; return True if a stop was requested."""
        end = time.monotonic() + max(0.0, seconds)
        while not self.stopped:
            left = end - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(1.0, left))
        return self.stopped


def alert_channels() -> list[str]:
    """Names of the configured alert channels, read from the environment."""
    chans = []
    if os.environ.get("PUSHOVER_TOKEN") and os.environ.get("PUSHOVER_USER"):
        chans.append("pushover")
    if os.environ.get("NTFY_TOPIC"):
        chans.append("ntfy")
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        chans.append("telegram")
    return chans


def send_alert(message: str, title: str = "Anemone tank") -> bool:
    """Deliver an alert on every configured channel.

    Returns True if at least one channel accepted it. With no channel
    configured the message is only logged and False is returned, so callers
    can tell an alert that reached nobody from one that was delivered.
    """
    log("alert", message=message, title=title)
    delivered = False
    token, user = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
    if token and user:
        try:
            data = urllib.parse.urlencode({
                "token": token, "user": user, "title": title, "message": message,
            }).encode()
            with urllib.request.urlopen(
                    "https://api.pushover.net/1/messages.json", data=data,
                    timeout=10):
                delivered = True
        except Exception as err:  # noqa: BLE001 - delivery must not raise
            log("alert_failed", channel="pushover", error=repr(err))
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                headers={"Title": title}, method="POST")
            with urllib.request.urlopen(req, timeout=10):
                delivered = True
        except Exception as err:  # noqa: BLE001
            log("alert_failed", channel="ntfy", error=repr(err))
    tg_token, tg_chat = (os.environ.get("TELEGRAM_BOT_TOKEN"),
                         os.environ.get("TELEGRAM_CHAT_ID"))
    if tg_token and tg_chat:
        try:
            data = urllib.parse.urlencode({
                "chat_id": tg_chat, "text": f"{title}\n{message}",
            }).encode()
            with urllib.request.urlopen(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    data=data, timeout=10):
                delivered = True
        except Exception as err:  # noqa: BLE001
            # Never log the URL: it contains the bot token.
            log("alert_failed", channel="telegram", error=type(err).__name__)
    return delivered


def ping_deadman(url: str | None = None) -> bool | None:
    """Tell an off-box dead-man's switch (e.g. a healthchecks.io URL) we are alive.

    Returns None when no URL is configured, else whether the ping landed. The
    service alerts you when pings *stop*, which covers everything this host
    cannot report about itself: power loss, sleep, Docker not running, a hung
    controller. Never raises.
    """
    url = url or os.environ.get("TANK_DEADMAN_URL")
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10):
            return True
    except Exception as err:  # noqa: BLE001
        log("deadman_ping_failed", error=repr(err))
        return False


def die(message: str, code: int = 2):
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(code)
