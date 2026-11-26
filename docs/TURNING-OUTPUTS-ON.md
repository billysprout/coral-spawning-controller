# Turning the outputs on

The controller starts in watch-only mode: it plans every minute and switches nothing. Turn the outputs on in this order, one step at a time. Everything below is in `data/tank_config.json`; `scripts/tank_config.example.json` shows every key, all off.

1. **Observe for at least a week** with `outputs.enabled` false. Read the tracking page and `tank_events.jsonl`: where do the planned lamp states disagree with how your lights actually run today?
2. **Calibrate the probe** and set `limits.temp_calibrated` true. Until then the controller can never block heat. Record the change in `data/calibration_events.json`.
3. **Assign the plug roles** in `tuya/plug_array.json` (`white`, `white_plus`, `blue`, `moon`, `heater` for the gate plug), or on the onboarding page's Plugs tab.
4. **Lights, one channel at a time.** Set `outputs.enabled` true and `channels.white` true. Watch `tank_commands.jsonl`. Switch the lamp by hand from the Smart Life app and confirm the controller puts it back on its next minute. Then `blue`, then `moon`.
5. **Heater gate** (a plug in front of your standalone heater thermostat; it can only block heat, never force it on).
   1. Pair the plug, set its driver to `tinytuya`, fill `id`, `ip` and `revert_dp`, and give it a DHCP reservation in your router. **`revert_dp` is model-specific** (the countdown data point: 9 on one common KMC plug, 11 on some "Smart Socket" models), so read it from the tinytuya wizard's `mapping` for the plug you actually use. **Set the plug's power-on state to ON** (often data point 38 `relay_status`: `power_on` / `power_off` / `last`; Smart Life shows it as the plug's power-on setting). With `last`, a power cut while the gate is off would bring the plug back off and leave the heater blocked. Test it: pull the plug out while it is off and plug it back in.
   2. Run `python scripts/conductor.py verify-countdown --plug <gate plug> --seconds 90 --yes` with a lamp (not the heater) on the plug. It must say PASS. Then set `countdown_verified` true.
   3. Thermostat power-return check: with the thermostat set the way it will actually run (heat-on point **above** the tank's current temperature) and the heater on it, switch the gate off for two minutes, switch it on, and confirm the thermostat resumes heating by itself with no button press. Then set `inkbird_resume_verified` true (the key name is historical; it applies to any thermostat).
   4. Set `inkbird_min_c` / `inkbird_max_c` to the thermostat's own band, then `heater.enabled` true.
   5. **Pull-the-plug test:** with the gate off (watch `tank_status.json`), cut the controller's power and time how long until the heater comes back. It should be about `revert_after_s`. Do not skip this one.
6. Restart the controller to apply (`docker compose restart tank`). It reads the config at start.

The heater must never depend on this controller: keep a standalone thermostat with its own cutoff (see the safety rule in [NEW-USER-HARDWARE.md](NEW-USER-HARDWARE.md)).
