# Recommended hardware for new users

Recorded 2026-11-01. Revisit if a vendor changes its cloud or local-control options.

## Recommendation

Buy Tuya-based gear, not Inkbird-style devices.

- **Plugs:** about 4 Tuya plugs. One each for the heater gate, the lights, and cooling or other equipment, with one spare. Plug-array control uses Tuya's local protocol. Tested models are in the table below.
- **Heater:** a standalone heater thermostat with its own probe and its own high cutoff or alarm. The controller never replaces it.
- **Temperature for the controller:** a DS18B20 probe on a Raspberry Pi or any Linux board is the usual choice; anything that writes a reading to a file or answers a web address also works (QUICKSTART, "Temperature and light sources"). There is no driver for a Tuya thermometer.
- **Redundant probe (optional):** a second DS18B20 as a cross-check.
- **Computer:** any always-on Windows, Mac or Linux machine. Docker's default memory setting is plenty: the whole controller uses about 200 MB (measured: controller 46 MB, setup page 24 MB, watchdog 16 MB, practice sandbox 96 MB).

## Plugs and dimmers tested with step 2

What a job needs: **lamps** need a plug the controller can switch locally (Tuya protocol 3.3 to 3.5) and, for the lamp backstop, a countdown data point. The **heater gate** also needs a power-on setting that can be fixed to ON, so a power cut never leaves the heater blocked.

| Model (as sold) | Tested on | Local control | Countdown data point | Power-on setting | Good for |
|---|---|---|---|---|---|
| KMC 30153 smart plug | the first tank, Nov 2026 | yes (3.3) | 9 | data point 38 `relay_status` (`power_on` / `power_off` / `last`) | lamps, heater gate |
| "Smart Socket" SP10 (unbranded Tuya) | the first tank, Nov 2026 | yes | 11 | none found: set it in Smart Life if offered | lamps |
| Tuya "Dimmer Switch", 2 gangs (category `tgkg`) | the first tank, Nov 2026 | yes (3.5) | per gang | none | moon and dimmed lamps (some LED drivers buzz below about 90 percent) |

Only these have been tried. Any other Tuya plug should work if it passes the check below; read its data points from the tinytuya wizard's `mapping` (countdown and `relay_status`) and add your result to this table.

## Docker Desktop

- Windows and Mac: <https://www.docker.com/products/docker-desktop/>. Linux: Docker Engine and the compose plugin, <https://docs.docker.com/engine/install/>.
- Settings, General: tick **Start Docker Desktop when you sign in to your computer**, so the controller comes back after a restart.
- Settings, Resources: the default memory limit is fine. (The 12 GB once quoted here was for the Inkbird phone emulator, which this release does not include.)

## Why not Inkbird

Inkbird controllers such as the ITC-306A are Wi-Fi devices, but they run in Inkbird's own Tuya tenant. Our attempt to read one locally failed:

- The Tuya IoT wizard only returns local keys for devices bound to accounts you link to your project. The ITC-306A is bound to Inkbird's app account, so the wizard can't see it.
- Its device list from Inkbird's API has IDs and names, but no local key.
- Reading it through the app would mean scripting the Inkbird app on an emulator, which is fragile and needs the app to keep working on the same screens. We don't recommend that path to new users.

## Check before buying

Before buying a controller, confirm it can be used locally:

1. Pair it in the Smart Life or Tuya app on your own account.
2. Run `python -m tinytuya scan` and confirm it appears with a version of 3.3 or 3.4.
3. Run `python -m tinytuya wizard` with your own Tuya IoT project. It should list the device with a local key.

If step 3 lists nothing, don't buy that model.

## Safety rule

The heater failsafe must not depend on the network. Use a standalone hardware thermostat or alarm for the heater cutoff, and use the networked controller only for monitoring and schedules. It's the one thing a new user shouldn't skip.
