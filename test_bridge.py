#!/usr/bin/env python3
"""Tests for fuldc-arr-bridge. Stdlib unittest, no network.

Run: python -m unittest -v test_bridge

The FulDC++ API calls are faked at the _call boundary, so these assert the
exact request bodies we send. Several of these encode behaviour that the API
requires but does not enforce — a missing use_params, for instance, produces a
working-looking AutoSearch item that silently never matches anything.
"""

from __future__ import annotations

import contextlib
import io
import os
import unicodedata
import time
import unittest
import unittest.mock
import xml.etree.ElementTree as ET

os.environ.setdefault("FULDC_PASS", "test")

import core
import fuldc_client
import httputil
import metadata
import qbit
import ranker
import store
import torznab
import webhook_server
from fuldc_client import PRIO_HIGH, PRIO_LOW, FulDCClient, FulDCError


class FakeClient(FulDCClient):
    """FulDCClient with the transport replaced by a scripted response table."""

    def __init__(self, responses=None):
        super().__init__("http://localhost:5600", "u", "p")
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses = responses or {}

    def _call(self, method, path, body=None):
        self.calls.append((method, path, body))
        key = (method, path)
        if key in self.responses:
            r = self.responses[key]
            return r.pop(0) if isinstance(r, list) else r
        if "/results/" in path or path.endswith("/items"):
            return 200, []          # list-shaped endpoints
        return 200, {"id": 1}

    def body_for(self, method, path) -> dict:
        for m, p, b in self.calls:
            if m == method and p == path:
                return b or {}
        raise AssertionError(f"no {method} {path} in {[(m, p) for m, p, _ in self.calls]}")

    # --- search-instance leak accounting ---------------------------------
    # A FulDC++ search instance lives server-side until DELETEd (or until the
    # session ends). Every path that creates one must release it, including the
    # exception paths — otherwise a flaky hub slowly fills the client with dead
    # instances and nothing in the bridge notices.
    @property
    def opened(self) -> int:
        return sum(1 for m, p, _ in self.calls if (m, p) == ("POST", "/search"))

    @property
    def closed(self) -> int:
        return sum(1 for m, p, _ in self.calls
                   if m == "DELETE" and p.startswith("/search/"))

    def assert_no_leak(self, test):
        test.assertEqual(self.opened, self.closed,
                         f"leaked {self.opened - self.closed} search instance(s): "
                         f"{[(m, p) for m, p, _ in self.calls]}")


class TestAutoSearchIncrementation(unittest.TestCase):
    """AutoSearch.cpp:207 returns early from formatParams when useParams is
    false, so %[inc] is searched for literally. The monitor looks fine in the
    UI and never matches."""

    def test_monitor_enables_use_params(self):
        c = FakeClient()
        core.monitor_tv_season(c, "Severance", 2, dc_root="S:\\dc", log=lambda m: None)
        body = c.body_for("POST", "/auto_search/items")
        self.assertTrue(body["use_params"], "%[inc] never expands without use_params")
        self.assertEqual(body["cur_number"], 1)
        self.assertEqual(body["max_number"], 0)      # 0 = no upper bound
        self.assertEqual(body["number_length"], 2)   # E01, not E1
        self.assertIn("%[inc]", body["search_string"])

    def test_monitor_can_start_mid_season(self):
        c = FakeClient()
        core.monitor_tv_season(c, "Severance", 2, first_episode=4, log=lambda m: None)
        self.assertEqual(c.body_for("POST", "/auto_search/items")["cur_number"], 4)

    def test_monitor_never_expires(self):
        c = FakeClient()
        core.monitor_tv_season(c, "Severance", 2, log=lambda m: None)
        body = c.body_for("POST", "/auto_search/items")
        self.assertNotIn("expire_time", body, "an ongoing show has no end date")
        self.assertFalse(body["remove_after_hit"])


class TestAutoSearchHygiene(unittest.TestCase):
    def test_always_checks_queue_and_share(self):
        c = FakeClient()
        c.create_autosearch("Dune 2021")
        body = c.body_for("POST", "/auto_search/items")
        self.assertTrue(body["check_already_queued"])
        self.assertTrue(body["check_already_shared"])

    def test_one_shot_items_expire(self):
        c = FakeClient()
        core.hybrid_grab(c, "Dune", 2021, wait=0, log=lambda m: None)
        body = c.body_for("POST", "/auto_search/items")
        self.assertIn("expire_time", body,
                      "an abandoned request would otherwise search hubs forever")

    def test_duplicate_updates_instead_of_raising(self):
        """POST 409s on a duplicate search_string (AutoSearchApi.cpp:322-325).
        Re-requesting a title must revive the existing item, not blow up."""
        c = FakeClient({
            ("POST", "/auto_search/items"): (409, {"message": "Duplicate"}),
            ("GET", "/auto_search/items"): (200, [{"id": 77, "search_string": "Dune 2021"}]),
            ("PATCH", "/auto_search/items/77"): (200, {"id": 77}),
        })
        item = c.create_autosearch("Dune 2021", target_directory="S:\\dc\\movies")
        self.assertEqual(item["id"], 77)
        patch = c.body_for("PATCH", "/auto_search/items/77")
        self.assertTrue(patch["enabled"], "a spent item must be re-enabled")
        self.assertNotIn("search_string", patch, "the match key is not patchable")

    def test_duplicate_with_no_match_still_raises(self):
        c = FakeClient({
            ("POST", "/auto_search/items"): (409, {"message": "Too short"}),
            ("GET", "/auto_search/items"): (200, []),
        })
        with self.assertRaises(FulDCError) as ctx:
            c.create_autosearch("x")
        self.assertEqual(ctx.exception.status, 409)


