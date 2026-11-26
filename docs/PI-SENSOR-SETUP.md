# Pi sensor setup (raspberrypi)

State as of 2026-10-30. Labels: **Verified** (read from the Pi or seen working),
**Assumed** (how it should be wired or how the part behaves; not seen).

## The Pi

| | |
|---|---|
| Model | Raspberry Pi Model B Rev 2 (26-pin header, single core, slow), Raspbian 12 (bookworm), Python 3.11.2, smbus2 0.4.2. **Verified** |
| Address | 192.0.2.100 (static), hostname `raspberrypi`. **Verified** |
| Runs | reef-pi 4.3 (port 80), the lux logger and the sensor server below. The plug conductor does NOT run here; it runs on the PC. **Verified** |
| Login | ssh from the PC with the PC's ed25519 key (`pi@your-pc`, fingerprint `SHA256:<your key fingerprint>`) as `pi`; `sudo` needs no password. **Verified** |
| Firewall | `ufw` on, default deny in. Open: 22, 80, 8080, 53, and **8099 from 192.0.2.0/24 only** (the sensor server). Something listens on 8080; I did not identify it. **Verified** |
| Boot config | `/boot/firmware/config.txt` active lines: `dtparam=i2c_arm=on`, `dtparam=audio=on`, `dtoverlay=vc4-kms-v3d`, `dtoverlay=dwc2,dr_mode=host`, `dtoverlay=w1-gpio`, `gpio=4=pu` (added 2026-10-30; original saved as `/home/pi/config.txt.bak-pullup`). That line does NOT hold: after a reboot GPIO 4 read low until the pull-up was set again by hand, so the `w1-pullup` service below does the real job. **Verified** |
| Pull-up service | `w1-pullup.service` (enabled; runs `/usr/local/sbin/w1-pullup.sh` at boot): waits for the 1-Wire bus master, then `pinctrl set 4 pu`. Tested by clearing the pull (`pinctrl set 4 pn`), starting the unit, and seeing the line go high and the probe read. **Verified across a real reboot (2026-10-31 00:37 Pi time)**: the unit ran about 6 s after start, the line was high, and `28-000000d5c6df` read 20.3 C with no manual step |

## Pins

Physical header pin numbers; GPIO numbers are the chip's names (BCM).

