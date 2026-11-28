# Coral Spawning Controller

An open, community project to get reef animals spawning in home aquaria. The
controller replays a real reef's spawning cues in your tank (seasonal
temperature, day length, moonlight and true after-sunset dark) from a cue
calendar. It plans each minute, switches smart plugs and dimmers with
read-back, reads temperature and light, and alerts your phone. With it come an
onboarding page (pick a reef, a species and a start point; set up the plugs and
the fail-safes) and a tracking page (where the tank is on its cue calendar,
with its own readings laid over the plan).

> **Unproven: this controller has not produced a spawn.** It runs on one tank
> so far, and whether replaying a reef's cues in an aquarium triggers spawning
> is still an open question. Treat it as an experiment, not a product. Help us
> prove or disprove the idea: every tank that runs it, every watch night logged
> (spawn or not) and every fix counts. **Pick a quest below.**

**Start here: [docs/QUICKSTART.md](docs/QUICKSTART.md).** Hardware list and the
"check before you buy" test: [docs/NEW-USER-HARDWARE.md](docs/NEW-USER-HARDWARE.md).

**It is not life support.** A heater, a return pump or a chiller must never
depend on it. Your heater keeps its own thermostat and its own high cutoff. The
controller starts in watch-only mode and switches nothing until you turn
outputs on ([docs/TURNING-OUTPUTS-ON.md](docs/TURNING-OUTPUTS-ON.md)).

**Watch the tank on spawn nights.** The tracking page counts down to each watch
night. Watching the animals yourself through the watch window is the main way
to catch a spawn; sensors are only a backup for one you miss or do not expect.

## Quests