class TestSearchPriority(unittest.TestCase):
    """FileSearchParser.cpp:34-37 defaults an absent priority to LOW, which is
    the 15s per-hub interval and the first class shed by the 503 overflow guard
    at SearchEntity.cpp:184."""

    def test_priority_is_sent_and_is_top_level(self):
        c = FakeClient({("GET", "/search/1/results/0/200"): (200, [])})
        c.search("Dune 2021", wait=0)
        body = c.body_for("POST", "/search/1/hub_search")
        self.assertEqual(body["priority"], PRIO_HIGH)
        self.assertNotIn("priority", body["query"], "priority is a sibling of query")

    def test_background_callers_can_opt_down(self):
        c = FakeClient({("GET", "/search/1/results/0/200"): (200, [])})
        c.search("Dune", wait=0, priority=PRIO_LOW)
        self.assertEqual(c.body_for("POST", "/search/1/hub_search")["priority"], PRIO_LOW)

    def test_overflow_status_is_preserved(self):
        """503 must be distinguishable so callers can back off rather than
        treating it as a hard failure."""
        c = FakeClient({("POST", "/search/1/hub_search"): (503, {"message": "overflow"})})
        with self.assertRaises(FulDCError) as ctx:
            c.search("Dune", wait=0)
        self.assertEqual(ctx.exception.status, 503)


class TestTargetPathSafety(unittest.TestCase):
    """The show name arrives from the Seerr webhook, i.e. off the network."""

    def test_traversal_is_neutralised(self):
        for bad in [r"..\..\Users\Public", r"C:\Windows\Temp", "..", "  .. . ",
                    "Sev/er:ance", "con.txt"]:
            got = core.resolve_target("series", bad, None, r"S:\dc", None, 1)
            self.assertTrue(got.startswith("S:\\dc\\series\\"), got)
            self.assertNotIn("..", got)

    def test_normal_names_survive(self):
        got = core.resolve_target("series", "The Expanse", None, r"S:\dc", None, 3)
        self.assertEqual(got, "S:\\dc\\series\\The.Expanse\\S03\\")


class TestRanker(unittest.TestCase):
    def _res(self, path, size, users=2):
        return {"path": path, "size": size, "users": {"count": users},
                "slots": {"free": 4}, "type": {"id": "directory"}}

    def test_quality_in_subfolder_is_matched(self):
        """parse_release_folder skips the quality segment, so require_quality
        must look at the whole path or it filters out everything."""
        res = [self._res("/-x264-Kids/Dune.2021.BluRay.x264-GRP/1080p/", 9 * 1024**3),
               self._res("/share/Dune.2021.CAM.XviD/480p/", 1 * 1024**3)]
        cands = ranker.rank(res, "Dune", 2021, ranker.Prefs(require_quality=["1080p"]))
        self.assertEqual(len(cands), 1)
        self.assertIn("1080p", cands[0].reasons)

    def test_episode_escapes_the_movie_size_floor(self):
        ep = self._res("/tv/Severance.S02E03.1080p.WEB.x265/", 400 * 1024**2)
        c = ranker.score_result(ep, "Severance", None, ranker.Prefs(), kind="series")
        self.assertNotIn("too-small", c.reasons)

    def test_movie_still_has_a_size_floor(self):
        mv = self._res("/m/Dune.2021.1080p/", 400 * 1024**2)
        c = ranker.score_result(mv, "Dune", 2021, ranker.Prefs())
        self.assertIn("too-small", c.reasons)

    def test_hub_root_folder_does_not_poison_bad_source(self):
        r = self._res("/-TS-Releases/Dune.2021.1080p.BluRay/1080p/", 9 * 1024**3)
        self.assertNotIn("BAD-source",
                         ranker.score_result(r, "Dune", 2021, ranker.Prefs()).reasons)

    def test_real_cam_is_still_rejected(self):
        r = self._res("/m/Dune.2021.CAM.x264/", 2 * 1024**3)
        self.assertIn("BAD-source",
                      ranker.score_result(r, "Dune", 2021, ranker.Prefs()).reasons)

    def test_season_pack_beats_single_episode(self):
        res = [self._res("/tv/Severance.S02.COMPLETE.1080p/", 20 * 1024**3),
               self._res("/tv/Severance.S02E01.1080p/", 2 * 1024**3)]
        cands = ranker.rank(res, "Severance", None, ranker.Prefs(), kind="series")
        self.assertIn("S02.COMPLETE", cands[0].release)


class TestSceneTitle(unittest.TestCase):
    """DC releases are dotted, punctuation-stripped. A search token like 'Rings:'
    (colon attached) matches nothing in a dotted filename, so the search strings
    must be scene-formatted."""

    def test_colon_and_spaces_become_dots(self):
        self.assertEqual(
            ranker.scene_title("Lord of the Rings: The Rings of Power"),
            "Lord.of.the.Rings.The.Rings.of.Power")

    def test_apostrophe_dropped_hyphen_kept(self):
        self.assertEqual(ranker.scene_title("Marvel's Agatha All Along"),
                         "Marvels.Agatha.All.Along")
        self.assertEqual(ranker.scene_title("Spider-Man: Brand New Day"),
                         "Spider-Man.Brand.New.Day")

    def test_ampersand_becomes_and(self):
        # scene releases spell '&' as 'and' (Minions & Monsters -> Minions.and.Monsters)
        self.assertEqual(ranker.scene_title("Minions & Monsters"),
                         "Minions.and.Monsters")
        self.assertEqual(ranker.scene_title("Tom & Jerry"), "Tom.and.Jerry")
        # and the ranker scores the 'and' release as a full title match
        self.assertEqual(ranker.normalize("Minions & Monsters"),
                         ranker.normalize("Minions.and.Monsters"))

    def test_scene_search_folds_diacritics(self):
        # DC transliterates accents; the search string must too or the hub's
        # AND-match returns nothing (Lotta på Bråkmakargatan -> ...Pa.Brakmakargatan)
        self.assertEqual(ranker.scene_search("Lotta på Bråkmakargatan"),
                         "Lotta.pa.Brakmakargatan")
        self.assertEqual(ranker.search_queries("Lotta på Bråkmakargatan", 1992),
                         ["Lotta.pa.Brakmakargatan 1992", "Lotta.pa.Brakmakargatan"])

    def test_separator_hyphen_dropped_intraword_kept(self):
        # ' - ' is a scene title separator (collapses to a dot); an intra-word
        # hyphen (Spider-Man) stays. DC has Lotta.2.Lotta.Flyttar.Hemifran, not
        # Lotta.2.-.Lotta... — a bare '-' token would AND-match nothing.
        self.assertEqual(ranker.scene_search("Lotta 2 - Lotta flyttar hemifrån"),
                         "Lotta.2.Lotta.flyttar.hemifran")
        self.assertEqual(ranker.scene_title("Spider-Man: Brand New Day"),
                         "Spider-Man.Brand.New.Day")

    def test_monitor_matcher_has_no_punctuation(self):
        c = FakeClient()
        core.monitor_tv_season(c, "Lord of the Rings: The Rings of Power", 3,
                               log=lambda m: None)
        ss = c.body_for("POST", "/auto_search/items")["search_string"]
        self.assertNotIn(":", ss)
        self.assertIn("Rings.of.Power", ss)

    def test_monitor_matches_directories_only(self):
        # a RAR set also surfaces loose .rNN file results; file_type=any grabs a
        # single part instead of the release folder
        c = FakeClient()
        core.monitor_tv_season(c, "Lanterns", 1, log=lambda m: None)
        body = c.body_for("POST", "/auto_search/items")
        self.assertEqual(body.get("file_type"), "directory")


