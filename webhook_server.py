#!/usr/bin/env python3
"""Seerr -> FulDC++ webhook receiver.

On a Seerr "Media Approved" (or auto-approved) notification for a MOVIE, kicks
off a hybrid grab (immediate download or AutoSearch fallback). Stdlib only.

Env: FULDC_URL, FULDC_USER, FULDC_PASS, DC_ROOT, PORT (default 8080),
     MOVIES_ONLY (default "0"), WEBHOOK_TOKEN (optional shared secret),
     MEDIASERVER (optional post-download library refresh).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fuldc_client import FulDCClient
from httputil import body_too_large, read_body, secure_equal
from ranker import Prefs, fold
from core import grab_tv_season, hybrid_grab
from metadata import request_meta

APPROVED = {"MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"}
YEAR_RE = re.compile(r"\((\d{4})\)")


def client() -> FulDCClient:
    return FulDCClient(os.environ.get("FULDC_URL", "http://host.docker.internal:5600"),
                       os.environ.get("FULDC_USER", "admin"),
                       os.environ["FULDC_PASS"])


def parse(payload: dict):
    """Pull (notification_type, media_type, title, year, tmdbId) from a payload.

    Every field is defensive: the payload template is user-editable in Seerr's
    settings, all values arrive substituted as strings, and `media` is nulled
    out entirely for notifications with no media attached (issue comments,
    test pings).
    """
    nt = str(payload.get("notification_type") or "")
    media = payload.get("media")
    media = media if isinstance(media, dict) else {}
    mtype = str(media.get("media_type") or "").lower()
    subject = str(payload.get("subject") or "").strip()
    m = YEAR_RE.search(subject)
    year = int(m.group(1)) if m else None
    title = (YEAR_RE.sub("", subject).strip(" -") if m else subject).strip()
    try:
        tmdb = int(media.get("tmdbId")) if media.get("tmdbId") not in (None, "") else None
    except (TypeError, ValueError):
        tmdb = None
    return nt, mtype, title, year, tmdb


def _request_dirs(kids: bool):
    """Return (movies_dir, series_dir) for this request. Kids content goes to
    dedicated folders (default <DC_ROOT>\\kids.movies / kids.series)."""
    if kids:
        root = os.environ.get("DC_ROOT", "S:\\dc").rstrip("\\/")
        mov = os.environ.get("KIDS_MOVIES_DIR") or f"{root}\\kids.movies"
        ser = os.environ.get("KIDS_SERIES_DIR") or f"{root}\\kids.series"
        return mov, ser
    return os.environ.get("MOVIES_DIR"), os.environ.get("SERIES_DIR")


# A season number outside this range is a parse artefact (a year, an id), not
# a season. Accepting one creates an AutoSearch that can never match.
MAX_SEASON = 100


def requested_seasons(payload: dict) -> list[int]:
    """Seerr puts requested seasons in the `extra` array as
    {"name": "Requested Seasons", "value": "1, 2"}."""
    for e in payload.get("extra") or []:
        if not isinstance(e, dict):
            continue
        if str(e.get("name", "")).lower().startswith("requested season"):
            found = {int(x) for x in re.findall(r"\d+", str(e.get("value", "")))}
            good = sorted(n for n in found if 0 <= n <= MAX_SEASON)
            for bad in sorted(found - set(good)):
                print(f"[skip] implausible season number {bad}", flush=True)
            return good
    return []


def _prefs() -> Prefs:
    p = Prefs()
    q = os.environ.get("QUALITY", "").strip().lower()
    if q:
        p.require_quality = [q]
        if q not in p.prefer_quality:
            p.prefer_quality = [q] + p.prefer_quality
    return p


def _after_download(c: FulDCClient, res: dict, kind: str) -> None:
    """If the operator configured a media server, wait for the bundle and poke it
    so Seerr flips to Available without waiting for the next periodic scan."""
    if res.get("mode") != "download" or not res.get("bundle_id"):
        return
    if os.environ.get("MEDIASERVER", "none").lower() in ("", "none"):
        return
    final = c.wait_bundle(res["bundle_id"])
    fsid = (final or {}).get("status", {}).get("id")
    print(f"[bundle] {res['bundle_id']} final status: {fsid}", flush=True)
    if fsid in c.DONE_ON_DISK:
        from notify import refresh
        refresh(kind)


def _grab(title, year, *, kind, season=None, movies_dir=None, series_dir=None,
          single_season=False):
    print(f"[grab] {title!r} ({year}) type={kind}" + (f" S{season:02d}" if season else ""),
          flush=True)
    try:
        c = client()
        res = hybrid_grab(c, title, year, kind=kind, season=season,
                          prefs=_prefs(), dc_root=os.environ.get("DC_ROOT", "S:\\dc"),
                          movies_dir=movies_dir, series_dir=series_dir,
                          complete_fallback=single_season,
                          log=lambda m: print(m, flush=True))
        print(f"[done] {res}", flush=True)
        _after_download(c, res, kind)
    except Exception:  # noqa: BLE001 - webhook must never crash the server
        print(f"[error] {title!r}:\n{traceback.format_exc()}", flush=True)


def _grab_season(title, season, *, series_dir=None, year=None):
    q = os.environ.get("QUALITY", "").strip() or None
    print(f"[grab] {title!r} series S{season:02d}", flush=True)
    try:
        c = client()
        res = grab_tv_season(c, title, season, year=year, prefs=_prefs(),
                             dc_root=os.environ.get("DC_ROOT", "S:\\dc"),
                             movies_dir=os.environ.get("MOVIES_DIR"),
                             series_dir=series_dir,
                             quality=q, log=lambda m: print(m, flush=True))
        print(f"[done] {res}", flush=True)
        _after_download(c, res, "series")
    except Exception:  # noqa: BLE001 - webhook must never crash the server
        print(f"[error] {title!r} S{season}:\n{traceback.format_exc()}", flush=True)


def handle(payload: dict) -> None:
    # Runs on a detached thread after the 200 was already sent, so an escaping
    # exception would vanish into a bare threading traceback with no record of
    # which request died.
    try:
        _handle(payload)
    except Exception:  # noqa: BLE001 - a bad payload must not die silently
        print(f"[error] unhandled webhook failure for "
              f"subject={payload.get('subject')!r}:\n{traceback.format_exc()}", flush=True)


def _handle(payload: dict) -> None:
    nt, mtype, title, year, tmdb = parse(payload)
    if nt not in APPROVED:
        print(f"[skip] notification_type={nt}", flush=True)
        return
    if mtype not in ("movie", "tv"):
        # Never guess. Falling through to the movie branch on an unknown type
        # is how a TV show ends up in DC_ROOT\movies\.
        print(f"[skip] unsupported media_type={mtype!r}", flush=True)
        return
    if os.environ.get("MOVIES_ONLY", "0") == "1" and mtype != "movie":
        print(f"[skip] media_type={mtype} (movies only)", flush=True)
        return
    if not title:
        print("[skip] empty title", flush=True)
        return
    kids, ended, alt, nseasons = request_meta(tmdb, mtype, log=lambda m: print(m, flush=True))
    if os.environ.get("KIDS_ROUTING", "1") != "1":
        kids = False
    # DC releases of a foreign film use its original title, but only when that
    # is Latin-script (Nordic/European). A non-Latin original (CJK, Cyrillic)
    # can't match ASCII scene names, so keep Seerr's romanized display title.
    if alt and alt != title and all(ord(c) < 128 for c in fold(alt) if c.isalpha()):
        print(f"[orig] searching original title {alt!r} (Seerr display: {title!r})", flush=True)
        title = alt
    mov_dir, ser_dir = _request_dirs(kids)
    if kids:
        print(f"[kids] routing {title!r} -> kids folders", flush=True)
    if mtype == "tv":
        seasons = requested_seasons(payload)
        if ended:
            # Ended/canceled show: episodes are already out (usually as season
            # packs). Grab each requested season as a pack instead of a %[inc]
            # per-episode monitor that would never find anything.
            print(f"[ended] {title!r} -> season-pack grab (no %[inc] monitor)", flush=True)
            single = nseasons == 1  # whole series may be shared as one COMPLETE pack
            for season in (seasons or [None]):
                _grab(title, year, kind="series", season=season,
                      series_dir=ser_dir, single_season=single)
        elif seasons:
            for season in seasons:
                _grab_season(title, season, series_dir=ser_dir, year=year)   # pack now, else %[inc] monitor
        else:
            _grab(title, year, kind="series", series_dir=ser_dir)
    else:
        _grab(title, year, kind="movie", movies_dir=mov_dir)


# /health talks to FulDC++, so cache it: k8s probes every 10s and a readiness
# check must not become a load source of its own.
_HEALTH_TTL = 15.0
_health_cache: tuple[float, bool, str] = (0.0, False, "")
_health_lock = threading.Lock()


def _fuldc_reachable() -> tuple[bool, str]:
    """Is the configured FulDC++ actually answering?

    The readiness answer has to depend on this. A probe that returns 200
    regardless reports a pod as healthy with FULDC_PASS unset and the client
    unreachable, which is how a broken deploy silently accepts and drops every
    request."""
    now = time.monotonic()
    with _health_lock:
        stamp, ok, detail = _health_cache
        if now - stamp < _HEALTH_TTL:
            return ok, detail
    try:
        info = client().system_info()
        ok, detail = True, f"ok: FulDC++ {info.get('client_version', '?')}"
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"FulDC++ unreachable: {e}"
    with _health_lock:
        globals()["_health_cache"] = (now, ok, detail)
    return ok, detail


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes = b"ok") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """Optional shared secret. This endpoint queues downloads on your box,
        so if WEBHOOK_TOKEN is set we require it.

        Stock Overseerr can only send one configured `Authorization` header, so
        that form is accepted alongside Jellyseerr's custom X-Webhook-Token and
        the ?token= query param. Unset = open (LAN-only); warned at startup.
        """
        want = os.environ.get("WEBHOOK_TOKEN", "")
        if not want:
            return True
        auth = self.headers.get("Authorization", "")
        for candidate in (self.headers.get("X-Webhook-Token", ""),
                          auth[7:] if auth[:7].lower() == "bearer " else auth,
                          urllib.parse.parse_qs(
                              urllib.parse.urlparse(self.path).query).get("token", [""])[0]):
            if candidate and secure_equal(candidate, want):
                return True
        return False

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path == "/health":
            ok, detail = _fuldc_reachable()
            return self._send(200 if ok else 503, detail.encode())
        # "/" stays an unconditional 200: it is the liveness answer, and a
        # liveness probe that depends on FulDC++ would restart this process
        # every time the client is restarted.
        self._send(200, b"fuldc-arr-bridge webhook up")

    def do_POST(self):
        # Authorize before buffering: an unauthenticated caller must not be
        # able to make us allocate on the strength of a Content-Length header.
        if not self._authorized():
            print(f"[deny] unauthorized webhook from {self.client_address[0]}", flush=True)
            return self._send(401, b"unauthorized")
        if body_too_large(self):
            return self._send(413, b"payload too large")
        raw = read_body(self)
        try:
            payload = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        # The payload template is user-editable in Seerr, so a JSON array or a
        # bare string can legitimately arrive here.
        if not isinstance(payload, dict):
            print(f"[skip] payload is {type(payload).__name__}, not an object", flush=True)
            return self._send(200, b"ignored")
        self._send(200, b"accepted")
        threading.Thread(target=handle, args=(payload,), daemon=True).start()

    def log_message(self, *_):
        pass


def _start_season_monitor() -> None:
    """Background sweep that adds %[inc] monitors for newly-aired seasons of
    shows you already follow. Off unless SEASON_CHECK_HOURS > 0."""
    try:
        hours = float(os.environ.get("SEASON_CHECK_HOURS", "0") or 0)
    except ValueError:
        hours = 0.0
    if hours <= 0:
        return
    import season_monitor

    def _loop():
        time.sleep(60)  # let the pod settle before the first sweep
        while True:
            try:
                season_monitor.sweep(client(),
                                     dc_root=os.environ.get("DC_ROOT", "S:\\dc"),
                                     movies_dir=os.environ.get("MOVIES_DIR"),
                                     log=lambda m: print(m, flush=True))
            except Exception as e:  # noqa: BLE001
                print(f"[season] sweep error: {e}", flush=True)
            time.sleep(hours * 3600)

    threading.Thread(target=_loop, daemon=True).start()
    print(f"season monitor on: checking for new seasons every {hours:g}h", flush=True)


if __name__ == "__main__":
    import sys
    port = int(os.environ.get("PORT", "8080"))
    # Fail at startup, not on the first webhook. Without this the service comes
    # up, answers every health probe 200, and only whispers a KeyError into
    # stdout once a real request arrives — by which point the request is lost.
    if not os.environ.get("FULDC_PASS"):
        sys.exit("FULDC_PASS is not set — the bridge cannot talk to FulDC++.")
    if not os.environ.get("DC_ROOT"):
        print("! DC_ROOT is not set; falling back to S:\\dc, which is probably "
              "not where your share lives.", flush=True)
    if not os.environ.get("WEBHOOK_TOKEN"):
        print("! WEBHOOK_TOKEN is not set — anyone who can reach this port can "
              "queue downloads. Set it (and add ?token=… to the Seerr webhook "
              "URL) unless this port is strictly LAN-internal.", flush=True)
    _start_season_monitor()
    print(f"fuldc-arr-bridge webhook listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
