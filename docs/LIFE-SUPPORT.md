# Life support: the tank's safety layers

Written 2026-11-15. What protects the animals when something fails, what each layer is copied from, how to arm it, and what it cannot do.

Everything here is **off or alert-only until you arm it**, like every other output in this project. With the defaults the controller behaves exactly as it did before these layers existed.

## The idea

One controller checking itself is not enough: when it hangs, goes blind or decides wrongly, the thing that should notice is the thing that failed. Systems that keep something alive add layers that do not depend on each other, and make each failure end in a state that cannot hurt. For this tank the harmless state is **lights off, heater left to its own thermostat**: the lights are the biggest heat source, darkness harms nothing, and the Inkbird holds the floor without any help from the PC.

## The layers

| # | Layer | Where | Copied from | Default |
|---|---|---|---|---|
| 1 | Thermometer voting and failover | `scripts/tank_vote.py`, in the controller's tick | Redundancy management in flight computers and reactor protection: mid-value select with three sensors, compare with two | Reported only; failover off |
| 2 | Over-temperature cutoff | `scripts/tank_cool.py` (already there) | A plain trip with hysteresis | As configured |
| 3 | Lamp backstop | `outputs.lamp_deadman` (already there): each lamp's own countdown | Dead-man's handle: the device fails to the safe state by itself | As configured |
| 4 | Independent watchdog | `scripts/lifeguard.py`, compose service `lifeguard` | The Space Shuttle's backup flight system: a fifth computer running a small, separately written program | On, alert-only |
| 5 | Safe mode | `scripts/tank_safe.py` (the latch), set by the lifeguard, obeyed by the controller | Spacecraft safe mode: stop the mission, hold a state that cannot hurt, wait | Off |
| 6 | One-out-of-N trip | In the lifeguard | Reactor protection: any healthy channel beyond the limit trips, confirmed over consecutive reads | On (alerts) |
| 7 | Caution and warning classes | Lifeguard status and the tracking page | The ISS classes: advisory, caution, warning, emergency | On |
| 8 | Alarm escalation | In the lifeguard | Hospital monitors: an unacknowledged alarm repeats | On, every 15 min |
| 9 | Cross-monitoring and two dead-man pings | Lifeguard watches the controller; controller watches the lifeguard; `TANK_DEADMAN_URL` and `LIFEGUARD_DEADMAN_URL` | Each computer monitored by another | On when the URLs are set |
| 10 | Go / no-go list | `lifeguard.py preflight`, the tracking page, the setup page's Launch check | The launch-control poll | On |
| 11 | Autoheal | compose `autoheal` (already there), now also for the lifeguard | Process supervisor | On |

### 1. Thermometer voting and failover

Channels: `inputs.temp` (the primary), `inputs.probe_temp`, `inputs.inkbird_temp`. A channel is healthy when it reads, is in range and is fresh.

* **Three healthy:** the middle value is the voted value, so one sensor failing high or low cannot move it. A channel more than `miscompare_c` from the middle is reported as outvoted.
* **Two healthy:** compared. Agreeing: the primary is used. Disagreeing: the primary is still used and `temp_miscompare` is reported, because with two nobody can say which is wrong.
* **One healthy:** that one. `temp_redundancy_lost` is reported.

Until 2026-11-15 the second probe was logged and never acted on: when the Inkbird feed went blind, the cutoff and the heater gate went blind with it. With `limits.redundancy.failover: true`, the backup becomes the reading the cutoff, the heater gate and the alarm band act on while the primary is unusable (alert `temp_failover`). With it off, the status says a backup is reading but idle (`temp_backup_idle`).

The vote is in every status record under `redundancy`.

### 4 to 9. The lifeguard

Its own container, its own loop, no import of the controller's planner or actuator. It shares the device drivers (`tank_plugs.py`, `tank_inputs.py`), the vote and the alert sender, and nothing else. Every minute it:

