"""One container, several loops: the tank service's process supervisor.

TANK_SUPERVISOR_MODE picks the set of children:

  live (default)   the compose `tank` service
    conductor  the plug-array controller. Runs the startup data-manifest check
               first (DATA_MANIFEST_MODE warn|enforce). Its death takes the whole
               container down so Docker restart and autoheal act on it.
    track      track_web.py on 0.0.0.0:8095, published as 127.0.0.1:8095
               (read-only page, cache in /cache).
    feed       tank_feed.py loop (Telegram feed bot). Skipped unless
               TELEGRAM_FEED_BOT_TOKEN and a chat id are set.
    ambient    ambient_temp.py loop, every 30 min (read-only HTTP GETs, one US
               zip code). Skipped when TANK_AMBIENT=0.
    archive    pi_log_archive.py loop (ssh to the Pi). Skipped unless the key
               /state/id_ed25519 exists.

  sandbox          the compose `sandbox` service: the practice stack on fake
                   plugs and dummy sensors, all in one container
    conductor, track (8101), setup (setup_web.py, 8098), sensors
    (sandbox_feed.py) and the lifeguard. On the live tank the lifeguard is its
    own container on purpose; here it is a child only so the practice stack
    shows the same pages.

Every child other than the conductor is restarted on exit with backoff; a
failure there never touches the controller. SIGTERM/SIGINT stops all children.
"""
import os
import signal
import subprocess
import sys
import time

SCRIPTS = "/app/scripts"
MODE = os.environ.get("TANK_SUPERVISOR_MODE", "live")
CRITICAL = "conductor"
MAX_BACKOFF = 300.0


def _py(script, *args):
    return ["python", "-u", f"{SCRIPTS}/{script}", *args]


def children(mode=MODE, env=os.environ):
    """{name: argv} for this mode, with the optional children that have nothing
    to do left out (a child that exits at once would only fill the log)."""
    if mode == "sandbox":
        return {
            "conductor": _py("conductor.py", "loop"),
            "sensors": _py("sandbox_feed.py", "--every-s", "30"),
            "setup": _py("setup_web.py", "serve", "--host", "0.0.0.0", "--port", "8098",
                         "--public-port", env.get("SANDBOX_SETUP_PORT", "8102")),
            "track": _py("track_web.py", "serve", "--host", "0.0.0.0", "--port", "8101",
                         "--public-port", env.get("SANDBOX_TRACK_PORT", "8101")),
            "lifeguard": _py("lifeguard.py", "loop"),
        }
    out = {
        # cue_import ensure: first start of a new install fetches the cue tables
        # (does nothing when a calendar or the locale tables are already in /data).
        "conductor": ["sh", "-c",
                      f"python {SCRIPTS}/cue_import.py ensure --data /data; "
                      f"python {SCRIPTS}/data_manifest.py verify --data /data --scripts {SCRIPTS} "
                      "--status /data/data_manifest_check.json --exit-zero-on-warn "
                      f"&& exec python -u {SCRIPTS}/conductor.py loop"],
        "track": _py("track_web.py", "serve", "--host", "0.0.0.0", "--port", "8095",
                     "--public-port", "8095", "--cache-dir", "/cache"),
    }
    if env.get("TELEGRAM_FEED_BOT_TOKEN") and (env.get("TELEGRAM_FEED_CHAT_ID")
                                               or env.get("TELEGRAM_CHAT_ID")):
        out["feed"] = _py("tank_feed.py", "loop")
    if env.get("TANK_AMBIENT", "1") != "0":
        out["ambient"] = _py("ambient_temp.py", "loop", "--every-min", "30")
    if os.path.exists(env.get("PI_ARCHIVE_KEY", "/state/id_ed25519")):
        out["archive"] = _py("pi_log_archive.py", "loop")
    return out


CHILDREN = children()
procs = {}
backoff = {name: 10.0 for name in CHILDREN}
next_start = {}
stopping = False


def log(msg):
    print(f"[supervisor] {time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)


def start(name):
    procs[name] = subprocess.Popen(CHILDREN[name])
    log(f"started {name} pid={procs[name].pid}")


def stop_all(code):
    for name, p in procs.items():
        if p.poll() is None:
            p.terminate()
    deadline = time.time() + 20
    for name, p in procs.items():
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()
    return code


def on_signal(signum, _frame):
    global stopping
    stopping = True
    log(f"signal {signum}: stopping children")


def main():
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log(f"mode={MODE} children={', '.join(CHILDREN)}")
    for name in CHILDREN:
        start(name)
    while True:
        if stopping:
            return stop_all(0)
        now = time.time()
        for name in CHILDREN:
            p = procs.get(name)
            if p is None or p.poll() is None:
                continue
            rc = p.returncode
            if name == CRITICAL:
                log(f"{name} exited rc={rc}: stopping container for restart")
                return stop_all(1)
            if name not in next_start:
                log(f"{name} exited rc={rc}; restart in {backoff[name]:.0f}s")
                next_start[name] = now + backoff[name]
                backoff[name] = min(backoff[name] * 2, MAX_BACKOFF)
            elif now >= next_start[name]:
                del next_start[name]
                start(name)
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
