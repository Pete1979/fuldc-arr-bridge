"""Shared grab logic used by both the CLI and the Seerr webhook service.

Hybrid strategy:
  * content available now  -> download the ranked-best release immediately
  * nothing shared now     -> create a persistent AutoSearch item so FulDC++
                              downloads it natively once it appears (no
                              bridge-side retry loop)
"""

from __future__ import annotations

import re
from contextlib import contextmanager

from fuldc_client import PRIO_HIGH, FulDCClient
from ranker import Prefs, rank, search_queries, strip_leading_article, scene_title, scene_search

# Excluded words for server-side AutoSearch.
#
# CAREFUL: this is NOT the same matching as ranker.BAD_TOKENS. FulDC++ splits
# this string on whitespace and tests each token as a SUBSTRING
# (SearchQuery::parseSearchString -> StringSearch, "a fast substring search
# algo", evaluated with match_any). A bare "ts" therefore excludes Ghosts,
# Roots, Nights and Beatstreet — the AutoSearch item looks perfect in the UI
# and can never fire.
#
# So: long, distinctive tags go in bare; short ambiguous ones only in
# delimiter-anchored form, which still covers scene naming
# (Movie.2021.TS.x264, Movie.2021.TS-GROUP) without eating real titles.
# ranker.BAD_TOKENS keeps the bare forms — it matches on whitespace-split
# tokens, so it is not affected.
_BAD_LONG = ["camrip", "telesync", "telecine", "hdcam", "screener",
             "workprint", "sample"]
_BAD_SHORT = ["cam", "ts", "tc", "scr"]
BAD_SOURCE = " ".join(
    _BAD_LONG + [f"{lead}{t}{trail}"
                 for t in _BAD_SHORT
                 for lead, trail in ((".", "."), (".", "-"), ("-", "-"))]
)

# One-shot AutoSearch items (a specific movie or season) stop searching after
# this long. Without it an abandoned request searches the hubs forever. The
# %[inc] episode monitor is deliberately exempt — an ongoing show has no end.
AUTOSEARCH_TTL_DAYS = 60

# characters Windows forbids in a path component, plus control chars
_UNSAFE_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_component(name: str) -> str:
    """Make an untrusted string safe to use as ONE folder name.

    The show name comes straight off the Seerr webhook, i.e. off the network —
    without this, a subject like '..\\..\\Users\\Public (2020)' would walk the
    download target out of DC_ROOT and write anywhere on the FulDC++ host.
    Stripping the separators kills traversal; stripping leading/trailing dots
    kills the '..' and '.' components themselves.
    """
    cleaned = _UNSAFE_COMPONENT.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120].strip() or "unknown"


def resolve_target(kind: str, title: str, series: str | None,
                   dc_root: str = "S:\\dc", explicit: str | None = None,
                   season: int | None = None, movies_dir: str | None = None,
                   series_dir: str | None = None, year: int | None = None) -> str:
    """Windows target path on the FulDC++ host. Users set their own share root
    via DC_ROOT (e.g. D:\\Media), or override the movie/TV folders directly with
    MOVIES_DIR / SERIES_DIR for non-standard layouts.

    dc_root / movies_dir / series_dir / explicit are operator config and are
    trusted; the show name is not (see safe_component)."""
    if explicit:
        return explicit
    root = dc_root.rstrip("\\/")
    if kind == "series":
        base = (series_dir or f"{root}\\series").rstrip("\\/")
        # DC/scene convention: dotted show folder, year appended (Show.Name.2025)
        show = scene_title(safe_component(series or title))
        if year:
            show = f"{show}.{year}"
        p = f"{base}\\{show}\\"
        return p + f"S{season:02d}\\" if season else p
    md = (movies_dir or f"{root}\\movies").rstrip("\\/")
    return f"{md}\\"


def _queries(title: str, year: int | None, kind: str, season: int | None) -> list[str]:
    if kind == "series" and season:
        base = scene_search(strip_leading_article(title))
        return [f"{base} S{season:02d}", f"{base} S{season}"]
    return search_queries(title, year)


