#!/usr/bin/env python3
"""track_web.py -- local tracking page: where the tank is on its cue table.

    python track_web.py serve [--host 127.0.0.1] [--port 8101] [--cache-dir DIR]

Reads the data dir in TANK_DATA (default <repo>/data): the active spawn
calendar (setup.json pin, else the newest), cue_params.json, tank_config.json,
tank_status.json and the JSONL logs. Writes nothing except its own cache
(<data>/track/, or --cache-dir / TRACK_CACHE; when that folder cannot be
written the cache stays in memory). Data shaping is in track_core.py.

    GET /                 the page (track_page.html)
    GET /api/summary      header: calendar, setup basis, cue date, countdowns
    GET /api/year         one row per calendar day, plan + daily actuals
    GET /api/recent?days=1|3|7   5-minute buckets, lamps, events
    GET /api/links        where the "Setup" link points: SETUP_URL (or
                          --setup-url), else the setup port paired with this
                          page's port (8095 -> 8098, sandbox 8101 -> 8102) on
                          the host the page was opened on

GET only. Only loopback Host headers are served (blocks DNS rebinding), the
same rule as setup_web.py. In compose it runs as `sandbox-track` (profile
sandbox) on ./staging/sandbox at http://127.0.0.1:8101/.

Standard library only (plus what track_core imports).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import track_core as core  # noqa: E402

PAGE = os.path.join(_HERE, "track_page.html")
# tracking page public port -> the setup page for the same tank
SETUP_PORT_FOR = {8095: 8098, 8101: 8102}
DEFAULT_SETUP_PORT = 8098


def links(public_port: int, setup_url: str | None = None) -> dict:
    url = (setup_url or os.environ.get("SETUP_URL") or "").strip() or None
    if url and not url.lower().startswith(("http://", "https://")):
        url = None                           # never render a javascript: link
    return {"setup_url": url,
            "setup_port": SETUP_PORT_FOR.get(int(public_port or 0), DEFAULT_SETUP_PORT)}


def _routes(data, link_info=None):
    d = data
    li = link_info or links(0)

    def recent(q):
        try:
            days = int((q.get("days") or ["1"])[0])
        except ValueError:
            raise core.TrackError("days must be a whole number") from None
        return core.recent(d, days)

    return {
        "/api/summary": lambda q: core.summary(d),
        "/api/year": lambda q: core.year(d),
        "/api/recent": recent,
        "/api/links": lambda q: li,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "tank-track/1"
    routes: dict = {}
    allowed_hosts: set = set()

    def log_message(self, fmt, *args):     # one short line
        sys.stderr.write(f"{self.command} {self.path.split('?')[0]} "
                         f"{args[1] if len(args) > 1 else ''}\n")

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(
            body, default=str, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in self.allowed_hosts

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host not allowed"})
        url = urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            with open(PAGE, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        fn = self.routes.get(url.path)
        if fn is None:
            return self._send(404, {"error": "not found"})
        try:
            return self._send(200, fn(parse_qs(url.query)))
        except core.TrackError as err:
            return self._send(400, {"error": str(err)})
        except Exception as err:  # noqa: BLE001 - report, keep serving
            traceback.print_exc()
            return self._send(500, {"error": f"{type(err).__name__}: {err}"[:300]})

    do_HEAD = do_GET

    def _no(self):
        self._send(405, {"error": "read-only page: GET only"})

    do_POST = do_PUT = do_DELETE = do_PATCH = _no


def serve(host, port, public_port=None, cache_dir=None, setup_url=None) -> int:
    data = core.data_dir()
    os.environ["TANK_DATA"] = data          # find_calendar / active_params_path read it
    if cache_dir:
        os.environ["TRACK_CACHE"] = cache_dir
    pp = public_port or port
    Handler.routes = _routes(data, links(pp, setup_url))
    Handler.allowed_hosts = {f"127.0.0.1:{pp}", f"localhost:{pp}", f"[::1]:{pp}"}
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"tracking page on http://127.0.0.1:{pp}/  data={data}  "
          f"cache={core.cache_dir(data)}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="track_web.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8101)
    s.add_argument("--public-port", type=int, default=None,
                   help="the host port when published under another number")
    s.add_argument("--cache-dir", default=None,
                   help="where the per-day aggregates are cached (default <data>/track)")
    s.add_argument("--setup-url", default=None,
                   help="the setup page's address (default: SETUP_URL, else the "
                        "paired port on the host the page was opened on)")
    a = ap.parse_args(argv)
    return serve(a.host, a.port, a.public_port, a.cache_dir, a.setup_url)


if __name__ == "__main__":
    raise SystemExit(main())
