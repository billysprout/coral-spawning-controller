# Recommended hardware for new users

Recorded 2026-11-01. Revisit if a vendor changes its cloud or local-control options.

## Recommendation

Buy Tuya-based gear, not Inkbird-style devices.

- **Plugs:** about 4 Tuya plugs. One each for the heater, lights, and cooling or other equipment, with one spare. Plug-array control uses Tuya's local protocol.
- **Temperature:** one Tuya-cloud-capable temperature controller, or a Tuya thermometer paired to one of the plugs. Confirm it is in the Smart Life / Tuya account before buying (see the check below).
- **Redundant probe:** one independent DS18B20 probe on the Pi, as a second opinion. Use it to cross-check the controller.

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