def run_search(client: FulDCClient, title: str, year: int | None,
               wait: float = 10.0, log=print, kind: str = "movie",
               season: int | None = None, priority: int = PRIO_HIGH):
    """Try fallback queries until one returns results. Returns (iid, results);
    iid may be None. Closes instances that yielded nothing.

    Pass PRIO_LOW for background/automated polling so those searches are the
    ones shed when the client's search queue backs up."""
    for q in _queries(title, year, kind, season):
        log(f"# search {q!r}")
        iid, results = client.search(q, wait=wait, priority=priority)
        if results:
            return iid, results
        client.close(iid)
    return None, []


@contextmanager
def searched(client: FulDCClient, title: str, year: int | None, *,
             wait: float = 10.0, log=print, kind: str = "movie",
             season: int | None = None, priority: int = PRIO_HIGH):
    """run_search, with the search instance guaranteed released.

    A FulDC++ search instance lives server-side until it is DELETEd or the
    session ends. Ranking, download_result and the network can all raise
    between opening one and closing it, so consumers go through here instead of
    pairing the calls by hand — every hand-paired site had at least one path
    that skipped the close.
    """
    iid, results = run_search(client, title, year, wait, log, kind, season, priority)
    try:
        yield iid, results
    finally:
        if iid is not None:
            try:
                client.close(iid)
            except Exception as e:  # noqa: BLE001 - never mask the real error
                log(f"# warning: could not close search instance {iid}: {e}")


def autosearch_matcher(title: str, year: int | None, kind: str = "movie",
                       season: int | None = None) -> str:
    base = scene_search(strip_leading_article(title))
    if kind == "series" and season:
        return f"{base} S{season:02d}"
    return f"{base} {year}" if year else base


def _autosearch_min_size(prefs: Prefs, kind: str, season: int | None) -> int:
    """Size floor to hand the server-side AutoSearch.

    A season request wants a pack, so the movie floor is right; a bare series
    request with no season may legitimately match one episode, so use the
    smaller episode floor there."""
    if kind == "series" and season is None:
        return prefs.min_size_episode
    return prefs.min_size


def hybrid_grab(client: FulDCClient, title: str, year: int | None, *,
                kind: str = "movie", series: str | None = None,
                season: int | None = None, prefs: Prefs | None = None,
                dc_root: str = "S:\\dc", movies_dir: str | None = None,
                series_dir: str | None = None, target: str | None = None,
                wait: float = 10.0, log=print) -> dict:
    prefs = prefs or Prefs()
    target = resolve_target(kind, title, series, dc_root, target, season,
                            movies_dir, series_dir, year)
    with searched(client, title, year, wait=wait, log=log,
                  kind=kind, season=season) as (iid, results):
        if results:
            cands = rank(results, title, year, prefs, kind=kind)
            if cands:
                best = cands[0]
                info = client.download_result(iid, best.result["id"], target,
                                              name=best.release)
                return {"mode": "download", "release": best.release,
                        "score": best.score, "bundle_id": info.get("bundle_id"),
                        "target": target, "season": season}
    # nothing available now -> persistent AutoSearch. Bake the required quality
    # into the search string so FulDC++ only grabs matching releases (the
    # server-side AutoSearch can't reuse the ranker's quality filter).
    matcher = autosearch_matcher(title, year, kind, season)
    if prefs.require_quality:
        matcher = f"{matcher} {prefs.require_quality[0]}"
    # For an ended-show SEASON, match a PACK not a single episode: partial
    # matching treats "S03" as a substring of "S03E02", so use a regex matcher
    # requiring the season token to be followed by a non-episode char (and the
    # required quality). The search_string still drives the hub search.
    matcher_type, matcher_string = "partial", ""
    if kind == "series" and season:
        looks = [f"(?=.*[Ss]{season:02d}(?:[^0-9Ee]|$))"]
        if prefs.require_quality:
            looks.append(f"(?=.*{re.escape(prefs.require_quality[0])})")
        matcher_type, matcher_string = "regex", "(?i)" + "".join(looks) + ".*"
    # Give the server the same size floor the ranker applies to live results —
    # otherwise AutoSearch happily grabs a 40 MB "sample" that rank() would
    # have thrown out at -40.
    item = client.create_autosearch(matcher, target_directory=target,
                                    excluded=BAD_SOURCE,
                                    expire_days=AUTOSEARCH_TTL_DAYS,
                                    matcher_type=matcher_type,
                                    matcher_string=matcher_string,
                                    file_type="directory",
                                    min_size=_autosearch_min_size(prefs, kind, season))
    return {"mode": "autosearch", "matcher": matcher,
            "autosearch_id": item.get("id"), "target": target, "season": season}


