"""Auto new-season detection.

Sonarr's one job that the Seerr flow doesn't cover on its own: when a show you
already follow gets a *new* season, start grabbing it without a fresh request.

Two things make this hard on this setup, both fixed here:

  * TMDB (all Seerr exposes) lags on announcing new seasons of continuing shows
    — Alien: Earth has no season-2 object on TMDB at all, though TheTVDB/IMDb/
    TVmaze list it. So aired seasons are the UNION of TMDB and TVmaze (keyless,
    matched by the TheTVDB/IMDb id Seerr hands us). Whichever dates the new
    season first wins.

  * A show grabbed as a season pack has no %[inc] monitor to key off, so the
    followed-show set is the UNION of live %[inc] monitors AND your Seerr TV
    requests. For a pack-grabbed show we read the seasons actually on the share
    (FulDC++ find_dupe_paths) to know the newest one you have.

Self-limiting: only seasons that (a) have aired, (b) are newer than the highest
one already present/monitored, and (c) aired recently (within SEASON_RECENT_DAYS,
default 540) are grabbed — so it never backfills old gaps, grabs a season before
it airs, or pulls the whole tail of an ended show you're a few seasons behind on
(that's what a Seerr request is for). Additive by default.

Two housekeeping passes run each sweep: it re-pins file_type=directory + the
loose-part excludes on every %[inc] monitor (FulDC++ resets file_type on its own
save cycle, which would otherwise let a lone RAR part get grabbed); it retires a
season-N monitor once a later season of the same show exists (a finished season
never gets new episodes); and, when PRUNE_UNSHARED=1, it removes a monitor whose
show folder has been deleted from the share (guarded: only if it had grabbed
episodes and nothing is downloading into it) — so deleting a show from the share
is the single 'stop following' switch.

When a new season is first followed, its already-aired episodes are grabbed in
one pass (not trickled one per %[inc] search cycle), then the monitor is pointed
at the first still-missing episode.

Needs a metadata source (TMDB_API_KEY or SEERR_URL+SEERR_API_KEY); a no-op
without one.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import urllib.request

from fuldc_client import FulDCClient
from metadata import (aired_season_dates, aired_seasons, details, external_ids,
                      find_tv_id, is_kids_details, seerr_tv_requests, title_of,
                      year_of)
from tvmaze import aired_season_dates as tvmaze_dates
from tvmaze import aired_seasons as tvmaze_aired
from tvmaze import aired_episodes as tvmaze_episodes
from ranker import scene_title
from core import (AUTOSEARCH_EXCLUDE, LOOSE_PART, backfill_episodes,
                  monitor_tv_season, resolve_target, safe_component)
import library

# capture the series root (series or kids.series), the show folder and season
_TARGET = re.compile(
    r"^(?P<root>.*\\(?:kids\.series|series))\\(?P<folder>[^\\]+)\\S(?P<season>\d{1,2})\\?$",
    re.I,
)
_YEAR = re.compile(r"\.(\d{4})$")
_DRIVE = re.compile(r"^[A-Za-z]:")


def _recent_days() -> int:
    """How recently a season must have aired to count as a *new* season worth
    auto-grabbing. Older aired seasons are treated as backfill (request via
    Seerr) rather than grabbed, so an ended show you're a few seasons behind on
    doesn't get its whole tail pulled in. Configurable via SEASON_RECENT_DAYS."""
    try:
        return max(1, int(os.environ.get("SEASON_RECENT_DAYS", "540")))
    except ValueError:
        return 540


def _first_missing(aired_eps, present) -> int:
    """Where to start the %[inc] monitor after a backfill: the first aired
    episode not yet present (so a not-yet-released latest episode is still
    watched), or one past the last aired episode if the season is complete."""
    if not aired_eps:
        return 1
    for e in sorted(aired_eps):
        if e not in present:
            return e
    return max(aired_eps) + 1


def _adc(win_path: str) -> str:
    """Windows share path -> ADC virtual path for find_dupe_paths. Assumes the
    share's virtual root is the last component of DC_ROOT (S:\\dc -> /dc), which
    holds for this setup. Trailing slash is preserved (find_dupe_paths needs it
    to treat a directory as a directory)."""
    p = _DRIVE.sub("", win_path).replace("\\", "/")
    return p if p.startswith("/") else "/" + p


def _series_dir(kids: bool, dc_root: str) -> str:
    root = dc_root.rstrip("\\/")
    if kids:
        return os.environ.get("KIDS_SERIES_DIR") or f"{root}\\kids.series"
    return os.environ.get("SERIES_DIR") or f"{root}\\series"


