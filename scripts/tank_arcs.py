#!/usr/bin/env python3
"""tank_arcs.py -- choosing the light arc and the temperature arc to follow.

Vocabulary (the project's own): an "ecosystem" is a real-world location whose
climate, light and moon data we hold. An "arc" is one series from one location:
a temperature curve, or a light-and-moon programme. arc_catalog.json lists them
all (built by build_arc_catalog.py from the research repo's tables) with where
each was observed, how fine it is (daily, monthly), how many samples, and its
sources.

What a tank follows is a SELECTION, three values:

    temp_arc     id of one temperature line in the catalog
    light_arc    id of one light line in the catalog
    offset_days  the arc day played on tank day D is D - offset_days
                 (the same meaning as offset_days in the spawn calendar)

stored in <data>/arc_selection.json. `conductor.py onboard` makes it; the
conductor checks it every tick and, until it exists, says so and keeps following
the spawn calendar.

STUB, for the next agent
    derive_plan() is where the selection becomes a plan. It returns None today,
    so the conductor keeps following the calendar and only RECORDS the selection
    (tank_status.json "arcs"). The finished version is meant to be computed from
    the two catalog rows plus the offset, using the pseudosync layer
    (PSEUDOSYNC-METHOD.md: season, lunar and night axes are shifted separately,
    and six months is not a whole number of moon cycles). When you build it:
      * each catalog row's "ref" says which file and columns hold its values;
      * return a dict with the same fields as tank_plan.Plan, or None to fall
        back to the calendar;
      * do not invent data: a gap in an arc is a gap in the plan (TANK-NOTES.md).
      * locale arcs (id locale-<id>-sky / -sst / -reef-temp, from data/locales)
        play the locale's OWN 2027 dates, not the Okinawa calendar, and carry a
        "window" (the observed spawning window and grade): the start date and
        offset should come from the window's season_start minus a lead time. A
        level 4 (satellite) temperature is a shape, not a reef reading.

Standard library only.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os

KINDS = ("temp", "light")
SORTS = ("name", "location", "samples", "granularity", "span", "distance")
MAX_OFFSET_DAYS = 366


class ArcError(ValueError):
    """A catalog or selection that cannot be used."""


# ---------------------------------------------------------------------------
# CATALOG
# ---------------------------------------------------------------------------
def find_catalog(data_dir: str, here: str) -> str | None:
    """<data>/arc_catalog.json (a rebuilt one wins), else the one shipped beside
    this file."""
    for folder in (data_dir, here):
        path = os.path.join(folder, "arc_catalog.json")
        if os.path.exists(path):
            return path
    return None


def load_catalog(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            cat = json.load(fh)
    except (OSError, ValueError) as err:
        raise ArcError(f"cannot read the arc catalog {path}: {err}") from err
    arcs = cat.get("arcs")
    if not isinstance(arcs, list) or not arcs:
        raise ArcError("the arc catalog has no arcs")
    seen = set()
    for a in arcs:
        for key in ("id", "kind", "name", "location", "granularity", "samples"):
            if key not in a:
                raise ArcError(f"arc {a.get('id', '?')}: missing {key!r}")
        if a["kind"] not in KINDS:
            raise ArcError(f"arc {a['id']}: kind must be one of {KINDS}")
        if a["id"] in seen:
            raise ArcError(f"duplicate arc id: {a['id']}")
        seen.add(a["id"])
    cat["by_id"] = {a["id"]: a for a in arcs}
    return cat


def arcs_of(cat: dict, kind: str) -> list:
    return [a for a in cat["arcs"] if a["kind"] == kind]


def distance_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def sort_arcs(arcs: list, key: str = "name", near=None) -> list:
    """Sort for display. key is one of SORTS. `near` is (lat, lon) and is needed
    for "distance" (nearest observed location first; arcs with no coordinates,
    like a tank, go last). samples sorts largest first."""
    if key not in SORTS:
        raise ArcError(f"sort must be one of {SORTS}")
    if key == "distance":
        if near is None:
            raise ArcError("sorting by distance needs a latitude and longitude")

        def d(a):
            loc = a["location"]
            if loc.get("lat") is None or loc.get("lon") is None:
                return float("inf")
            return distance_km(near[0], near[1], loc["lat"], loc["lon"])
        return sorted(arcs, key=lambda a: (d(a), a["name"]))
    if key == "samples":
        return sorted(arcs, key=lambda a: (-a["samples"], a["name"]))
    pick = {
        "name": lambda a: a["name"].lower(),
        "location": lambda a: (a["location"]["name"].lower(), a["name"].lower()),
        "granularity": lambda a: (a["granularity"], a["name"].lower()),
        "span": lambda a: ((a.get("span") or {}).get("start") or "", a["name"].lower()),
    }[key]
    return sorted(arcs, key=pick)


def describe(a: dict, number: int | None = None, near=None) -> str:
    """Two or three plain lines for one arc."""
    loc = a["location"]
    where = loc["name"]
    if loc.get("lat") is not None:
        where += f" ({loc['lat']:.1f}, {loc['lon']:.1f})"
        if near is not None:
            where += f", {distance_km(near[0], near[1], loc['lat'], loc['lon']):,.0f} km away"
    span = a.get("span") or {}
    yrs = f", {a['years']} years" if a.get("years") else ""
    head = f"{number:>3}. " if number is not None else ""
    lines = [
        f"{head}{a['name']}  [{a['id']}]",
        f"     {where}",
        f"     {a['granularity']}, {a['samples']:,} samples{yrs}, "
        f"{span.get('start', '?')} to {span.get('end', '?')}; {a.get('basis', '?')}",
        f"     sources: {'; '.join(a.get('sources') or ['?'])}",
    ]
    w = a.get("window")
    if w:
        lines.append(f"     spawning window: {w.get('grade', '?')}, {w.get('focus', '?')}; season "
                     f"{w.get('season_start', '?')} to {w.get('season_end', '?')}, moon "
                     f"{w.get('daf_lo', '?')} to {w.get('daf_hi', '?')} days after full "
                     f"({w.get('n_nights', '?')} nights)")
    if a.get("note"):
        lines.append(f"     note: {a['note']}")
    return "\n".join(lines)


def format_list(arcs: list, near=None) -> str:
    return "\n".join(describe(a, i, near) for i, a in enumerate(arcs, 1))


# ---------------------------------------------------------------------------
# SELECTION
# ---------------------------------------------------------------------------
def selection_path(data_dir: str) -> str:
    return os.path.join(data_dir, "arc_selection.json")


def validate_selection(cat: dict, sel: dict) -> dict:
    """The selection with its values coerced, or ArcError."""
    by = cat["by_id"]
    out = {}
    for field, kind in (("temp_arc", "temp"), ("light_arc", "light")):
        aid = sel.get(field)
        if aid not in by:
            raise ArcError(f"{field}: no arc with id {aid!r}")
        if by[aid]["kind"] != kind:
            raise ArcError(f"{field}: {aid} is a {by[aid]['kind']} arc, "
                           f"not a {kind} arc")
        out[field] = aid
    try:
        off = float(sel.get("offset_days", 0))
    except (TypeError, ValueError):
        raise ArcError("offset_days must be a number") from None
    if not math.isfinite(off) or abs(off) > MAX_OFFSET_DAYS:
        raise ArcError(f"offset_days must be within +/-{MAX_OFFSET_DAYS}")
    out["offset_days"] = off
    return out


def load_selection(data_dir: str, cat: dict | None = None):
    """(selection or None, problem or None). None with no problem means the
    tank has not been onboarded. A selection that no longer validates (the
    catalog was rebuilt without one of its arcs) comes back as a problem."""
    path = selection_path(data_dir)
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as err:
        return None, f"arc_selection.json unreadable: {err}"
    if cat is None:
        return raw, None
    try:
        sel = validate_selection(cat, raw)
    except ArcError as err:
        return None, str(err)
    sel["chosen_utc"] = raw.get("chosen_utc")
    return sel, None


def save_selection(data_dir: str, cat: dict, sel: dict, now=None) -> dict:
    clean = validate_selection(cat, sel)
    clean["chosen_utc"] = (now or dt.datetime.now(dt.timezone.utc)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(data_dir, exist_ok=True)
    path = selection_path(data_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(clean, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return clean


def clear_selection(data_dir: str) -> bool:
    try:
        os.remove(selection_path(data_dir))
        return True
    except FileNotFoundError:
        return False


def summarize(cat: dict, sel: dict) -> dict:
    """What tank_status.json records about the choice."""
    def brief(aid):
        a = cat["by_id"][aid]
        return {"id": aid, "name": a["name"], "location": a["location"]["name"],
                "granularity": a["granularity"], "samples": a["samples"]}
    return {"onboarded": True, "temp": brief(sel["temp_arc"]),
            "light": brief(sel["light_arc"]), "offset_days": sel["offset_days"],
            "chosen_utc": sel.get("chosen_utc"), "driving_plan": False,
            "note": "recorded only: derive_plan() is not built, so the conductor "
                    "still follows the spawn calendar"}


# ---------------------------------------------------------------------------
# THE STUB
# ---------------------------------------------------------------------------
def derive_plan(cat: dict, sel: dict, when: dt.datetime, params=None):
    """Plan fields for `when` from the two chosen arcs and the offset, or None.

    NOT BUILT. See the module docstring. Returning None is the contract for
    "no opinion": the conductor then uses the spawn calendar.
    """
    return None


# ---------------------------------------------------------------------------
# ONBOARDING (the interactive part of `conductor.py onboard`)
# ---------------------------------------------------------------------------
def parse_near(text: str | None):
    if not text:
        return None
    try:
        lat, lon = (float(x) for x in text.split(","))
    except ValueError:
        raise ArcError("--near wants LAT,LON, for example 26.2,127.7") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ArcError("--near is out of range")
    return lat, lon


def onboard(cat: dict, data_dir: str, ask=input, say=print, sort="name",
            near=None, now=None) -> dict:
    """Walk through picking both arcs and the offset, then save. `ask` and `say`
    are injectable so the flow can be tested without a terminal."""
    chosen = {}
    for field, kind, label in (("temp_arc", "temp", "TEMPERATURE"),
                               ("light_arc", "light", "LIGHT")):
        arcs = sort_arcs(arcs_of(cat, kind), sort, near)
        say(f"\n=== Choose a {label} arc ({len(arcs)} available; sorted by "
            f"{sort}) ===\n")
        say(format_list(arcs, near))
        while True:
            reply = ask(f"\n{label} arc number (or an id): ").strip()
            pick = None
            if reply.isdigit() and 1 <= int(reply) <= len(arcs):
                pick = arcs[int(reply) - 1]
            else:
                pick = next((a for a in arcs if a["id"] == reply), None)
            if pick:
                chosen[field] = pick["id"]
                break
            say("  not on the list; try again")
    while True:
        reply = ask("\nOffset in days (0 = play the arc on its own dates; "
                    "positive = the tank runs that many days AHEAD of the arc): "
                    ).strip() or "0"
        try:
            chosen["offset_days"] = float(reply)
            validate_selection(cat, dict(chosen))
            break
        except (ValueError, ArcError) as err:
            say(f"  {err}")
    return save_selection(data_dir, cat, chosen, now)