| Sensor | Signal | GPIO | Header pin | Status |
|---|---|---|---|---|
| BH1750 lux (I2C, address 0x23) | 3.3 V | | 1 | **Assumed** |
| | GND | | 9 (owner, 2026-10-30; earlier assumed 6) | **Assumed** |
| | SDA | GPIO 2 | 3 | **Verified** by software: the logger finds the sensor on bus `/dev/i2c-1`, which is fixed to GPIO 2/3 |
| | SCL | GPIO 3 | 5 | same |
| DS18B20 temperature (1-Wire) | 3.3 V | | 17 (pin 1 also works but the lux sensor uses it) | **Not wired yet** (waiting on cables) |
| | data | GPIO 4 | 7 | the `w1-gpio` overlay is on at its default pin |
| | GND | | 14 (pin 9 is the lux sensor's; 20 and 25 also work) | **Not wired yet** |
| | pull-up | | internal ~50 k, set at boot by `w1-pullup.service` (see the Pull-up service row), so no resistor is wired for now | **Verified** on the Pi: before the change `pinctrl` showed GPIO 4 floating low (no pull); after `pinctrl set 4 pu` it reads high. **Assumed** to be enough for one probe; if it fails, add a 4.7 k resistor between data (pin 7) and 3.3 V (pin 17). Never pull up to 5 V |

Notes:
* The Pi has on-board pull-ups on GPIO 2/3, so the BH1750 needs none.
* With no probe attached, the 1-Wire bus lists bogus devices (`00-6cd000000000`, `00-acd000000000`, and others; the IDs change each look). They are noise from the floating line. The logger only looks at IDs that start `28-`.
* The chip (BCM2835) cannot report a pin's pull state: `pinctrl get 4` shows `--` even with the pull-up on. Judge it by the level (low floating, high with the pull-up) and by whether the probe reads.
* GPIO 4 is used only by the 1-Wire driver (`onewire@0`); no other overlay, script or reef-pi connector claims it (checked 2026-10-30). Suggested wiring uses pin 17 for 3.3 V so the probe does not share pin 1 with the lux sensor.
* `/dev/i2c-2` also exists. It is not the sensor's bus (the logger found it on bus 1).
* **2026-11-03: the sensor on the Pi is now a VEML7700 (I2C 0x10, bus 1); `lux_logger.py --sensor auto` (default) finds it, and falls back to a BH1750. The old script is kept on the Pi as `lux_logger.py.bh1750-only`. Output adds `sensor`, `gain`, `it_ms` and drops `mt`; the conductor reads only `ts` and `lux`.** Older note: the BH1750 resolves about 0.11 lux per step, so moonlight (0.1 to 0.3 lux) is only a few counts. The VEML7700 on order is finer (pi-contingency-plan.md 2.8).

## Moving a sensor to other pins

Nothing in the loggers or the conductor names a pin. The boot config does, once.

| Sensor | Change | Then |
|---|---|---|
| Lux | Bus 1 is fixed to GPIO 2/3. For other pins add a software bus to `config.txt`, for example `dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24` | Reboot. `lux_logger.py --find` finds bus 3 by itself |
| Temperature | Change `dtoverlay=w1-gpio` to `dtoverlay=w1-gpio,gpiopin=N` | Reboot. The probe keeps its `28-...` ID, so no other file changes |

## What runs on the Pi

All three are systemd services, enabled at boot and active. **Verified**

| Service | Does | Files it uses |
|---|---|---|
| `lux-logger` | `scripts/lux_logger.py --path /home/pi/lux.jsonl --latest-file /home/pi/lux_latest.txt`: reads the BH1750 every 10 s, auto-ranging, finds bus and address itself | writes `/home/pi/lux.jsonl` (one JSON line per reading, rotated at 2 MB) and `/home/pi/lux_latest.txt` (bare number, for reef-pi) |
| `lux-server` | `scripts/lux_server.py --source lux=/home/pi/lux.jsonl --source temp=/home/pi/temp.jsonl --port 8099`: serves the newest line over HTTP | `GET http://192.0.2.100:8099/lux`, `/temp`, `/health`, `/` |
| `reef-pi` | the reef-pi controller | its own database |

Scripts live in `/home/pi/reef-pi/scripts/`. The earlier `lux_logger.py` and
`lux_server.py` are kept as `.bak`. `read_lux.py`, `read_lux2.py` and `race.sh`
are the original test scripts: do not run them while `lux-logger` is running
(they reprogram the sensor's range).

reef-pi: the `lux-analog` driver (type `file-analog`) reads `lux_latest.txt`, and
the pH module is switched on. No analog input or probe exists yet because the
driver manager logs `can't build hal pin ...: parsing "109+rpt"` at start and the
driver then offers no pins. Unresolved; the conductor does not depend on it.

## Conductor side (on the PC, in `data/tank_config.json`)

```
"lux":  {"driver": "http_json", "url": "http://192.0.2.100:8099/lux",  "field": "lux", "timeout_s": 4}
"temp": {"driver": "http_json", "url": "http://192.0.2.100:8099/temp", "field": "temp_c", "offset_c": 4.06, "timeout_s": 4}
```

Both are live (verified end to end 2026-10-31: the conductor's own `Inputs` class read temp 20.312 C and lux 12.68 from the Pi). The `temp-logger` service is enabled on the Pi, writes `/home/pi/temp.jsonl` every ~10 s, and `lux-server` serves it at `/temp`. Calibration offsets belong in `offset_c` there, not on the Pi. The raw value is uncalibrated: the owner's hand calibration lives in reef-pi (same sensor `28-000000d5c6df`, shown in F), not on the Pi. On 2026-10-31 reef-pi's `current_reading` was 78.35 F (25.75 C) while the raw probe read 21.687 C (71.0 F), so the live config carries `offset_c: 4.06`, and `limits.temp_calibrated` is true there. This is one point, hand-matched to reef-pi and the Inkbird, with no reference thermometer; reef-pi's own calibration points are not readable through its API, so a slope would show as a small drift away from 78 F. Re-check at another temperature. To compare again: sign in to reef-pi (`POST /auth/signin`, then `GET /api/tcs/1/current_reading`) and read `http://192.0.2.100:8099/temp` at the same moment; the offset is `(F - 32) / 1.8 - raw_c`. Alerts from the conductor are worded in F; config and logs stay in C. The conductor runs as the `tank-controller` Docker service (observe-only).

## When the temperature probe cables arrive

1. Wire it with the Pi off (table above: 3.3 V to pin 17, GND to pin 14, data to pin 7). The internal pull-up is already on; the overlay is on too, so no reboot is needed unless you change the pin.
2. On the Pi: `python3 ~/reef-pi/scripts/temp_logger.py --list` should print a `28-...` ID and a temperature. Two probes: the logger refuses to guess, so pass `--device 28-...`.
   If no `28-` ID appears, or readings drop out or show CRC errors, the internal pull-up is too weak (more likely on a long cable): add a 4.7 k resistor between data (pin 7) and 3.3 V (pin 17). Anything from 2.2 k to 10 k works.
3. Create the service (`sudo tee /etc/systemd/system/temp-logger.service`, then `sudo systemctl daemon-reload && sudo systemctl enable --now temp-logger`):

```
[Unit]
Description=DS18B20 temperature logger
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/reef-pi/scripts
ExecStart=/usr/bin/python3 /home/pi/reef-pi/scripts/temp_logger.py --path /home/pi/temp.jsonl
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

4. Check `curl http://192.0.2.100:8099/temp` from the PC (it is a 503 until a line exists), then switch the conductor's `temp` input (above).
5. Before the heater gate is ever allowed to switch (`countdown_verified`, `inkbird_resume_verified`, `temp_calibrated` all true), calibrate the probe against a reference thermometer and record it in `data/calibration_events.json`.

## Boot-order gotcha (fixed 2026-10-30)

The first versions of the two unit files said `After=multi-user.target` (logger) and
`After=network-online.target lux-logger.service` (server). A service that is wanted by
`multi-user.target` must not also start after it: systemd found an ordering cycle at boot and
silently dropped the server's start job, so `lux-server` did not start after the first reboot.
Both now say `After=network.target` (the server also `lux-logger.service`). Use that pattern for
`temp-logger` too. Backups of the old units: `/home/pi/lux-logger.service.bak`,
`/home/pi/lux-server.service.bak`. Look for this kind of problem with
`journalctl -b | grep -i "ordering cycle"`.

## To do: measure each lamp (`measure-lamp`)

`outputs.verify.channels.<ch>.min_lux_change` is how much brighter the lux sensor must get before the verifier calls a lamp command `confirmed`. It depends on the lamp, the sensor's position and the room, so it is measured per lamp, not guessed. Status as of 2026-10-30:

| Plug | Lamp | Measured? |
|---|---|---|
| plug-8 | Halo 120 W on the KMC plug since 2026-11-03 (it was the 200 W until then; swapped because plug-8 wedges). Role `white` since 2026-11-06 (scenario B), `white_plus` before | About 49 lux at the glass (2026-11-03) and about 35-50 lux at the Pi sensor (`outputs.verify.channels.white` since 2026-11-06, `white_plus` before; confirmed by the verifier). The 2026-10-30 run used a desk lamp in its place (+4.10 lux, suggested 1.6); that number does not carry over |
| plug-9 | Blue LED (teck; first used as the moon lamp, renamed `blue` 2026-11-03 10:40 PST because it is too bright for a moon) | At the glass about 126 lux (2026-11-03); about 100 lux at the Pi sensor. Role `blue` |
| plug-10 | 200 W (teck plug) since 2026-11-03. Role `white` (the main light) | About 2,300 lux at the glass (2026-11-03) and about 1,100-1,164 lux at the Pi sensor (`outputs.verify.channels.white`; 2026-11-05 the plug-10 step was +1,100 lux) |

1. Roles are assigned (plug-8 `white` = Halo, plug-9 `blue`, plug-10 `white_plus` = 200 W, since 2026-11-06); the measured value lands on that channel's `min_lux_change`. `white` and `white_plus` have their own entries, so the Halo and the 200 W lamp are measured separately. Roles follow the lamp on the plug, not the plug's history; see TANK-NOTES "Plug roles reversed by mistake and restored (2026-11-05)" before changing them.
2. By day (or pass `--even-if-dark`), with the lamp installed over the tank: `python conductor.py measure-lamp --plug plug-N` to see the plan, then rerun with `--yes`. It switches the plug off and on twice, reads the lux sensor, prints the suggested `min_lux_change` and a config snippet, and puts the plug back as it found it.
3. Paste the snippet into `data/tank_config.json`. A lamp the sensor cannot see (the tool says "No threshold suggested") stays on the watts check only, and none of these plugs report watts, so such a lamp would be unverified.
4. **Moon lamp:** moonlight is 0.1 to 0.3 lux and the BH1750 resolves about 0.11 lux per step, so expect plug-9 to need the finer VEML7700 or a close sensor position.

**Redo all of it when the lux meter moves.** It is now about 2 m from the tank; it is to be installed in the shaded enclave. Every `min_lux_change` is specific to the old position, so on the move: delete the measured values, rerun `measure-lamp` for every lamp, and recheck the dark baseline against `limits.dark_lux_max` (0.2 lux since 2026-11-03) with all lamps off, since the enclave should read darker than the room does now. The sensor's I2C address and the services do not change; only the position does.

**Glass-position readings (2026-11-03, VEML7700, not the enclave).** Room fully dark, VEML held against the tank glass, gain 1/8, 100 ms. Each state is three 20-second blocks at 1 Hz. These are for comparison only. They are far brighter than the enclave will read, so they do not set any `min_lux_change` value.

**Correction, 2026-11-03.** The first version of this table converted counts with 0.0576 lux per count, which is the VEML7700's resolution at gain x1 (100 ms), not at gain 1/8. The run script wrote the configuration word 0x1000 to register 0x00 before every block: bits 12:11 = 10 is gain x1/8 and bits 9:6 = 0000 is 100 ms, and the datasheet gives 0.0036 x (800 / IT in ms) x (2 / gain) = **0.4608 lux per count** there. That made every value 8 times too low. Checked on the Pi the same day, with the lux logger stopped for a minute and one steady daylight source read at four gain settings (100 ms): counts at 1/8, 1/4, 1 and 2 stood in the ratio 1 : 2.0 : 7.7 : 15.5 (expected 1 : 2 : 8 : 16), and the gain 1/8 count times 0.4608 gave 109 lux against 105 lux from the logger's own reading at gain 2 and 800 ms in the same minute (4 percent apart, daylight drifting), where 0.0576 would have given 14 lux. Above 1,000 lux the datasheet's non-linearity correction is applied (the 200 W lamp only). The corrected values below are recomputed from the original per-block means (count = old value / 0.0576). The check shows the ratios between settings are right; the absolute scale is the datasheet's, not yet compared with a reference lux meter.

| State | Block 1 | Block 2 | Block 3 |
|---|---|---|---|
| All lamps off (below 0.46 lux: zero counts at this gain) | 0.0 | 0.0 | 0.0 |
| plug-8, 200 W white | 2,339 | 2,301 | 2,305 |
| plug-9, moon | 127.2 | 125.8 | 125.8 |
| plug-10, Halo 120 W (white_plus) | 51.1 | 47.9 | 47.9 |

The zero reading means no counts at 0.46 lux per count, so it rules out a taped-LED contribution above about 0.5 lux only. The lux logger's finer setting (0.0036 lux per count) read 0.144 to 0.169 lux overnight on 2026-11-03, so a floor of about 0.15 lux, from the LED, the room or the chip, is not excluded. Earlier runs in this session (the 200 W swing to 33 lux, the flat moon count) came from the sensor pointing the wrong way and are not valid.

Run script: `lamp_run.py` in the session scratchpad (not in the repo). It switches each plug alone, reads the VEML over ssh, and restores the starting plug states in a `finally` block.

**Sensor-position readings (2026-11-03 about 10:00 to 10:15 PST, VEML7700 where it sits now, gain 2, 100 to 800 ms).** Roles at the time: plug-10 = 200 W white, plug-8 = Halo (white_plus), plug-9 = moon. Run with `conductor.py measure-lamp` per plug, then a script that set every combination (all off first, 20 s settle, 40 s of 2 s samples, plug states restored afterwards).

| State | Lux at the sensor | Adds to the all-off baseline |
|---|---|---|
| All lamps off | 0.30 (0.15 overnight) | 0 |
| 200 W white alone | 1,164 (sd 5.7) | +1,164 |
| Halo alone | 49.9 (sd 1.5) | +49.6 |
| Moon alone | about 100 (first samples 103.7; measure-lamp +100.0, sd 2.2) | about +100 |
| 200 W + Halo | 1,129 (sd 11) | +1,129 |
| All three on | 1,206 (sd 12) | +1,206 |

Notes and checks:
* **The moon lamp was on all morning.** Something switched plug-9 on at 09:00 PST (the SmartLife schedule, since removed) and the "daylight" of about 100 lux I had been reading was that lamp. The room reads 0.30 lux in daytime with everything off. The first `measure-lamp` runs for plug-10 and plug-8 therefore had the moon lamp as their baseline (the Halo's own sd of 5 lux at 100 lux made `measure-lamp` call it not visible); the pair and single numbers above, taken with everything else off, are the ones to use.
* **The "moon only" block was contaminated** (the 200 W lamp came on about 10 s into it, cause unknown: not the conductor, whose outputs were off), so the moon figure is from the first clean samples and the separate `measure-lamp` run.
* **Against last night's glass readings** (corrected: 200 W 2,315, moon 126, Halo 49): the Halo matches (49.6), which also confirms the 0.4608 lux-per-count correction; the moon is 21 percent lower and the 200 W 50 percent lower here, so the sensor sits off the 200 W lamp's axis. The three singles sum to about 1,314 and all three together read 1,206 (8 percent less: the 200 W lamp's output drifts by that much within a block, and the sensor is near the top of its range here).
* These are valid only while the sensor stays where it is; redo them (`measure-lamp`) when it moves to the enclave.

**Daylight curve at glass position (planner schedule, 2026-11-03, tank site, Los Angeles time).** Built by scaling each channel's planner level by its (corrected) glass reading: 200 W white x 2,315 lux, Halo x 49 lux, moon x 126 lux. Lux is additive here, an approximation. (The first version used the uncorrected readings 254, 6.1 and 15.8, which were 8 times too low.)

| Local time | 200 W white | Halo 120 W | Moon | Total (lux) |
|---|---|---|---|---|
| 00:00 | 0 | 0 | 0 | 0 |
| 00:30 to 04:30 | 0 | 0 | 126.3 | 126 |
| 05:00 | 811 | 0 | 0 | 811 |
| 05:30 to 07:00 | 2,315 | 0 | 0 | 2,315 |
| 08:00 | 2,315 | 32.9 | 0 | 2,348 |
| 12:00 | 2,315 | 49.0 | 0 | 2,364 |
| 16:00 | 2,315 | 25.7 | 0 | 2,341 |
| 16:30 to 17:30 | 2,315 | 0 | 0 | 2,315 |
| 18:00 | 2,142 | 0 | 0 | 2,142 |
| 18:30 to 23:30 | 0 | 0 | 0 | 0 |

Limits of this curve:
- Blue is not included: no plug in the array drives the blue channel.
- White is a plateau with 30-minute ramps, not a sine. Only the Halo follows the sun's height.
- The moon value is far above real moonlight (0.1 to 0.3 lux at the enclave). Replace it with a dim LED calibrated to moonlight.
- All values are at glass distance. The enclave reads much lower, so the shape matters more than the absolute numbers.
- The planner's schedule is for the tank site, not Okinawa.

## Backups and restore

Two copies, made 2026-11-02:

| Copy | Where | What it holds |
|---|---|---|
| Full SD image | `D:\pi-images\raspberrypi-2026-11-02.img.gz` (outside the repo; 1.2 GB gzipped) | Whole card (3.7 GB, `/dev/mmcblk0`): boot, root, reef-pi, Wi-Fi, ssh keys, database. Taken live while services ran. **Status:** complete; `gzip -t` passes. |
| Snapshot in repo | `data/pi-snapshot/` via `python scripts/pi_snapshot.py` | Boot config, service units, sensor scripts, ufw rules, live state. Text only, diffable, safe to commit. Rerun after any change on the Pi. |

SHA-256 of `raspberrypi-2026-11-02.img.gz`: `3bbad65c704945e5c7f375cd54940c8d1236e7a492fcb43b711d30be169e3e61`

**Restore the whole card:** write `raspberrypi-2026-11-02.img.gz` to a card of at least 4 GB with Raspberry Pi Imager ("Use custom") or balenaEtcher. They read `.img.gz` directly. Then boot the Pi. Expect the static address 192.0.2.100 and the ssh key login to work again.

**Restore one file:** see the header of `scripts/pi_snapshot.py`.

## Known gaps

* The wiring in the pin table is as designed; only the I2C bus is confirmed by software.
* The lux reading at the sensor's position is about 5 lux with the room lights on. The conductor's dark-leak limit was 0.5 lux (`limits.dark_lux_max`), so check the reading in the dark before the first dark night. (Update 2026-11-03: the limit is now 0.2 lux, from the VEML7700's overnight readings of 0.144 to 0.169 lux.)
* `outputs.verify.channels.<ch>.min_lux_change` is unset until a lamp-on change at the sensor is measured; see "To do: measure each lamp" above (and redo it when the sensor moves to the enclave).
* Code: scripts `lux_logger.py`, `temp_logger.py`, `lux_server.py`; conductor input `http_json` in `tank_inputs.py`; background in `pi-contingency-plan.md` 3.6b.
