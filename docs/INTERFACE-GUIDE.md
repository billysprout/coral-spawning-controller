# Interface guide: from pseudosync to the cue table to the plugs

Written 2026-10-30. Audience: whoever develops the cue table (`cue_params.json`,
`spawn_calendar.py`) and the pseudosync layer next, and the agents helping them.
It says what crosses each boundary between the layers, with the exact fields,
units and rules, so a change on one side can be checked against what the other
side needs. It ends with the places where the layers do not yet agree (section 10).

Where things live. Everything is on master since the plug-array conductor branch (`claude/pi-contingency-plan`) was merged on 2026-10-30: code in `scripts/` (the cue table generator, `tank_*.py`, the Pi scripts), docs in `docs/`, the calendar and the Okinawa light data in `data/`. The second checkout `anemones-pi` was retired on 2026-10-31: its live local data (`data/tuya/`, `data/tank_config.json`, the `tank_*` runtime files) was copied into this checkout's `data/` (all gitignored), the worktree removed and the old branch deleted locally (its tip is inside master; `origin/claude/pi-contingency-plan` is left alone until master is pushed).
File names below are repo-relative; bare module names (`tank_plan.py`, `conductor.py`) are in `scripts/`, bare doc names (`pi-contingency-plan.md`, `PI-SENSOR-SETUP.md`) are in `docs/`.

Labels: **Read** = checked in code or data while writing this; **Measured** =
produced by a run; **Assumed** = how it should be, not seen.

## 1. The chain

```
 reference data           pseudosync                cue table               plan                 conductor tick
 meta_table.csv   --->   sites / ladder /   --->   cue_params.json  --->   tank_plan.Plan  ---> intents -> tank_out -> plugs
 okinawa_light/          events / reference        spawn_calendar.py        (per minute)             |            |
 (signals)               (3 axes)                  -> calendar CSV                                  inputs      verify (tank_verify)
                                                                                                    (lux, temp)    |
                                                                                                    tank_status.json, tank_commands.jsonl
```

| Layer | Code and files | Produces | Consumed by |
|---|---|---|---|
| Reference signals | `data/meta_table.csv` (2020-2027, daily, Okinawa), `data/okinawa_light/*` | sun, moon, temperature and light series for the reference site | pseudosync, cue table checks, future `derive_plan` |
| Pseudosync | `pseudosync.py`, `pseudosync_events.py`, `PSEUDOSYNC-METHOD.md`, `data/pseudosync/*` | other sites' events and curves on Okinawa's axes | nothing at run time yet (section 5) |
| Cue table | `cue_params.json`, `cues.py`, `spawn_calendar.py` | `spawn_calendar_YYYY-MM-DD.csv` + `.meta.json` | `tank_plan.py` |
| Plan | `tank_plan.py` | `Plan`, one per minute | `tank_tick.py` |
| Arcs | `tank_arcs.py`, `arc_catalog.json`, `build_arc_catalog.py` | a recorded light arc, temperature arc and offset | `derive_plan()` stub (returns None today) |
| Sensors | `scripts/lux_logger.py`, `temp_logger.py`, `lux_server.py` (on the Pi), `tank_inputs.py` | `Reading`s | `tank_tick.py`, `tank_verify.py` |
| Plugs and outputs | `tank_plugs.py`, `tank_out.py`, `tank_heat.py`, `tank_cool.py`, `tank_verify.py`, `tank_health.py` | commands, verdicts, findings | `tank_status.json`, `tank_commands.jsonl`, alerts |

## 2. Conventions that cross every boundary

