# Lux sensors: BH1750 now, VEML7700 next

> **Carried over from the unmerged branch `claude/veml7700-light-sensor-wiring-fu6hf7` (commit 231c0ab, 2026-10-25).**
> Written before the conductor read the Pi directly. `scripts/lux_to_file.py` (the file writer this guide uses) was never merged: the Pi now runs
> `scripts/lux_logger.py` (JSON lines, auto-ranging) and `scripts/lux_server.py` (serves `/lux` and `/temp`), documented in `PI-SENSOR-SETUP.md`.
> Treat the `lux_to_file.py` commands and reef-pi Route A below as the older side path; the sensor facts, observations and the VEML7700 notes still stand.

Last updated 2026-10-25. Everything under "Observations" was reported by the user from bench sessions on 2026-10-23 to 2026-10-25 and is unconfirmed. No lux data is committed to the repo yet.

Purpose: audit that dark windows are dark (`curtain-plan.md`). Thresholds from that plan: alert above 0.5 lux during a dark window; the moonlight LED targets 0.1 to 0.3 lux. Lux is photopic (human-eye weighted), so a blue moon LED reads low. Treat readings as an index until calibrated against a reference meter.

## Status

| Sensor | State |
|---|---|
| BH1750 breakout, ADDR to GND (address 0x23) | Wired, detected, reads. |
| VEML7700 (Adafruit, STEMMA QT, address 0x10) | **Fitted and logging since 2026-11-03** (device ID register 0x07 reads 0xC481; first readings 0.17 lux in the room at gain x2, 800 ms). `lux_logger.py --sensor auto` finds it on its own; the BH1750 was no longer answering. Dark floor, noise and calibration against a reference are not measured yet. |

## Host

- Raspberry Pi Model 1 B (user-reported), hostname `raspberrypi`, 192.0.2.100. ARMv6 and slow.
- `i2cdetect -y 1` shows `23`, so the sensor is on bus 1. On an early Rev 1 board the header I2C is bus 0 (`i2cdetect -y 0`); `cat /proc/cpuinfo` gives the revision.
- Pi-hole also runs on this Pi (its web UI is on port 8080).

## Wiring (BH1750 to the Pi header)

| BH1750 | Pi header pin | Note |
|---|---|---|
| VCC | 1 (3.3 V) | Power from 3.3 V only. Many breakouts pull SDA and SCL up to VCC, so 5 V would put 5 V on the Pi's GPIO. |
| GND | 6 | |
| SDA | 3 | GPIO2 on Rev 2, GPIO0 on Rev 1. |
| SCL | 5 | GPIO3 on Rev 2, GPIO1 on Rev 1. |
| ADDR | GND | 0x23. Tie to 3.3 V for 0x5C (second sensor on the same bus). |

- Pin 1 is the header pin nearest the SD card slot (square pad on the underside). "P1" is the name of the whole header, not a pin.
- The Pi already has I2C pull-ups. Do not add more.
- Keep the I2C cable short (about 1 m is usually fine at 100 kHz; longer needs twisted pair or a buffer).
- The Adafruit VEML7700 uses a STEMMA QT (JST-SH) connector: use a STEMMA QT cable with female sockets for the Pi end. The standard board is about 16.6 x 16.5 mm; mounting-hole size is unconfirmed (probably M2.5, measure it).

## Reading the BH1750

The Pi's `scripts/lux_logger.py` reads the sensor (see the note at the top; the older `lux_to_file.py` it replaced wrote a plain file and an optional CSV).

```bash
sudo apt install -y --no-install-recommends i2c-tools python3-pip python3-setuptools python3-wheel
pip3 install --break-system-packages smbus2        # or: sudo apt install python3-smbus2
python3 scripts/lux_to_file.py --once              # one reading
python3 scripts/lux_to_file.py --out ~/lux.txt --csv ~/lux_log.csv
```

- Mode: continuous high-resolution mode 2 (`0x11`) with the measurement-time register at 254. That gives about 0.11 lux per count; the default (MTreg 69) is 0.5 lux per count, which cannot resolve 0.1 to 0.3 lux.
- Conversion: `lux = raw / 1.2 x (69 / MTreg) / 2`. The final `/ 2` is required in mode 2. The first example script omitted it, so the first readings (7.5 and 0.83 lux) were double the true values.
- Each reading takes about 0.7 s at MTreg 254. If a read fails the script exits with the error and the output file keeps its last value (see the staleness caution below).
- Expected from the datasheet, not yet tested: the VEML7700 resolves about 0.0036 lux per count at gain x2 and 800 ms, and should handle the moonlight band better. At that setting it saturates near 236 lux, so it needs auto-ranging or two settings (dark windows and daytime).

## Observations (user-reported, unconfirmed)