def _quality(search_string: str) -> str | None:
    """Trailing quality token after the %[inc] placeholder, if any."""
    parts = search_string.split("%[inc]", 1)
    return (parts[1].strip() or None) if len(parts) == 2 else None


def _target_path(item) -> str | None:
    tgt = item.get("target") if isinstance(item, dict) else None
    if isinstance(tgt, dict):
        return tgt.get("path")
    return tgt if isinstance(tgt, str) else None


def _occupied(client: FulDCClient) -> list[str]:
    """Lowercased target paths of everything already queued or monitored, so a
    season already being worked isn't grabbed twice (a pack still downloading
    isn't in the share yet, so find_dupe_paths wouldn't catch it)."""
    segs: list[str] = []
    for lister in (client.list_bundles, client.list_autosearch):
        try:
            items = lister()
        except Exception:  # noqa: BLE001 - best-effort dedupe
            items = None
        if isinstance(items, list):
            for it in items:
                tp = _target_path(it)
                if tp:
                    segs.append(tp.lower())
    return segs


def _busy(occupied: list[str], folder: str, season: int) -> bool:
    seg = f"{folder}\\s{season:02d}".lower()
    return any(seg in o for o in occupied)


def _folder_present(client: FulDCClient, adc_show: str) -> bool:
    return bool(client.find_dupe_paths(adc_show)
                or client.find_dupe_paths(adc_show.rstrip("/")))


def _present_seasons(client: FulDCClient, adc_show: str, upto: int) -> set[int]:
    out: set[int] = set()
    for n in range(1, min(upto, 100) + 1):
        if client.find_dupe_paths(f"{adc_show}S{n:02d}/"):
            out.add(n)
    return out


def _locate(client: FulDCClient, dc_root: str, folder: str):
    """Which series root (series vs kids.series) actually holds this show on the
    share, if any. Lets a library show route correctly without needing genres —
    and drops shows not in the DC share (nothing to grab into)."""
    for kids in (False, True):
        sdir = _series_dir(kids, dc_root).rstrip("\\/")
        adc = _adc(sdir + "\\" + folder + "\\")
        if _folder_present(client, adc):
            return sdir, adc
    return None, None


def _monitored(client: FulDCClient) -> dict[tuple[str, str], dict]:
    """Group the live %[inc] monitors by (series-root, show-folder)."""
    shows: dict[tuple[str, str], dict] = {}
    for it in client.list_autosearch():
        ss = it.get("search_string") or ""
        if "%[inc]" not in ss:
            continue
        m = _TARGET.search(_target_path(it) or "")
        if not m:
            continue
        key = (m.group("root"), m.group("folder"))
        d = shows.setdefault(key, {"seasons": set(), "quality": None})
        d["seasons"].add(int(m.group("season")))
        d["quality"] = d["quality"] or _quality(ss)
    return shows


