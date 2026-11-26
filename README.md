# Coral Spawning Controller

A plug-array tank controller that replays a reef's spawning cues (seasonal
temperature, daylight, moonlight and true after-sunset dark) from a cue
calendar: it plans each minute, switches smart plugs and dimmers with read-back,
reads temperature and lux, and alerts. With it come the onboarding page (pick a
reef, a species and a start point; set up the plugs and the fail-safes) and the
tracking page (where the tank is on its cue calendar, with its own readings laid
over the plan).

**Start here: [docs/QUICKSTART.md](docs/QUICKSTART.md).** Hardware list and the
"check before you buy" test: [docs/NEW-USER-HARDWARE.md](docs/NEW-USER-HARDWARE.md).

**It is not life support.** A heater, a return pump or a chiller must never
depend on it. It starts in watch-only mode and switches nothing until you turn
outputs on ([docs/TURNING-OUTPUTS-ON.md](docs/TURNING-OUTPUTS-ON.md)).

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

## What is in it

* `scripts/`: the controller (`conductor.py`, `tank_*.py`), the independent
  watchdog (`lifeguard.py`), the onboarding page (`setup_*`), the tracking page
  (`track_*`), the practice sandbox feed, the watch-night recorder, the cue-table
  importer (`cue_import.py`, `cue_schema.py`) and the cue-data check
  (`data_manifest.py`).
* `docker-compose.yml`: `tank`, `lifeguard` and `autoheal` start by default;
  `setup`, `sandbox`, `watch` and `test` are profiles. `sh scripts/start.sh`
  starts what a new user needs.
* `tests/`: 22 test files. Import the cue tables first (several tests read
  them), then `docker compose --profile test run --rm tests`.

Built from commit `11bbbbc` of the project repository.

## Known limits

* The onboarding page works on a staging copy (`staging/data`) only; it has
  not been run against a live data folder and real plugs.
* The containers run on Pacific time (`TZ` in `Dockerfile`); change it to yours.
* Validated on one tank only. No spawn has yet been produced by it.

## Licence

MIT (`LICENSE`). The cue tables have their own licence (CC BY-NC 4.0) in their
repository.
