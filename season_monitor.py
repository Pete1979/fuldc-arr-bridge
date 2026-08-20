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

Self-limiting: only seasons that (a) have aired and (b) are newer than the
highest one already present/monitored are grabbed — it never backfills old gaps
or grabs a season before it airs. Additive only; nothing is removed.

Needs a metadata source (TMDB_API_KEY or SEERR_URL+SEERR_API_KEY); a no-op
without one.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

from fuldc_client import FulDCClient
from metadata import (aired_seasons, details, external_ids, find_tv_id,
                      is_kids_details, seerr_tv_requests, title_of, year_of)
from tvmaze import aired_seasons as tvmaze_aired
from core import monitor_tv_season, resolve_target

# capture the series root (series or kids.series), the show folder and season
_TARGET = re.compile(
    r"^(?P<root>.*\\(?:kids\.series|series))\\(?P<folder>[^\\]+)\\S(?P<season>\d{1,2})\\?$",
    re.I,
)
_YEAR = re.compile(r"\.(\d{4})$")
_DRIVE = re.compile(r"^[A-Za-z]:")


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
        res = monitor_tv_season(client, t["show"], s, year=t["year"],
                                dc_root=dc_root, movies_dir=movies_dir,
                                series_dir=t["series_dir"], quality=t["quality"],
                                log=log)
        _notify(t["query"], s, res, log)
        log(f"# [season] NEW: {t['query']} S{s:02d} -> monitor")
        added += 1
    return added


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
    log(f"# [season] sweep done: {added} new-season monitor(s) across "
        f"{len(targets)} shows")
    return added
