"""Tiny Plex helper: list library sections and trigger a (targeted) scan.

rar2fs breaks Plex's inotify change-detection, so after a DC download lands we
trigger a scan explicitly. A path-scoped refresh is cheaper than a full section
scan.
"""

from __future__ import annotations

import re
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# Plex disambiguates duplicate titles by appending "(YYYY)" to the title itself;
# the year is also in the `year` attr, so strip it or the folder double-years
# (Littlest Pet Shop (2012) -> Littlest.Pet.Shop.2012.2012).
_YEAR_SUFFIX = re.compile(r"\s*\((\d{4})\)\s*$")

# Plex serves HTTPS on 32400 with a *.plex.direct cert that never matches a raw
# LAN IP, so direct-IP access can't verify the hostname. The link is still TLS —
# we just skip cert validation, which is the norm for local Plex API access.
_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


class Plex:
    def __init__(self, base_url: str, token: str, timeout: int = 20):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> bytes:
        params = dict(params or {})
        params["X-Plex-Token"] = self.token
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        ctx = _TLS if url.lower().startswith("https") else None
        with urllib.request.urlopen(url, timeout=self.timeout, context=ctx) as r:
            return r.read()

    def sections(self) -> list[dict]:
        root = ET.fromstring(self._get("/library/sections"))
        return [{"key": d.get("key"), "type": d.get("type"), "title": d.get("title")}
                for d in root.findall("Directory")]

    def find_section(self, title: str) -> str | None:
        for s in self.sections():
            if (s["title"] or "").lower() == title.lower():
                return s["key"]
        return None

    def all_shows(self) -> list[dict]:
        """Every series in every TV (type=show) section, with the tmdb/tvdb/imdb
        ids Plex resolved. includeGuids=1 attaches <Guid id="tmdb://..."> children
        (the new Plex agent's own guid is an opaque plex:// id otherwise)."""
        out: list[dict] = []
        for s in self.sections():
            if s.get("type") != "show":
                continue
            root = ET.fromstring(self._get(f"/library/sections/{s['key']}/all",
                                           {"type": "2", "includeGuids": "1"}))
            for d in root.findall("Directory"):
                ids: dict[str, str] = {}
                for g in d.findall("Guid"):
                    gid = g.get("id") or ""
                    for pref, key in (("tmdb://", "tmdb"), ("tvdb://", "tvdb"),
                                      ("imdb://", "imdb")):
                        if gid.startswith(pref):
                            ids[key] = gid[len(pref):]
                yr = d.get("year") or ""
                raw = d.get("title") or ""
                m = _YEAR_SUFFIX.search(raw)
                out.append({
                    "title": _YEAR_SUFFIX.sub("", raw),
                    "year": int(yr) if yr.isdigit() else (int(m.group(1)) if m else None),
                    "tmdb": int(ids["tmdb"]) if ids.get("tmdb", "").isdigit() else None,
                    "tvdb": int(ids["tvdb"]) if ids.get("tvdb", "").isdigit() else None,
                    "imdb": ids.get("imdb"),
                })
        return out

    def scan(self, section_key: str, path: str | None = None) -> None:
        """Trigger a scan of a section, optionally scoped to a folder path
        (the path must be as PLEX sees it, not the FulDC++ Windows path)."""
        params = {"path": path} if path else None
        self._get(f"/library/sections/{section_key}/refresh", params)
