# Watch capture (spawn-night video)

Goal: film the three *S. gigantea* through each watch night so a spawn can be seen and each animal sexed by what it releases (sperm: a smoky stream; eggs: visible grains). Owner's decision 2026-11-07: sex by watching spawns, no biopsy.

First nights: tank dates **2026-11-30, 12-01, 12-02**, 16:35 to 21:35 PT (calendar `watch_start_utc` / `watch_end_utc`). The recorder pads each window by 15 minutes before and 30 after.

## Chain

```
Arducam B0490 (IMX462, 940 nm LEDs) -USB-> Pi raspberrypi 192.0.2.100 (ustreamer, :8090)
    -LAN-> tank-watch container on the PC (ffmpeg) -> D:\anemones-watch\<UTC night>\*.mp4
```

The Pi is a **Raspberry Pi Model B Rev 2** (a Pi 1: armv6, 512 MB, 360 MB free on the card). It cannot encode or store video, so it only serves the camera's own MJPEG frames. The PC encodes and stores.

## Status (2026-11-09)

| Part | State |
|---|---|
| Camera | **Not bought.** Arducam B0490, about $68. Order with time to arrive and do the dry run, ideally by 2026-11-20 |
| Mount | Foam collar on the glass, lens square to the front pane, out of the light path |
| Pi stream (ustreamer) | Not installed. Steps below, once the camera is on the Pi |
| Recorder (`scripts/watch_record.py`, compose service `watch`, profile `watch`) | Built. Dry run against a synthetic stream passed (window start and stop, 10 fps H.264 segments, fragmented MP4) |
| Observation log (`data/watch/observations.csv`) | Template committed |
| Dark dry run with the real camera | Not done. Also shows whether the 940 nm LEDs trip the `light_leak` alert or read on the lux sensor |

## Pi side (when the camera arrives)

1. Plug the camera into the Pi. `ls /dev/video*` should show a new low-numbered device (the `video10`-`video21` nodes are the Pi's own codec and ISP devices). `v4l2-ctl --list-devices` names it.
2. List its formats and controls: `v4l2-ctl -d /dev/video0 --list-formats-ext` and `v4l2-ctl -d /dev/video0 -l`. Pick an MJPEG mode at 1280x720 (or 1920x1080 if the Pi keeps up).
3. Install: `sudo apt install ustreamer`.
4. Lock exposure, gain and white balance so a night's frames are comparable. Control names vary by camera: typically `auto_exposure=1` (manual), `exposure_time_absolute=<n>`, `gain=<n>`, `white_balance_automatic=0`. Find values in a dark room with the LEDs on, then put the `v4l2-ctl -d /dev/video0 -c ...` line in the unit's `ExecStartPre`.
5. Service `/etc/systemd/system/watch-cam.service`:

```ini
[Unit]
Description=Tank camera MJPEG stream
After=network-online.target

[Service]
ExecStartPre=/usr/bin/v4l2-ctl -d /dev/video0 -c auto_exposure=1
ExecStart=/usr/bin/ustreamer --device=/dev/video0 --format=MJPEG --encoder=HW --resolution=1280x720 --desired-fps=10 --host=0.0.0.0 --port=8090 --persistent --drop-same-frames=0
Restart=always
RestartSec=5
User=pi
SupplementaryGroups=video

[Install]
WantedBy=multi-user.target
```

`sudo systemctl daemon-reload && sudo systemctl enable --now watch-cam`. Check `http://192.0.2.100:8090/` in a browser. `--encoder=HW` passes the camera's own JPEG through and costs the Pi almost nothing; if ustreamer refuses it, try `--encoder=CPU` at a lower fps.

Note: the Pi's clock zone is America/New_York. Everything here uses UTC, so it does not matter, but local times in Pi logs are Eastern.

## PC side

```
docker compose --profile watch up -d --build watch
docker exec tank-watch python /app/watch_record.py probe     # 3 s read of the stream
docker exec tank-watch python /app/watch_record.py windows   # upcoming windows
```

- Video: `D:\anemones-watch\<YYYYMMDD of the UTC window start>\<UTC start>.mp4`, 10-minute segments, 10 fps, x264 CRF 26. About 0.5 to 1 GB per night at 720p, depending on noise in the dark. Recording stops under 20 GB free.
- Log: `data/watch/watch_record.jsonl` (start, stop, stream loss and retry). Live state: `data/watch/watch_record_status.json` (not committed).
- Extra windows (the dark dry run, or any night you want filmed): `data/watch/extra_windows.json`, a list of `{"start_utc", "end_utc", "label"}`. Read every 15 s, no restart needed.
- Settings are environment variables on the service (`WATCH_FPS`, `WATCH_CRF`, `WATCH_SEGMENT_S`, `WATCH_PAD_BEFORE_MIN`, `WATCH_PAD_AFTER_MIN`, `WATCH_MIN_FREE_GB`, `WATCH_INPUT_FORMAT`).
- The recorder only reads the stream. It never touches the lights, the lux reading or the controller.

## Dry run in the dark (before 2026-11-30)

1. Add a 30-minute window after lights-out to `extra_windows.json`.
2. During it: check the stream shows all three animals, how much noise there is, and whether the `light_leak` alert fires or the lux reading rises while the IR LEDs are on. A light-proof check: lux with the camera unplugged against lux with it on, same minute of the night.
3. Afterwards: play a segment, and set exposure and gain again if needed.

## Observation log

`data/watch/observations.csv`, one row per sighting:

| Column | Meaning |
|---|---|
| `night_local` | Tank date of the watch night (2026-11-30) |
| `cue_date` | Its cue date from the calendar (2027-06-26) |
| `time_local` | Clock time of the sighting, PT, from the clip timestamp |
| `animal` | `blue-tip` (big one, centre left), `pink` (behind it), `tan` (upper right, on the glass) |
| `event` | `sperm`, `eggs`, `pre-spawn` (swelling, posture), `none` |
| `confidence` | `sure`, `likely`, `unsure` |
| `clip` | Segment file name |
| `notes` | Anything else |

Log nights with no spawn too (`event` `none`), so a quiet night is on record rather than missing.
