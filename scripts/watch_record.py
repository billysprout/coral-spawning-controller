#!/usr/bin/env python3
"""watch_record.py -- record the tank camera during the spawn calendar's watch nights.

The camera (Arducam B0490, IMX462 + 940 nm LEDs) plugs into the Pi next to the
tank. The Pi (a Pi 1, too slow and too small to record) only serves the camera
as an MJPEG stream over HTTP (ustreamer, see data/docs/WATCH-CAPTURE.md). This
service runs on the PC in docker compose, reads that stream and writes H.264
segments to the video folder:

    camera -USB-> Pi (ustreamer :8090) -LAN-> watch container (ffmpeg) -> D:

Windows come from the calendar's `watch`, `watch_start_utc` and `watch_end_utc`
columns, padded by --pad-before / --pad-after minutes. Extra windows (a dark
dry run, a night you want filmed anyway) go in <state>/extra_windows.json:

    [{"start_utc": "2026-11-20T03:00:00Z", "end_utc": "2026-11-20T03:30:00Z",
      "label": "dry run"}]

While a window is open ffmpeg is kept running (restarted with backoff if the
stream drops); outside a window it is stopped. Recording also stops when the
video disk falls under --min-free-gb. Events go to <state>/watch_record.jsonl,
the current state to <state>/watch_record_status.json. Times are UTC.

    watch_record.py run                 # the service loop
    watch_record.py windows             # list upcoming windows and exit
    watch_record.py probe               # read 3 s of the stream, report and exit
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import time

UTC = dt.timezone.utc


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00")).astimezone(UTC)


def load_windows(calendar: str, extra: str, pad_before: float, pad_after: float):
    out = []
    if calendar and os.path.exists(calendar):
        with open(calendar, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if str(r.get("watch", "")).strip().lower() != "true":
                    continue
                if not r.get("watch_start_utc") or not r.get("watch_end_utc"):
                    continue
                out.append({
                    "start": parse_utc(r["watch_start_utc"]) - dt.timedelta(minutes=pad_before),
                    "end": parse_utc(r["watch_end_utc"]) + dt.timedelta(minutes=pad_after),
                    "label": f"watch tank {r.get('date', '')} cue {r.get('cue_date', '')}",
                })
    if extra and os.path.exists(extra):
        with open(extra, encoding="utf-8") as fh:
            for w in json.load(fh):
                out.append({"start": parse_utc(w["start_utc"]), "end": parse_utc(w["end_utc"]),
                            "label": w.get("label", "extra")})
    return sorted(out, key=lambda w: w["start"])


def current_window(windows, t):
    for w in windows:
        if w["start"] <= t < w["end"]:
            return w
    return None


def free_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1e9


class Recorder:
    def __init__(self, a):
        self.a = a
        self.proc: subprocess.Popen | None = None
        self.started: dt.datetime | None = None
        self.fails = 0
        self.next_try = 0.0
        os.makedirs(a.state, exist_ok=True)
        os.makedirs(a.video, exist_ok=True)

    def log(self, event: str, **kw):
        row = {"ts": iso(now_utc()), "event": event, **kw}
        with open(os.path.join(self.a.state, "watch_record.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    def status(self, window, note=""):
        s = {"ts": iso(now_utc()), "recording": self.proc is not None and self.proc.poll() is None,
             "window": None if not window else {"label": window["label"],
                                                "start_utc": iso(window["start"]),
                                                "end_utc": iso(window["end"])},
             "since": iso(self.started) if self.started else None,
             "restarts": self.fails, "free_gb": round(free_gb(self.a.video), 1),
             "source": self.a.source, "note": note}
        p = os.path.join(self.a.state, "watch_record_status.json")
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(s, fh, indent=2)
        os.replace(p + ".tmp", p)

    def cmd(self, window):
        night = window["start"].strftime("%Y%m%d")
        folder = os.path.join(self.a.video, night)
        os.makedirs(folder, exist_ok=True)
        # Exposure is locked on the camera (Pi side). Here: constant frame rate,
        # x264 CRF, fixed-length segments named by UTC start time, so a dropped
        # stream loses at most the segment in progress.
        return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-rw_timeout", "15000000",
                # MJPEG over HTTP carries no timestamps; without this ffmpeg
                # assumes 25 fps and a 10 fps camera plays 2.5x fast
                "-use_wallclock_as_timestamps", "1",
                "-f", self.a.input_format, "-i", self.a.source,
                "-vf", f"fps={self.a.fps}", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", str(self.a.crf), "-pix_fmt", "yuv420p",
                "-g", str(int(self.a.fps * 2)), "-sc_threshold", "0",
                # fragmented MP4: a segment cut short by a crash or power loss
                # is still playable up to its last fragment
                "-f", "segment", "-segment_time", str(self.a.segment_s),
                "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
                "-reset_timestamps", "1", "-strftime", "1",
                os.path.join(folder, "%Y%m%dT%H%M%SZ.mp4")]

    def start(self, window):
        if time.monotonic() < self.next_try:
            return
        c = self.cmd(window)
        self.proc = subprocess.Popen(c, stdin=subprocess.DEVNULL)
        self.started = now_utc()
        self.log("record_start", window=window["label"], pid=self.proc.pid)

    def stop(self, reason):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)      # lets ffmpeg close the segment
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log("record_stop", reason=reason, rc=self.proc.returncode)
        self.proc, self.started = None, None

    def tick(self, windows):
        t = now_utc()
        w = current_window(windows, t)
        note = ""
        if w and free_gb(self.a.video) < self.a.min_free_gb:
            note = f"disk under {self.a.min_free_gb} GB free"
            if self.proc:
                self.log("disk_low", free_gb=round(free_gb(self.a.video), 1))
            self.stop("disk_low")
        elif w:
            if self.proc is not None and self.proc.poll() is not None:
                rc = self.proc.returncode
                self.fails += 1
                backoff = min(300, 10 * 2 ** min(self.fails, 5))
                self.next_try = time.monotonic() + backoff
                self.log("ffmpeg_exit", rc=rc, retry_in_s=backoff)
                self.proc, self.started = None, None
                note = "stream lost, retrying"
            if self.proc is None:
                self.start(w)
        else:
            self.stop("window_closed")
            self.fails = 0
        self.status(w, note)

    def run(self):
        stop = {"flag": False}

        def _term(*_):
            stop["flag"] = True
        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
        self.log("service_start", source=self.a.source, calendar=self.a.calendar)
        while not stop["flag"]:
            windows = load_windows(self.a.calendar, self.a.extra, self.a.pad_before, self.a.pad_after)
            try:
                self.tick(windows)
            except Exception as e:                    # keep the service alive
                self.log("tick_error", error=repr(e))
            for _ in range(int(self.a.interval_s)):
                if stop["flag"]:
                    break
                time.sleep(1)
        self.stop("service_stop")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["run", "windows", "probe"])
    ap.add_argument("--source", default=os.environ.get("WATCH_SOURCE", "http://192.0.2.100:8090/stream"))
    # ustreamer's /stream is multipart MJPEG (mpjpeg); a raw MJPEG feed is "mjpeg".
    ap.add_argument("--input-format", default=os.environ.get("WATCH_INPUT_FORMAT", "mpjpeg"))
    ap.add_argument("--calendar", default=os.environ.get("WATCH_CALENDAR", "/data/spawn_calendar_2026-11-01.csv"))
    ap.add_argument("--state", default=os.environ.get("WATCH_STATE", "/data/watch"))
    ap.add_argument("--video", default=os.environ.get("WATCH_VIDEO", "/video"))
    ap.add_argument("--pad-before", type=float, default=float(os.environ.get("WATCH_PAD_BEFORE_MIN", 15)))
    ap.add_argument("--pad-after", type=float, default=float(os.environ.get("WATCH_PAD_AFTER_MIN", 30)))
    ap.add_argument("--fps", type=float, default=float(os.environ.get("WATCH_FPS", 10)))
    ap.add_argument("--crf", type=int, default=int(os.environ.get("WATCH_CRF", 26)))
    ap.add_argument("--segment-s", type=int, default=int(os.environ.get("WATCH_SEGMENT_S", 600)))
    ap.add_argument("--min-free-gb", type=float, default=float(os.environ.get("WATCH_MIN_FREE_GB", 20)))
    ap.add_argument("--interval-s", type=float, default=15)
    a = ap.parse_args(argv)
    a.extra = os.path.join(a.state, "extra_windows.json")

    if a.action == "windows":
        t = now_utc()
        for w in load_windows(a.calendar, a.extra, a.pad_before, a.pad_after):
            if w["end"] > t:
                print(iso(w["start"]), iso(w["end"]), w["label"])
        return 0
    if a.action == "probe":
        r = subprocess.run(["ffprobe", "-hide_banner", "-v", "error", "-rw_timeout", "10000000",
                            "-f", a.input_format, "-i", a.source, "-show_entries",
                            "stream=codec_name,width,height", "-of", "json"],
                           capture_output=True, text=True, timeout=30)
        print(r.stdout or r.stderr)
        return r.returncode
    Recorder(a).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
