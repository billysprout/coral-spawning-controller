#!/usr/bin/env python3
"""host_helper.py -- the jobs the setup page cannot do itself, run by start.sh.

    sh scripts/start.sh telegram   bot token and chat into .env, test alert (then start.sh restarts)
    sh scripts/start.sh golive     the practice copy's calendar and settings into data/ (then restart)
    sh scripts/start.sh check      health check: cue data, controller, thermometer, alerts

start.sh runs this in a throwaway container of the setup image with the
controller folder mounted at /repo, so it can write .env and data/ (the setup
page cannot: it only sees staging/data, and never the token). Every job
reports what it did, without secrets, to host_status.json in data/ and in
staging/data/, which the setup page reads to tick its steps.

Standard library only. Nothing here talks to a plug.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ask(prompt: str, default: str = "") -> str:
    try:
        v = input(prompt).strip()
    except EOFError:
        v = ""
    return v or default


def yes(prompt: str) -> bool:
    return ask(prompt + " [y/N] ").lower() in ("y", "yes")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def report(repo: str, key: str, value: dict):
    """host_status.json in data/ and staging/data/ (no secrets, ever)."""
    for d in ("data", os.path.join("staging", "data")):
        folder = os.path.join(repo, d)
        if d != "data" and not os.path.isdir(folder):
            continue
        path = os.path.join(folder, "host_status.json")
        st = read_json(path, {}) or {}
        st[key] = value
        write_json(path, st)


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def env_read(path: str) -> dict:
    out = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def env_set(path: str, values: dict):
    """Set KEY=value lines in place (comments and other keys kept), append the rest."""
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    left = dict(values)
    for i, line in enumerate(lines):
        k = line.partition("=")[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and k in left:
            lines[i] = f"{k}={left.pop(k)}"
    lines += [f"{k}={v}" for k, v in left.items()]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# telegram
# ---------------------------------------------------------------------------
def tg(token: str, method: str, **params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except OSError as e:
        return {"ok": False, "description": f"could not reach Telegram ({type(e).__name__})"}


def cmd_telegram(repo: str) -> int:
    envp = os.path.join(repo, ".env")
    if not os.path.exists(envp) and os.path.exists(os.path.join(repo, ".env.example")):
        shutil.copyfile(os.path.join(repo, ".env.example"), envp)
    print("Phone alerts through Telegram.\n")
    print("You need the bot token BotFather gave you (it looks like 123456:ABC-DEF...).")
    print("Typing is hidden. Paste it and press Enter.\n")
    token = getpass.getpass("Bot token: ").strip()
    if not token:
        print("No token: nothing changed.")
        return 1
    me = tg(token, "getMe")
    if not me.get("ok"):
        print(f"Telegram did not accept that token: {me.get('description', 'unknown error')}. Nothing changed.")
        return 1
    bot = me["result"].get("username", "your bot")
    print(f"\nToken works: @{bot}.")
    chats = {}
    for attempt in range(3):
        if attempt or not chats:
            ask(f"Send @{bot} any message from the phone that should get alerts, then press Enter here. ")
        upd = tg(token, "getUpdates")
        for u in upd.get("result", []):
            c = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
            if c.get("id") is not None:
                chats[c["id"]] = c.get("title") or " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or str(c["id"])
        if chats:
            break
        print("No message has reached the bot yet.")
    if not chats:
        print("Still no message: check you wrote to the right bot, then run this again. Nothing changed.")
        return 1
    ids = list(chats)
    if len(ids) == 1:
        chat = ids[0]
    else:
        for n, cid in enumerate(ids, 1):
            print(f"  {n}. {chats[cid]}")
        pick = ask(f"Which chat gets the alerts? [1-{len(ids)}] ", "1")
        chat = ids[max(0, min(len(ids) - 1, int(pick) - 1 if pick.isdigit() else 0))]
    print(f"Alerts will go to: {chats[chat]}.")
    vals = {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat)}
    print("\nRecommended: a 'this computer is off' ping. Make a free check at https://healthchecks.io")
    print("(period 5 minutes, grace 10 minutes) and paste its ping URL here, or press Enter to skip.")
    dead = ask("Ping URL: ")
    if dead and not dead.startswith("https://"):
        print("That does not look like an https URL: skipped.")
        dead = ""
    if dead:
        vals["TANK_DEADMAN_URL"] = dead
    env_set(envp, vals)
    print("\nWritten to .env.")
    sent = tg(token, "sendMessage", chat_id=chat,
              text="Test alert from your spawning controller. If you can read this, alerts work.")
    ok = bool(sent.get("ok"))
    print("Test alert sent: check your phone." if ok else f"Test alert failed: {sent.get('description')}")
    if dead:
        try:
            urllib.request.urlopen(dead, timeout=10).read()
            print("First ping sent to the dead-man check.")
        except OSError:
            print("Could not reach the ping URL now; the controller will keep trying.")
    report(repo, "telegram", {"configured": now(), "bot": f"@{bot}", "test_delivered": ok})
    if dead:
        report(repo, "deadman", {"configured": now()})
    return 0 if ok else 2


# ---------------------------------------------------------------------------
# go live
# ---------------------------------------------------------------------------
def _same(a: str, b: str) -> bool:
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read().replace(b"\r\n", b"\n") == fb.read().replace(b"\r\n", b"\n")
    except OSError:
        return False


def cmd_golive(repo: str) -> int:
    src, dst = os.path.join(repo, "staging", "data"), os.path.join(repo, "data")
    st = read_json(os.path.join(src, "setup.json"), {}) or {}
    cal = (st.get("calendar") or {}).get("file")
    if not cal or not os.path.exists(os.path.join(src, cal)):
        print("The practice copy has no confirmed calendar yet: finish step 1 on the setup page first.")
        return 1
    files = ["setup.json", "cue_params.json", cal, cal[:-4] + ".meta.json", "arc_selection.json", "tank_config.json"]
    notes = []
    arr = read_json(os.path.join(src, "tuya", "plug_array.json"), {}) or {}
    plugs = arr.get("plugs", [])
    if plugs and not any(str(p.get("driver", "")).startswith("fake") for p in plugs):
        files.append(os.path.join("tuya", "plug_array.json"))
    elif plugs:
        notes.append("plugs: the practice copy still has practice (fake) plugs, so your real plug list is kept")
    devs = read_json(os.path.join(src, "tuya", "devices.json"), []) or []
    if devs and all(d.get("key") for d in devs):
        files.append(os.path.join("tuya", "devices.json"))
    elif devs:
        notes.append("device keys: the practice copy has no local keys, so your real devices.json is kept")
    rows = []
    for f in files:
        a, b = os.path.join(src, f), os.path.join(dst, f)
        if not os.path.exists(a):
            continue
        state = "same" if _same(a, b) else ("new" if not os.path.exists(b) else "changed")
        rows.append((f, state))
    print("Moving the practice copy to the live tank (data/):\n")
    for f, state in rows:
        print(f"  {state:8} {f}")
    for n in notes:
        print(f"  kept     {n}")
    todo = [f for f, s in rows if s != "same"]
    if not todo:
        print("\nNothing to change: the live tank already has these.")
        return 0
    print(f"\nThe controller will follow {cal} after the restart.")
    if not yes("Make this live?"):
        print("Nothing changed.")
        return 1
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    bak = os.path.join(dst, "backups", f"golive-{stamp}")
    for f in todo:
        b = os.path.join(dst, f)
        if os.path.exists(b):
            os.makedirs(os.path.dirname(os.path.join(bak, f)), exist_ok=True)
            shutil.copy2(b, os.path.join(bak, f))
    for f in todo:
        os.makedirs(os.path.dirname(os.path.join(dst, f)), exist_ok=True)
        shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    print(f"\nCopied {len(todo)} files. The old ones are in data/backups/golive-{stamp}/.")
    report(repo, "golive", {"ts": now(), "calendar": cal, "files": todo, "backup": f"data/backups/golive-{stamp}"})
    return 0


# ---------------------------------------------------------------------------
# health check
# ---------------------------------------------------------------------------
def cmd_check(repo: str) -> int:
    data = os.path.join(repo, "data")
    problems, lines = [], []

    def line(ok, what, detail=""):
        lines.append(("ok  " if ok else "FIX ") + what + (f": {detail}" if detail else ""))
        if not ok:
            problems.append(what + (f": {detail}" if detail else ""))

    cue = read_json(os.path.join(data, "cue_tables.json"))
    cals = [f for f in os.listdir(data) if f.startswith("spawn_calendar_") and f.endswith(".csv")] if os.path.isdir(data) else []
    line(bool(cue or cals), "cue tables installed",
         (f"from {cue.get('source')}" if cue else f"{len(cals)} calendar(s)") if (cue or cals) else "run sh scripts/start.sh again")
    man = read_json(os.path.join(data, "data_manifest_check.json"))
    if man is not None:
        line(bool(man.get("ok")), "cue data matches its fingerprints",
             "" if man.get("ok") else f"changed {len(man.get('changed', []))}, missing {len(man.get('missing', []))} (see QUICKSTART, data_manifest_mismatch)")
    sp = os.path.join(data, "tank_status.json")
    status = read_json(sp, {}) or {}
    age = time.time() - os.path.getmtime(sp) if os.path.exists(sp) else None
    line(age is not None and age < 300, "controller running",
         "no status yet: is the tank container up?" if age is None else f"last minute {round(age)} s ago")
    t = ((status.get("inputs") or {}).get("temp") or {})
    temp = None
    if t.get("configured") is False or not t:
        line(False, "thermometer", "none set: pick a source in step 5 of the setup page")
    else:
        ok = bool(t.get("ok")) and (t.get("age_s") or 1e9) < 900
        temp = {"temp_c": t.get("value"), "age_s": t.get("age_s")}
        line(ok, "thermometer reading", f"{t.get('value')} C, {t.get('age_s')} s old" if ok else (t.get("detail") or "no fresh reading"))
    env = env_read(os.path.join(repo, ".env"))
    for k, v in env.items():          # the preflight below reads them from the environment (never printed)
        if v and k not in os.environ:
            os.environ[k] = v
    tg_ok = bool(env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"))
    line(tg_ok, "phone alerts", "set" if tg_ok else "run sh scripts/start.sh telegram")
    line(bool(env.get("TANK_DEADMAN_URL")), "'computer is off' ping",
         "set" if env.get("TANK_DEADMAN_URL") else "recommended: sh scripts/start.sh telegram asks for it")
    try:
        import setup_core
        pf = setup_core.preflight(data)
        nogo = [x["name"] for x in pf.get("layers", []) if x.get("state") == "nogo"]
        line(not nogo, "safety layers", "all go" if not nogo else "not ready: " + ", ".join(nogo))
    except Exception as e:  # noqa: BLE001  (a check must not crash the report)
        line(False, "safety layers", f"could not check ({type(e).__name__})")
    print("Health check\n")
    for ln in lines:
        print("  " + ln)
    ok = not problems
    print("\nAll good." if ok else f"\n{len(problems)} to fix. The setup page shows the same list in step 3.")
    report(repo, "check", {"checked_utc": now(), "ok": ok, "problems": problems,
                           "temp": temp, "temp_ok": bool(temp and t.get("ok"))})
    return 0 if ok else 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["telegram", "golive", "check"])
    ap.add_argument("--repo", default=os.environ.get("REPO", "/repo"))
    a = ap.parse_args(argv)
    return {"telegram": cmd_telegram, "golive": cmd_golive, "check": cmd_check}[a.cmd](a.repo)


if __name__ == "__main__":
    sys.exit(main())
