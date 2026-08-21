"""Optional media-server library enumeration for auto new-season detection.

The season monitor's follow set is normally your %[inc] monitors plus your Seerr
TV requests. That misses shows you *own* but never re-requested. When you point
the bridge at your media server (MEDIASERVER=plex|jellyfin) and opt in with
MONITOR_LIBRARY=1, the whole owned TV library joins the follow set — so "it's in
my library" is all it takes for new seasons to be picked up.

Media-server-agnostic like the rest of the bridge: unknown/unset backend simply
returns nothing and the monitor falls back to Seerr requests + monitors, which
every bridge user has (Seerr is required). Stdlib only, best-effort.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request


def owned_shows(*, log=print) -> list[dict]:
    """Series in the configured media server's library, each as
    {title, year, tmdb, tvdb, imdb} (ids may be None). Empty unless
    MONITOR_LIBRARY=1 and a supported MEDIASERVER is configured."""
    if os.environ.get("MONITOR_LIBRARY", "0") != "1":
        return []
    backend = os.environ.get("MEDIASERVER", "none").lower()
    try:
        if backend == "plex":
            return _plex_shows(log)
        if backend in ("jellyfin", "emby"):
            return _jellyfin_shows(log)
    except Exception as e:  # noqa: BLE001 - enumeration is best-effort
        log(f"# library enumeration ({backend}) failed: {e}")
    if backend not in ("plex", "jellyfin", "emby"):
        log(f"# MONITOR_LIBRARY set but MEDIASERVER={backend!r} has no library "
            "enumerator — following Seerr requests + monitors only")
    return []


def _plex_shows(log) -> list[dict]:
    from plex import Plex
    url, tok = os.environ.get("PLEX_URL"), os.environ.get("PLEX_TOKEN")
    if not (url and tok):
        log("# PLEX_URL/PLEX_TOKEN not set — skipping Plex library enumeration")
        return []
    shows = Plex(url, tok).all_shows()
    log(f"# library: {len(shows)} series from Plex")
    return shows


def _jellyfin_shows(log) -> list[dict]:
    url, tok = os.environ.get("JELLYFIN_URL"), os.environ.get("JELLYFIN_TOKEN")
    if not (url and tok):
        log("# JELLYFIN_URL/JELLYFIN_TOKEN not set — skipping library enumeration")
        return []
    q = urllib.parse.urlencode({"IncludeItemTypes": "Series", "Recursive": "true",
                                "Fields": "ProviderIds,ProductionYear"})
    req = urllib.request.Request(f"{url.rstrip('/')}/Items?{q}",
                                 headers={"X-Emby-Token": tok})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    out: list[dict] = []
    for it in data.get("Items", []):
        pid = it.get("ProviderIds") or {}
        # ProviderIds keys vary in case across server versions
        low = {k.lower(): v for k, v in pid.items()}
        tmdb, tvdb = low.get("tmdb"), low.get("tvdb")
        out.append({
            "title": it.get("Name"),
            "year": it.get("ProductionYear"),
            "tmdb": int(tmdb) if str(tmdb or "").isdigit() else None,
            "tvdb": int(tvdb) if str(tvdb or "").isdigit() else None,
            "imdb": low.get("imdb"),
        })
    log(f"# library: {len(out)} series from Jellyfin/Emby")
    return out