def _notify(show: str, season: int, res: dict, log) -> None:
    mode = res.get("mode")
    log(f"# [season] PING: new season available — {show} S{season:02d} ({mode})")
    hook = os.environ.get("SEASON_NOTIFY_WEBHOOK", "").strip()
    if not hook:
        return
    try:
        data = json.dumps({"event": "new_season", "show": show, "season": season,
                           "mode": mode, "target": res.get("target")}).encode()
        req = urllib.request.Request(hook, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
        log(f"# [season] notified {hook}")
    except Exception as e:  # noqa: BLE001 - notification is best-effort
        log(f"# [season] notify webhook failed: {e}")


def _collect_targets(client: FulDCClient, dc_root: str,
                     movies_dir: str | None, log) -> dict[str, dict]:
    """Followed shows keyed by their ADC show path: %[inc] monitors UNION Seerr
    TV requests, merged so a show followed both ways is processed once."""
    targets: dict[str, dict] = {}

    for (root, folder), d in _monitored(client).items():
        ym = _YEAR.search(folder)
        year = int(ym.group(1)) if ym else None
        base = folder[: ym.start()] if ym else folder
        adc = _adc(f"{root}\\{folder}\\")
        t = targets.setdefault(adc.lower(), dict(
            show=base, query=base.replace(".", " ").strip(), year=year,
            series_dir=root, folder=folder, adc_show=adc, tmdb=None,
            monitored=set(), quality=None))
        t["monitored"] |= d["seasons"]
        t["quality"] = t["quality"] or d["quality"]

    default_quality = os.environ.get("QUALITY", "").strip() or None
    for tid in seerr_tv_requests(log=log):
        dd = details(tid, "tv", log=log)
        if not dd:
            continue
        name = title_of(dd)
        if not name:
            continue
        sdir = _series_dir(is_kids_details(dd), dc_root)
        show_win = resolve_target("series", name, None, dc_root, None, None,
                                  movies_dir, sdir, year_of(dd))
        folder = show_win.rstrip("\\/").rsplit("\\", 1)[-1]
        adc = _adc(show_win)
        t = targets.setdefault(adc.lower(), dict(
            show=name, query=name, year=year_of(dd), series_dir=sdir,
            folder=folder, adc_show=adc, tmdb=tid, monitored=set(),
            quality=default_quality))
        t["tmdb"] = t["tmdb"] or tid

    # optional: every show in your media-server library (MONITOR_LIBRARY=1),
    # located to whichever DC root actually holds it
    for sh in library.owned_shows(log=log):
        name = sh.get("title") or ""
        if not name:
            continue
        yr = sh.get("year")
        folder = scene_title(safe_component(name))
        if yr:
            folder = f"{folder}.{yr}"
        sdir, adc = _locate(client, dc_root, folder)
        if not sdir:
            continue  # not in the DC share -> nothing to grab into
        t = targets.setdefault(adc.lower(), dict(
            show=name, query=name, year=yr, series_dir=sdir, folder=folder,
            adc_show=adc, tmdb=sh.get("tmdb"), monitored=set(),
            quality=default_quality))
        t["tmdb"] = t["tmdb"] or sh.get("tmdb")
    return targets


def _process(client: FulDCClient, t: dict, occupied: list[str], dc_root: str,
             movies_dir: str | None, log) -> int:
    tid = t["tmdb"] or find_tv_id(t["query"], log=log)
    if not tid:
        log(f"# [season] no TMDB match for {t['query']!r} — skipping")
        return 0
    imdb, tvdb = external_ids(tid, log=log)
    aired = aired_seasons(tid, log=log) | tvmaze_aired(imdb_id=imdb, tvdb_id=tvdb, log=log)
    if not aired:
        return 0
    # Air dates let us tell a genuinely new season from a decade-old one on an
    # ended show the user is merely behind on: the latter must NOT be
    # auto-backfilled (that's what a Seerr request is for). A season with no
    # known date falls through to the old "grab it" behaviour.
    dates = {**tvmaze_dates(imdb_id=imdb, tvdb_id=tvdb, log=log),
             **aired_season_dates(tid, log=log)}
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=_recent_days())).isoformat()

    if t["monitored"]:
        baseline = max(t["monitored"])
    else:
        # a Seerr-requested show with no monitor: only follow it if it's really
        # on the share (grabbed), and treat the newest present season as the
        # baseline so we don't re-grab what's already there.
        if not _folder_present(client, t["adc_show"]):
            return 0
        present = _present_seasons(client, t["adc_show"], max(aired))
        if not present:
            return 0
        baseline = max(present)

    added = 0
    for s in sorted(aired):
        if s <= baseline or _busy(occupied, t["folder"], s):
            continue
        premiere = dates.get(s)
        if premiere and premiere < cutoff:
            # aired too long ago to be a *new* season — this is backfill on a
            # show you're behind on, not a new drop. Leave it for a Seerr request.
            log(f"# [season] skip {t['query']} S{s:02d} (aired {premiere}, "
                f"older than {_recent_days()}d) — backfill, request via Seerr")
            continue
        # Grab every already-aired episode now (a new season is often several
        # episodes deep), then point the %[inc] monitor at the first one still
        # missing so it only watches for what's left / future episodes.
        eps = tvmaze_episodes(imdb_id=imdb, tvdb_id=tvdb, season=s, log=log)
        present = backfill_episodes(client, t["show"], s, eps, dc_root=dc_root,
                                    movies_dir=movies_dir, series_dir=t["series_dir"],
                                    year=t["year"], quality=t["quality"], log=log) if eps else set()
        res = monitor_tv_season(client, t["show"], s, year=t["year"],
                                dc_root=dc_root, movies_dir=movies_dir,
                                series_dir=t["series_dir"], quality=t["quality"],
                                first_episode=_first_missing(eps, present), log=log)
        _notify(t["query"], s, res, log)
        log(f"# [season] NEW: {t['query']} S{s:02d} -> monitor "
            f"(backfilled {len(present)}/{len(eps)} aired eps)")
        added += 1
    return added


