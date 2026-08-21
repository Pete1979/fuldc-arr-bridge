"""Keyless secondary season source (TVmaze).

TMDB — the only thing Seerr exposes — lags on announcing new seasons of
continuing shows. Alien: Earth is the motivating case: TMDB has no season-2
object at all, so the TMDB-only season sweep can never see it, while TheTVDB
("Continuing"), IMDb and TVmaze all already list season 2.

TVmaze has a free, open, no-API-key endpoint and lets us look a show up by its
TheTVDB or IMDb id (both of which Seerr hands us in `externalIds`), so the match
is exact — no fuzzy name search. The sweep unions these aired seasons with
TMDB's; whichever source dates a new season first wins.

Stdlib only. Every call is best-effort: any failure returns an empty set so the
sweep just falls back to TMDB.
"""

from __future__ import annotations

import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TVMAZE_BASE = "https://api.tvmaze.com"


def _get_json(url: str, *, log=print):
    """GET + parse JSON, tolerating TVmaze's 429 rate limit once. Returns the
    decoded body, or None on any error (404 for an unknown id included)."""
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:  # rate limited: back off once
                time.sleep(0.5)
                continue
            return None
        except Exception as e:  # noqa: BLE001 - secondary source, never fatal
            log(f"# tvmaze {url}: {e}")
            return None
    return None


def _show_id(imdb_id: str | None, tvdb_id: int | None, name: str | None,
             *, log=print) -> int | None:
    """Resolve a TVmaze show id. Prefers an exact external-id lookup (TheTVDB or
    IMDb, both from Seerr); name search is a last resort and is skipped unless a
    name is explicitly passed."""
    if tvdb_id:
        d = _get_json(f"{TVMAZE_BASE}/lookup/shows?thetvdb={tvdb_id}", log=log)
        if isinstance(d, dict) and d.get("id"):
            return int(d["id"])
    if imdb_id:
        d = _get_json(f"{TVMAZE_BASE}/lookup/shows?imdb={urllib.parse.quote(imdb_id)}",
                      log=log)
        if isinstance(d, dict) and d.get("id"):
            return int(d["id"])
    if name:
        d = _get_json(f"{TVMAZE_BASE}/singlesearch/shows?q={urllib.parse.quote(name)}",
                      log=log)
        if isinstance(d, dict) and d.get("id"):
            return int(d["id"])
    return None


def aired_seasons(imdb_id: str | None = None, tvdb_id: int | None = None,
                  name: str | None = None, *, log=print) -> set[int]:
    """Season numbers whose premiere date is on/before today, per TVmaze.

    An undated season (announced but not yet airing — e.g. Alien: Earth S2
    today) is deliberately excluded: we only want to grab a season once it has
    actually started."""
    sid = _show_id(imdb_id, tvdb_id, name, log=log)
    if sid is None:
        return set()
    seasons = _get_json(f"{TVMAZE_BASE}/shows/{sid}/seasons", log=log)
    if not isinstance(seasons, list):
        return set()
    today = datetime.date.today().isoformat()
    out: set[int] = set()
    for s in seasons:
        n = s.get("number") or 0
        premiere = (s.get("premiereDate") or "")[:10]
        if n > 0 and premiere and premiere <= today:
            out.add(int(n))
    return out


def aired_season_dates(imdb_id: str | None = None, tvdb_id: int | None = None,
                       name: str | None = None, *, log=print) -> dict[int, str]:
    """{season number -> ISO premiere date} for seasons already airing, per
    TVmaze. Same source as aired_seasons(), but keeps the dates so the sweep can
    distinguish a brand-new season from a decade-old one on an ended show."""
    sid = _show_id(imdb_id, tvdb_id, name, log=log)
    if sid is None:
        return {}
    seasons = _get_json(f"{TVMAZE_BASE}/shows/{sid}/seasons", log=log)
    if not isinstance(seasons, list):
        return {}
    today = datetime.date.today().isoformat()
    out: dict[int, str] = {}
    for s in seasons:
        n = s.get("number") or 0
        premiere = (s.get("premiereDate") or "")[:10]
        if n > 0 and premiere and premiere <= today:
            out[int(n)] = premiere
    return out