| Topic | Rule |
|---|---|
| Time zones | The conductor works in **UTC**. The tank is in **America/Los_Angeles** (`cue_params.json` `tank_site`); calendar `*_local` columns and `Plan.local` use it, `*_utc` columns drive scheduling, so a DST change cannot move an event. The reference site is Okinawa, **UTC+9 (JST)**, no DST: the daily and 10-minute files in `data/okinawa_light/` and the `oki_*` columns of the meta table are JST. Sensor timestamps are UTC with an explicit offset. **Read** |
| Dates | **Tank date D** = the local (Los Angeles) date. **Cue date C** = `D - offset_days(D)`: the date whose Okinawa sun is played on D. A calendar row is one NIGHT anchored on the evening of D: lighting columns describe D's lit day, dark/moon/watch/curtain columns describe the night that begins that evening. **Read** (`spawn_calendar.py` docstring) |
| Units | Degrees C in code, F in extra columns for people. Illumination and levels: percent in CSVs (`moon_illum_pct`), 0..1 fractions in `Plan` levels. Lux is horizontal illuminance. |
| Blanks | A blank interval means "none this night" (for example no moonrise). A zero-length interval is also "none". Never fill a gap with an interpolated value (`TANK-NOTES.md` data rule: no synthetic environmental data). |
| Booleans | `full_dark`, `any_sun`, `any_moon` mean different things in different layers. See section 10, item 2 before comparing them. |
| Labels | Every claim about data carries its basis: observed, computed, tank-programmed, planned (`arc_catalog.json` `basis`). |

## 3. The cue table

Three files that must agree: `cue_params.json` (inputs), `spawn_calendar_<start>.csv`
(the table), `spawn_calendar_<start>.meta.json` (provenance). **Read.**

### 3.1 `cue_params.json`

| Key | Meaning |
|---|---|
| `offset_schedule` | list of `{from, offset_days, ramp_days?}`; the season shift. Moves linearly over `ramp_days` (0 or absent = a step). Today `[{"from": "2026-11-01", "offset_days": 0}]` |
| `reference_site` | Okinawa 26.2 N, 127.7 E, UTC+9: whose sun and moon are used |
| `tank_site` | Los Angeles / San Diego, 34.0 N, -118.0, `America/Los_Angeles`: whose clock is used |
| `sst_fit` | one-harmonic fit of Okinawa SST: mean 24.837 C, k=1, cos -2.1738, sin -2.8300, period 365.25 d, rmse 0.626 C (leave-one-year-out 0.664) |
| `safety_band_c` | [23.0, 29.0]: setpoints are clamped into it. An 83 F (28.33 C) cap was committed on 2026-10-30 (cc6f981) for the lamp's heat and reverted to 29.0 the same day on the owner's decision; the fitted curve itself peaks at 28.41 C, so the cap clips nothing |
| `max_step_c_per_day` | 0.1: the daily target moves at most this per day |
| `season_gate_c` | 26.1: temperature gate for the watch season (not used by `watch` itself today) |
| `photoperiod` | `blue_before_sunrise_min` 0 (the 30 min lead was dropped 2026-10-30), `blue_after_sunset_min` 0 |
| `watch` | watch window: days after full 6..9, season 05-30 to 07-05 (JAZA dates +/- 3 days, Okinawa and S. gigantea only), start 120 min before sunset, end 180 min after |

### 3.2 How a row is built

* **Sun** events (sunrise, sunset, astro dusk/dawn) are computed at Okinawa for the cue date C, as whole minutes from Okinawa solar noon on C, then added to the tank's solar noon on D (NOAA equation of time at -118.0, rounded to a minute) and converted to Los Angeles wall time. DST changes the clock reading, not the light.
* **Moon** is never shifted. Rise and set are Okinawa's for the 24 h starting at Okinawa solar noon on the tank date D itself, again as minutes from solar noon. Phase (`moon_illum_pct`, `moon_age_d`, `days_after_full`) is taken at the middle of that same Okinawa night.
* **Temperature**: `raw_c` = SST fit at `doy - offset`; `clamped` = into the safety band; `target_c` = step-limited walk from the first `offset_schedule` date. Rows carry both C and F, plus `delta_f` (target minus raw) and `clamped` (true/false).
* **Lights**: `blue_on` = sunrise - 30 min, `white_on` = sunrise, `white_off` = `blue_off` = sunset. `moon_on/off` (and `moon2_*` for a second interval) = moon above the horizon while the sun is below it, clipped to [sunset, next sunrise].
* **Dark**: `dark_*` is the first moon-down block from sunset (the Lin et al. 2021 framing, conflict X010); `astro_dark_*` and `astro_dark2_*` are the one `full_dark` definition (astronomical dusk to moonrise, then moonset to astronomical dawn; the second window is blank on most nights); `cue_dark_min` is sunset to first moonrise.
* **Curtain**: closes at sunset, opens at next morning's `blue_on`. **Watch**: cue date inside the season and `days_after_full` in [6, 9].

