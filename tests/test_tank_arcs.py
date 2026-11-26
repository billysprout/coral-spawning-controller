"""Tests for the arc catalog, the selection, onboarding, and the conductor's use
of them. Offline.

    python -m unittest tests.test_tank_arcs -v     (from the repo root)
"""
import contextlib
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import tank_arcs                        # noqa: E402
import tank_inputs                      # noqa: E402
import tank_plan                        # noqa: E402
import tank_tick                        # noqa: E402

UTC = dt.timezone.utc
CAL = os.path.join(ROOT, "data", "spawn_calendar_2026-11-01.csv")
SHIPPED = os.path.join(ROOT, "scripts", "arc_catalog.json")
if not os.path.exists(SHIPPED):          # public controller: imported from the cue-table repo
    SHIPPED = os.path.join(ROOT, "data", "arc_catalog.json")
T0 = dt.datetime(2026, 11, 2, 20, 0, tzinfo=UTC)


def arc(aid, kind, loc="Somewhere", lat=None, lon=None, samples=10,
        gran="daily", start="2020-01-01"):
    return {"id": aid, "kind": kind, "name": aid.upper(),
            "location": {"site_id": aid, "name": loc, "lat": lat, "lon": lon},
            "granularity": gran, "samples": samples, "years": None,
            "span": {"start": start, "end": "2021-01-01"}, "basis": "observed",
            "sources": ["a.csv"], "note": ""}


def write_catalog(folder, arcs):
    path = os.path.join(folder, "arc_catalog.json")
    with open(path, "w") as fh:
        json.dump({"version": 1, "arcs": arcs}, fh)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cat = tank_arcs.load_catalog(write_catalog(self.tmp, [
            arc("t-near", "temp", "Near", 26.0, 127.0, 100, "daily"),
            arc("t-far", "temp", "Far", -30.0, 150.0, 12, "monthly"),
            arc("t-tank", "temp", "Tank", None, None, 500, "daily"),
            arc("l-1", "light", "Near", 26.0, 127.0, 365, "daily"),
        ]))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class CatalogTests(Base):
    def test_shipped_catalog_loads_and_has_both_kinds(self):
        cat = tank_arcs.load_catalog(SHIPPED)
        self.assertTrue(tank_arcs.arcs_of(cat, "temp"))
        self.assertTrue(tank_arcs.arcs_of(cat, "light"))
        for a in cat["arcs"]:
            self.assertGreater(a["samples"], 0, a["id"])
            self.assertTrue(a["sources"], a["id"])

    def test_shipped_catalog_offers_todays_calendar_as_a_choice(self):
        cat = tank_arcs.load_catalog(SHIPPED)
        self.assertIn("spawn-calendar-temp", cat["by_id"])
        self.assertIn("spawn-calendar-light", cat["by_id"])

    def test_bad_catalogs_are_refused(self):
        for arcs in ([], [arc("a", "temp"), arc("a", "temp")],
                     [dict(arc("a", "temp"), kind="humidity")]):
            with self.assertRaises(tank_arcs.ArcError):
                tank_arcs.load_catalog(write_catalog(self.tmp, arcs))
        bad = arc("a", "temp")
        del bad["samples"]
        with self.assertRaises(tank_arcs.ArcError):
            tank_arcs.load_catalog(write_catalog(self.tmp, [bad]))


class SortTests(Base):
    def ids(self, key, near=None):
        return [a["id"] for a in tank_arcs.sort_arcs(
            tank_arcs.arcs_of(self.cat, "temp"), key, near)]

    def test_sorts(self):
        self.assertEqual(self.ids("samples"), ["t-tank", "t-near", "t-far"])
        self.assertEqual(self.ids("location"), ["t-far", "t-near", "t-tank"])
        self.assertEqual(self.ids("granularity"), ["t-near", "t-tank", "t-far"])

    def test_distance_puts_nearest_first_and_unlocated_last(self):
        self.assertEqual(self.ids("distance", (26.2, 127.7)),
                         ["t-near", "t-far", "t-tank"])

    def test_distance_needs_a_point_and_unknown_sorts_are_refused(self):
        with self.assertRaises(tank_arcs.ArcError):
            self.ids("distance")
        with self.assertRaises(tank_arcs.ArcError):
            self.ids("colour")

    def test_distance_is_sane(self):
        km = tank_arcs.distance_km(26.2, 127.7, 26.2, 127.7)
        self.assertAlmostEqual(km, 0, places=3)
        # one degree of longitude on the equator is 111.19 km
        self.assertAlmostEqual(tank_arcs.distance_km(0, 0, 0, 1), 111.19, places=1)
        # Okinawa to Singapore is roughly 3,760 km
        self.assertTrue(3650 < tank_arcs.distance_km(26.2, 127.7, 1.35, 103.8) < 3850)

    def test_near_parsing(self):
        self.assertEqual(tank_arcs.parse_near("26.2,127.7"), (26.2, 127.7))
        self.assertIsNone(tank_arcs.parse_near(None))
        for bad in ("26", "a,b", "95,0"):
            with self.assertRaises(tank_arcs.ArcError):
                tank_arcs.parse_near(bad)

    def test_description_shows_location_granularity_samples_and_sources(self):
        text = tank_arcs.describe(self.cat["by_id"]["t-near"], 1, (26.2, 127.7))
        for bit in ("Near", "daily", "100 samples", "a.csv", "km away"):
            self.assertIn(bit, text)