Open work, biggest wins first in each line. Open an issue to claim one or to
ask, then send a pull request. Cue-data quests go to
[reef-cue-tables](https://github.com/realfakedoors-idaho/reef-cue-tables) instead.

1. **Harden the Inkbird path.** Reading and setting an Inkbird ITC-306A goes
   through its phone app in an Android emulator. It is the path of least
   resistance today, and fragile. Optimize, harden or refactor it (the emulator
   is not shipped here; the controller side is `scripts/tank_itc*.py` and
   `scripts/itc_setpoints.py`).
2. **Better cue tables.** Find new reefs or refine the existing tables: better
   temperature data, more dated spawning nights, better light data
   ([reef-cue-tables](https://github.com/realfakedoors-idaho/reef-cue-tables)).
3. **More life-support hardware.** Add drivers for non-Tuya plugs, Home
   Assistant and similar hubs, and other temperature controllers
   (`scripts/tank_plugs.py`, `scripts/tank_inputs.py`).
4. **reef-pi and the lifeguard in onboarding.** Make reef-pi (a separate open
   controller) and this repo's independent watchdog (`scripts/lifeguard.py`) a
   formal part of the onboarding page and the docs.
5. **A crowdsourcing framework** (larger project). Let hobbyists log their
   controller setup, schedules, errors caught and missed, and spawning events,
   with a way to audit and correct or hone the cue tables from what tanks
   report.
6. **A spawning competition.** Spread the word and get more tanks on a cue
   cycle.
7. **Jamie Craggs' aquarium spawns.** The Horniman Coral Spawning Project's
   dated spawns are not in the cue tables yet. Someone with lawful access to the
   paywalled papers can collect the facts (species, date, start time, citation;
   never the papers themselves) and add them to the cue tables, or ask Craggs
   for the data with permission to publish it under the tables' licence
   (CC BY-NC 4.0).
8. **A daily temperature pattern.** The hour-by-hour temperature offsets are
   flat zeros. Find a measured daily pattern on spawning reefs (reef loggers)
   to replace them.
9. **Make it affordable.** Run everything on a Raspberry Pi instead of an
   always-on computer. Find cheaper or discounted Tuya plugs, or a much cheaper
   non-Tuya route, including DIY plugs and relays on an ESP32 (for example
   ESPHome or Tasmota firmware). Grow the framework cheaply with add-on
   sensors: a turbidity sensor to catch a spawn haze (a $10 analog probe on an
   ADC), an infrared watch camera. Drive the lights people already own (Aqua
   Illumination, Kessil and similar dimmable lights) for sunrise, sunset and
   moonlight, instead of extra lamps on plugs.
10. **Turbidity as a spawn detector.** Build on or rethink a low-cost turbidity
    sensor that logs a spawn haze through the night: probe choice, placement,
    sampling, filtering bubbles, calibration, and telling a real release from
    drift. It is a data-driven backup for spawns that are missed or come
    unexpectedly, never a replacement for watching the tank. A starting design:
    a $10 analog probe (DFRobot SEN0189) on an ADS1115 ADC on a Pi, read once a
    second as relative transmission against clear RO water.
11. **Cheap, correct moonlight.** Reef moonlight is about 0.1 to 0.3 lux at the
    animal; a cheap LED strip on a plug easily runs several times brighter. Find
    common lights, dimmers, diffusers or resistors that hit the target, and a
    cheap way to check it at the animal (a lux sensor that reads that low, or a
    calibrated phone method).
12. **Summer cooling on a budget.** The cue calendar's warm peaks and summer
    rooms push tanks over their limit. Find cheap, reliable cooling: fans over
    the water, DIY chillers (pump, heatsink and fans), room-level fixes, and
    how the controller should drive and watch them (never as life support).
13. **Two-season and split spawning.** Some species spawn in two seasons about
    six months apart (Western Australia, the Maldives, Palau, Singapore); others
    split one season across two full moons in some years. The onboarding page
    labels each species' spawning pattern from the shipped windows; make it
    exact from the dated nights in [reef-cue-tables](https://github.com/realfakedoors-idaho/reef-cue-tables) (a pattern per
    species and reef, two windows where a reef has two seasons, a moon rule for
    split years), then let the controller plan two windows a year.
14. **A standard pull request format for cue data.** One format for changes to
    the cue tables and the captive and wild spawning records, in two strengths.
    **Adding** (loose): new spawning nights, new reefs, new aquarium spawns; a
    short template with the required facts (species, date, site, start time if
    known, captive or wild, citation, licence), the format check passing, and
    reviewers fixing small gaps. **Editing** (strict): any change to a value
    already there; one change per pull request, the old and new value, the
    reason and the source for each, the audit re-run and the data manifest
    re-pinned. Deliver it as pull request templates and an automatic check.

## Repository map

| Path | What it is |
|---|---|
| `scripts/start.sh` | The one command for users: start, stop, status, `telegram`, `golive`, `check` |
| `scripts/tank_supervisor.py` | Container entry point: imports the cue tables, checks them, runs the controller |
| `scripts/conductor.py`, `scripts/tank_*.py` | The controller: plan (`tank_plan.py`), inputs, plugs and outputs, lamps, heat and cooling, safe mode, health, alerts |
| `scripts/cues.py`, `scripts/spawn_calendar.py`, `scripts/tank_arcs.py` | Cue parameters, calendar building, the light and temperature curves |
| `scripts/lifeguard.py` | Independent watchdog: alerts when the controller or the thermometers go quiet |
| `scripts/setup_web.py`, `setup_core.py`, `setup_page.html` | Onboarding page (port 8098), works on a practice copy |
| `scripts/host_helper.py` | What the onboarding page cannot do itself: Telegram setup, go-live copy, health check (run by `start.sh`) |
| `scripts/track_web.py`, `track_core.py`, `track_page.html` | Tracking page (port 8095) |
| `scripts/cue_import.py`, `cue_schema.py`, `data_manifest.py`, `audit_cue_tables.py` | Cue-table import, format check, fingerprint check, audit |
| `scripts/sandbox_feed.py` | Practice stack: fake sensors and plugs for trying it with no hardware |
| `scripts/*.example.json` | Example tank config and plug lists |
| `docker-compose.yml` | Services: `tank`, `lifeguard`, `autoheal` by default; `setup`, `sandbox`, `watch`, `test` as profiles |
| `docs/` | Quickstart, hardware, life support, turning outputs on, lux sensors, Pi sensors, watch capture, interface guide |
| `tests/` | 22 test files |
| `data/` | The working folder (mounted at `/data`), empty in git; see `data/README.md` |

## Commands

```
sh scripts/start.sh             # build and start: controller, watchdog, onboarding page
sh scripts/start.sh status      # services and their health
sh scripts/start.sh telegram    # phone alerts: bot token (hidden), .env, restart, test alert
sh scripts/start.sh golive      # copy the onboarding page's practice choices to the live tank
sh scripts/start.sh check       # health check: cue data, controller, thermometer, alerts
sh scripts/start.sh stop        # stop (data is kept)
docker compose --profile test run --rm tests                # tests (import the cue tables first)
docker compose --profile sandbox up -d --build sandbox      # practice stack, no hardware
```

Pages: tracking <http://127.0.0.1:8095/>, onboarding <http://127.0.0.1:8098/>.
Needs only Docker; on Windows run `start.sh` from Git Bash.

## Where things live

| What | Where |
|---|---|
| Secrets (Telegram token, chat id, dead-man URL) | `.env` (from `.env.example`; gitignored, never shown on a page) |
| Tank settings | `data/tank_config.json` (from `scripts/tank_config.example.json`) |
| Plugs and their local keys | `data/tuya/plug_array.json`, `data/tuya/devices.json` |
| The calendar the tank follows | `data/spawn_calendar_*.csv`, chosen in `data/setup.json` |
| Cue tables | `data/locales/`, `data/reference/` and friends, installed from reef-cue-tables; `data/cue_tables.json` says which |
| Onboarding page's practice copy | `staging/data/` (`golive` copies it to `data/`, with a backup in `data/backups/`) |
| Logs and readings | `data/` (written by your tank, gitignored) |

## Cue tables

This repository holds no cue tables. They live in **[reef-cue-tables](https://github.com/realfakedoors-idaho/reef-cue-tables)**,
in the format that repository's `SCHEMA.md` defines (`index.json` with a schema
version, every file's kind, sha256 and install path). By default the controller
downloads them on its first start and installs them in `data/`:

```
CUE_TABLES_URL=https://github.com/realfakedoors-idaho/reef-cue-tables
CUE_TABLES_REF=main
CUE_TABLES_AUTO_IMPORT=1
```

Point `CUE_TABLES_URL` at a fork to use your own tables, pin `CUE_TABLES_REF`
to a tag for a fixed set, or import by hand:

```
python scripts/cue_import.py fetch                          # from CUE_TABLES_URL
python scripts/cue_import.py fetch --from-dir ../reef-cue-tables
python scripts/cue_import.py status
```

The import validates the whole repository first (schema version, every hash,
the columns each kind of table must have) and never overwrites a file you
changed unless you pass `--force`. `scripts/cue_schema.py validate <dir>` runs
the same check on a cue-table repository.

The spawning records the reef windows come from: the
[Spawning Array](https://realfakedoors-idaho.github.io/reef-cue-tables/).

## Notes for contributors and coding agents

* **Safety first.** Nothing here may become the only thing keeping an animal
  alive. Changes that switch plugs keep read-back, the lamp backstops
  (countdowns), safe mode and the watchdog working; `tests/` covers them.
* **Practice before live.** The onboarding page writes only to `staging/data`
  and refuses real plugs; only `start.sh golive` touches the live folder.
* **Units and time.** Files and code hold temperatures in Celsius; the pages
  show Fahrenheit. The tank's longitude sets its solar noon (4 minutes per
  degree); the time zone only formats clock times.
* **Cue data is checked.** `data_manifest.json` pins the audited cue files; a
  changed file raises `data_manifest_mismatch`. Change cue data in
  reef-cue-tables, not here.
* **External APIs.** Follow a posted rate limit, else at most 1 request per
  second; back off exponentially on every failure; stop after 3 failed
  requests in a row and report the host as unreachable.
* **Tests.** Import the cue tables first (several tests read them), then run
  the `test` profile.

Built from commit `059cc13` of the project repository.

## Known limits

* Validated on one tank only. No spawn has yet been produced by it.
* The onboarding page has been run end to end on a practice copy. The
  `golive` and `telegram` helpers are new.
* The containers run on Pacific time (`TZ` in `Dockerfile`); change it to yours.

## Licence

MIT (`LICENSE`). The cue tables have their own licence (CC BY-NC 4.0) in their
repository.
