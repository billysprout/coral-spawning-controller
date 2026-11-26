#!/usr/bin/env python3
"""tank_plugs.py -- the plug array: one driver per plug brand, status only.

A plug_array.json lists the plugs. Each entry names a driver, so the array can
mix plug brands ("tinytuya" today; Kasa, Shelly, Tasmota, anything else later
by adding a driver class and registering it):

    {"plugs": [
      {"name": "main-white", "driver": "tinytuya", "role": "white",
       "id": "<device id>", "ip": "192.0.2.10", "version": 3.3},
      {"name": "halo", "driver": "tinytuya", "role": "halo", "follows": "white",
       "id": "...", "ip": "...", "watt_dp": 19, "watt_scale": 10},
      {"name": "spare", "driver": "tbd", "role": "unassigned"}
    ]}

Two arrays feed the conductor: plug_array.json (the Tuya plugs) and an optional
extra_plugs.json (same schema, for whatever plug brand comes next). Entries in
the extra-plugs file default to the "tbd" driver. A tbd entry is a placeholder:
it reports "not selected", is never alarmed on, and cannot switch. A driver name
that is NOT registered and is not "tbd" is a typo, and that does alarm.

A plug reports state (on/off, watts when the plug supplies them) and can be
switched with set_on(). This module only provides the capability; whether
anything is ever switched is decided by tank_out.Actuator, which is off unless
a human enables it in the config. A driver that cannot switch (tbd) raises
OutputsDisabled.

Local keys never live in plug_array.json. They are read from the tinytuya
wizard's devices.json and are never logged or included in repr().

Roles
    white / blue / moon     driven by that plan channel
    white_plus              driven by the plan's white_plus channel: the second
                            light step, on only for the brighter hours of the day
                            (see tank_plan white_plus). "white-plus" is accepted
    anything else           set "follows": "white" | "white_plus" | "blue" |
                            "moon" to follow a channel, otherwise the plug is
                            observed only
    heater / fan            observed only (temperature output not decided)
    unassigned              observed only

Dimmers (2026-11-08). A plug whose driver can set a brightness has
supports_level and set_level(0..1); the actuator then drives it with the plan's
dawn/dusk ramps instead of on/off (tank_out, outputs.levels). "tinytuya_dimmer"
is one gang of a Tuya "tgkg" dimmer switch (the double dimmers are two gangs on
one device, so each gang is its own array entry sharing id, ip and key):

      {"name": "dim-1a", "driver": "tinytuya_dimmer", "role": "white",
       "id": "<device id>", "ip": "192.0.2.12", "gang": 1, "level_cap": 0.8}
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

CHANNELS = ("white", "white_plus", "blue", "moon")


def _norm(name):
    """"white-plus" and "white_plus" are the same channel."""
    return name.replace("-", "_") if isinstance(name, str) else name

DRIVERS: dict[str, type] = {}


class OutputsDisabled(RuntimeError):
    """This driver cannot switch (for example the tbd placeholder)."""


class PlugError(RuntimeError):
    """A switch command failed or was refused by the plug."""


def register(name: str):
    def deco(cls):
        DRIVERS[name] = cls
        cls.driver = name
        return cls
    return deco


@dataclass
class PlugReading:
    name: str
    ok: bool
    on: bool | None = None
    power_w: float | None = None
    detail: str = ""
    revert_remaining_s: float | None = None   # plug-side countdown, if reported
    # Health fields, filled by drivers that can (see tank_health.py). All are
    # observations from the status query; none of them ever causes a command.
    latency_ms: float | None = None   # how long the query took
    error: str | None = None          # short class of the failure, see ERROR_CLASSES
    dps: dict | None = None           # the plug's raw data points, if it replied
    configured: bool = True           # False: a tbd placeholder, nothing to alarm on
    level: float | None = None        # dimmers: the brightness setting 0..1 (kept
    #                                   while switched off; see Plug.effective_level)
    level_min: float | None = None    # dimmers: the lowest brightness the device
    #                                   will give while on (its own minimum setting)


# tinytuya's Err codes (tinytuya/core/error_helper.py) in plain classes
ERROR_CLASSES = {
    "900": "bad_reply", "904": "bad_reply", "901": "unreachable",
    "905": "unreachable", "902": "timeout", "914": "bad_key_or_version",
}


class Plug:
    driver = "base"
    supports_revert = False         # can arm a plug-side "turn back on" timer
    supports_level = False          # can be set to a brightness (set_level)

    def __init__(self, name: str, role: str = "unassigned",
                 follows: str | None = None, **opts):
        role, follows = _norm(role), _norm(follows)
        if follows is not None and follows not in CHANNELS:
            raise ValueError(f"{name}: follows must be one of {CHANNELS}")
        self.name = name
        self.role = role
        self.follows = follows
        self.opts = opts
        self.array = opts.get("array", "plugs")     # which array file it came from
        # Optional wattage sanity (tank_health.power_findings): the range a plug
        # draws while ON, and the most it may draw while OFF.
        ew = opts.get("expect_w")
        if ew is not None:
            if (not isinstance(ew, (list, tuple)) or len(ew) != 2
                    or not 0 <= float(ew[0]) <= float(ew[1])):
                raise ValueError(f"{name}: expect_w must be [min_w, max_w]")
            ew = (float(ew[0]), float(ew[1]))
        self.expect_w = ew
        self.off_max_w = float(opts.get("off_max_w", 1.0))
        # A plug that must never sit OFF without its plug-side countdown armed
        # (the heater gate: if the Pi dies, the plug has to come back ON by
        # itself). Default: the heater role. tank_health.countdown_findings.
        self.revert_to_on = bool(opts.get("revert_to_on", role == "heater"))
        # Dimmers: the brightest this lamp may ever be set (a hard ceiling on
        # whatever the plan asks for), 0 < level_cap <= 1.
        self.level_cap = float(opts.get("level_cap", 1.0))
        if not 0.0 < self.level_cap <= 1.0:
            raise ValueError(f"{name}: level_cap must be above 0 and at most 1")
        # Dimmers: the dimmest this lamp may be set while on, 0 <= min_level <=
        # level_cap. A plan level under it means off, never a brightness in the
        # lamp's bad range (the Halo's driver buzzes below about 90 percent). It is
        # the controller's own floor, on top of the device's brightness_min.
        self.min_level = float(opts.get("min_level", 0.0))
        if not 0.0 <= self.min_level <= self.level_cap:
            raise ValueError(f"{name}: min_level must be from 0 to level_cap")
        # Dimmers: when set, the brightness whenever the plan has this lamp on,
        # instead of the plan's level (the moon lamp: the plan's level is the moon's
        # illumination fraction, which says nothing about how bright this lamp is).
        fl = opts.get("fixed_level")
        self.fixed_level = None if fl is None else float(fl)
        if self.fixed_level is not None and not 0.0 < self.fixed_level <= self.level_cap:
            raise ValueError(f"{name}: fixed_level must be above 0 and at most level_cap")
        # A second channel this lamp also serves while its main channel is off (the
        # blue lamp doubling as the moon until the moon LEDs arrive): "also_follows"
        # names it, "also_level" is the brightness to hold then (the moon's own
        # level says nothing about this lamp). The main channel keeps priority.
        af = _norm(opts.get("also_follows"))
        if af is not None and af not in CHANNELS:
            raise ValueError(f"{name}: also_follows must be one of {CHANNELS}")
        self.also_follows = af
        al = opts.get("also_level")
        self.also_level = None if al is None else float(al)
        if self.also_level is not None and not 0.0 < self.also_level <= self.level_cap:
            raise ValueError(f"{name}: also_level must be above 0 and at most level_cap")

    @property
    def channel(self) -> str | None:
        """The plan channel this plug follows, or None (observed only)."""
        if self.follows:
            return self.follows
        return self.role if self.role in CHANNELS else None

    def status(self) -> PlugReading:
        raise NotImplementedError

    def set_on(self, on: bool, revert_after_s: float | None = None):
        """Switch the plug. With revert_after_s, also arm a plug-side timer that
        flips the plug to the opposite state when it runs out unless somebody
        refreshes it (the dead-man): with on=False it turns the plug back on
        (the heater gate), with on=True it turns it off (the lamp backstop,
        2026-11-06). Raises PlugError on failure. The caller must read the state
        back; a reply is not proof."""
        raise OutputsDisabled(f"{self.name}: this driver cannot switch")

    def set_level(self, level: float):
        """Set the brightness, 0..1 (0 switches off). Only dimmers can."""
        raise OutputsDisabled(f"{self.name}: this driver cannot dim")

    @staticmethod
    def effective_level(reading: "PlugReading") -> float | None:
        """The light actually being given: 0 when switched off, the brightness
        setting when on, None when the plug did not say."""
        if reading.on is None:
            return None
        if not reading.on:
            return 0.0
        return reading.level

    def __repr__(self) -> str:
        return (f"<{type(self).__name__} {self.name} role={self.role}"
                f" channel={self.channel}>")


@register("tbd")
class TbdPlug(Plug):
    """Placeholder for a plug brand not chosen yet. Always reports not-ok, but
    "not configured" rather than "offline", so it never raises an alert. The
    loader gives it `_unknown` when the entry named a driver that does not
    exist; that is a typo, and it is reported as an error.
    """

    def status(self) -> PlugReading:
        bad = self.opts.get("_unknown")
        if bad:
            return PlugReading(self.name, False, error="unknown_driver",
                               detail=f"no driver named {bad!r}")
        return PlugReading(self.name, False, detail="driver not selected (tbd)",
                           error="not_selected", configured=False)


@register("fake")
class FakePlug(Plug):
    """In-memory plug for dry runs and tests.

    Options: on, power_w, ok (False = offline), fail_set (set_on raises),
    stuck (set_on succeeds but the state does not change), revert_s.
    """

    def __init__(self, name, role="unassigned", follows=None, **opts):
        super().__init__(name, role, follows, **opts)
        self.commands: list[tuple] = []

    @property
    def supports_revert(self) -> bool:
        return bool(self.opts.get("supports_revert", True))

    def status(self) -> PlugReading:
        ok = bool(self.opts.get("ok", True))
        return PlugReading(self.name, ok,
                           on=self.opts.get("on") if ok else None,
                           power_w=self.opts.get("power_w") if ok else None,
                           detail="" if ok else "fake plug offline",
                           revert_remaining_s=(self._fake_countdown()
                                               if ok else None))

    def _fake_countdown(self):
        left = self.opts.get("revert_s")
        return self.opts.get("off_s") if left is None else left

    def set_on(self, on: bool, revert_after_s: float | None = None):
        if self.opts.get("fail_set"):
            raise PlugError("fake set failed")
        self.commands.append((bool(on), revert_after_s))
        if not self.opts.get("stuck"):
            self.opts["on"] = bool(on)
        self.opts["revert_s"] = revert_after_s if (revert_after_s and not on) else None
        self.opts["off_s"] = revert_after_s if (revert_after_s and on) else None


@register("fake_dimmer")
class FakeDimmerPlug(Plug):
    """In-memory dimmer for dry runs and tests.

    Options: on, level (0..1, the stored brightness), ok, fail_set, stuck (a
    command succeeds but nothing changes), off_below (below this, set_level
    switches off; default 0.01).
    """
    supports_level = True

    def __init__(self, name, role="unassigned", follows=None, **opts):
        super().__init__(name, role, follows, **opts)
        self.commands: list[tuple] = []
        self.off_below = float(opts.get("off_below", 0.01))

    def status(self) -> PlugReading:
        ok = bool(self.opts.get("ok", True))
        return PlugReading(self.name, ok,
                           on=self.opts.get("on") if ok else None,
                           level=self.opts.get("level") if ok else None,
                           level_min=self.opts.get("level_min", 0.0) if ok else None,
                           detail="" if ok else "fake dimmer offline")

    def set_on(self, on: bool, revert_after_s: float | None = None):
        if self.opts.get("fail_set"):
            raise PlugError("fake set failed")
        self.commands.append(("on", bool(on)))
        if not self.opts.get("stuck"):
            self.opts["on"] = bool(on)

    def set_level(self, level: float):
        if self.opts.get("fail_set"):
            raise PlugError("fake set failed")
        level = min(max(float(level), 0.0), self.level_cap)
        self.commands.append(("level", round(level, 4)))
        if self.opts.get("stuck"):
            return
        if level < self.off_below:
            self.opts["on"] = False
        else:
            self.opts["on"] = True
            self.opts["level"] = round(level, 4)


@register("tinytuya")
class TinyTuyaPlug(Plug):
    """Tuya local protocol over the LAN (no cloud at runtime).

    Options: id, ip, version (3.3), power_dp (1), watt_dp (None), watt_scale
    (10, i.e. the dp is in tenths of a watt), timeout_s (3), revert_dp (None).
    The local key is passed in as `key`, from the wizard's devices.json, never
    from the array.

    revert_dp is the plug's countdown data point (often 9, in seconds). When set,
    set_on(False, revert_after_s=N) also arms it so the plug turns itself back on
    after N seconds unless it is re-armed. Whether a given plug's countdown flips
    the switch ON when it expires is model-specific: verify it with the
    pull-the-plug test before trusting it.
    """

    def __init__(self, name, role="unassigned", follows=None, key=None, **opts):
        super().__init__(name, role, follows, **opts)
        self._key = key
        self.dev_id = opts.get("id")
        self.ip = opts.get("ip")
        self.version = float(opts.get("version", 3.3))
        self.power_dp = str(opts.get("power_dp", 1))
        wd = opts.get("watt_dp")
        self.watt_dp = None if wd is None else str(wd)
        self.watt_scale = float(opts.get("watt_scale", 10))
        self.timeout_s = float(opts.get("timeout_s", 3))
        rd = opts.get("revert_dp")
        self.revert_dp = None if rd is None else str(rd)

    @property
    def supports_revert(self) -> bool:
        return self.revert_dp is not None

    def _device(self, tinytuya):
        kwargs = dict(address=self.ip, local_key=self._key,
                      version=self.version, connection_timeout=self.timeout_s)
        try:
            return tinytuya.OutletDevice(self.dev_id, connection_retry_limit=1,
                                         connection_retry_delay=1, **kwargs)
        except TypeError:       # older tinytuya without the retry arguments
            return tinytuya.OutletDevice(self.dev_id, **kwargs)

    def status(self) -> PlugReading:
        """Read-only: one data-point query, never a command."""
        if not (self.dev_id and self.ip):
            return PlugReading(self.name, False, detail="id and ip are required",
                               error="not_configured")
        if not self._key:
            return PlugReading(self.name, False, error="no_key",
                               detail="no local key for this id in devices.json")
        try:
            import tinytuya
        except ImportError:
            return PlugReading(self.name, False, detail="tinytuya not installed",
                               error="no_library")
        started = time.monotonic()
        try:
            data = self._device(tinytuya).status()
        except Exception as err:  # noqa: BLE001 - one bad plug must not stop a tick
            return PlugReading(self.name, False, error="exception",
                               detail=f"{type(err).__name__}: {err}"[:120],
                               latency_ms=round((time.monotonic() - started) * 1000))
        latency = round((time.monotonic() - started) * 1000)
        if not isinstance(data, dict):
            return PlugReading(self.name, False, detail="unexpected reply",
                               error="bad_reply", latency_ms=latency)
        if data.get("Error") or data.get("Err"):
            return PlugReading(
                self.name, False, latency_ms=latency,
                error=ERROR_CLASSES.get(str(data.get("Err")), "error"),
                detail=f"{data.get('Error')} (Err {data.get('Err')})"[:120])
        dps = data.get("dps") or {}
        if self.power_dp not in dps:
            return PlugReading(self.name, False, error="dp_missing",
                               latency_ms=latency, dps=dict(dps),
                               detail=f"dp {self.power_dp} absent from reply")
        on = bool(dps[self.power_dp])
        watts = None
        if self.watt_dp is not None and self.watt_dp in dps:
            try:
                watts = round(float(dps[self.watt_dp]) / self.watt_scale, 2)
            except (TypeError, ValueError):
                watts = None
        remaining = None
        if self.revert_dp is not None and self.revert_dp in dps:
            try:
                remaining = float(dps[self.revert_dp])
            except (TypeError, ValueError):
                remaining = None
        return PlugReading(self.name, True, on=on, power_w=watts,
                           revert_remaining_s=remaining, latency_ms=latency,
                           dps=dict(dps))

    @staticmethod
    def _check_reply(reply, what):
        if isinstance(reply, dict) and (reply.get("Error") or reply.get("Err")):
            raise PlugError(
                f"{what}: {reply.get('Error')} (Err {reply.get('Err')})"[:120])

    def set_on(self, on: bool, revert_after_s: float | None = None):
        if not (self.dev_id and self.ip and self._key):
            raise PlugError("id, ip and a local key are required to switch")
        # The countdown flips the plug to the opposite state when it expires: ON
        # after an OFF command (the heater gate), OFF after an ON command (the
        # lamp backstop; checked on plug-8, -9 and -10 on 2026-11-06).
        want_revert = bool(revert_after_s)
        if want_revert and self.revert_dp is None:
            raise PlugError("revert requested but revert_dp is not configured")
        try:
            import tinytuya
        except ImportError:
            raise PlugError("tinytuya not installed") from None
        try:
            dev = self._device(tinytuya)
            self._check_reply(dev.set_status(bool(on), switch=int(self.power_dp)),
                              "switch")
            if self.revert_dp is not None:
                # arm the dead-man when asked to; otherwise clear it
                secs = int(revert_after_s) if want_revert else 0
                self._check_reply(dev.set_value(int(self.revert_dp), secs),
                                  "countdown")
        except PlugError:
            raise
        except Exception as err:  # noqa: BLE001
            raise PlugError(f"{type(err).__name__}: {err}"[:120]) from err


# Data points of a Tuya "tgkg" dimmer switch (read from the wizard's mapping of the
# two Dimmer Switch devices, 2026-11-08): per gang a switch, a brightness 10..1000,
# a minimum brightness, a lamp type and a countdown in seconds.
DIMMER_GANGS = {
    1: {"power_dp": 1, "level_dp": 2, "min_dp": 3, "type_dp": 4, "countdown_dp": 6},
    2: {"power_dp": 7, "level_dp": 8, "min_dp": 9, "type_dp": 10, "countdown_dp": 12},
}


@register("tinytuya_dimmer")
class TinyTuyaDimmerPlug(TinyTuyaPlug):
    """One gang of a Tuya dimmer switch over the local protocol (version 3.5).

    Options: id, ip, gang (1 or 2; picks the data points), version (3.5),
    level_cap (the brightest it may be set, default 1), off_below (a requested
    level under this switches off, default 0.01), level_full (the data point's
    maximum, 1000), level_floor (its minimum, 10), and any DP number overridden
    (power_dp, level_dp).

    Brightness is a fraction of full scale: level 0.5 is data point 500. The
    device also has its own minimum brightness (data point 3 or 9, 100 on both
    dimmers when first read), which it enforces whatever is sent. The data point
    keeps its value while the gang is switched off, so a reading's level is the
    setting, not the light: use Plug.effective_level.

    revert_dp (the countdown) is NOT set here: what a dimmer's countdown does when
    it expires (off, like the plugs, or something else) has not been tested, so
    the lamp backstop does not apply to dimmers yet.
    """
    supports_level = True

    def __init__(self, name, role="unassigned", follows=None, key=None, **opts):
        gang = int(opts.get("gang", 1))
        if gang not in DIMMER_GANGS:
            raise ValueError(f"{name}: gang must be 1 or 2")
        dps = DIMMER_GANGS[gang]
        opts.setdefault("power_dp", dps["power_dp"])
        opts.setdefault("version", 3.5)
        super().__init__(name, role, follows, key=key, **opts)
        self.gang = gang
        self.level_dp = str(opts.get("level_dp", dps["level_dp"]))
        self.min_dp = str(opts.get("min_dp", dps["min_dp"]))
        self.level_full = int(opts.get("level_full", 1000))
        self.level_floor = int(opts.get("level_floor", 10))
        self.off_below = float(opts.get("off_below", 0.01))
        if not 0 < self.level_floor < self.level_full:
            raise ValueError(f"{name}: need 0 < level_floor < level_full")

    def status(self) -> PlugReading:
        r = super().status()
        if not r.ok or r.dps is None:
            return r
        raw = r.dps.get(self.level_dp)
        try:
            level = round(min(1.0, max(0.0, float(raw) / self.level_full)), 4)
        except (TypeError, ValueError):
            r.ok, r.on, r.error = False, None, "dp_missing"
            r.detail = f"brightness dp {self.level_dp} absent or not a number"
            return r
        r.level = level
        try:
            r.level_min = round(min(1.0, max(0.0, float(r.dps[self.min_dp]) / self.level_full)), 4)
        except (KeyError, TypeError, ValueError):
            r.level_min = None
        return r

    def set_level(self, level: float):
        if not (self.dev_id and self.ip and self._key):
            raise PlugError("id, ip and a local key are required to switch")
        level = min(max(float(level), 0.0), self.level_cap)
        if level < self.off_below:
            return self.set_on(False)            # the brightness setting is kept
        value = max(self.level_floor, min(self.level_full, round(level * self.level_full)))
        try:
            import tinytuya
        except ImportError:
            raise PlugError("tinytuya not installed") from None
        try:
            dev = self._device(tinytuya)
            # switch and brightness in one message, so a lamp that is off comes on at
            # the asked brightness and never flashes at the old one
            self._check_reply(dev.set_multiple_values(
                {self.power_dp: True, self.level_dp: value}), "level")
        except PlugError:
            raise
        except Exception as err:  # noqa: BLE001
            raise PlugError(f"{type(err).__name__}: {err}"[:120]) from err


def load_keys(path: str) -> dict:
    """{device id: local key} from the tinytuya wizard's devices.json."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {d["id"]: d["key"] for d in data
            if isinstance(d, dict) and d.get("id") and d.get("key")}