class SelectionTests(Base):
    def test_valid_selection_round_trips(self):
        tank_arcs.save_selection(self.tmp, self.cat, {
            "temp_arc": "t-near", "light_arc": "l-1", "offset_days": 182.6})
        sel, problem = tank_arcs.load_selection(self.tmp, self.cat)
        self.assertIsNone(problem)
        self.assertEqual((sel["temp_arc"], sel["light_arc"], sel["offset_days"]),
                         ("t-near", "l-1", 182.6))
        self.assertTrue(sel["chosen_utc"].endswith("Z"))

    def test_wrong_kind_unknown_id_and_silly_offsets_are_refused(self):
        bad = [{"temp_arc": "l-1", "light_arc": "l-1"},
               {"temp_arc": "nope", "light_arc": "l-1"},
               {"temp_arc": "t-near", "light_arc": "t-near"},
               {"temp_arc": "t-near", "light_arc": "l-1", "offset_days": 400},
               {"temp_arc": "t-near", "light_arc": "l-1", "offset_days": "x"},
               {"temp_arc": "t-near", "light_arc": "l-1", "offset_days": float("nan")}]
        for sel in bad:
            with self.assertRaises(tank_arcs.ArcError, msg=sel):
                tank_arcs.save_selection(self.tmp, self.cat, sel)
        self.assertFalse(os.path.exists(tank_arcs.selection_path(self.tmp)))

    def test_missing_selection_is_not_a_problem_but_a_stale_one_is(self):
        self.assertEqual(tank_arcs.load_selection(self.tmp, self.cat), (None, None))
        with open(tank_arcs.selection_path(self.tmp), "w") as fh:
            json.dump({"temp_arc": "gone", "light_arc": "l-1"}, fh)
        sel, problem = tank_arcs.load_selection(self.tmp, self.cat)
        self.assertIsNone(sel)
        self.assertIn("gone", problem)
        with open(tank_arcs.selection_path(self.tmp), "w") as fh:
            fh.write("{not json")
        self.assertIn("unreadable", tank_arcs.load_selection(self.tmp, self.cat)[1])

    def test_clear(self):
        tank_arcs.save_selection(self.tmp, self.cat, {"temp_arc": "t-near",
                                                      "light_arc": "l-1"})
        self.assertTrue(tank_arcs.clear_selection(self.tmp))
        self.assertFalse(tank_arcs.clear_selection(self.tmp))

    def test_derive_plan_is_a_stub_that_has_no_opinion(self):
        sel = tank_arcs.validate_selection(self.cat, {"temp_arc": "t-near",
                                                      "light_arc": "l-1"})
        self.assertIsNone(tank_arcs.derive_plan(self.cat, sel, T0))


class OnboardFlowTests(Base):
    def run_flow(self, answers, **kw):
        it = iter(answers)
        said = []
        done = tank_arcs.onboard(self.cat, self.tmp, ask=lambda _: next(it),
                                 say=said.append, **kw)
        return done, "\n".join(said)

    def test_pick_by_number_and_by_id_with_an_offset(self):
        done, text = self.run_flow(["1", "l-1", "10"])
        self.assertEqual(done["temp_arc"], "t-far")         # first by name
        self.assertEqual(done["light_arc"], "l-1")
        self.assertEqual(done["offset_days"], 10.0)
        self.assertIn("TEMPERATURE", text)
        self.assertIn("LIGHT", text)

    def test_bad_answers_are_asked_again_and_blank_offset_is_zero(self):
        done, text = self.run_flow(["99", "nonsense", "T-NEAR", "t-near",
                                    "l-1", "abc", "9999", ""])
        self.assertEqual(done["temp_arc"], "t-near")
        self.assertEqual(done["offset_days"], 0.0)
        self.assertIn("not on the list", text)

    def test_list_follows_the_requested_sort(self):
        _, text = self.run_flow(["1", "1", ""], sort="samples")
        self.assertLess(text.index("T-TANK"), text.index("T-NEAR"))