- Room lights off, curtain not in place: 0.0 lux.
- TV or bathroom light on: 0.3 lux and above. The 0.5 lux alert level is only slightly higher, so room light alone can approach it. The planned fix is the curtain, aiming for 0.
- With the lights on, a hand partly shading the sensor moved the reading from 3.96 to about 3.3 lux; a hand mostly covering it gave 0.45 to 0.57 lux (light leaks around fingers, so that is not the sensor floor).
- Steps of about 0.11 lux (3.96, 3.85, 3.74) confirm the MTreg 254 setting is applied.

Not done yet: a fully covered-sensor floor check, calibration against a reference lux meter, and a test with the moonlight LED.

## Pi status LEDs

LEDs near the sensor add to the reading. Unverified on this Pi:

- ACT (green): software-controllable.
  ```bash
  ls /sys/class/leds                                   # ACT (older kernels: led0)
  echo none | sudo tee /sys/class/leds/ACT/trigger
  echo 0 | sudo tee /sys/class/leds/ACT/brightness     # if it stays lit, use 1
  ```
  To persist, add to `/boot/firmware/config.txt` and reboot: `dtparam=act_led_trigger=none` and `dtparam=act_led_activelow=off` (if it relights, try `act_led_activelow=on`).
- PWR (red): on the original Model B it is hardwired to the 3.3 V rail and cannot be turned off in software. Cover it.
- Ethernet jack LEDs: run by the network chip; cover them.
- Also cover or keep out of view: USB dongle LEDs, any other controller's status LEDs, the router.
- Test: with the Pi and everything else powered and the room dark, the sensor should read 0.

## reef-pi on the Pi

reef-pi can run on the same Pi and log the lux reading on its own, independent of this controller. It does not replace the conductor.

- Service: `reef-pi` (systemd, enabled; running since 2026-10-01 when checked). Binary `/usr/bin/reef-pi`, config `/etc/reef-pi/config.yaml`, data `/var/lib/reef-pi`.
- UI: `http://192.0.2.100:80`. Browsers can silently upgrade a bare address to https, which reef-pi does not serve; type `:80` explicitly. Default login is `reef-pi` / `reef-pi`: change it (`sudo reef-pi reset-password`).
- The installed version is not recorded. The UI files are dated 2021-08-21. Run `/usr/bin/reef-pi -version` (a flag, not a subcommand) and note it here. `reef-pi reset-password` resets the web login.

Upstream has no BH1750 or VEML7700 driver (searched `reef-pi/reef-pi` and `reef-pi/drivers` at reef-pi commit `26dab60`, 2026-06-05).

### Route A: file driver (works today)

1. Keep a file with the latest lux value fresh (`lux_to_file.py` did this on the old branch; `lux_logger.py` writes `lux_latest.txt` on the Pi).
2. Configuration, Drivers: add type `analog-file`, parameter `Path` = the file (for example `/home/pi/lux.txt`).
3. Configuration, Connectors, Analog Inputs: add one on that driver, pin 0.
4. Temperature: add a controller, pick that analog input as the sensor, enable it, and leave Control, Heater and Cooler unset. Leave Fahrenheit off.

Why the temperature module and not pH, from the source:
- Temperature (`controller/modules/temperature/control.go`): a read failure logs an error and raises an alert, and no value is recorded.
- pH (`controller/modules/ph/probe.go`): a read failure is returned as `0, nil`, which looks identical to total darkness. Avoid it.
- The analog-file driver returns the raw file value (`drivers/file/analog_driver.go`, `Value()`); `AnalogInputs.Read` calls that, so no calibration points are needed.

Cautions:
- The chart is titled and labelled as temperature, but the values are lux.
- If the script stops, reef-pi keeps reading the last value in the file. Check the script is running if the line goes flat.
- The sensor's alert thresholds fire at any time of day (including lights-on), so they cannot implement dark-window alerting. That belongs in the planned `darkness_controller.py`.

### Route B: native driver (optional, not started)

A driver is three files in `reef-pi/drivers` (`driver.go`, `channel.go`, `factory.go`, modelled on `sht3x`, an I2C analog-input sensor with an Address parameter) plus one registration line in `controller/device_manager/drivers/factories.go`. It would let the UI add the sensor directly with no helper script.

- Needs a custom reef-pi binary cross-compiled for the Pi 1 (`GOOS=linux GOARCH=arm GOARM=6`), built from the same release as the installed version, and rebuilt on upgrades.
- The UI would still show the value as a temperature (or pH); reef-pi has no lux module.
- The sandbox used for development could not download the Go toolchain, so the build is untested.

## Open items

- Record the reef-pi version.
- Cover-test the BH1750 floor; calibrate against a reference lux meter.
- Install the VEML7700 and compare it with the BH1750 in the same spot.
- Decide the dark-window lux threshold after the curtain is in (current proposal: below 0.5 lux).
- Disable or cover the Pi LEDs and confirm the effect.
- Write `darkness_controller.py` (dark-window logic, curtain, lux alert).
- Decide whether to build the native driver or keep the file route.
