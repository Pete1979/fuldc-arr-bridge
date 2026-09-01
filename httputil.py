"""Shared helpers for the two HTTP front ends.

Both servers take requests from the network before they know who is calling, so
the first two things they do — compare a secret and read a body — have to be
safe against hostile input.
"""

from __future__ import annotations

import hmac

# Overseerr payloads are a few KB; a Radarr magnet POST is smaller still. A
# sanity ceiling, not a tuning knob.
MAX_BODY_BYTES = 1 * 1024 * 1024

# Socket timeout for a single request, applied by both handlers as a class attribute
# (StreamRequestHandler.setup calls settimeout with it).
#
# Without one, read_body below blocks until the declared number of bytes arrives or the peer
# closes. A caller that announces Content-Length: 1000000 and then sends nothing therefore pins a
# ThreadingHTTPServer thread for as long as it likes -- and both servers read the body BEFORE they
# know who is calling: the qBittorrent login route has nothing to authenticate against yet, and the
# webhook has no token to check when WEBHOOK_TOKEN is unset. A handful of those and the bridge
# stops answering anything.
#
# Generous enough that a slow but real client is never cut off; short enough that a stalled one is.
REQUEST_TIMEOUT_SECONDS = 30


def secure_equal(got: str | None, want: str | None) -> bool:
    """Constant-time comparison that tolerates arbitrary input.

    hmac.compare_digest raises TypeError when handed a str containing
    non-ASCII, and every secret we compare arrives from the network — a header,
    a query param, a cookie, a form field. Comparing UTF-8 bytes keeps the
    constant-time property while turning a hostile token into an ordinary
    mismatch instead of an exception that escapes before the auth decision is
    even made.
    """
    if got is None or want is None:
        return False
    return hmac.compare_digest(got.encode("utf-8", "surrogatepass"),
                               want.encode("utf-8", "surrogatepass"))


def content_length(handler) -> int:
    """Declared body length, or 0 if it isn't a valid one.

    RFC 9110 says Content-Length is ASCII digits. Python's int() is more
    generous — int("٣") is 3 and int(" -1 ") is -1 — and read(-1) blocks until
    EOF, which pins a request thread. Parse strictly, and treat anything else
    as absent rather than raising: a bare int() here would blow up before any
    response could be sent.
    """
    raw = (handler.headers.get("Content-Length") or "0").strip()
    if not raw.isascii() or not raw.isdigit():
        return 0
    return int(raw)


def read_body(handler, max_bytes: int = MAX_BODY_BYTES) -> bytes:
    """Read a request body without trusting the declared length.

    Never allocates more than max_bytes, so an unauthenticated caller cannot
    make us buffer arbitrarily on the strength of a header before the auth
    check has run.
    """
    n = content_length(handler)
    if n <= 0:
        return b""
    return handler.rfile.read(min(n, max_bytes))


def body_too_large(handler, max_bytes: int = MAX_BODY_BYTES) -> bool:
    return content_length(handler) > max_bytes