class TestOriginalTitle(unittest.TestCase):
    """Seerr sends the translated display title, but DC scene releases of a
    foreign film use its original-language title (Lotta on Rascal Street ->
    Lotta på Bråkmakargatan)."""

    def test_foreign_movie_returns_original(self):
        d = {"original_language": "sv",
             "original_title": "Lotta på Bråkmakargatan",
             "title": "Lotta on Rascal Street"}
        self.assertEqual(metadata._original_title(d), "Lotta på Bråkmakargatan")

    def test_english_returns_none(self):
        self.assertIsNone(metadata._original_title(
            {"original_language": "en", "original_title": "Whatever"}))

    def test_seerr_camelcase_tv(self):
        d = {"originalLanguage": "sv", "originalName": "Svenska Serien"}
        self.assertEqual(metadata._original_title(d), "Svenska Serien")


class TestSeasonPackPlacement(unittest.TestCase):
    """A season pack is a directory of episode folders; it must land AS the
    S<NN> folder, not nested series\\Show\\S<NN>\\<pack>\\<episodes>."""

    def _show(self):
        return core.resolve_target("series", "Norsemen", None, r"S:\dc",
                                   None, None, None, None, 2016)

    def _season(self):
        return core.resolve_target("series", "Norsemen", None, r"S:\dc",
                                   None, 2, None, None, 2016)

    def test_pack_contents_go_into_season_folder(self):
        tgt, name = core._download_placement(
            "series", "Norsemen", None, r"S:\dc", 2, None, None, 2016,
            "Norsemen.S02.iNTERNAL.1080p.WEB.X264-EDHD", self._season())
        self.assertEqual(tgt, self._show())
        self.assertEqual(name, "S02")

    def test_single_episode_keeps_release_folder(self):
        season = self._season()
        tgt, name = core._download_placement(
            "series", "Norsemen", None, r"S:\dc", 2, None, None, 2016,
            "Norsemen.S02E03.iNTERNAL.1080p.WEB.X264-EDHD", season)
        self.assertEqual(tgt, season)
        self.assertEqual(name, "Norsemen.S02E03.iNTERNAL.1080p.WEB.X264-EDHD")

    def test_movie_unchanged(self):
        tgt, name = core._download_placement(
            "movie", "Dune", None, r"S:\dc", None, None, None, 2021,
            "Dune.2021.1080p.WEB", r"S:\dc\movies" + "\\")
        self.assertEqual((tgt, name), (r"S:\dc\movies" + "\\", "Dune.2021.1080p.WEB"))


class TestCompleteFallback(unittest.TestCase):
    """A single-season ended show (esp. anime) is often shared as one COMPLETE
    pack or absolute-numbered episodes with no S<NN> token, so the season grab
    widens its search when the show has exactly one season."""

    def test_single_season_adds_complete_and_bare_queries(self):
        qs = core._queries("Fullmetal Alchemist Brotherhood", 2009, "series", 1,
                           complete=True)
        self.assertEqual(qs, [
            "Fullmetal.Alchemist.Brotherhood S01",
            "Fullmetal.Alchemist.Brotherhood S1",
            "Fullmetal.Alchemist.Brotherhood COMPLETE",
            "Fullmetal.Alchemist.Brotherhood",
        ])

    def test_default_keeps_only_season_queries(self):
        qs = core._queries("Fullmetal Alchemist Brotherhood", 2009, "series", 1)
        self.assertEqual(qs, ["Fullmetal.Alchemist.Brotherhood S01",
                              "Fullmetal.Alchemist.Brotherhood S1"])

    def test_request_meta_returns_season_count(self):
        import metadata
        orig = metadata._details
        metadata._details = lambda *a, **k: {
            "genres": [], "status": "Ended",
            "number_of_seasons": 1, "original_language": "en"}
        try:
            self.assertEqual(metadata.request_meta(123, "tv"),
                             (False, True, None, 1))
        finally:
            metadata._details = orig