### 3.3 Columns the conductor reads

`tank_plan.py` uses: `date`, `target_c`, `offset_days`, `cue_date`, `moon_illum_pct`,
and the `*_utc` interval pairs below. Everything else (F columns, `*_local`, minutes)
is for people. **Read**

| Kind | Columns | Becomes |
|---|---|---|
| white | `white_on_utc`, `white_off_utc` | `white_on` |
| blue | `blue_on_utc`, `blue_off_utc` | `blue_on` |
| moon, moon2 | `moon_on_utc`/`moon_off_utc`, `moon2_*` | `moon_on`, `any_moon` |
| astro_dark, astro_dark2 | `astro_dark_start_utc`/`astro_dark_end_utc`, `astro_dark2_*` | `full_dark`: inside either window, with no lamp or moon interval open |
| dark | `dark_start_utc`, `dark_end_utc` | nothing (the sunset-based Lin et al. block, kept as a column for people) |
| watch | `watch_start_utc`, `watch_end_utc` | `watch` |
| curtain | `curtain_close_utc`, `curtain_open_utc` | `curtain_closed` |

### 3.4 What the loader checks (`tank_plan.load_calendar`)

Problems become `calendar:<problem>` findings (alerts); the conductor keeps following the calendar and raises the alarm, it never silently changes the offset.

* `calendar_meta_missing` / `calendar_meta_unreadable`, `calendar_csv_hash_mismatch` (CSV edited after generation), `calendar_params_stale` (`cue_params.json` changed after generation: regenerate), `calendar_empty`
* `calendar_bad_interval:<kind>:<date>` (end before start), `calendar_overlap:<kind>:<d1>/<d2>`
* At run time: `calendar_exhausted` (no row for today), `calendar_low` (fewer than `limits.calendar_low_days` = 30 days left), and `plan:<violation>` findings for the violations in section 4.

Today's file: `spawn_calendar_2026-11-01.csv`, 365 rows (2026-11-01 to 2027-10-31), `offset_days` -208 on every row (since 2026-11-03: tank 2026-11-01 plays Okinawa 2027-05-28 and 2026-11-03 plays 2027-05-30, three days before the JAZA spawn window opens on 2 Jun; it was -206 until then; the three watch nights fall on 2026-11-30 to 2026-12-02), `generator_version` 1.1. **Read**

## 4. The plan

`tank_plan.plan_at(calendar, plan_config, when)` returns a `Plan`, or None when the calendar has no row for the local date. **Read**

| Field | Type | Meaning |
|---|---|---|
| `t_utc`, `local`, `tank_date` | text | the instant (rounded down to the minute), in UTC and Los Angeles time; the local date |
| `offset_days`, `cue_date` | number, date | copied from the row |
| `temp_daily_c` | C | the row's `target_c` |
| `temp_hourly_c` | C | daily target + hourly offset (linear between hour marks), clamped to the safety band. Offsets default to 24 zeros: **flat** until a measured diel profile exists |
| `full_dark` | bool | astronomical dusk to moonrise, then moonset to astronomical dawn; the lit tank is never full dark |
| `any_sun` | bool | white **or** blue lamp window is open (so it starts 30 min before sunrise) |
| `any_moon` | bool | inside a `moon` or `moon2` interval |
| `white_on`, `blue_on`, `moon_on` | bool | channel windows |
| `white_plus_on`, `white_plus_level` | bool, 0..1 | the second light step (channel `white_plus`, the 120 W Halo). The simulated day brightness is a half sine over the lit window, `sin(pi * fraction)`, 0 at sunrise and sunset and 1 at the middle; `white_plus_on` is true while it is at or above `plan.white_plus.threshold` (default 0.5), so the plug is on for the brighter middle of the day and the window grows and shrinks with day length. `white_plus_level` is the curve value. On/off plugs cannot dim, so this is the step approximation of the curve |
| `watch`, `curtain_closed` | bool | |
| `white_level`, `blue_level` | 0..max | ramp inside the window: `curves.<ch>` `ramp_up_min` 30, `ramp_down_min` 30, `shape` smoothstep (provisional) |
| `moon_illum_pct` | percent or None | the row's illumination while the moon is on |
| `moon_target_lux` | lux or None | `lo + (hi - lo) * illum / 100` with `moon_lux_range` [0.1, 0.3] (provisional) |
| `violations` | list | `sun_during_full_dark`, `moon_during_full_dark`, `temp_outside_safety_band` |

