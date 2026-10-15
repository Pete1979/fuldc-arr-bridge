"""Prometheus exposition of FulDC++ health, served at GET /metrics.

The bridge already holds the FulDC++ credentials, so it is the natural place to
watch the client: is it up, are the hubs connected, is the share still there.
Stdlib only, text exposition format 0.0.4.
"""

from __future__ import annotations

import threading
import time
from typing import Callable
from urllib.parse import urlparse

from fuldc_client import FulDCClient

# Prometheus scrapes every 30s; never hit FulDC++ more often than this.
CACHE_TTL = 20.0
_cache: tuple[float, str] = (0.0, "")
_lock = threading.Lock()


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._seen: set[str] = set()

    def add(self, name: str, value, help_: str, kind: str = "gauge",
            labels: dict | None = None) -> None:
        if name not in self._seen:
            self._seen.add(name)
            self.lines.append(f"# HELP {name} {help_}")
            self.lines.append(f"# TYPE {name} {kind}")
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items()) + "}"
        v = float(value)
        self.lines.append(f"{name}{lab} {int(v) if v.is_integer() else v!r}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _hub_label(h: dict) -> str:
    name = ((h.get("identity") or {}).get("name") or "").strip()
    return name or urlparse(str(h.get("hub_url", ""))).hostname or str(h.get("id", "?"))


def collect(c: FulDCClient) -> str:
    out = _Out()
    try:
        info = c.system_info()
    except Exception:  # noqa: BLE001 - "down" is the answer, not an error
        out.add("fuldc_up", 0, "1 if the FulDC++ Web API answers.")
        return out.text()

    out.add("fuldc_up", 1, "1 if the FulDC++ Web API answers.")
    out.add("fuldc_info", 1, "FulDC++ client version.",
            labels={"version": info.get("client_version", "?")})
    if info.get("client_started"):
        out.add("fuldc_client_started_timestamp_seconds", info["client_started"],
                "Unix time the FulDC++ client was started.")

    def section(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            ok = 0
        except Exception:  # noqa: BLE001 - one bad endpoint must not hide the rest
            ok = 1
        out.add("fuldc_collect_error", ok, "1 if this section could not be read.",
                labels={"section": name})

    def hubs() -> None:
        hs = c.hubs()
        connected = 0
        for h in hs:
            up = (h.get("connect_state") or {}).get("id") == "connected"
            connected += up
            out.add("fuldc_hub_connected", int(up), "1 if this hub is connected.",
                    labels={"hub": _hub_label(h)})
        out.add("fuldc_hubs_total", len(hs), "Hubs configured in FulDC++.")
        out.add("fuldc_hubs_connected", connected, "Hubs currently connected.")

    def share() -> None:
        s = c.share_stats()
        out.add("fuldc_share_files", s.get("total_file_count", 0), "Files in the share.")
        out.add("fuldc_share_directories", s.get("total_directory_count", 0),
                "Directories in the share.")
        out.add("fuldc_share_searches_total", s.get("total_searches", 0),
                "Incoming searches since client start.", kind="counter")

    def transfers() -> None:
        t = c.transfer_stats()
        out.add("fuldc_download_speed_bytes", t.get("speed_down", 0), "Download speed (B/s).")
        out.add("fuldc_upload_speed_bytes", t.get("speed_up", 0), "Upload speed (B/s).")
        out.add("fuldc_queued_bytes", t.get("queued_bytes", 0), "Bytes left in the queue.")
        out.add("fuldc_downloads_active", t.get("downloads", 0), "Active download connections.")
        out.add("fuldc_uploads_active", t.get("uploads", 0), "Active upload connections.")

    def queue() -> None:
        bundles = c.list_bundles(0, 500)
        by_status: dict[str, int] = {}
        no_sources = 0
        for b in bundles:
            sid = (b.get("status") or {}).get("id", "unknown")
            by_status[sid] = by_status.get(sid, 0) + 1
            unfinished = float(b.get("downloaded_bytes") or 0) < float(b.get("size") or 0)
            if unfinished and not (b.get("sources") or {}).get("online"):
                no_sources += 1
        for sid, n in sorted(by_status.items()):
            out.add("fuldc_bundles", n, "Queue bundles by status.", labels={"status": sid})
        out.add("fuldc_bundles_failed",
                sum(n for s, n in by_status.items() if s in FulDCClient.DONE_BAD),
                "Bundles that ended in a download or validation error.")
        out.add("fuldc_bundles_no_sources", no_sources,
                "Unfinished bundles with no online source.")

    for name, fn in (("hubs", hubs), ("share", share),
                     ("transfers", transfers), ("queue", queue)):
        section(name, fn)
    return out.text()


def render(client_factory: Callable[[], FulDCClient]) -> str:
    global _cache
    now = time.monotonic()
    with _lock:
        stamp, text = _cache
        if text and now - stamp < CACHE_TTL:
            return text
    try:
        text = collect(client_factory())
    except Exception:  # noqa: BLE001 - e.g. FULDC_PASS missing
        text = "# HELP fuldc_up 1 if the FulDC++ Web API answers.\n# TYPE fuldc_up gauge\nfuldc_up 0\n"
    with _lock:
        _cache = (now, text)
    return text