class TestQualityPreference(unittest.TestCase):
    """QUALITY is a preference, not a hard filter: grab 1080p when it exists,
    but don't exclude unlabeled/other-quality releases when no 1080p is shared
    (anime/complete packs often carry no quality tag)."""

    def _res(self, path):
        return {"path": path, "type": {"id": "directory"},
                "size": 3_000_000_000, "users": {"count": 5}}

    def test_prefers_1080p_and_drops_720p_when_1080p_exists(self):
        prefs = ranker.Prefs(require_quality=["1080p"])
        cands = ranker.rank(
            [self._res("/x/Show.S02.720p.BluRay-A"),
             self._res("/x/Show.S02.1080p.BluRay-B")],
            "Show", None, prefs, kind="series")
        self.assertEqual(len(cands), 1)
        self.assertIn("1080p", cands[0].release.lower())

    def test_keeps_unlabeled_pack_when_no_1080p(self):
        prefs = ranker.Prefs(require_quality=["1080p"])
        cands = ranker.rank(
            [self._res("/Anime/Show.S02.Stardust.Crusaders")],
            "Show", None, prefs, kind="series")
        self.assertEqual(len(cands), 1)  # unlabeled pack NOT filtered out


class TestYearFolder(unittest.TestCase):
    def test_series_folder_gets_year(self):
        got = core.resolve_target("series", "Shameless", None, r"S:\dc", None, 3,
                                  year=2011)
        self.assertEqual(got, "S:\\dc\\series\\Shameless.2011\\S03\\")

    def test_dotted_scene_folder(self):
        got = core.resolve_target("series", "Star Trek: Strange New Worlds", None,
                                  r"S:\dc", None, 4, year=2022)
        self.assertEqual(
            got, "S:\\dc\\series\\Star.Trek.Strange.New.Worlds.2022\\S04\\")

    def test_no_year_is_unchanged(self):
        got = core.resolve_target("series", "Silo", None, r"S:\dc", None, 2)
        self.assertEqual(got, "S:\\dc\\series\\Silo\\S02\\")


class TestSeasonPackMatcher(unittest.TestCase):
    """An ended-show season AutoSearch must match a PACK, not a single episode
    (partial matching treats S03 as a substring of S03E02)."""

    def test_ended_season_uses_pack_regex(self):
        import re as _re
        c = FakeClient()
        core.hybrid_grab(c, "Shameless", None, kind="series", season=3,
                         prefs=ranker.Prefs(require_quality=["1080p"]),
                         wait=0, log=lambda m: None)
        body = c.body_for("POST", "/auto_search/items")
        self.assertEqual(body["matcher_type"], "regex")
        rx = _re.compile(body["matcher_string"])
        self.assertTrue(rx.search("Shameless.US.S03.1080p.BluRay.x264-ROVERS"))
        self.assertFalse(rx.search("Shameless.US.S03E02.1080p.BluRay.x264-ROVERS"),
                         "must not match a single episode")
        self.assertFalse(rx.search("Shameless.US.S03.720p.BluRay"),
                         "must not match the wrong quality")


class TestDirectoryDownloadResolution(unittest.TestCase):
    """Some FulDC++ builds return `directory_download_ids` as full objects, not
    bare ids — resolution must handle both or the download tracking crashes."""

    def test_object_form_dd_ids(self):
        c = FakeClient({
            ("POST", "/search/1/results/r1/download"):
                (200, {"directory_download_ids": [{"id": 50, "queue_info": None}]}),
            ("GET", "/filelists/directory_downloads/50"):
                (200, {"queue_info": {"bundle": {"id": 999, "merged": True}}}),
        })
        out = c.download_result(1, "r1", "S:\\dc\\series\\X\\S01\\", name="X.S01")
        self.assertEqual(out["bundle_id"], 999)


class TestExcludedWords(unittest.TestCase):
    """FulDC++ splits excluded_string on whitespace and matches each token as a
    SUBSTRING (SearchQuery::parseSearchString -> StringSearch.match_any, and
    StringSearch.h:27 calls itself "a fast substring search algo").

    A bare "ts" therefore excludes any title containing those two letters. The
    AutoSearch item looks correct in the UI and simply never fires."""

    # Real shows/films that contain a bad-source token as a substring
    INNOCENT = ["Ghosts", "Roots", "Nights", "Beatstreet", "Watchmen",
                "Scrubs", "Camelot", "The Last of Us", "Outlander",
                "Ted Lasso", "Notting Hill", "Scream"]

    # Genuinely bad releases, in the naming DC actually sees
    JUNK = ["Dune.2021.TS.x264-GRP", "Dune.2021.CAM.XviD",
            "Dune-2021-TS-GRP", "Movie.2020.TELESYNC.x264",
            "Movie.2020.HDCAM.x264", "Movie.2020.sample",
            "Movie.2020.SCR-GRP", "Movie.2020.WORKPRINT"]

    def _excluded(self, name: str) -> list[str]:
        return [tok for tok in core.BAD_SOURCE.split() if tok in name.lower()]

    def test_real_titles_are_not_excluded(self):
        for title in self.INNOCENT:
            self.assertEqual(self._excluded(title), [],
                             f"{title!r} would never match its own AutoSearch")

    def test_junk_is_still_excluded(self):
        for name in self.JUNK:
            self.assertTrue(self._excluded(name), f"{name!r} slipped through")

    def test_no_bare_short_tokens(self):
        """The guard: any token under 5 chars must be delimiter-anchored."""
        for tok in core.BAD_SOURCE.split():
            if len(tok.strip(".-")) < 5:
                self.assertTrue(tok[0] in ".-" and tok[-1] in ".-",
                                f"{tok!r} is short and unanchored — it will "
                                f"match inside ordinary words")

    def test_ranker_keeps_bare_tokens(self):
        """ranker.BAD_TOKENS matches on whitespace-split tokens, not
        substrings, so the bare forms are correct there and must stay."""
        self.assertIn("ts", ranker.BAD_TOKENS)
        clean = ranker.score_result(
            {"path": "/tv/Ghosts.S01.1080p/", "size": 9 * 1024**3,
             "users": {"count": 2}}, "Ghosts", None, ranker.Prefs(), kind="series")
        self.assertNotIn("BAD-source", clean.reasons)


