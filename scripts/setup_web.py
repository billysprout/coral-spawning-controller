#!/usr/bin/env python3
"""setup_web.py -- local web page for the onboarding flow (setup_core.py).

    python setup_web.py serve [--host 127.0.0.1] [--port 8098]
    python setup_web.py seed --from ../data --to ../staging/data

`serve` reads and writes the data dir in TANK_DATA. In compose it runs as the
opt-in `setup` service against ./staging/data with SETUP_STAGING=1, so it
cannot touch the live tank's files or write to a real plug:

    python scripts/setup_web.py seed --from data --to staging/data
    docker compose --profile setup up -d --build setup
    open http://127.0.0.1:8098/

Only loopback Host headers are served (blocks DNS rebinding), and every POST
must carry the X-Setup header and a JSON body (a cross-site page cannot send
either without a CORS preflight, which this server never grants).

The page links to the tracking page (track_web.py) as the setup's "home". Its
address comes from TRACK_URL (or --track-url); without one the page uses the
host it was opened on and the tracking port paired with this one (setup 8098
-> live tracking 8095, sandbox setup 8102 -> sandbox tracking 8101). Never a
LAN address unless you set one.

Standard library only (plus what setup_core imports).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import setup_core as core  # noqa: E402

PAGE = os.path.join(_HERE, "setup_page.html")
MAX_BODY = 256 * 1024
# setup page public port -> the tracking page that reads the tank it sets up
TRACK_PORT_FOR = {8098: 8095, 8102: 8101}
DEFAULT_TRACK_PORT = 8095


def data_dir() -> str:
    return os.environ.get("TANK_DATA") or os.path.join(os.path.dirname(_HERE), "data")


def links(public_port: int, track_url: str | None = None) -> dict:
    """Where the page sends people when setup is done. An explicit URL wins;
    else a port, which the page joins to the host it was opened on."""
    url = (track_url or os.environ.get("TRACK_URL") or "").strip() or None
    if url and not url.lower().startswith(("http://", "https://")):
        url = None                           # never render a javascript: link
    return {"track_url": url,
            "track_port": TRACK_PORT_FOR.get(int(public_port or 0), DEFAULT_TRACK_PORT)}


def _routes(data, link_info=None):
    d = data
    li = link_info or links(0)
    return {
        ("GET", "/api/links"): lambda b: li,
        ("GET", "/api/overview"): lambda b: core.overview(d),
        ("GET", "/api/locales"): lambda b: core.list_locales(d),
        ("GET", "/api/tank_site"): lambda b: core.current_limits(),
        ("POST", "/api/calendar/preview"): lambda b: core.preview(d, b),
        ("POST", "/api/calendar/apply"): lambda b: core.apply_calendar(d, b.get("choice") or {},
                                                                    b.get("confirm")),
        ("GET", "/api/plugs"): lambda b: {**core.plugs_state(d), "suggested": core.suggest_array(d)},
        ("POST", "/api/plugs/cloud"): lambda b: core.cloud_fetch(
            d, b.get("region"), b.get("api_key"), b.get("api_secret"), b.get("device_id"),
            bool(b.get("remember"))),
        ("POST", "/api/plugs/scan"): lambda b: core.scan(d, b.get("timeout_s", 8)),
        ("POST", "/api/plugs/save"): lambda b: core.save_array(d, b.get("plugs"),
                                                              bool(b.get("ack_role_changes"))),
        ("POST", "/api/plugs/health"): lambda b: core.health(d),
        ("POST", "/api/plugs/power_on"): lambda b: core.power_on_read(d),
        ("POST", "/api/plugs/power_on/set"): lambda b: core.power_on_set(
            d, b.get("name"), b.get("value"), b.get("confirm")),
        ("GET", "/api/itc"): lambda b: core.itc_status(d),
        ("GET", "/api/heater"): lambda b: core.heater_status(d),
        ("POST", "/api/heater/check"): lambda b: core.heater_check(d, b.get("item"), bool(b.get("done"))),
        ("GET", "/api/host_status"): lambda b: core.host_status(d),
        ("POST", "/api/itc/check"): lambda b: core.itc_check(d, b.get("item"), bool(b.get("done"))),
        ("GET", "/api/telegram"): lambda b: core.telegram_status(d),
        ("POST", "/api/telegram/check"): lambda b: core.telegram_check(d, b.get("item"), bool(b.get("done"))),
        ("POST", "/api/telegram/chats"): lambda b: core.telegram_chats(),
        ("POST", "/api/telegram/test"): lambda b: core.telegram_test(d),
        ("GET", "/api/decisions"): lambda b: core.decisions(d),
        ("GET", "/api/preflight"): lambda b: core.preflight(d),
        ("POST", "/api/decisions/confirm"): lambda b: core.confirm_decision(
            d, b.get("id"), b.get("value"), bool(b.get("change")), b.get("answer")),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "tank-setup/1"
    routes: dict = {}
    allowed_hosts: set = set()

    def log_message(self, fmt, *args):     # one short line, never a body
        sys.stderr.write(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(raw)

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in self.allowed_hosts

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host not allowed"})
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(PAGE, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        return self._dispatch("GET", path, {})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "host not allowed"})
        if self.headers.get("X-Setup") != "1" or not (
                self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(403, {"error": "missing X-Setup header or JSON body"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._send(413, {"error": "body too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})
        self._dispatch("POST", self.path.split("?")[0], body if isinstance(body, dict) else {})

    def _dispatch(self, method, path, body):
        fn = self.routes.get((method, path))
        if fn is None:
            return self._send(404, {"error": "not found"})
        try:
            return self._send(200, fn(body))
        except core.SetupError as err:
            return self._send(400, {"error": str(err)})
        except Exception as err:  # noqa: BLE001 - report, keep serving
            traceback.print_exc()
            return self._send(500, {"error": f"{type(err).__name__}: {err}"[:300]})


def serve(host, port, public_port=None, track_url=None) -> int:
    data = data_dir()
    os.environ["TANK_DATA"] = data          # cues.active_params_path reads it
    pp = public_port or port
    Handler.routes = _routes(data, links(pp, track_url))
    Handler.allowed_hosts = {f"127.0.0.1:{pp}", f"localhost:{pp}", f"[::1]:{pp}"}
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"setup page on http://127.0.0.1:{pp}/  data={data}  "
          f"staging={'yes' if core.staging() else 'NO (live data)'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="setup_web.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8098)
    s.add_argument("--public-port", type=int, default=None,
                   help="the host port when published under another number")
    s.add_argument("--track-url", default=None,
                   help="the tracking page's address (default: TRACK_URL, else the "
                        "paired port on the host the page was opened on)")
    sd = sub.add_parser("seed")
    sd.add_argument("--from", dest="src", required=True)
    sd.add_argument("--to", dest="dst", required=True)
    sd.add_argument("--sandbox", action="store_true",
                    help="also wire the copy to the dummy sensor feed (sandbox stack)")
    sc = sub.add_parser("scenario", help="set the sandbox's fake plugs to a named state")
    sc.add_argument("name", nargs="?", help="omit to list the scenarios")
    sc.add_argument("--to", dest="dst", default=os.path.join(os.path.dirname(_HERE),
                                                           "staging", "sandbox"))
    a = ap.parse_args(argv)
    if a.cmd == "scenario":
        if not a.name:
            for k, v in core.list_scenarios().items():
                print(f"{k:28} {v}")
            return 0
        for line in core.apply_scenario(a.dst, a.name):
            print(line)
        print("restart sandbox-tank to load it: docker restart sandbox-tank")
        return 0
    if a.cmd == "seed":
        lines = core.seed_staging(a.src, a.dst)
        if a.sandbox:
            lines += core.sandbox_config(a.dst)
        for line in lines:
            print("seeded", line)
        return 0
    return serve(a.host, a.port, a.public_port, a.track_url)


if __name__ == "__main__":
    raise SystemExit(main())