`plan_config` keys: `dark_source` (only `astro` is accepted; kept so old configs load), `hourly_offsets_c` (24 values, each within `max_hourly_offset_c` = 1.0), `moon_lux_range`, `curves`. `tank_plan.day_events()` lists every minute a boolean flips in a local day; `python scripts/conductor.py plan --date 2026-11-02` prints it.

## 5. Pseudosync

Method and decisions: `PSEUDOSYNC-METHOD.md` (v1 draft). In one line: an event has three coordinates that obey different physics, so each gets its own rule. **Read**

| Axis | Column | Rule |
|---|---|---|
| Season | `season_axis_doy` | native day + `season_shift_days` (the ladder) |
| Lunar | `days_after_full_at_sunset`, `nafm_computed` | not shifted; computed from the true moon at the site (tanks with a programmed moon use their own full-moon dates) |
| Night | `tas_min` | minutes after local sunset; not shifted |

Season ladder (thresholds provisional): Okinawa 0; northern 0 (quality good at |lat| 20 or more, low at 10 to 20); southern +182.6 d (medium, low in the tropics); |lat| under 10 `unaligned`; a tank running its own cue programme keeps its own offset. Where an annual temperature curve exists (Davies Reef only) the ladder also records a **band**: the primary shift plus two thermal alternatives.

Tables (`data/pseudosync/`):

| File | Key columns |
|---|---|
| `sites.csv` | `site_id, name, lat, lon, tz_offset_h, coord_basis, climate_ref, note` (13 sites) |
| `sources.csv` | `source_id, description, site, shift_months, clock_shift_hours, basis, flag, site_id, align_method, season_shift_days` |
| `pseudosync_ladder.csv` | `site_id, lat, align_method, season_shift_days, shift_lo_days, shift_hi_days, quality, basis` |
| `pseudosync_events.csv` | `event_id, event_kind, record_id, site_id, species, setting, native_date, native_doy, align_method, season_quality, season_shift_days, season_axis_doy, season_axis_md, ..., days_after_full_at_sunset, nafm_computed, nafm_reported, lunar_basis, full_moon_utc, sunset_local, clock_local, tas_min, tas_basis, temp_c, oisst_c, claim_ids, note` |
| `pseudosync_reference.csv` | `source_id, site, native_year, native_month, shift_months, shifted_month, variable, value, unit, basis` (monthly rows: Craggs temperatures and sun/moon times; Dryad temperature profiles) |
| `pseudosync_checks.csv` | `check_id, what, value, expected, verdict` |