class TestAutoSearchSizeFloor(unittest.TestCase):
    """The ranker rejects undersized results at -40, but the server-side
    AutoSearch had no floor at all — so the fallback path would happily grab a
    40 MB "sample" that a live search would have discarded."""

    def test_movie_fallback_sets_min_size(self):
        c = FakeClient()
        core.hybrid_grab(c, "Dune", 2021, wait=0, log=lambda m: None)
        body = c.body_for("POST", "/auto_search/items")
        self.assertEqual(body.get("min_size"), ranker.Prefs().min_size)

    def test_episode_monitor_uses_the_episode_floor(self):
        c = FakeClient()
        core.monitor_tv_season(c, "Severance", 2, log=lambda m: None)
        body = c.body_for("POST", "/auto_search/items")
        self.assertEqual(body.get("min_size"), ranker.Prefs().min_size_episode)
        self.assertLess(body["min_size"], ranker.Prefs().min_size)


class TestSearchInstanceLifetime(unittest.TestCase):
    """Every created search instance must be released on every path."""

    RESULT = [{"id": "ABC", "path": "/m/Dune.2021.1080p/", "size": 9 * 1024**3,
               "users": {"count": 3}, "slots": {"free": 4}}]

    def _client(self, extra=None):
        r = {("GET", "/search/1/results/0/200"): (200, list(self.RESULT)),
             ("POST", "/search/1/results/ABC/download"):
                 (200, {"bundle_info": {"id": 42}})}
        r.update(extra or {})
        return FakeClient(r)

    def test_closed_on_successful_grab(self):
        c = self._client()
        core.hybrid_grab(c, "Dune", 2021, wait=0, log=lambda m: None)
        c.assert_no_leak(self)

    def test_closed_when_download_fails(self):
        """This is the path that leaked: download_result raises, so the
        close() on the following line never runs."""
        c = self._client({("POST", "/search/1/results/ABC/download"):
                          (500, {"message": "boom"})})
        with self.assertRaises(FulDCError):
            core.hybrid_grab(c, "Dune", 2021, wait=0, log=lambda m: None)
        c.assert_no_leak(self)

    def test_closed_when_ranking_raises(self):
        c = self._client()
        with unittest.mock.patch("core.rank", side_effect=RuntimeError("bad")):
            with self.assertRaises(RuntimeError):
                core.hybrid_grab(c, "Dune", 2021, wait=0, log=lambda m: None)
        c.assert_no_leak(self)

    def test_closed_when_no_results(self):
        c = FakeClient({("GET", "/search/1/results/0/200"): (200, [])})
        core.hybrid_grab(c, "Dune", 2021, wait=0, log=lambda m: None)
        c.assert_no_leak(self)

    def test_closed_on_season_grab_failure(self):
        c = self._client({("POST", "/search/1/results/ABC/download"):
                          (500, {"message": "boom"})})
        with self.assertRaises(FulDCError):
            core.grab_tv_season(c, "Severance", 2, wait=0, log=lambda m: None)
        c.assert_no_leak(self)

    def test_closed_when_hub_search_rejected(self):
        """503 overflow: the instance already exists, so it must be released
        before the error propagates."""
        c = FakeClient({("POST", "/search/1/hub_search"): (503, {"message": "overflow"})})
        with self.assertRaises(FulDCError):
            c.search("Dune", wait=0)
        c.assert_no_leak(self)

    def test_closed_when_results_fetch_raises(self):
        c = FakeClient()
        c.responses[("GET", "/search/1/results/0/200")] = None   # -> TypeError
        with self.assertRaises(Exception):
            c.search("Dune", wait=0)
        c.assert_no_leak(self)

    def test_closed_when_indexer_ranking_raises(self):
        c = self._client()
        with unittest.mock.patch("torznab.rank", side_effect=RuntimeError("bad")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                torznab.search_items(c, query="Dune", kind="movie", season=None,
                                     limit=10, prefs=ranker.Prefs(), wait=0)
        c.assert_no_leak(self)


class TestSecureEqual(unittest.TestCase):
    """hmac.compare_digest raises TypeError on a non-ASCII str, and every
    secret we compare arrives from the network — so the raw call blew up
    before the auth decision was ever reached, resetting the connection
    instead of returning 401."""

    def test_matches_and_rejects(self):
        self.assertTrue(httputil.secure_equal("hunter2", "hunter2"))
        self.assertFalse(httputil.secure_equal("hunter2", "hunter3"))

    def test_non_ascii_is_a_mismatch_not_a_crash(self):
        for hostile in ["tökén", "日本語", "\udcff", "café", "Ω"]:
            self.assertFalse(httputil.secure_equal(hostile, "secret"))
            self.assertFalse(httputil.secure_equal("secret", hostile))
        self.assertTrue(httputil.secure_equal("tökén", "tökén"))

    def test_none_is_a_mismatch(self):
        self.assertFalse(httputil.secure_equal(None, "secret"))
        self.assertFalse(httputil.secure_equal("secret", None))
        self.assertFalse(httputil.secure_equal(None, None))


class FakeHandler:
    """Just enough of BaseHTTPRequestHandler for the body helpers."""

    def __init__(self, body: bytes, content_length=None):
        self.headers = {"Content-Length":
                        str(len(body)) if content_length is None else content_length}
        self.rfile = io.BytesIO(body)


class TestReadBody(unittest.TestCase):
    def test_reads_exactly(self):
        self.assertEqual(httputil.read_body(FakeHandler(b"hello")), b"hello")

    def test_malformed_length_returns_empty(self):
        # int("٣") is 3 in Python, but that is not a valid Content-Length
        for bad in ["abc", "", "1.5", "٣", "0x10"]:
            self.assertEqual(httputil.read_body(FakeHandler(b"data", bad)), b"")

    def test_negative_length_does_not_block(self):
        """read(-1) blocks until EOF, pinning a request thread."""
        self.assertEqual(httputil.read_body(FakeHandler(b"data", "-1")), b"")

    def test_caps_allocation(self):
        h = FakeHandler(b"x" * 100, str(10 * 1024**3))   # claims 10 GB
        self.assertLessEqual(len(httputil.read_body(h, max_bytes=64)), 64)

    def test_body_too_large_detects_the_claim(self):
        self.assertTrue(httputil.body_too_large(FakeHandler(b"", "999999999")))
        self.assertFalse(httputil.body_too_large(FakeHandler(b"", "10")))
        self.assertFalse(httputil.body_too_large(FakeHandler(b"", "junk")))


class TestWebhookPayloadHandling(unittest.TestCase):
    """handle() runs on a detached thread after the 200 was already sent, and
    the payload template is user-editable in Seerr, so anything can arrive."""

    def _capture(self, payload):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            webhook_server.handle(payload)
        return buf.getvalue()

    def test_survives_null_media(self):
        out = self._capture({"notification_type": "MEDIA_APPROVED",
                             "media": None, "subject": "Dune (2021)"})
        self.assertIn("[skip]", out)
        self.assertNotIn("Traceback", out)

    def test_survives_wrong_types(self):
        for payload in [{"notification_type": 5, "media": "nope", "subject": 12},
                        {"media": {"media_type": None}},
                        {"notification_type": "MEDIA_APPROVED", "media": []}]:
            self.assertNotIn("Traceback", self._capture(payload))

    def test_unknown_media_type_is_never_treated_as_a_movie(self):
        out = self._capture({"notification_type": "MEDIA_APPROVED",
                             "media": {"media_type": "anime"},
                             "subject": "Frieren (2023)"})
        self.assertIn("unsupported media_type", out)

    def test_errors_are_logged_with_a_traceback(self):
        with unittest.mock.patch("webhook_server.parse", side_effect=RuntimeError("x")):
            out = self._capture({"subject": "Dune"})
        self.assertIn("Traceback", out)
        self.assertIn("Dune", out)


class TestRequestedSeasons(unittest.TestCase):
    def _p(self, value):
        return {"extra": [{"name": "Requested Seasons", "value": value}]}

    def test_parses_a_list(self):
        self.assertEqual(webhook_server.requested_seasons(self._p("1, 2")), [1, 2])

    def test_specials_are_kept(self):
        self.assertEqual(webhook_server.requested_seasons(self._p("0")), [0])

    def test_implausible_numbers_are_dropped(self):
        """A stray year would otherwise create a permanent S2024 monitor."""
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(webhook_server.requested_seasons(self._p("2024, 3")), [3])

    def test_tolerates_junk(self):
        for payload in [{}, {"extra": None}, {"extra": ["nope"]},
                        self._p("All Seasons"), self._p(None)]:
            self.assertIsInstance(webhook_server.requested_seasons(payload), list)
class TestUnicodeDecoding(unittest.TestCase):
    """FulDC++ is a Windows app serving filenames that came off the hubs, so a
    cp1252 byte is normal traffic. A bare .decode() raised UnicodeDecodeError,
    which is not FulDCError and so escaped every caller and killed the request
    thread."""

    def test_cp1252_body_does_not_raise(self):
        raw = "Kärlek och Anarki".encode("cp1252")
        self.assertIsInstance(fuldc_client._decode(raw), str)

    def test_utf8_still_decodes_exactly(self):
        self.assertEqual(fuldc_client._decode("Kärlek".encode("utf-8")), "Kärlek")

    def test_lone_surrogate_bytes_do_not_raise(self):
        self.assertIsInstance(fuldc_client._decode(b"\xed\xa0\x80abc"), str)


class TestSwedishTitles(unittest.TestCase):
    """Swedish content is the stated primary use case and had no coverage."""

    def _res(self, path, size=9 * 1024**3, users=3):
        return {"path": path, "size": size, "users": {"count": users},
                "slots": {"free": 4}, "type": {"id": "directory"}}

    def test_accented_title_matches_transliterated_release(self):
        """'Kärlek' and 'Karlek' are the same film; without folding the title
        tokens score zero and an unrelated result wins."""
        res = [self._res("/m/Karlek.Och.Anarki.2020.1080p.WEB/"),
               self._res("/m/Something.Else.2020.1080p.WEB/")]
        best = ranker.rank(res, "Kärlek och Anarki", 2020, ranker.Prefs())[0]
        self.assertIn("Karlek", best.release)

    def test_nfd_input_matches_nfc_release(self):
        """Decomposed names arrive from macOS-originated shares and compare
        unequal to the composed form byte-for-byte."""
        nfd = unicodedata.normalize("NFD", "Kärlek")
        self.assertEqual(ranker.normalize(nfd), ranker.normalize("Karlek"))

    def test_more_swedish_titles_fold(self):
        for accented, plain in [("Sällskapsresan", "Sallskapsresan"),
                                ("Änglagård", "Anglagard"),
                                ("Jägarna", "Jagarna"),
                                ("Så som i himmelen", "Sa som i himmelen")]:
            self.assertEqual(ranker.normalize(accented), ranker.normalize(plain))

    def test_swedish_release_survives_the_feed(self):
        xml = torznab.feed_xml([{"title": "Änglagård.1992.1080p", "guid": "h",
                                 "size": 1, "magnet": "magnet:?xt=urn:btih:" + "e" * 40,
                                 "cat": 2000, "seeders": 1,
                                 "pubdate": "Tue, 12 Jan 2010 03:51:47 GMT",
                                 "infohash": "e" * 40}])
        self.assertIn("Änglagård", ET.fromstring(xml).findtext(".//item/title"))


class TestFeedWellFormedness(unittest.TestCase):
    def test_control_characters_do_not_blind_the_feed(self):
        """RssParser rejects the ENTIRE feed when the document is malformed, so
        one hostile folder name would hide every other release."""
        xml = torznab.feed_xml([{"title": "Dune\x00.2021\x08 & <x>", "guid": "h",
                                 "size": 1, "magnet": "magnet:?xt=urn:btih:" + "e" * 40,
                                 "cat": 2000, "seeders": 1,
                                 "pubdate": "Tue, 12 Jan 2010 03:51:47 GMT",
                                 "infohash": "e" * 40}])
        ET.fromstring(xml)          # raises if not well-formed


class TestRankerQuality(unittest.TestCase):
    def _res(self, path, size=9 * 1024**3, users=2):
        return {"path": path, "size": size, "users": {"count": users},
                "slots": {"free": 4}, "type": {"id": "directory"}}

    def test_short_title_words_need_a_whole_word_match(self):
        """A substring test lets 'up' hit almost any release, so a one-word
        title scored 1/1 against unrelated content and rode the size and user
        bonuses to the top."""
        self.assertTrue(ranker._token_in("up", "pixar up 2009 1080p"))
        self.assertFalse(ranker._token_in("up", "superman 2025 1080p"))

    def test_hub_root_folder_does_not_count_as_real_quality(self):
        """A hub root named /1080p-Releases/ must not make a 480p release count
        as 1080p: when a real 1080p exists, the DVDRip is dropped for it
        (quality is now a preference, so a lone DVDRip would still be kept)."""
        res = [self._res("/1080p-Releases/Dune.2021.DVDRip/480p/"),
               self._res("/x/Dune.2021.BluRay/1080p/")]
        cands = ranker.rank(res, "Dune", 2021, ranker.Prefs(require_quality=["1080p"]))
        self.assertEqual(len(cands), 1)
        self.assertIn("bluray", cands[0].release.lower())

    def test_real_quality_subfolder_still_passes(self):
        res = [self._res("/1080p-Releases/Dune.2021.BluRay/1080p/")]
        self.assertEqual(
            len(ranker.rank(res, "Dune", 2021, ranker.Prefs(require_quality=["1080p"]))), 1)

    def test_ties_break_on_sources_then_size(self):
        res = [self._res("/m/Dune.2021.1080p.WEB/", size=8 * 1024**3, users=1),
               self._res("/m/Dune.2021.1080p.WEB/", size=8 * 1024**3, users=9)]
        self.assertEqual(ranker.rank(res, "Dune", 2021, ranker.Prefs())[0]
                         .result["users"]["count"], 9)

class TestStoreEviction(unittest.TestCase):
    def setUp(self):
        store._store.clear()

    def test_get_promotes_so_active_entries_survive(self):
        """Insertion order is not LRU: without promotion on read, two indexers
        RSS-syncing evict the map within hours, so a release found in the
        morning fails as 'unknown magnet' that evening."""
        store.put("keep", {"release": "A"})
        for i in range(store._MAX):
            if i == store._MAX // 2:
                store.get("keep")          # touched halfway through
            store.put(f"f{i}", {"release": str(i)})
        self.assertIsNotNone(store.get("keep"),
                             "an entry read recently was evicted anyway")

    def test_cap_is_enforced(self):
        for i in range(store._MAX + 50):
            store.put(f"f{i}", {"release": str(i)})
        self.assertLessEqual(len(store._store), store._MAX)


class TestQbitReporting(unittest.TestCase):
    """Radarr's queue is driven entirely by /torrents/info. Reporting the
    wrong thing there loses downloads silently."""

    def setUp(self):
        qbit._torrents.clear()
        store._store.clear()

    def test_partial_bundle_failure_keeps_the_rest(self):
        """One unreachable bundle must not empty the whole response — Radarr
        reads [] as 'every download disappeared' and clears its queue."""
        qbit._track("a" * 40, {"name": "A", "category": "radarr", "size": 100,
                               "save_path": "S:\\dc\\movies", "added_on": 0,
                               "bundle_id": 1})
        qbit._track("b" * 40, {"name": "B", "category": "radarr", "size": 100,
                               "save_path": "S:\\dc\\movies", "added_on": 0,
                               "bundle_id": 2})

        class Boom(FakeClient):
            def list_bundles(self, *a, **kw):
                raise RuntimeError("FulDC++ unreachable")

            def get_bundle(self, bid):
                if bid == 1:
                    raise RuntimeError("gone")
                return {"id": 2, "status": {"id": "queued", "str": "50%"}}

        with contextlib.redirect_stdout(io.StringIO()):
            out = qbit.info(Boom(), "radarr")
        self.assertEqual(len(out), 2, "a failed lookup dropped the other torrent")

    def test_duplicate_add_is_ignored(self):
        h = "c" * 40
        qbit._track(h, {"name": "A", "category": "radarr", "size": 1,
                        "save_path": "", "added_on": 0, "bundle_id": 9})
        with contextlib.redirect_stdout(io.StringIO()):
            qbit.add(FakeClient(), [f"magnet:?xt=urn:btih:{h}"], "radarr")
        self.assertEqual(qbit._torrents[h]["bundle_id"], 9,
                         "a retried add re-queued the same release")

    def test_unknown_magnet_is_reported_failed(self):
        h = "d" * 40
        with contextlib.redirect_stdout(io.StringIO()):
            qbit.add(FakeClient(), [f"magnet:?xt=urn:btih:{h}"], "radarr")
        self.assertTrue(qbit._torrents[h]["failed"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(qbit.info(FakeClient(), "radarr")[0]["state"], "error")


class _NoBundles(FakeClient):
    def list_bundles(self, *a, **kw):
        return []


class TestArrPathBlockers(unittest.TestCase):
    """Five independent faults, each of which alone prevents a release ever
    reaching the library. Verified against Radarr/Sonarr's own source."""

    NAME = "Dune.2021.1080p.BluRay.x264-GRP"

    def setUp(self):
        qbit._torrents.clear()

    def _row(self, entry):
        qbit._track("a" * 40, entry)
        with contextlib.redirect_stdout(io.StringIO()):
            rows = qbit.info(_NoBundles(), "radarr")
        return rows[0] if rows else None

    def test_content_path_differs_from_save_path(self):
        """Radarr: if ContentPath == SavePath it sets the item to Warning and
        refuses to import — 'Path matches client base download directory'."""
        sp, cp = qbit._paths({"name": self.NAME, "save_path": "S:\\dc\\movies\\"},
                             {"target": "S:\\dc\\movies\\"})
        self.assertNotEqual(sp, cp)
        self.assertTrue(cp.endswith(self.NAME), cp)

    def test_content_path_is_not_doubled_when_target_is_the_item(self):
        sp, cp = qbit._paths({"name": self.NAME},
                             {"target": "S:\\dc\\movies\\" + self.NAME})
        self.assertEqual(cp, "S:\\dc\\movies\\" + self.NAME)
        self.assertEqual(sp, "S:\\dc\\movies")

    def test_seed_limits_say_use_global(self):
        """Omitted limits deserialize to 0, and HasReachedSeedLimit then reads
        that as an explicit limit already met — so Radarr deletes every
        completed download when 'Remove Completed Downloads' is on."""
        row = self._row({"name": self.NAME, "category": "radarr", "size": 100,
                         "save_path": "S:\\dc\\movies\\", "added_on": 0,
                         "bundle_id": None})
        for field in ("ratio_limit", "seeding_time_limit",
                      "inactive_seeding_time_limit"):
            self.assertEqual(row[field], -2, field)

    def test_failed_grab_is_dropped_so_radarr_retries(self):
        """Radarr maps 'error' to Warning, never Failed: the item would sit in
        the queue forever, never blocklisted, never retried."""
        entry = {"name": self.NAME, "category": "radarr", "size": 1,
                 "save_path": "", "bundle_id": None, "failed": True,
                 "added_on": int(time.time())}
        self.assertIsNotNone(self._row(dict(entry)), "should show during grace")
        qbit._torrents.clear()
        entry["added_on"] = int(time.time()) - qbit.FAILED_GRACE_SECONDS - 1
        self.assertIsNone(self._row(entry), "should be dropped after grace")

    def test_custom_category_survives_create(self):
        """The client Test creates its category, re-reads /categories, and
        fails if it still isn't listed."""
        qbit.create_category("radarr-4k", "S:\\dc\\4k")
        self.assertIn("radarr-4k", qbit.categories())

    def test_rss_query_is_not_empty(self):
        """Radarr's indexer Test is one RSS request with no search terms, and
        zero items is a hard ValidationFailure — so an empty result made the
        indexer impossible to add at all."""
        c = FakeClient({("GET", "/search/1/results/0/200"): (200, [])})
        with contextlib.redirect_stdout(io.StringIO()):
            torznab.search_items(c, query="", kind="movie", season=None,
                                 limit=10, prefs=ranker.Prefs(), wait=0)
        self.assertTrue(any(p.endswith("/hub_search") for _, p, _ in c.calls),
                        "an empty query never reached the hubs")


class TestTorznabFeedFields(unittest.TestCase):
    def test_pubdate_is_stable(self):
        """Radarr matches pending releases on title+pubDate+indexer, so a
        pubDate of 'now' produces duplicate pending entries and re-grabs."""
        h = "c" * 40
        self.assertEqual(torznab._pubdate({"time": 0}, h),
                         torznab._pubdate({"time": None}, h))

    def test_millisecond_timestamps_do_not_poison_the_feed(self):
        """RssParser rejects the ENTIRE feed when a pubDate fails to parse."""
        got = torznab._pubdate({"time": 1700000000000}, "d" * 40)
        self.assertIn("2023", got)

    def test_size_and_infohash_are_torznab_attrs(self):
        """A bare <size> element is not parsed; blocklisting keys on infohash."""
        xml = torznab.feed_xml([{"title": "X", "guid": "h", "size": 123,
                                 "magnet": "magnet:?xt=urn:btih:" + "e" * 40,
                                 "cat": 2000, "seeders": 1,
                                 "pubdate": "Tue, 12 Jan 2010 03:51:47 GMT",
                                 "infohash": "e" * 40}])
        self.assertIn('name="size" value="123"', xml)
        self.assertIn('name="infohash"', xml)


class TestSeasonMonitor(unittest.TestCase):
    """New-season sweep: add a %[inc] monitor when a season beyond the highest
    one you follow has aired. find_tv_id / aired_seasons are stubbed (network)."""

    def _client(self, search_string, target):
        return FakeClient({("GET", "/auto_search/items"):
                           (200, [{"id": 1, "search_string": search_string,
                                   "target": {"path": target}}])})

    def test_new_aired_season_creates_monitor(self):
        import season_monitor
        c = self._client("The.Boys S05E%[inc] 1080",
                         "S:\\dc\\series\\The.Boys.2019\\S05\\")
        season_monitor.find_tv_id = lambda name, log=print: 999
        season_monitor.aired_seasons = lambda tid, log=print: {1, 2, 3, 4, 5, 6}
        self.assertEqual(season_monitor.sweep(c, log=lambda m: None), 1)
        body = c.body_for("POST", "/auto_search/items")
        self.assertIn("The.Boys S06E%[inc]", body["search_string"])
        self.assertEqual(body["target"], "S:\\dc\\series\\The.Boys.2019\\S06\\")
        self.assertTrue(body["use_params"])

    def test_nothing_new_is_a_noop(self):
        import season_monitor
        c = self._client("Silo S03E%[inc] 1080", "S:\\dc\\series\\Silo.2023\\S03\\")
        season_monitor.find_tv_id = lambda name, log=print: 5
        season_monitor.aired_seasons = lambda tid, log=print: {1, 2, 3}
        self.assertEqual(season_monitor.sweep(c, log=lambda m: None), 0)

    def test_kids_series_root_preserved(self):
        import season_monitor
        c = self._client("VeggieTales S01E%[inc] 1080",
                         "S:\\dc\\kids.series\\VeggieTales.2014\\S01\\")
        season_monitor.find_tv_id = lambda name, log=print: 7
        season_monitor.aired_seasons = lambda tid, log=print: {1, 2}
        season_monitor.sweep(c, log=lambda m: None)
        self.assertEqual(c.body_for("POST", "/auto_search/items")["target"],
                         "S:\\dc\\kids.series\\VeggieTales.2014\\S02\\")


if __name__ == "__main__":
    unittest.main()
