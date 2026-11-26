# Quickstart: from nothing to a running spawning controller

This guide takes you from an empty computer to a controller that follows a spawning cue calendar and a tracking page you check every day. No programming needed. You will type a few commands; each one is shown exactly.

**What it is.** A program that runs all the time on a computer next to your tank. Each minute it reads a cue calendar (temperature, daylight, moonlight and dark nights copied from a real reef's spawning season), reads your sensors, and switches your smart plugs to match. It sends you an alert when something is off.

**What it is not.** It is not life support. A heater, a return pump or a chiller must never depend on it. Keep a standalone heater thermostat with its own cutoff (see the safety rule at the end of [NEW-USER-HARDWARE.md](NEW-USER-HARDWARE.md)).

**It starts in watch-only mode.** Out of the box the controller plans and watches but switches nothing. You turn the outputs on yourself, one at a time, after a week of watching (step 7).

Words used below:

* **Terminal**: the window where you type commands (Windows: "Git Bash", installed with Git in step 3; Mac: "Terminal"; Linux: any terminal).
* **Docker**: a free program that runs the controller in sealed boxes called **containers**, so you do not have to install Python or anything else yourself.
* **Compose**: Docker's way of starting several containers together from one file (`docker-compose.yml`).
* **Loopback address** `127.0.0.1`: "this computer". The pages are only reachable from the computer they run on.

---

## 1. Get the hardware

The full shopping list and the "check before you buy" test are in **[NEW-USER-HARDWARE.md](NEW-USER-HARDWARE.md)**. In short:

1. **An always-on computer** (Windows 10 or 11, a Mac, or Linux) with about 4 GB of free memory and a wired or reliable Wi-Fi connection to the same network as your plugs. If it sleeps, the controller sleeps.
2. **About 4 Tuya smart plugs** (Smart Life app): lights, heater gate, a spare. Give each one a fixed address in your router ("DHCP reservation") so it keeps the same address.
3. **A temperature reading** the controller can see, and optionally a **light sensor**. No controller function needs a Raspberry Pi; it is one way to supply a reading. The options are listed under "Temperature and light sources" below. The first install used a Pi with a DS18B20 probe and a VEML7700 lux sensor: [PI-SENSOR-SETUP.md](PI-SENSOR-SETUP.md).
4. **A standalone heater thermostat** with its own cutoff. Not optional.

## 2. Install Docker

1. Download **Docker Desktop** from <https://www.docker.com/products/docker-desktop/> and install it (Windows and Mac). On Linux, install "Docker Engine" and the "compose plugin" from <https://docs.docker.com/engine/install/>.
2. Windows only: if it asks to install or update **WSL 2** (the Linux layer Docker uses), say yes and restart.
3. Open Docker Desktop, go to **Settings, General** and tick **Start Docker Desktop when you sign in**. Then the controller comes back by itself after a reboot (Windows still has to be signed in).
4. Optional: if your Docker Desktop has **Settings, Resources, Network, Enable host networking**, turn it on. It lets the controller hear the plugs announce themselves on the network. Without it the controller still reaches each plug by its address; the plug scan just reports `scan_blind`.
5. Check it works. In a terminal type:

   ```
   docker compose version
   ```

   You should see a version number. If you see "command not found", Docker is not installed or not running.

## 3. Get the code

1. Install **Git** from <https://git-scm.com/downloads> (on Windows this also installs **Git Bash**, the terminal used below).
2. Open a terminal in the folder where you want the project (Windows: right-click the folder, "Open Git Bash here").
3. Type, replacing the address with the one you were given:

   ```
   git clone https://github.com/billysprout/coral-spawning-controller anemones
   cd anemones
   ```

Every command from here on is typed inside that `anemones` folder.

## 4. Make your settings file (.env)

The settings file holds your alert tokens. It stays on your computer and is never uploaded.

```
cp .env.example .env
```

Then open `.env` in any text editor (Notepad is fine) and fill in what you use. Every line is explained inside the file. Summary:

| Setting | What it is | Needed? |
|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alerts to Telegram. Make a bot with @BotFather, send it one message, read your chat id (how: in `.env`) | Optional, recommended |
| `PUSHOVER_TOKEN`, `PUSHOVER_USER` | Alerts to the Pushover app | Optional |
| `NTFY_TOPIC` | Alerts to the free ntfy app. Use a long, hard-to-guess name | Optional |
| `TANK_DEADMAN_URL` | A ping address from a service such as healthchecks.io. If the pings stop, that service alerts you: the only alert that works when this computer is dead | Optional, recommended |
| `reeflive_token`, `TELEGRAM_FEED_CHAT_ID` | A daily noon message with the tank's numbers, on a second Telegram bot | Optional |
| `DATA_MANIFEST_MODE` | What happens when the audited cue files have changed: `warn` (start and alert, the default) or `enforce` (refuse to start) | Leave as `warn` |
| `LIFEGUARD_DEADMAN_URL` | A second ping address, for the lifeguard (the watchdog). You then also hear when the watchdog itself stops | Optional |
| `TANK_AMBIENT` | The outdoor-air recorder, fixed to the original owner's zip code | Leave at `0` |

With no alert channel at all the controller still runs, but nobody hears an alarm. Set at least one.

If you skip this step, `scripts/start.sh` makes the `.env` for you with everything off.

## 5. Start it (one command)

```
sh scripts/start.sh
```

The first run builds the containers (a few minutes), downloads the reef cue tables into `data/` (from [reef-cue-tables](https://github.com/realfakedoors-idaho/reef-cue-tables), set by `CUE_TABLES_URL` and `CUE_TABLES_REF` in `.env`; no account or token needed), and then starts exactly these:

| Container | What it does | Address |
|---|---|---|
| `tank-controller` | The controller. Watch-only until you switch outputs on | none |
| `tank-controller` (same container) | The tracking page, your everyday home | <http://127.0.0.1:8095/> |
| `tank-lifeguard` | The watchdog: alerts when the controller or the thermometers go quiet or the water passes the hard limits. Switches nothing until you arm safe mode (step 5 of the onboarding page) | none |
| `tank-autoheal` | Restarts the controller or the lifeguard if it hangs | none |
| `tank-setup` | The onboarding page | <http://127.0.0.1:8098/> |

The daily Telegram message runs inside `tank-controller` once its two `.env` lines are filled in.

**Without Git Bash** (for example in PowerShell), the same thing in three commands:

```
docker compose --profile setup build tank lifeguard setup
docker compose run --rm --no-deps tank python /app/scripts/cue_import.py ensure --data /data
docker run --rm -v "${PWD}:/repo" -w /repo anemones-setup python scripts/setup_web.py seed --from data --to staging/data
docker compose --profile setup up -d tank lifeguard autoheal setup
```

(The middle two commands are only needed once.)

## 6. Check it is running

```
sh scripts/start.sh status
```

After two or three minutes `tank-controller` should say `healthy`. `starting` means wait a little longer. `unhealthy` or `restarting` means go to [Troubleshooting](#troubleshooting).

## 7. Onboarding page: choose your reef and set up the plugs

Open <http://127.0.0.1:8098/>. It has five tabs, done in order:

1. **Locale and target**: pick a reef (each is marked ready or half-ready, with its caveats), the species, the temperature curve, and how many days before the first spawning season you are starting. It shows a preview before it writes anything.
2. **Plugs**: pair each plug in Smart Life, fetch the local keys from your Tuya cloud project, find the plugs on the network, name each one and give it a job (white light, blue light, moon, heater gate).
3. **Inkbird**: only for an Inkbird read through an Android emulator. That path is fragile and not recommended for new users (see [NEW-USER-HARDWARE.md](NEW-USER-HARDWARE.md)); skip the tab and pick a temperature source from "Temperature and light sources" below.
4. **Alerts**: the checklist for your Telegram bot and dead-man ping. The page runs without your `.env` values on purpose, so its live checks read empty; your alerts are tested from the controller once it is live.
5. **Safety and settings**: every fail-safe (alarm band, over-temperature cutoff, dark protection, each plug's power-on state, and so on), each with its current value and why. None counts as done until you confirm it.

### Important: the onboarding page works on a practice copy

Right now the onboarding page only writes to a **staging copy** of the data, the folder `staging/data`, never to the live `data` folder the controller reads. It also refuses to send any command to a real plug. This is deliberate: the page has been tested on a staging copy but not yet run against a live tank. So when you finish the tabs, nothing in the running controller has changed yet.

To make your choices live, copy them across by hand:

1. Stop the controller: `docker compose stop tank`
2. Back up the live folder first (see [Back up](#back-up)).
3. Copy these files from `staging/data` to `data` (same names, same sub-folders):
   * `setup.json` (it records which calendar is active)
   * `cue_params.json`
   * your new calendar, `spawn_calendar_<start date>_<reef>.csv` **and** its `.meta.json`
   * `tank_config.json`
   * `tuya/plug_array.json` and `tuya/devices.json` (your plugs and their keys; open `plug_array.json` and check no plug says `"driver": "fake"`)
4. Do not delete or edit the other `spawn_calendar_*` files already in `data`: they are checked at start (see `data_manifest_mismatch` below).
5. Start the controller again: `docker compose start tank`
6. Open the tracking page. Its header should now name your calendar and reef.

The outputs are still off at this point. Leave them off for at least a week and read the tracking page each day: does the plan match what your lights actually do? Then switch outputs on one channel at a time, lights first, heater gate last. The step-by-step order (with the tests the heater gate needs) is in [TURNING-OUTPUTS-ON.md](TURNING-OUTPUTS-ON.md).

## 8. Tracking page: your everyday home

Open <http://127.0.0.1:8095/> and bookmark it. It shows:

* a **health card** at the top: all clear, or the few things that need you. It also reports the lifeguard (the independent watchdog) when it is not running, silent, in caution or worse, or in safe mode, and a calendar with under 30 days left;
* three tiles with one number each: the **water** temperature (the target and thermometer count appear only when something is off), which **lights** are on (the plan appears only when it disagrees), and the **next watch night**;
* four folds (the page remembers which you opened, per browser): **Last 24 hours** (open the first time; the plan against the tank, with a one-line "on plan" percentage), **Light** (the light sensor on a log scale and one lane per lamp, with the plan under each lane and red outlines where a lamp differed from it), **Problems** (only what is not fine: lifeguard conditions, safety layers that are not GO, and problem events; it opens by itself when there is something to read) and **Cue year** (the planned year with the tank's daily mean laid over it). All but the first start closed.

The calendar file, reef, curve and start date are on the setup page, not here. Temperatures are shown in °F.

### What the health card colours mean

| Colour | Heading | Meaning |
|---|---|---|
| Green | **All clear** | The controller is running, every plug answers, the calendar is fine |
| Amber (orange) | **n things need attention** | Look at it today, not an emergency: the tank is 2 °F or more off the plan, the over-temperature cutoff is not armed, a lamp check failed in the last 24 hours, the plan broke one of its own rules, or the controller raised a warning |
| Red | **n things need attention** | Act now: the controller has stopped or is stuck (no status, or status older than 5 minutes), a plug is not answering, the over-temperature cutoff has tripped, the calendar has a problem (this includes `data_manifest_mismatch`), or the controller raised an alert |

The small grey line under the heading shows how old the controller's status is, how many plugs answer, and the tank and plan temperatures.

### When something is red

1. **Look at the tank first.** Temperature on your standalone thermometer, lights as expected, heater thermostat working.
2. **Read the red line on the card.** Each one names the problem; most are covered in [Troubleshooting](#troubleshooting) below.
3. **Over-temperature cutoff tripped**: the controller has switched the listed plugs off because the tank got too warm (never above 84 °F / 28.9 °C, the top of the default alarm band). Cool the tank; the plugs come back by themselves once it has cooled and stayed off for a while.
4. **Not sure?** Restart the controller (`docker compose restart tank`) and reload the page after two minutes. If it is still red, collect the last lines of its log (`docker compose logs --tail 100 tank`) and ask for help.

## 9. Stop, update, back up

### Stop

```
sh scripts/start.sh stop
```

This stops the controller and the pages; your data stays. Lamps stay in whatever state they were in at that moment. Start again with `sh scripts/start.sh`.

### Update

```
git pull
sh scripts/start.sh
```

It asks before restarting the running containers; answer `y`. If `git pull` refuses because a file in `data` changed, do not force it: back up `data` and ask for help.

### Back up

Everything that is yours lives in three places: the `data` folder, the `staging` folder, and the `.env` file. To back up, copy all three to a USB stick or a cloud folder, for example:

```
tar czf ~/anemones-backup-$(date +%Y-%m-%d).tgz data staging .env
```

(or just copy the folders in Explorer or Finder). They contain your plug keys and tokens: keep the backup private. To restore, stop the stack, copy them back into a fresh `anemones` folder, and start again. A weekly backup is a good habit.

---

## Temperature and light sources (is a Raspberry Pi needed?)

No. The controller follows the calendar, switches the lamps, keeps full-dark nights dark and arms each plug's own backstop timer using only the plugs and the calendar. Sensors add checks, and what is missing when you have none:

| You have | What works | What you lose |
|---|---|---|
| Plugs only (no temperature, no lux) | Calendar, lamps, dark protection, lamp backstop, tracking page | No temperature alerts; the over-temperature cutoff cannot act and the heater gate stays on (heat allowed); no light-leak or lux checks. Set `inputs.temp.driver` to `none` in `tank_config.json` so you get one warning instead of a standing `temp_missing` alert. Your standalone heater thermostat is then the only temperature protection. |
| A temperature feed | Everything above plus temperature alerts, the heater gate and the over-temperature cutoff | Lux checks, if no lux sensor |
| Temperature and lux feeds | Everything | |

Ways to supply a temperature reading, in `tank_config.json` under `inputs.temp`: `ds18b20_sysfs` (a DS18B20 probe wired to any Linux board, a Raspberry Pi is the usual one), `jsonl` (any program that appends a line `{"ts": ..., "temp_c": ...}` to a file), `http_json` (any device that answers a URL with JSON), or the Inkbird ITC-306A read through the Android emulator (fragile; see above). There is no driver for a Tuya thermometer.

A second probe (`probe_temp`) and a lux sensor (`lux`) are optional. If you have none, delete those inputs from `tank_config.json`, or leave them: a missing lux reading only warns.

---

## Try it with no hardware (practice stack)

You can run the whole controller on **fake plugs and dummy sensors** before you buy anything, or any time you want to try a setting safely. Nothing here can reach a real plug, a real tank or Telegram.

1. Make the settings file once (it can stay as it is, the practice stack needs no tokens): `cp .env.example .env`
2. Make the practice data. A fresh copy has no plugs of its own, so this uses the example plug list, turns every plug into a fake, gives the fakes the lamp roles (white, white-plus, blue, moon) and switches outputs on, for this practice copy only:

   ```
   python scripts/setup_web.py seed --sandbox --from data --to staging/sandbox
   ```

   No Python on your computer? Use the setup image instead, as in step 7 (`docker run ... anemones-setup python scripts/setup_web.py seed --sandbox ...`).
3. Start it. Always name the service (a bare `up` with the profile would also touch the real tank services). It is one container named `sandbox-tank` that runs the controller, the dummy sensors, the lifeguard and both pages:

   ```
   docker compose --profile sandbox up -d --build sandbox
   ```

4. Open the setup page <http://127.0.0.1:8102/> and the tracking page <http://127.0.0.1:8101/>. Within two minutes the fake lamps switch on and off to follow the calendar, and the dummy light sensor answers with a matching lux value.

To break things on purpose, set the fake plugs to a named fault (a plug that drops off Wi-Fi, a stuck relay, ...), then restart: `python scripts/setup_web.py scenario` lists them, `python scripts/setup_web.py scenario white_offline --to staging/sandbox` applies one, `docker restart sandbox-tank` loads it.

Things to know:

* **Start date.** The setup page's start date should be today. With a start in the future the controller has no calendar row for today and reports `no calendar row for today`; it begins following the calendar on the start date. The default lead time is long (up to 365 days), so the first watch night can be months after the start.
* **Second copy.** To run a second practice stack beside another one, set `SANDBOX_PREFIX`, `SANDBOX_SETUP_PORT` and `SANDBOX_TRACK_PORT` in `.env` (for example `SANDBOX_PREFIX=trial`, `SANDBOX_SETUP_PORT=8197`, `SANDBOX_TRACK_PORT=8196`); container names and ports then differ.
* **Restart after a calendar change.** After applying a calendar on the setup page, run `docker restart sandbox-tank` to be sure it is loaded.
* Stop and remove: `docker compose --profile sandbox down` (the data stays in `staging/sandbox`).

---

## Troubleshooting

### A page will not load

* Is Docker running? (Docker Desktop's whale icon in the taskbar or menu bar.) If not, start it and wait a minute.
* `sh scripts/start.sh status`: is `tank-controller` (it serves port 8095) or `tank-setup` (port 8098) listed as running? If not, run `sh scripts/start.sh` again.
* Use the exact address `http://127.0.0.1:8095/`, on the same computer. The pages refuse other addresses and other computers on purpose.
* Still blank? `docker compose logs --tail 50 track` (or `setup`) shows the error.

### "Controller status is N min old" (status stale)

The controller writes its status every minute; older than 5 minutes means it stopped or is stuck.

1. `sh scripts/start.sh status`. `restarting` usually means a mistake in `data/tank_config.json`; the log says which line: `docker compose logs --tail 50 tank`.
2. A hung controller is restarted by `tank-autoheal` about 8 minutes after its last good minute. To do it now: `docker compose restart tank`.
3. Was the computer asleep? Turn sleep off in the power settings.

### A plug is not answering

1. Is it plugged in, with its light on? Can the Smart Life app switch it?
2. Did its address change? Routers hand out new addresses unless you reserve one. Ask the controller to listen for it:

   ```
   docker compose exec tank python /app/scripts/conductor.py scan
   ```

   If it reports a new address, give the plug a DHCP reservation in your router, then run the same command with `--write` added to update `plug_array.json`, and `docker compose restart tank`.
3. One-shot check of every plug (reads only, switches nothing): `docker compose exec tank python /app/scripts/conductor.py health`
4. A plug that answers pings but not commands is "wedged": unplug it for ten seconds and plug it back in.

### data_manifest_mismatch alert

At start the controller checks the audited cue files (the calendars, `cue_params.json` and the reef cue tables) against the fingerprints in `data/data_manifest.json` (`scripts/data_manifest.py`). The alert means one of them changed or is missing since it was audited. With `DATA_MANIFEST_MODE=warn` the controller keeps running and keeps alerting; with `enforce` it will not start.

1. See which files: `cat data/data_manifest_check.json` (lists `changed` and `missing`).
2. **If you did not mean to change them**, put them back from the cue-table repo: `docker compose run --rm --no-deps tank python /app/scripts/cue_import.py fetch --force --data /data`, then `docker compose restart tank`.
3. **If you changed them on purpose**, re-audit and re-pin. Run the audit (it must report no defects), then pin, from the repo folder:

   ```
   docker run --rm -v "$(pwd -W 2>/dev/null || pwd):/repo" -w /repo anemones-tank sh -c "pip install -q astral==3.2 && python scripts/audit_cue_tables.py --out data/cue_audit_report.json"
   docker run --rm -v "$(pwd -W 2>/dev/null || pwd):/repo" -w /repo anemones-tank python scripts/data_manifest.py pin --audit-report data/cue_audit_report.json
   docker compose restart tank
   ```

   (Git Bash on Windows: put `MSYS_NO_PATHCONV=1 ` in front of each `docker run`.) The pin step refuses a report that has defects or bias flags. The method is in the cue-table repo's [AUDIT.md](https://github.com/realfakedoors-idaho/reef-cue-tables/blob/main/AUDIT.md). A calendar made by the onboarding page is a new file, not a pinned one, so it does not trip this check.

### Clock wrong (`clock_skew` warning, or times on the page are off)

The controller compares sensor times with this computer's clock and warns when they differ by more than 2 minutes.

* **Windows**: Settings, Time and language, Date and time: turn on **Set time automatically** and press **Sync now**. If the Windows Time service is stopped, that button does nothing; start the service ("Services", Windows Time, Start, set to Automatic).
* **Docker Desktop's clock** can drift after the computer sleeps. Restart Docker Desktop (Windows: `wsl --shutdown` in PowerShell, then start Docker Desktop again).
* **Raspberry Pi**: `timedatectl` on the Pi should say "System clock synchronized: yes".
* **Time zone**: the containers run on US Pacific time (it is set in the Dockerfiles). If you live elsewhere, times on the pages and in the logs are Pacific, not yours. See "Known limits" below.

---

## Known limits (be aware)

* **Time zone is fixed to US Pacific** in the images (`TZ=America/Los_Angeles` in `Dockerfile` and `Dockerfile.sandbox`).
* **The onboarding page writes only to the staging copy**; making it live is the manual copy in step 7.
* **`data` starts empty.** The cue tables are downloaded on first start; no logs or readings from any other tank are included.
* **Outputs start off.** Nothing is switched until you turn it on.
* Not started by `scripts/start.sh`: the Android/Inkbird emulator (`android` profile), the night camera recorder (`watch`) and the practice sandbox (`sandbox`). Inside the tank container, the outdoor-weather recorder is off with `TANK_AMBIENT=0` and the Pi log archive runs only when its ssh key exists.
* **The lifeguard runs on the same computer as the controller.** It catches a hung or crashed controller and a blind or hot tank; it cannot report a computer that is off. Only the off-site dead-man pings do that, so set them.

More detail: [INTERFACE-GUIDE.md](INTERFACE-GUIDE.md) (how the parts fit), [PI-SENSOR-SETUP.md](PI-SENSOR-SETUP.md) (sensors), [reef-cue-tables](https://github.com/realfakedoors-idaho/reef-cue-tables) (the cue tables, their format in SCHEMA.md and how they are checked in AUDIT.md), and the top of the project [README](../README.md).