1. reads the age of `tank_status.json` (the controller's heartbeat);
2. reads every thermometer itself;
3. checks free disk space where the data lives;
4. classes what it found, alerts, writes `data/lifeguard/status.json` and pings its own dead-man URL.

| Condition | Class | When |
|---|---|---|
| `over_temperature`, `under_temperature` | EMERGENCY | Any healthy thermometer beyond the hard limits on `confirm_reads` consecutive passes. Hard limits: `limits.alarm_c` widened by `hard_margin_c` (72.1 to 84.9 F by default), or `hard_c` |
| `safe_mode_failed` | EMERGENCY | Safe mode could not switch a plug |
| `controller_silent` | WARNING | No controller status for `controller_silent_min` (10) |
| `blind` | WARNING | No usable thermometer for `blind_min` (30) |
| `safe_mode` | WARNING | The latch is set |
| `redundancy_lost`, `miscompare`, `outvoted:*`, `disk_low`, `config` | CAUTION | Shown, not sent |
| `layer:*` | ADVISORY | A layer in the go / no-go list is NO-GO |

WARNING and EMERGENCY are sent when they open, again every `repeat_min` until `lifeguard.py ack`, and once more when they clear.

The controller raises `lifeguard_silent` when the lifeguard's status is older than `limits.lifeguard_silent_min` (5). A lifeguard that never ran here raises nothing.

### 5. Safe mode

Armed with `lifeguard.safe_mode.enabled: true`. Trips on over-temperature and on a silent controller (each can be switched off).

* The lifeguard writes the latch `data/lifeguard/safe_mode.json`. A latch file that cannot be read counts as set.
* The controller, on its next tick, switches every lit lighting plug off and keeps it off: no hand-switch hold applies. The heater rules are untouched. Finding `safe_mode`.
* The lifeguard switches plugs itself only when the controller is silent, or has not put the lights out `direct_after_s` (150) after the latch. It never turns a light on and never switches the heater plug off. With the controller gone (and not on an over-temperature trip) it switches the heater plug **on**, so heat is left to the Inkbird.
* Release: `latch: "auto"` releases once the cause has stayed clear for `release_after_min` (15; over-temperature must also be `resume_band_c` back inside the limit). `latch: "manual"` waits for `lifeguard.py reset --yes`. A hand trip (`lifeguard.py trip --reason ... --yes`) is always released by hand.

Safe mode also covers the dimmers, which the lamp backstop does not (they have no countdown).

## Commands

```
docker compose up -d lifeguard
docker compose exec lifeguard python /app/scripts/lifeguard.py preflight
docker compose exec lifeguard python /app/scripts/lifeguard.py ack
docker compose exec lifeguard python /app/scripts/lifeguard.py trip --reason "tank maintenance" --yes
docker compose exec lifeguard python /app/scripts/lifeguard.py reset --yes
```

## Arming it

In `data/tank_config.json` (or step 5 of the setup page, group "Life support"):

```json
"limits": { "redundancy": { "failover": true } },
"lifeguard": { "safe_mode": { "enabled": true } }
```

The controller reads its config at start, so restart `tank` after changing `limits.redundancy`. The lifeguard re-reads the file every pass.

Suggested order: run the lifeguard alert-only for a few days and read what it reports; then failover; then safe mode, after one deliberate `trip` and `reset` at a time when dark lamps do no harm.

## What was checked, and how

2026-11-15, on fake plugs in a temporary data folder (a throwaway simulation, not a repo test; no test file was added): the vote in all five modes; a nominal pass; controller silent 11 min (latch, direct lights-off, heater gate on, pump untouched); no repeat before 15 min, repeat after, none after `ack`; release 5 min after the controller returned; one hot read not a trip, two a trip from the backup probe alone; the lifeguard not switching while the controller is alive; the controller obeying the latch, switching a hand-lit lamp off again and resuming the plan after reset; failover off leaving the cutoff blind and failover on tripping it from the backup. 27 checks, all passed. The repo suite passed after the changes (776 passed, 1 skipped). In the `sandbox` container (fake plugs, real processes): the controller, lifeguard and both pages started together with no false alarm from the stale status file; a hand `trip` was obeyed on the next tick (the lit moon dimmer commanded off with reason `safe_mode`, read back, and still off a tick later) and showed on the tracking page; `reset` released it.

**Not checked:** anything on real plugs or the real tank. Nothing here has been deployed to the owner's rig.

## What it cannot do

* **It cannot act when the PC is off.** The lifeguard runs on the same computer as the controller. A dead PC, a stopped Docker or a power cut is reported only by the off-site dead-man pings, and only the lamp backstop and the Inkbird act. A lifeguard on a second host (the Pi, or a microcontroller) is the next step.
* **It shares drivers with the controller.** A bug in `tank_plugs.py` or `tank_inputs.py` is common to both.
* **Two thermometers cannot outvote each other.** A third channel makes the vote real.
* **It cannot cool.** Lights off removes the largest heat source; a room hotter than the limit still wins.
* **It does not replace the hardware layers**: GFCI, drip loops, the Inkbird's own alarm, plug power-on defaults.
