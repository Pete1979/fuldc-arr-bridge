"""Optional metadata lookup used to route kids content to dedicated folders.

Determines a title's genres from either TMDB directly (TMDB_API_KEY) or the
user's own Seerr/Jellyseerr/Overseerr instance (SEERR_URL + SEERR_API_KEY),
so an approved kids show/movie can be sent to kids.series / kids.movies instead
of the normal series / movies folders.

Stdlib only. If no source is configured the classifier simply returns False and
routing behaves exactly as before.
"""

from __future__ import annotations

import datetime
import json
import os
import urllib.parse
import urllib.request

TMDB_BASE = "https://api.themoviedb.org/3"
DEFAULT_KIDS_GENRES = {"kids", "family"}
ENDED_STATUSES = {"ended", "canceled", "cancelled"}


def _get_json(url: str, headers: dict | None = None, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _tmdb_details(tmdb_id: int, media_type: str, api_key: str) -> dict:
    kind = "tv" if media_type == "tv" else "movie"
    url = f"{TMDB_BASE}/{kind}/{tmdb_id}?api_key={urllib.parse.quote(api_key)}"
    return _get_json(url)


def _seerr_details(tmdb_id: int, media_type: str, base: str, api_key: str) -> dict:
    kind = "tv" if media_type == "tv" else "movie"
    url = f"{base.rstrip('/')}/api/v1/{kind}/{tmdb_id}"
    return _get_json(url, headers={"X-Api-Key": api_key})


def _details(tmdb_id: int | None, media_type: str, *, log=print) -> dict | None:
    """Fetch a title's metadata (genres + status) from TMDB or Seerr, or None if
    no source is configured / the lookup fails."""
    if not tmdb_id:
        return None
    tmdb_key = os.environ.get("TMDB_API_KEY", "").strip()
    seerr_url = os.environ.get("SEERR_URL", "").strip()
    seerr_key = os.environ.get("SEERR_API_KEY", "").strip()
    try:
        if tmdb_key:
            return _tmdb_details(tmdb_id, media_type, tmdb_key)
        if seerr_url and seerr_key:
            return _seerr_details(tmdb_id, media_type, seerr_url, seerr_key)
    except Exception as e:  # noqa: BLE001 - metadata is best-effort, never fatal
        log(f"# metadata lookup failed for {media_type} {tmdb_id}: {e}")
    return None


def genres_for(tmdb_id: int | None, media_type: str, *, log=print) -> list[str] | None:
    """Return the title's genre names, or None if unavailable."""
    d = _details(tmdb_id, media_type, log=log)
    return None if d is None else [g.get("name", "") for g in d.get("genres", [])]


def _kids_genre_set() -> set[str]:
    raw = os.environ.get("KIDS_GENRES", "").strip()
    if not raw:
        return set(DEFAULT_KIDS_GENRES)
    return {g.strip().lower() for g in raw.split(",") if g.strip()}


def _flags(d: dict, media_type: str) -> tuple[bool, bool]:
    genres = [g.get("name", "") for g in d.get("genres", [])]
    kids = any(name.lower() in _kids_genre_set() for name in genres)
    ended = (media_type == "tv"
             and (d.get("status") or "").strip().lower() in ENDED_STATUSES)
    return kids, ended


def _original_title(d: dict) -> str | None:
    """The title in its original language, when that language isn't English.

    DC/scene releases of a foreign film use its original title (Nordic content
    on Swedish hubs especially), not Seerr's translated display title. Handles
    TMDB (snake_case) and Seerr (camelCase); movie=title, tv=name.
    """
    lang = (d.get("original_language") or d.get("originalLanguage") or "").lower()
    if lang == "en":
        return None
    orig = (d.get("original_title") or d.get("originalTitle")
            or d.get("original_name") or d.get("originalName") or "").strip()
    return orig or None


def classify(tmdb_id: int | None, media_type: str, *, log=print) -> tuple[bool, bool]:
    """Return (is_kids, is_ended) from a single metadata lookup.

    is_kids: genres include a configured kids genre (default Kids/Family;
             'Animation' alone is NOT kids).
    is_ended: TV show whose status is Ended/Canceled — such shows should be
              grabbed as season packs, not monitored per-episode with %[inc].
    """
    d = _details(tmdb_id, media_type, log=log)
    if not d:
        return False, False
    return _flags(d, media_type)


def request_meta(tmdb_id: int | None, media_type: str,
                 *, log=print) -> tuple[bool, bool, str | None, int | None]:
    """(is_kids, is_ended, original_title, number_of_seasons) from one lookup.

    original_title is the non-English original-language title (what DC scene
    releases of foreign films are named), or None when English/unavailable.
    number_of_seasons lets the grab widen its search for a single-season show
    (whole series often shared as one COMPLETE pack with no S<NN> token).
    """
    d = _details(tmdb_id, media_type, log=log)
    if not d:
        return False, False, None, None
    kids, ended = _flags(d, media_type)
    nseasons = d.get("number_of_seasons") or d.get("numberOfSeasons")
    return kids, ended, _original_title(d), nseasons


def is_kids(tmdb_id: int | None, media_type: str, *, log=print) -> bool:
    return classify(tmdb_id, media_type, log=log)[0]


def _tmdb_search_tv(name: str, api_key: str) -> list[dict]:
    url = (f"{TMDB_BASE}/search/tv?api_key={urllib.parse.quote(api_key)}"
           f"&query={urllib.parse.quote(name)}")
    return _get_json(url).get("results", [])


def _seerr_search(name: str, base: str, api_key: str) -> list[dict]:
    url = f"{base.rstrip('/')}/api/v1/search?query={urllib.parse.quote(name)}"
    return _get_json(url, headers={"X-Api-Key": api_key}).get("results", [])


def find_tv_id(name: str, *, log=print) -> int | None:
    """Best-effort TMDB id for a show name (first TV match), or None."""
    if not name or not name.strip():
        return None
    tmdb_key = os.environ.get("TMDB_API_KEY", "").strip()
    seerr_url = os.environ.get("SEERR_URL", "").strip()
    seerr_key = os.environ.get("SEERR_API_KEY", "").strip()
    try:
        if tmdb_key:
            res = _tmdb_search_tv(name, tmdb_key)
            return res[0]["id"] if res else None
        if seerr_url and seerr_key:
            tv = [x for x in _seerr_search(name, seerr_url, seerr_key)
                  if x.get("mediaType") == "tv"]
            return tv[0]["id"] if tv else None
    except Exception as e:  # noqa: BLE001 - best-effort
        log(f"# tv search failed for {name!r}: {e}")
    return None


def aired_seasons(tmdb_id: int | None, *, log=print) -> set[int]:
    """Season numbers (>0) whose air date is on/before today — i.e. that have
    started airing and so have episodes to grab. Handles both TMDB (snake_case)
    and Seerr (camelCase) season objects."""
    d = _details(tmdb_id, "tv", log=log)
    if not d:
        return set()
    today = datetime.date.today().isoformat()
    out: set[int] = set()
    for s in d.get("seasons", []):
        n = s.get("seasonNumber", s.get("season_number", 0)) or 0
        air = (s.get("airDate") or s.get("air_date") or "")[:10]
        if n > 0 and air and air <= today:
            out.add(int(n))
    return out