def grab_tv_season(client: FulDCClient, show: str, season: int, *,
                   year: int | None = None, prefs: Prefs | None = None,
                   dc_root: str = "S:\\dc",
                   movies_dir: str | None = None, series_dir: str | None = None,
                   quality: str | None = None, wait: float = 10.0,
                   log=print) -> dict:
    """Season request: grab a season pack now if one is shared, otherwise fall
    back to the %[inc] per-episode monitor.

    The monitor alone never picks up an already-complete older season until its
    next scheduled run, and it skips the season-pack preference in the ranker —
    so try a real search first, exactly like the movie path does."""
    prefs = prefs or Prefs()
    target = resolve_target("series", show, None, dc_root, None, season,
                            movies_dir, series_dir, year)
    with searched(client, show, None, wait=wait, log=log,
                  kind="series", season=season) as (iid, results):
        if results:
            cands = rank(results, show, None, prefs, kind="series")
            if cands:
                best = cands[0]
                info = client.download_result(iid, best.result["id"], target,
                                              name=best.release)
                log(f"# season pack {best.release!r} -> {target}")
                return {"mode": "download", "release": best.release,
                        "score": best.score, "bundle_id": info.get("bundle_id"),
                        "target": target, "season": season}
    return monitor_tv_season(client, show, season, year=year, dc_root=dc_root,
                             movies_dir=movies_dir, series_dir=series_dir,
                             quality=quality, prefs=prefs, log=log)


def monitor_tv_season(client: FulDCClient, show: str, season: int, *,
                      year: int | None = None, dc_root: str = "S:\\dc",
                      movies_dir: str | None = None,
                      series_dir: str | None = None, quality: str | None = None,
                      first_episode: int = 1, prefs: Prefs | None = None,
                      log=print) -> dict:
    """Create a persistent per-episode AutoSearch for an ongoing season using the
    AirDC++ %[inc] increment token — grabs each episode as it appears (existing
    and future). This is the Sonarr-style 'monitor an airing show' behavior,
    done natively by FulDC++. remove_after_hit stays False so it keeps going.

    use_params=True is what actually turns on %[inc] expansion; without it the
    token is searched for literally and the monitor silently never matches.
    """
    target = resolve_target("series", show, None, dc_root, None, season,
                            movies_dir, series_dir, year)
    base = scene_search(strip_leading_article(show))
    q = f" {quality}" if quality else ""
    matcher = f"{base} S{season:02d}E%[inc]{q}"
    # Match release DIRECTORIES only: a RAR set also surfaces its loose .rNN
    # parts as individual file results, and file_type=any would grab a single
    # 150 MB part instead of the folder.
    item = client.create_autosearch(matcher, target_directory=target,
                                    excluded=BAD_SOURCE, remove_after_hit=False,
                                    use_params=True, cur_number=first_episode,
                                    max_number=0, number_length=2,
                                    file_type="directory",
                                    min_size=(prefs or Prefs()).min_size_episode)
    log(f"# monitor {matcher!r} (from E{first_episode:02d}) -> {target}")
    return {"mode": "monitor", "matcher": matcher,
            "autosearch_id": item.get("id"), "target": target, "season": season}