class ConductorTests(Base):
    def tank(self, catalog, sent=None):
        cfg = tank_tick._merge(tank_tick.DEFAULT_CONFIG, {})
        cfg["plan"] = tank_plan.plan_config()

        class In:
            def read_all(self, now):
                return {"temp": tank_inputs.Reading("temp", True, 25.0, now),
                        "lux": tank_inputs.Reading("lux", True, 5.0, now)}
        return tank_tick.Tank(cfg, tank_plan.load_calendar(CAL), [], In(),
                              tank_tick.DryRunSink(self.tmp),
                              notify=lambda m, title="": (sent or []).append(m),
                              deadman=lambda: None, arc_catalog=catalog,
                              data_dir=self.tmp)

    def test_before_onboarding_it_says_so_without_alerting(self):
        sent = []
        rec = self.tank(self.cat, sent).tick(T0)
        self.assertFalse(rec["arcs"]["onboarded"])
        found = [f for f in rec["findings"] if f["key"] == "onboarding_needed"]
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0]["alert"])
        self.assertIn("onboard", found[0]["message"])
        self.assertEqual(sent, [])
        self.assertIsNotNone(rec["plan"])               # the calendar still runs

    def test_choosing_in_another_terminal_takes_effect_on_the_next_tick(self):
        t = self.tank(self.cat)
        t.tick(T0)
        tank_arcs.save_selection(self.tmp, self.cat, {
            "temp_arc": "t-near", "light_arc": "l-1", "offset_days": 5})
        rec = t.tick(T0 + dt.timedelta(minutes=1))
        self.assertTrue(rec["arcs"]["onboarded"])
        self.assertEqual(rec["arcs"]["temp"]["id"], "t-near")
        self.assertEqual(rec["arcs"]["offset_days"], 5.0)
        self.assertFalse(rec["arcs"]["driving_plan"])
        self.assertNotIn("onboarding_needed", [f["key"] for f in rec["findings"]])

    def test_the_selection_does_not_change_the_plan_yet(self):
        before = self.tank(self.cat).tick(T0)["plan"]
        tank_arcs.save_selection(self.tmp, self.cat, {
            "temp_arc": "t-far", "light_arc": "l-1", "offset_days": 90})
        after = self.tank(self.cat).tick(T0)["plan"]
        self.assertEqual(before, after)

    def test_stale_selection_and_missing_catalog_warn(self):
        with open(tank_arcs.selection_path(self.tmp), "w") as fh:
            json.dump({"temp_arc": "gone", "light_arc": "l-1"}, fh)
        keys = [f["key"] for f in self.tank(self.cat).tick(T0)["findings"]]
        self.assertIn("arc_selection_invalid", keys)
        rec = self.tank(None).tick(T0)
        self.assertIn("arc_catalog_missing", [f["key"] for f in rec["findings"]])
        self.assertTrue(all(not f["alert"] for f in rec["findings"]
                            if f["key"].startswith("arc_")))

    def test_summary_line_mentions_arcs(self):
        self.assertIn("arcs: not chosen", tank_tick._summary(
            self.tank(self.cat).tick(T0)))
        tank_arcs.save_selection(self.tmp, self.cat, {"temp_arc": "t-near",
                                                      "light_arc": "l-1"})
        self.assertIn("recorded only", tank_tick._summary(
            self.tank(self.cat).tick(T0 + dt.timedelta(minutes=1))))


class CliTests(Base):
    def setUp(self):
        super().setUp()
        self._env = os.environ.get("TANK_DATA")
        os.environ["TANK_DATA"] = self.tmp
        self.cfg = os.path.join(self.tmp, "cfg.json")
        with open(self.cfg, "w") as fh:
            json.dump({}, fh)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("TANK_DATA", None)
        else:
            os.environ["TANK_DATA"] = self._env
        super().tearDown()

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tank_tick.main(["onboard", "--config", self.cfg, *args])
        return code, out.getvalue()

    def test_list_show_save_reset(self):
        code, text = self.run_cli("--list", "--kind", "temp", "--sort", "samples")
        self.assertEqual(code, 0)
        self.assertIn("T-TANK", text)
        self.assertNotIn("L-1", text)
        code, text = self.run_cli("--list", "--near", "26.2,127.7",
                                  "--sort", "distance", "--kind", "temp")
        self.assertIn("km away", text)
        self.assertEqual(self.run_cli("--show")[0], 1)          # nothing yet
        code, text = self.run_cli("--temp-arc", "t-far", "--light-arc", "l-1",
                                  "--offset-days", "182.6")
        self.assertEqual(code, 0)
        self.assertIn("+182.6", text)
        code, text = self.run_cli("--show")
        self.assertEqual(code, 0)
        self.assertIn("t-far", text)
        self.assertIn("recorded only", text)
        self.assertEqual(self.run_cli("--reset")[0], 0)
        self.assertEqual(self.run_cli("--show")[0], 1)

    def test_without_a_terminal_it_explains_instead_of_hanging(self):
        saved = sys.stdin
        sys.stdin = io.StringIO("")             # not a tty
        try:
            code, text = self.run_cli()
        finally:
            sys.stdin = saved
        self.assertEqual(code, 2)
        self.assertIn("--temp-arc", text)
        self.assertIn("-it", text)

    def test_registered_with_the_conductor(self):
        import conductor
        self.assertIn("onboard", conductor.TANK_COMMANDS)
        self.assertIn("onboard", tank_tick.COMMANDS)


if __name__ == "__main__":
    unittest.main()