def _retire_finished(client: FulDCClient, log) -> int:
    """Retire a season-N %[inc] monitor once a later season of the same show
    exists (monitored or on the share): a show never adds episodes to an older
    season, so the monitor is dead weight sitting past the last episode. The
    latest season's monitor is always kept (nothing later exists yet)."""
    by_show: dict[tuple[str, str], dict[int, dict]] = {}
    for it in client.list_autosearch():
        if "%[inc]" not in (it.get("search_string") or ""):
            continue
        m = _TARGET.search(_target_path(it) or "")
        if m:
            by_show.setdefault((m.group("root"), m.group("folder")), {})[int(m.group("season"))] = it
    retired = 0
    for (root, folder), seasons in by_show.items():
        for n, it in seasons.items():
            later = any(x > n for x in seasons)
            if not later:
                later = bool(client.find_dupe_paths(_adc(f"{root}\\{folder}\\S{n + 1:02d}\\")))
            if later and client.delete_autosearch(it["id"]):
                log(f"# [season] RETIRE {folder} S{n:02d}: a later season exists "
                    f"-> removed finished-season monitor")
                retired += 1
    return retired


def _reassert_guards(client: FulDCClient, log) -> int:
    """FulDC++ resets an AutoSearch's directory-only file_type back to "any" (0)
    on its own save cycle (verified), which lets a lone RAR part get grabbed. Re-
    pin file_type=directory AND the loose-part excludes on every enabled %[inc]
    monitor each sweep — the excludes are the part that actually persists."""
    fixed = 0
    for it in client.list_autosearch():
        if "%[inc]" not in (it.get("search_string") or "") or not it.get("enabled"):
            continue
        patch: dict = {}
        if str(it.get("file_type")) not in ("directory", "7"):
            patch["file_type"] = "directory"
        exc = it.get("excluded_string") or ""
        if ".r0" not in exc:
            patch["excluded_string"] = f"{exc} {LOOSE_PART}".strip() if exc else AUTOSEARCH_EXCLUDE
        if patch:
            client.update_autosearch(it["id"], patch)
            fixed += 1
    if fixed:
        log(f"# [season] re-pinned directory/loose-part guards on {fixed} monitor(s)")
    return fixed


def _downloading_into(client: FulDCClient, folder: str) -> bool:
    """True if any bundle is queued/downloading into this show folder, so a
    first grab that the share hasn't indexed yet is never pruned."""
    seg = f"{folder}\\".lower()
    try:
        bundles = client.list_bundles(0, 400)
    except Exception:  # noqa: BLE001 - can't tell -> keep the monitor
        return True
    return any(seg in (_target_path(b) or "").lower() for b in bundles)


def _prune_unshared(client: FulDCClient, dc_root: str, log) -> int:
    """Opt-in (PRUNE_UNSHARED=1): make deleting a show from the share the single
    off-switch for following it. Remove a %[inc] monitor once its show folder is
    gone from the share. Guards: only if it has grabbed at least one episode
    (cur>1 = content existed) and nothing is downloading into it right now."""
    if os.environ.get("PRUNE_UNSHARED", "0").strip().lower() not in ("1", "true", "yes"):
        return 0
    removed = 0
    for it in client.list_autosearch():
        ss = it.get("search_string") or ""
        if "%[inc]" not in ss:
            continue
        m = _TARGET.search(_target_path(it) or "")
        if not m or (it.get("cur_number") or 1) <= 1:
            continue
        adc = _adc(f"{m.group('root')}\\{m.group('folder')}\\")
        if _folder_present(client, adc) or _downloading_into(client, m.group("folder")):
            continue
        if client.delete_autosearch(it["id"]):
            log(f"# [season] UNFOLLOW: {m.group('folder')} gone from share -> removed {ss!r}")
            removed += 1
    return removed


def sweep(client: FulDCClient, *, dc_root: str = "S:\\dc",
          movies_dir: str | None = None, log=print) -> int:
    """Add a %[inc] monitor for every newly-aired season beyond the highest one
    already followed. Returns the number of monitors created."""
    occupied = _occupied(client)
    targets = _collect_targets(client, dc_root, movies_dir, log)
    added = 0
    for t in targets.values():
        try:
            added += _process(client, t, occupied, dc_root, movies_dir, log)
        except Exception as e:  # noqa: BLE001 - one bad show must not stop the sweep
            log(f"# [season] error on {t.get('folder')!r}: {e}")
    _reassert_guards(client, log)
    retired = _retire_finished(client, log)
    pruned = _prune_unshared(client, dc_root, log)
    log(f"# [season] sweep done: {added} new-season monitor(s) across "
        f"{len(targets)} shows; retired {retired}; pruned {pruned}")
    return added
