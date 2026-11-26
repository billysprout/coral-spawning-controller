"""tank_feed.py -- daily "live feed" message for the tank Telegram bot.

Sends one message a day at noon (America/Los_Angeles) to TELEGRAM_FEED_CHAT_ID:

    recorded tank temperature and lux   (latest row of data/tank_inputs-*.jsonl)
    target temperature and lux          (data/tank_status.json plan + the
                                         ITC-306A band last applied from
                                         data/itc/applied.json)

The message carries one [Refresh] button. Pressing it edits the same message
with the current values. The feed runs on its OWN bot token
(TELEGRAM_FEED_BOT_TOKEN): getUpdates is a single-consumer API, and the chat
bridge already long-polls the alert token, so sharing it would split updates
between the two pollers.

  python tank_feed.py loop          daemon: noon send + refresh button handling
  python tank_feed.py send          send one feed message now (testing)
  python tank_feed.py show          print the feed text, no Telegram

Read-only: this module never writes to the controller or the ITC-306A.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
SEND_HOUR = 12                        # noon local; the send window is the whole hour
REFRESH_DATA = "feed_refresh"
STATE_NAME = "tank_feed_state.json"
HEARTBEAT_NAME = "tank_feed_heartbeat.json"
POLL_TIMEOUT_S = 20                   # getUpdates long-poll
TICK_S = 5.0                          # loop cadence when nothing is pending


# ---------------------------------------------------------------- readings

def _parse_ts(text) -> dt.datetime | None:
    try:
        ts = dt.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts


def latest_recorded(data: str) -> dict:
    """Last recorded temperature and lux from the archived tank inputs.

    Walks the daily tank_inputs files newest first and returns the last row
    that has a timestamp. temp_c / lux stay None when that row had no reading.
    """
    folder = data
    try:
        names = sorted((n for n in os.listdir(folder)
                        if n.startswith("tank_inputs-") and n.endswith(".jsonl")),
                       reverse=True)
    except OSError:
        names = []
    for name in names:
        rows = []
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
        for row in reversed(rows):
            ts = _parse_ts(row.get("ts"))
            if ts is None:
                continue
            return {"ts": ts, "temp_c": _num(row.get("temp_c")),
                    "lux": _num(row.get("lux"))}
    return {"ts": None, "temp_c": None, "lux": None}


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def targets(data: str) -> dict:
    """What the controller is aiming for, from the plan and the last applied band."""
    status = common.read_json(os.path.join(data, "tank_status.json"), {}) or {}
    plan = status.get("plan") or {}
    applied = common.read_json(os.path.join(data, "itc", "applied.json"), {}) or {}
    cue = applied.get("cue") or {}
    return {
        "plan_ts": _parse_ts(status.get("ts")),
        "temp_c": _num(plan.get("temp_hourly_c")),
        "temp_daily_c": _num(plan.get("temp_daily_c")),
        "moon_on": plan.get("moon_target_lux") is not None,
        "moon_target_lux": _num(plan.get("moon_target_lux")),
        "white_level": _num(plan.get("white_level")),
        "blue_level": _num(plan.get("blue_level")),
        "white_plus_level": _num(plan.get("white_plus_level")),
        "band_status": applied.get("status"),
        "band_ts": _parse_ts(applied.get("changed_ts") or applied.get("ts")),
        "floor_f": _num(applied.get("floor_f")),
        "ceil_f": _num(applied.get("ceil_f")),
        "band_target_c": _num(cue.get("temp_hourly_c")),
    }


OKINAWA_LON = 127.7
TANK_LON = -118.0
JST = dt.timezone(dt.timedelta(hours=9))
LUX_SERIES = "okinawa_light/okinawa_light_clearsky_spawn_year_10min.csv"


def _cue_date(data: str, tank_date: dt.date) -> dt.date | None:
    """The Okinawa date the cue table uses for this tank date (the calendar's cue_date)."""
    try:
        names = sorted((n for n in os.listdir(data)
                        if n.startswith("spawn_calendar_") and n.endswith(".csv")),
                       reverse=True)
    except OSError:
        return None
    for name in names:
        try:
            with open(os.path.join(data, name), encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    if row.get("date") == tank_date.isoformat():
                        return dt.date.fromisoformat(row["cue_date"])
        except (OSError, ValueError, KeyError):
            continue
    return None


def expected_lux(data: str, now: dt.datetime) -> dict | None:
    """Clear-sky lux the cue table asks for right now, from the Okinawa light series.

    The cue table places Okinawa's sky on the cue date at the same offset from
    solar noon that the tank has today. Okinawa's solar noon sits
    (TANK_LON - OKINAWA_LON) / 15 hours earlier in UTC than the tank's. The
    equation of time (up to about 15 minutes) is ignored. The 10-minute series
    is read at the JST slot containing that instant.
    """
    tank_date = now.date()
    cue = _cue_date(data, tank_date)
    if cue is None:
        return None
    ok_utc = (now.astimezone(dt.timezone.utc) + (cue - tank_date)
              + dt.timedelta(hours=(TANK_LON - OKINAWA_LON) / 15))
    jst = ok_utc.astimezone(JST)
    key = jst.replace(minute=jst.minute // 10 * 10, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    try:
        with open(os.path.join(data, LUX_SERIES), encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("local_time_jst") == key:
                    return {"total": _num(row.get("total_lux_clear")),
                            "sun": _num(row.get("sun_lux_clear")),
                            "moon": _num(row.get("moon_lux_clear")),
                            "slot_jst": key, "cue_date": cue}
    except (OSError, KeyError):
        return None
    return None


def snapshot(data: str) -> dict:
    now = dt.datetime.now(LOCAL_TZ)
    return {"now": now, "recorded": latest_recorded(data), "target": targets(data),
            "expected": expected_lux(data, now)}


# ---------------------------------------------------------------- text

def _clock(ts: dt.datetime | None) -> str:
    """Local clock time, e.g. '16:44 PST'. A dash when there is no time."""
    if ts is None:
        return "—"
    return ts.astimezone(LOCAL_TZ).strftime("%H:%M %Z")


def _f(c: float) -> float:
    return c * 9 / 5 + 32


def _temp(c: float | None) -> str:
    """'28.1 °C · 82.6 °F', or 'no reading'."""
    if c is None:
        return "no reading"
    return f"{c:.1f} °C · {_f(c):.1f} °F"


def format_feed(snap: dict) -> str:
    """Telegram HTML. Every value is a number or a fixed phrase, so nothing needs escaping."""
    rec, tgt = snap["recorded"], snap["target"]
    lux_now = "no reading" if rec["lux"] is None else f"{rec['lux']:,.0f} lx"
    lines = [f"<b>Tank feed</b> · {snap['now'].strftime('%a %b %d, %H:%M %Z')}", "",
             "<b>Now</b>",
             f"{_temp(rec['temp_c'])} · {lux_now}",
             f"read {_clock(rec['ts'])}", "",
             "<b>Target</b>"]
    lines.append("no plan target" if tgt["temp_c"] is None
                 else f"{_temp(tgt['temp_c'])} · plan")
    if tgt["floor_f"] is not None and tgt["ceil_f"] is not None:
        band = f"Band {tgt['floor_f']:.1f}–{tgt['ceil_f']:.1f} °F"
        if tgt["band_ts"] is not None:
            band += f" · set {_clock(tgt['band_ts'])}"
        lines.append(band)
    exp = snap.get("expected")
    if exp and exp.get("total") is not None:
        lines.append(f"Lux {exp['total']:,.0f} lx · expected, clear sky")
    else:
        lines.append("Lux no expected value")
    if tgt["moon_on"]:
        lines.append(f"Moon target {tgt['moon_target_lux']:.2f} lx")
    levels = []
    for label, key in (("white", "white_level"), ("blue", "blue_level"),
                       ("white+", "white_plus_level")):
        if tgt[key] is not None:
            levels.append(f"{label} {tgt[key] * 100:.0f}%")
    if levels:
        lines += ["", "<b>Lamps</b> " + " · ".join(levels)]
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram

class TelegramError(RuntimeError):
    pass


def tg_call(token: str, method: str, **params):
    """POST one Bot API method. Never put the token in an error message."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
         for k, v in params.items() if v is not None}).encode()
    timeout = POLL_TIMEOUT_S + 15 if method == "getUpdates" else 15
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        # Telegram's description says why (chat not found, bad markup). It never
        # echoes the token, so it is safe to show.
        try:
            detail = json.loads(err.read().decode("utf-8")).get("description", "")
        except (OSError, ValueError, AttributeError):
            detail = ""
        raise TelegramError(f"{method}: HTTP {err.code} {detail}".rstrip()) from None
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise TelegramError(f"{method}: {type(err).__name__}") from None
    if not body.get("ok"):
        raise TelegramError(f"{method}: {body.get('description', 'not ok')}")
    return body.get("result")


def _keyboard() -> dict:
    return {"inline_keyboard": [[{"text": "Refresh", "callback_data": REFRESH_DATA}]]}


def send_feed(call, chat: str, snap: dict) -> int:
    result = call("sendMessage", chat_id=chat, text=format_feed(snap),
                  parse_mode="HTML", reply_markup=_keyboard(),
                  disable_web_page_preview=True)
    return int(result["message_id"])


def handle_callback(call, chat: str, callback: dict, snap_fn) -> bool:
    """Answer one Refresh press. Returns True when the message was edited."""
    message = callback.get("message") or {}
    if (callback.get("data") != REFRESH_DATA
            or str((message.get("chat") or {}).get("id")) != str(chat)):
        return False
    text = format_feed(snap_fn())
    edited = True
    try:
        call("editMessageText", chat_id=chat, message_id=message.get("message_id"),
             text=text, parse_mode="HTML", reply_markup=_keyboard(),
             disable_web_page_preview=True)
    except TelegramError as err:
        # Telegram refuses an edit that changes nothing. That is still a refresh.
        if "not modified" not in str(err):
            raise
        edited = False
    call("answerCallbackQuery", callback_query_id=callback.get("id"),
         text="Refreshed" if edited else "No change")
    return edited


# ---------------------------------------------------------------- loop

def _state_path(data): return os.path.join(data, STATE_NAME)


def run_loop(data: str, token: str, chat: str) -> int:
    stop = common.StopFlag()
    state = common.read_json(_state_path(data), {}) or {}
    offset = int(state.get("offset", 0))
    call = lambda method, **params: tg_call(token, method, **params)  # noqa: E731
    common.log("tank_feed_start", chat_set=bool(chat))
    while not stop.stopped:
        now = dt.datetime.now(LOCAL_TZ)
        today = now.date().isoformat()
        try:
            if now.hour == SEND_HOUR and state.get("sent_date") != today:
                mid = send_feed(call, chat, snapshot(data))
                state.update(sent_date=today, message_id=mid)
                common.write_json(_state_path(data), {**state, "offset": offset})
                common.log("tank_feed_sent", date=today, message_id=mid)
            updates = call("getUpdates", offset=offset, timeout=POLL_TIMEOUT_S,
                           allowed_updates=["callback_query"]) or []
            for upd in updates:
                offset = int(upd["update_id"]) + 1
                cb = upd.get("callback_query")
                if cb:
                    edited = handle_callback(call, chat, cb, lambda: snapshot(data))
                    common.log("tank_feed_refresh", edited=edited)
            common.write_json(_state_path(data), {**state, "offset": offset})
            common.write_json(os.path.join(data, HEARTBEAT_NAME),
                              {"ts": common.now_iso(), "offset": offset})
        except TelegramError as err:
            common.log("tank_feed_error", error=str(err))
            if stop.sleep(30):
                break
        except (OSError, ValueError, KeyError) as err:
            common.log("tank_feed_error", error=type(err).__name__)
            if stop.sleep(30):
                break
        if stop.sleep(TICK_S):
            break
    return 0


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    common.load_env()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("loop", "send", "show"))
    args = parser.parse_args(argv)
    data = common.data_dir()
    if args.command == "show":
        print(format_feed(snapshot(data)))
        return 0
    token = os.environ.get("TELEGRAM_FEED_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_FEED_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        common.die("set TELEGRAM_FEED_BOT_TOKEN and TELEGRAM_FEED_CHAT_ID in .env")
    if args.command == "send":
        call = lambda method, **params: tg_call(token, method, **params)  # noqa: E731
        print(send_feed(call, chat, snapshot(data)))
        return 0
    return run_loop(data, token, chat)


if __name__ == "__main__":
    sys.exit(main())