def load_plugs(array_path: str, devices_path: str | None = None,
               extra_path: str | None = None) -> list[Plug]:
    """Build the array: plug_array.json, then extra_plugs.json if it exists.

    A driver that is not registered becomes a TbdPlug that reports an error, so
    one bad entry never takes the others down. Names are unique across both
    files.
    """
    keys = load_keys(devices_path) if devices_path else {}
    plugs, seen = [], set()
    sources = [(array_path, "plugs", "tbd")]
    if extra_path and os.path.exists(extra_path):
        sources.append((extra_path, "extra", "tbd"))
    for path, label, default_driver in sources:
        with open(path, encoding="utf-8") as fh:
            spec = json.load(fh)
        for entry in spec.get("plugs", []):
            entry = dict(entry)
            name = entry.pop("name")
            if name in seen:
                raise ValueError(f"duplicate plug name: {name}")
            seen.add(name)
            driver = entry.pop("driver", default_driver)
            role = entry.pop("role", "unassigned")
            follows = entry.pop("follows", None)
            entry["array"] = label
            cls = DRIVERS.get(driver)
            if cls is None:
                cls = TbdPlug
                entry["_unknown"] = driver
            if isinstance(cls, type) and issubclass(cls, TinyTuyaPlug):
                plugs.append(cls(name, role, follows,
                                 key=keys.get(entry.get("id")), **entry))
            else:
                plugs.append(cls(name, role, follows, **entry))
    return plugs
