#!/usr/bin/env python3
"""Radarr/Sonarr integration server for fuldc-arr-bridge.

Serves two faces on one port (default 9117):
  * Torznab indexer   -> /torznab/api   (search Direct Connect via FulDC++)
  * qBittorrent shim   -> /api/v2/...     (download client; added next)

Radarr/Sonarr config:
  Indexer (Generic Torznab):  URL http://<host>:9117/torznab   API key = TORZNAB_APIKEY
  Download client (qBittorrent): host <host>, port 9117

Env: FULDC_URL, FULDC_USER, FULDC_PASS, DC_ROOT, MOVIES_DIR, SERIES_DIR,
     TORZNAB_APIKEY (required), QBIT_USER / QBIT_PASS (optional download-client
     credentials), ARR_PORT (default 9117).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fuldc_client import FulDCClient
from httputil import REQUEST_TIMEOUT_SECONDS, read_body, secure_equal
from ranker import Prefs
import torznab
import qbit

# Session id handed out by /api/v2/auth/login. Random per process so a stale
# cookie from a previous run can't drive this one.
_SID = secrets.token_hex(16)


def client() -> FulDCClient:
    return FulDCClient(os.environ.get("FULDC_URL", "http://host.docker.internal:5600"),
                       os.environ.get("FULDC_USER", "admin"),
                       os.environ["FULDC_PASS"])


def _apikey_ok(params: dict) -> bool:
    # TORZNAB_APIKEY is required at startup, so `want` is always non-empty here
    want = os.environ.get("TORZNAB_APIKEY", "")
    return secure_equal(params.get("apikey", [""])[0], want)


class Handler(BaseHTTPRequestHandler):
    # See httputil.REQUEST_TIMEOUT_SECONDS: without this a caller can announce a body and never
    # send it, holding a request thread for as long as it likes -- before any authentication.
    timeout = REQUEST_TIMEOUT_SECONDS

    def _send(self, code: int, body: bytes, ctype: str = "application/xml; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _read_body(self) -> bytes:
        return read_body(self)

    def _form(self) -> dict:
        """Parse an urlencoded or multipart POST body into {field: value}."""
        body = self._read_body()
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ctype:
            m = re.search(r"boundary=(.+)", ctype)
            fields: dict = {}
            if m:
                for part in body.split(b"--" + m.group(1).strip().encode()):
                    if b"Content-Disposition" not in part:
                        continue
                    head, _, val = part.partition(b"\r\n\r\n")
                    nm = re.search(rb'name="([^"]+)"', head)
                    if nm:
                        fields[nm.group(1).decode()] = val.rstrip(b"\r\n").decode("utf-8", "replace")
            return fields
        return {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace")).items()}

    def _qbit_authed(self) -> bool:
        """Radarr/Sonarr log in once and then send the SID cookie. If no
        QBIT_USER/QBIT_PASS is configured we stay open (the original behaviour,
        for LAN-only setups); when they are set, every call must carry the SID."""
        if not os.environ.get("QBIT_PASS"):
            return True
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "SID" and secure_equal(v, _SID):
                return True
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = urllib.parse.parse_qs(parsed.query)
        if path in ("/torznab/api", "/api"):
            return self._torznab(params)
        if path.startswith("/api/v2/"):
            # version probes run before login, everything else needs a session
            if path not in ("/api/v2/app/version", "/api/v2/app/webapiVersion") \
                    and not self._qbit_authed():
                return self._send(403, b"Forbidden", "text/plain")
            return self._qbit_get(path)
        if path in ("", "/health"):
            return self._send(200, b"fuldc-arr-bridge arr server up", "text/plain")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path == "/api/v2/auth/login":
            f = self._form()
            want_pass = os.environ.get("QBIT_PASS", "")
            if want_pass:
                want_user = os.environ.get("QBIT_USER", "admin")
                ok = (secure_equal(f.get("username", ""), want_user)
                      and secure_equal(f.get("password", ""), want_pass))
                if not ok:
                    print(f"[qbit] failed login from {self.client_address[0]}", flush=True)
                    return self._send(200, b"Fails.", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Set-Cookie", f"SID={_SID}; HttpOnly; path=/")
            self.end_headers()
            return self.wfile.write(b"Ok.")
        if not self._qbit_authed():
            self._read_body()
            return self._send(403, b"Forbidden", "text/plain")
        if path == "/api/v2/torrents/add":
            f = self._form()
            urls = [u for u in re.split(r"[\r\n]+", f.get("urls", "")) if u.strip()]
            try:
                qbit.add(client(), urls, f.get("category", ""))
            except Exception:  # noqa: BLE001
                # "Ok." makes Radarr record a successful grab for a download
                # that will never exist: nothing in the queue, no retry, no
                # blocklist. "Fails." is what lets it try the next release.
                print(f"[qbit] add error:\n{traceback.format_exc()}", flush=True)
                return self._send(200, b"Fails.", "text/plain")
            return self._send(200, b"Ok.", "text/plain")
        if path == "/api/v2/torrents/delete":
            f = self._form()
            hashes = [h for h in f.get("hashes", "").split("|") if h]
            try:
                qbit.delete(client(), hashes,
                            f.get("deleteFiles", "false").lower() == "true")
            except Exception:  # noqa: BLE001
                print("[qbit] delete error:\n" + traceback.format_exc(), flush=True)
            return self._send(200, b"Ok.", "text/plain")
        if path in ("/api/v2/torrents/createCategory",
                    "/api/v2/torrents/editCategory"):
            # Radarr's client Test creates its category then re-reads
            # /categories and fails if it still isn't listed, so this must
            # actually be remembered.
            f = self._form()
            qbit.create_category(f.get("category", ""), f.get("savePath", ""))
            return self._send(200, b"Ok.", "text/plain")
        # setCategory / setForceStart / etc. — accept silently
        self._read_body()
        self._send(200, b"Ok.", "text/plain")

    def _qbit_get(self, path: str):
        if path == "/api/v2/app/version":
            return self._send(200, qbit.version().encode(), "text/plain")
        if path == "/api/v2/app/webapiVersion":
            return self._send(200, qbit.webapi_version().encode(), "text/plain")
        if path == "/api/v2/app/preferences":
            return self._json(qbit.preferences())
        if path == "/api/v2/torrents/categories":
            return self._json(qbit.categories())
        if path == "/api/v2/torrents/info":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cat = params.get("category", [None])[0]
            try:
                return self._json(qbit.info(client(), cat))
            except Exception:  # noqa: BLE001
                # An empty list reads to Radarr as "every download vanished",
                # which clears its queue on one transient blip. A 502 reads as
                # "client unreachable" — true, and recoverable.
                print(f"[qbit] info error:\n{traceback.format_exc()}", flush=True)
                return self._send(502, b"[]", "application/json")
        if path == "/api/v2/torrents/files":
            # Radarr deserializes this as a JSON *list*; {} is a parse error.
            return self._json([])
        if path == "/api/v2/torrents/properties":
            # IsTorrentLoaded() is literally this call after every add, so
            # answering {} for an unknown hash makes a failed add look
            # successful. 404 unless we are actually tracking it.
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            h = (params.get("hash", [""])[0] or "").lower()
            if h and h in qbit._torrents:
                return self._json(qbit.properties(h))
            return self._send(404, b"Not found", "text/plain")
        self._json({})

    def _torznab(self, params: dict):
        if not _apikey_ok(params):
            # Newznab convention is HTTP 200 with an <error> element, which is
            # what makes the *arr report "invalid API key" rather than a
            # generic connection failure.
            return self._send(
                200, b'<?xml version="1.0" encoding="UTF-8"?>\n'
                     b'<error code="100" description="Incorrect user credentials"/>')
        t = params.get("t", ["search"])[0]
        if t == "caps":
            return self._send(200, torznab.caps_xml().encode())

        q = params.get("q", [""])[0]
        cats = params.get("cat", [""])[0]
        kind = "series" if (t == "tvsearch" or str(torznab.TV_CAT) in cats) else "movie"
        season = None
        if params.get("season", [""])[0].isdigit():
            season = int(params["season"][0])
        # Guarded the way `season` above it is. This sat outside the try below, so a non-numeric
        # limit -- a typo, a probe, anything -- raised ValueError straight out of the handler and
        # the request got no response at all, only a traceback in the log. Clamped too: the value
        # comes from whatever indexer client is calling, and a huge limit is a search we then run.
        raw_limit = params.get("limit", ["50"])[0]
        limit = max(1, min(int(raw_limit) if raw_limit.isdigit() else 50, 500))
        try:
            items = torznab.search_items(client(), query=q, kind=kind,
                                         season=season, limit=limit, prefs=Prefs())
            print(f"[torznab] t={t} q={q!r} kind={kind} season={season} -> {len(items)} items", flush=True)
            self._send(200, torznab.feed_xml(items).encode())
        except Exception as e:  # noqa: BLE001
            print(f"[torznab] error: {e}", flush=True)
            self._send(200, torznab.feed_xml([]).encode())

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    import sys
    port = int(os.environ.get("ARR_PORT", "9117"))
    # Fail at startup, not on the first request. client() reads FULDC_PASS with os.environ[...],
    # so without it every search and every download raised a KeyError inside the handler thread:
    # the service came up, answered health probes 200, and returned a 500 to Radarr/Sonarr for
    # everything, with only a traceback in the log to say why. webhook_server.py has had this
    # check since it hit the same thing; this half was never given one.
    if not os.environ.get("FULDC_PASS"):
        sys.exit("FULDC_PASS is not set — the bridge cannot talk to FulDC++.")
    if not os.environ.get("TORZNAB_APIKEY"):
        sys.exit("TORZNAB_APIKEY is not set. This port can queue downloads on "
                 "your box — pick any random string, set it here and use the "
                 "same value as the indexer API key in Radarr/Sonarr.")
    if not os.environ.get("QBIT_PASS"):
        print("! QBIT_PASS is not set — the download-client endpoints accept "
              "any caller. Set QBIT_USER/QBIT_PASS and use them in the "
              "Radarr/Sonarr qBittorrent client config.", flush=True)
    print(f"fuldc-arr-bridge arr server listening on :{port} "
          f"(torznab /torznab/api, qbit /api/v2)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