**What the cue table takes from pseudosync today: nothing at run time.** The calendar's season axis is the single stepped `offset_schedule` in `cue_params.json` (0 now); the ladder's `season_shift_days` and bands are not read by `cues.py`. The lunar and night axes are consistent with the method (the calendar's moon is the true Okinawa moon on the tank date, unshifted; its dark interval is measured from sunset), so the calendar is, in effect, the pseudosync rule applied to one target. What would change for another source site is the season shift and the temperature curve, not the moon. See section 9 for how to connect them.

## 6. Arcs: the recorded choice and the unbuilt calculation

`arc_catalog.json` lists 17 arcs (9 temperature, 8 light), each with location, granularity, sample count, span, sources, `basis`, and a `ref` saying which file and columns hold it. `python conductor.py onboard` records a **selection** in `<data>/arc_selection.json`:

```
{"temp_arc": "<id>", "light_arc": "<id>", "offset_days": <number>, "chosen_utc": "..."}
```

`offset_days` has the calendar's meaning: the arc day played on tank day D is `D - offset_days`. The choice is read every tick and recorded under `arcs` in `tank_status.json` with `driving_plan: false`.

**The stub's contract.** `tank_arcs.derive_plan(catalog, selection, when, params=None)` returns either the fields of a `Plan` or `None`; `None` means "no opinion, follow the calendar". Rules for whoever builds it: use each row's `ref`; shift by the three axes, not one calendar shift; a gap in an arc is a gap in the plan; label what is computed. Notes on what the arcs contain:

* One temperature arc plus one light arc, as chosen; **a light arc is not complete on its own**. The light rows hold different signals (astronomical times, lamp on/off, brightness percent, moon level); none holds all of the booleans, the sun curve and the moon target. The decided direction is to add computed signals per location so a pattern is signal-complete, then list those options at onboarding, before building the dynamic composition.
* Monthly arcs (4 temperature, 3 light, all non-Okinawa) are too coarse to drive a daily plan without a stated modelling decision.

## 7. Okinawa light signals

`data/okinawa_light/` (master, uncommitted at the time of writing; README there). Computed for 26.2 N 127.7 E with the same sun and moon code as `dark_window.py`. **Measured** against USNO: sunrise, sunset, moonrise and moonset within 1 minute on 1,165 days.

| File | Content |
|---|---|
| `okinawa_light_clearsky_daily.csv` | 2,922 local days, JST: sunrise, sunset, astro dawn/dusk, moonrise, moonset (blank = none that day), noon altitude, minutes of day, twilight, astro dark, full dark and moon-up, moon illumination and age, peak sun lux, daily PAR (kWh/m2), peak moon lux on dark nights |
| `okinawa_light_clearsky_spawn_year_10min.csv` | the calendar year every 10 min (JST): sun/moon altitude, illumination, `any_sun`, `any_moon`, `full_dark`, sun/moon/total lux, clear sky |
| `okinawa_lux_by_sun_elevation.csv`, `okinawa_lux_by_moon_phase_altitude.csv` | the two curves as lookup tables |
| `okinawa_cloud_daily.csv`, `okinawa_cloud_climatology_monthly.csv`, `okinawa_cloud_filter_report.json` | daily cloud transmittance (NASA POWER all-sky over clear-sky PAR, 2,403 days) and its predictors; the monthly climatology for dates with no observation |
| `okinawa_water_attenuation_monthly.csv` | offshore K490 and PAR left at 1 to 20 m |
| `okinawa_light_model_params.json`, `okinawa_light_checks.json`, `AUDIT.md`, `audit_flags.csv` | fit constants, checks, the statistical audit |

Importable model: `scripts/okinawa_light_model.py` (`lux_now(jd)`, `sun_lux(h)`, `moon_lux(...)`). Brightness basis: the daytime sun is fitted to NASA POWER clear-sky PAR (3.7% rms over 12 monthly means, day-by-day about 7%); the twilight tail and the moon formulas are not instrument-calibrated; cloud is the largest uncertainty (daily sd 0.24). Audit open items are in that folder's README.

## 8. Sensors and plugs

### 8.1 Readings

`tank_inputs.Reading(name, ok, value, ts, detail, configured)`; `age_s(now)`. A source never raises: a failure is `ok=False` with a `detail`. A `none` source is `configured=False` and is not an alarm. Drivers: `ds18b20_sysfs` (a probe on the machine running the conductor), `jsonl` (newest line of a file), `http_json` (GET a URL that returns one object). Both bridge drivers take `{"ts": ISO8601 with zone, "<field>": number}`, apply `offset_c` (the calibration offset belongs here, in the conductor's config, never on the Pi), reject a missing or zone-less `ts` and a non-finite value. Staleness: `limits.input_max_age_s` = 180.

Pi side (**Read**, see `PI-SENSOR-SETUP.md`: `lux_logger.py` (BH1750, finds its I2C bus and address, auto-ranges, writes `/home/pi/lux.jsonl` and a bare-number file for reef-pi), `temp_logger.py` (DS18B20 found by `28-` ID, not pin; drops bad CRC, 85.0 and -127), `lux_server.py` (serves `GET /lux`, `/temp`, `/health` on port 8099, LAN only, with `age_s`; 503 when there is no reading). Conductor config: `"lux": {"driver": "http_json", "url": "http://192.0.2.100:8099/lux", "field": "lux"}`.

### 8.2 Plugs

`tank_plugs.Plug` has two methods: `status() -> PlugReading` (never commands) and `set_on(on, revert_after_s=None)` (raises `PlugError`; the caller reads back). `PlugReading(name, ok, on, power_w, detail, revert_remaining_s, latency_ms, error, dps, configured)`.

| Concept | Rule |
|---|---|
| Array files | `data/tuya/plug_array.json` (tinytuya plugs; `devices.json` holds the local keys and is never printed) then optional `extra_plugs.json`; names unique across both |
| Drivers | `tinytuya`, `fake`, `tbd` (placeholder: `configured=False`, no alarm; an unknown driver name does alarm). New brand: one subclass with `@register("name")` implementing the two methods |
| Roles and channels | `role` is `white`/`white_plus`/`blue`/`moon` (a lighting channel; `white-plus` is accepted), `heater`, or free text; `follows` can bind a plug to a channel. Only channel plugs, the one `heater` plug, and plugs named in `cooling.plugs` are ever commanded; everything else is observed |
| Options | `expect_w [min, max]`, `off_max_w` (default 1.0) for the watts check; `revert_to_on` (default true for the heater) for the countdown warning; tinytuya: `id, ip, version, power_dp, watt_dp, watt_scale, revert_dp` |
| KMC 30153 (plug-8) | DP 1 switch, DP 9 countdown, DP 38 power-on state (`off`: after a power cut it stays off). **The DP 9 reading holds its set value until it fires, then reads 0; it does not count down, and there is no watts data point.** **Measured** |

## 9. Conductor, actuator, verification, records

Per-minute tick (`tank_tick.Tank.tick(when)`): plan -> read inputs and plugs -> intents (`desired_on` per plug from the plan) -> findings -> actuator -> verify -> record. Outputs are off until a human sets `outputs.enabled` and a channel true in `tank_config.json`.

| Stage | Rule |
|---|---|
| Intent | per plug: `observe_only` (no channel), `unknown` (plug not reachable), `none` (already right), `would_turn_on` / `would_turn_off` |
| Action (`tank_out`) | light plugs follow the plan; manual changes are respected for `override_hold_min` (30) except a lit white/blue plug during full dark; at most `max_commands_per_hour` (20), at least `min_command_interval_s` (30) apart; every command is read back and audited. The heater gate is only ever switched OFF when `countdown_verified`, `inkbird_resume_verified` and `limits.temp_calibrated` are all true; its plug-side countdown is refreshed by the Pi's own clock |
| Cooling | `outputs.cooling`: over-temperature cutoff for listed plugs; fan driver is a stub |
| Verify (`tank_verify`) | each command that read back leaves an expectation (saved in `tank_verify.json`); judged on later ticks from watts and, for channels with `outputs.verify.channels.<ch>.min_lux_change`, the lux sensor. Verdicts: `confirmed`, `failed` (alert `effect_failed:<plug>`), `plug_only`, `plug_changed`, `superseded`. `python conductor.py measure-lamp --plug NAME --yes` measures the lamp's change at the sensor and suggests the threshold |
| Findings | `{key, level, message, alert}`; `alert: true` raises a notification (cooldown `alert_cooldown_min` 30); warn-only checks never switch anything |

Files written to the data folder: `tank_status.json` (latest record), `tank_events.jsonl` (full records on change and hourly), `tank_inputs-YYYYMMDD.jsonl` (one compact line per tick), `tank_commands.jsonl` (commands, `override_detected`, `verify` events), `tank_verify.json`, `tank_setpoint.json`, `tank_overtemp.json`, `arc_selection.json`.

The record (`tank_status.json`) has: `ts, plan, inputs, temp_error_c, plugs, plug_health, arcs, intents, findings, alerts_sent, commands, heater, cooling, verify, executed`.

Operator commands: `conductor.py tick | loop | plan --date D | health [--plug N] | switch --plug N on|off [--revert-after S] --yes | verify-countdown --plug N --yes | measure-lamp --plug N --yes | onboard`.

## 10. Where the layers do not yet agree

Numbered so they can be cited. Each says what is known, not what to do.

1. **Temperature target against the alarm band. RESOLVED 2026-10-30.** The safety band top is 29.0 C (owner's decision, after an 83 F cap was tried and reverted the same day) and `limits.alarm_c` is [22.78, 28.89] (was 27.78, then 28.5; raised to the Inkbird's 84 F alarm on 2026-10-31, with the heat cut-off at 83 F, fans by 83.5 F and a "still warming" alert: pi-contingency-plan.md 4.3b). The fitted curve peaks at 28.41 C, so a tank following the calendar does not alarm and the cooling cutoff (set point + 1.0 C, never above the alarm ceiling) stays above the target; on the warmest days the margin to the ceiling is only 0.09 C (`cooling.limited_by_alarm` in the record). 120 days still sit on the 23.0 C floor (`clamped` is true). The Inkbird app's high alarm (was 82 F) was raised by hand to 83 F on 2026-10-30 (user-reported; the device cannot be read from here). The safety band and the alarm band are still two numbers in two files (`cue_params.json`, `tank_tick.py` defaults): keep alarm top above the band top.
2. **Three meanings of the booleans.**
   * Plan `any_sun` = a lamp window is open: starts 30 min before sunrise, ends at sunset.
   * `data/okinawa_light` `any_sun` = sun centre above -0.833 deg (geometric sunrise to sunset).
   * Plan `full_dark` (default `astro`) = night, minus the moon, minus lit lamps: ends at first light (blue_on), up to about 50 min before astronomical dawn on some nights. `okinawa_light` `full_dark` = sun below -18 deg and moon below its horizon.
   They agree except at the edges. Decided 2026-11-13: the plan's meanings serve the conductor, alerts and the tracking page; the `okinawa_light` meanings serve research tables and the Spawning Array.
3. **Moon brightness.** The plan scales `moon_lux_range` [0.1, 0.3] linearly with illumination percent (a half moon asks for 0.2 lux). The phase law used in `okinawa_light` gives about 0.2 to 0.25 lux for a full moon overhead and roughly a tenth of that at the quarter. The plan's range is marked provisional. The moon is a lamp to be set by the closed loop on the lux sensor once the moonlight rig exists; its target and the sensor's floor (about 0.11 lux per step for the BH1750) both matter.
4. **Season axis.** The cue table has one stepped offset; pseudosync has a per-site ladder with bands and a still-open choice between a flat flip (182.6 d, matches photoperiod) and a thermal lag (201 to 221 d, matches temperature). `cues.py` reads only `offset_schedule`. Changing the offset mid-year moves the sun by days at once, so use `ramp_days`.
5. **Temperature curve.** The calendar uses a one-harmonic fit of Okinawa SST (rmse 0.63 C). The reference temperature series available are the JAZA composite (outdoor tank), NOAA CRW SST, JMA air, Davies Reef (monthly, 1998 to 2015) and the SeaSim tank (monthly). Temperature sources are also catalogued in `data/temperature_sources.csv` and `data/temperature_abstraction_key.csv` (uncommitted on master; not reviewed for this guide).
6. **Hourly temperature profile is flat** (no measured diel pattern; data rule).
7. **Brightness curves.** The plan ramps lamps with a 30 min smoothstep. `okinawa_light` has a computed clear-sky curve (Okinawa twilight from sunset to -18 deg lasts about 80 to 90 min each side, by season) and a cloud factor; neither is used by the plan.
8. **Coarse arcs.** Monthly arcs cannot drive a daily plan without a modelling decision (the data rule forbids interpolating across them unannounced).
9. **`derive_plan()` is a stub.** The selection is recorded and ignored.
10. **Uncalibrated inputs.** `limits.temp_calibrated` is false (no probe is wired yet); `min_lux_change` is unset; the dark-leak limit (`dark_lux_max` 0.5 lux) has had one reading in a dark room (0.00 lux at 01:33 local on 2026-10-30), not a night's worth.
11. **Moon times in `meta_table.csv`.** Blank `oki_moonrise`, `oki_moonset` and `oki_dark1_*` cells are real nights with no such event, not gaps (checked on every row); an older note in `PSEUDOSYNC-METHOD.md` claimed a 23 minute disagreement with USNO. Rechecked 2026-10-30: it was a window-basis artifact. Putting USNO's events (`data/okinawa_light/raw/usno_okinawa_rstt_spawn_year.csv`) on the meta table's noon-to-noon window gives 352 nights within 1 minute (mean -0.5 min) with matching event counts; the local-day `okinawa_light` times also agree to 1 minute. RESOLVED. **Measured**

12. **Line endings are part of the hash.** `spawn_calendar_*.meta.json` records the sha256 of the CSV and of `cue_params.json` as LF bytes. Master now has a `.gitattributes` that pins those files to LF (merged 2026-10-30), so a fresh checkout holds the right bytes; the old CRLF mismatch (`f33dbb94d3` against `4482660ae7`) only affects a checkout made before that file existed. RESOLVED. **Measured**

## 11. Connecting pseudosync to the cue table

Two routes. They are not exclusive.

**A. Generate a calendar from pseudosync outputs (recommended for the first season).** Make the pseudosync layer emit the `cue_params.json` inputs it controls (the `offset_schedule`, and later the temperature curve) and let `spawn_calendar.py` produce the CSV as now. The conductor, its validators (hash, stale params, overlaps), the audit trail and all tests already understand that CSV; nothing downstream changes. Needed: a rule that turns a ladder rung (or a band) into `offset_days` for the chosen source site, and a way to carry a non-Okinawa temperature curve into the `sst_fit`/target step.

**B. Compute the plan at run time in `derive_plan()`.** Needed only for behaviour the calendar cannot hold: per-minute brightness from the computed lux curve, a cloud-aware moon, light arcs from several locations. It must return `Plan` fields that satisfy the invariants in section 4 (`violations` empty), may return `None`, and should write the same fields the CSV does so the two can be compared day by day. Keep the calendar as the fallback until the two agree on a trial year.

Whichever route, the checks to run after a change:

```
python spawn_calendar.py --start 2026-11-01 --days 365 --out spawn_calendar_2026-11-01.csv
python scripts/conductor.py plan --date 2026-11-02          # the day's flips
python -m unittest discover -s tests -p "test_*.py"    # the conductor tests
python scripts/okinawa_light_model.py check            # (master) sun/moon against USNO
```

## 12. Recipes

* **New plug brand.** Subclass `Plug`, implement `status()` and `set_on()`, register with `@register("name")`; list it in `extra_plugs.json` with `driver: "name"` (see `extra_plugs.example.json`). Add tests with the fake bus pattern in `tests/test_tank_array.py`.
* **New sensor on the Pi.** A logger that writes `{"ts", "<field>"}` lines; add a `--source name=path` to `lux_server.py`; point an `http_json` input at it. Remember that the Pi file is raw and the calibration offset goes in the conductor's config.
* **New calendar year.** Run `spawn_calendar.py` with the new start; keep the old file (the conductor takes the newest `spawn_calendar_*.csv`, or `TANK_CALENDAR`); generate it at least 30 days before the old one ends.
* **Change the offset.** Edit `offset_schedule` (add a `ramp_days` if the sun should move gradually), regenerate. The old calendar then reports `calendar_params_stale` until replaced.
* **Add an arc.** Add a row to `ARCS` in `build_arc_catalog.py` with its `ref`, rebuild the catalog, run `onboard --list` to see it.

## 13. File map

| Topic | Files |
|---|---|
| Cue table | `cue_params.json`, `cues.py`, `spawn_calendar.py`, `spawn_calendar_2026-11-01.csv` (+ `.meta.json`), `spawn-calendar-plan.md` |
| Plan and conductor | `tank_plan.py`, `tank_tick.py`, `conductor.py`, `tank_config.example.json` |
| Pseudosync | `PSEUDOSYNC-METHOD.md`, `pseudosync.py`, `pseudosync_events.py`, `data/pseudosync/*` |
| Reference signals | `data/meta_table.csv` (`meta_table.py`), `data/okinawa_light/*`, `scripts/okinawa_light_model.py`, `scripts/okinawa_light_fetch.py` |
| Arcs | `tank_arcs.py`, `arc_catalog.json`, `build_arc_catalog.py` |
| Sensors and Pi | `tank_inputs.py`, `scripts/lux_logger.py`, `temp_logger.py`, `lux_server.py`, `PI-SENSOR-SETUP.md` |
| Plugs and outputs | `tank_plugs.py`, `tank_out.py`, `tank_heat.py`, `tank_cool.py`, `tank_verify.py`, `tank_health.py`, `tank_lamp.py`, `pi-contingency-plan.md` |
