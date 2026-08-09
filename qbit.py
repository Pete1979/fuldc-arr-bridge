"""qBittorrent WebUI-API shim — lets Radarr/Sonarr use FulDC++ as a download client.

Radarr/Sonarr add this as a "qBittorrent" download client. When they grab a
release from our Torznab indexer, they POST the synthetic magnet here; we look up
what it pointed at (store.py), re-run the DC search, match the release, and queue
it into the right library folder. Progress/state is reported back by mapping the
FulDC++ bundle status onto qBittorrent torrent states.

Only the subset of the qBittorrent v2 API that Radarr/Sonarr actually use is
implemented.
"""

from __future__ import annotations

import os
import re
import threading
import time

from fuldc_client import FulDCClient
from core import resolve_target, searched
import store

_BTIH = re.compile(r"btih:([0-9a-fA-F]{40})", re.IGNORECASE)
_PCT = re.compile(r"([\d.]+)%")

# hash -> tracked "torrent". Mutated from ThreadingHTTPServer request threads
# (Radarr adds and polls concurrently), so every access takes the lock.
_torrents: dict[str, dict] = {}
_lock = threading.Lock()


def _track(h: str, entry: dict) -> None:
    with _lock:
        _torrents[h] = entry


def version() -> str:
    return "v4.6.2"


def webapi_version() -> str:
    return "2.9.2"


def preferences() -> dict:
    return {
        "save_path": os.environ.get("DC_ROOT", "S:\\dc"),
        "max_active_downloads": 5,
        "max_active_torrents": 10,
        "dht": False,
        "queueing_enabled": False,
    }


# How long a failed grab stays visible before it is dropped (see info()).
FAILED_GRACE_SECONDS = 120

# Radarr/Sonarr's download-client Test creates its configured category if it is
# missing, then re-reads /categories and fails if it still isn't there. A fixed
# list therefore fails the Test for anyone using a custom category name.
_categories: dict[str, dict] = {c: {"name": c, "savePath": ""}
                                for c in ("radarr", "sonarr", "tv-sonarr")}


def categories() -> dict:
    with _lock:
        return dict(_categories)


def create_category(name: str, save_path: str = "") -> None:
    if not name:
        return
    with _lock:
        _categories[name] = {"name": name, "savePath": save_path}
    print(f"[qbit] category {name!r} registered", flush=True)


def _cat_kind(category: str | None) -> str:
    c = (category or "").lower()
    return "series" if ("sonarr" in c or c == "tv") else "movie"


def _btih(magnet: str) -> str | None:
    m = _BTIH.search(magnet or "")
    return m.group(1).lower() if m else None


def _reacquire(client: FulDCClient, info: dict):
    """Re-run the DC search and match the exact release, then queue it."""
    with searched(client, info["pattern"], None, wait=8, kind=info["kind"],
                  season=info.get("season")) as (iid, results):
        return _match_and_queue(client, info, iid, results)


def _match_and_queue(client: FulDCClient, info: dict, iid, results):
    match = None
    for r in results:
        if info.get("tth"):
            if (r.get("tth") or "") == info["tth"]:
                match = r
                break
        elif r.get("path") == info.get("path") and int(r.get("size") or 0) == info.get("size"):
            match = r
            break
    if match is None:
        return None
    # For series the folder name should be the show, not Radarr's raw query
    # string (which carries season/quality terms) — fall back to the pattern
    # only if the indexer didn't record a show name.
    show = info.get("show") or info["pattern"]
    target = resolve_target(info["kind"], show, None,
                            os.environ.get("DC_ROOT", "S:\\dc"), None,
                            info.get("season"), os.environ.get("MOVIES_DIR"),
                            os.environ.get("SERIES_DIR"))
    dl = client.download_result(iid, match["id"], target, name=info["release"])
    return dl.get("bundle_id"), target


def add(client: FulDCClient, urls: list[str], category: str) -> None:
    for u in urls:
        h = _btih(u)
        if not h:
            continue
        with _lock:
            if h in _torrents:
                # Radarr retries an add it thinks failed; without this both
                # threads re-acquire and queue two bundles for one release.
                print(f"[qbit] add: {h[:12]} already tracked — ignoring", flush=True)
                continue
        info = store.get(h)
        if not info:
            # store is in-memory, so a restart loses the mapping. Surface it as
            # a failed torrent instead of accepting silently — otherwise Radarr
            # waits forever on something that will never appear.
            print(f"[qbit] add: unknown magnet {h[:12]} (no stored search) — "
                  f"reporting as failed", flush=True)
            _track(h, {"name": h[:12], "category": category or "", "size": 0,
                       "save_path": "", "added_on": int(time.time()),
                       "bundle_id": None, "failed": True})
            continue
        res = _reacquire(client, info)
        if not res:
            print(f"[qbit] add: {info['release']!r} not found on hubs right now", flush=True)
            _track(h, {"name": info["release"], "category": category or "",
                       "size": info["size"], "save_path": "",
                       "added_on": int(time.time()), "bundle_id": None,
                       "failed": True})
            continue
        bundle_id, target = res
        if bundle_id is None:
            # `if not res` above cannot catch this: _reacquire returns the tuple
            # (None, target), and a 2-tuple is always truthy. Tracked without "failed" the
            # entry became a permanent phantom — _bundle_for() returns None for a null id and
            # _state(None) answers ("downloading", 0.0) on every poll, so the Radarr queue item
            # never completed, never errored, was never blocklisted, and no alternative was
            # sought. Report it as failed like the other unresolved paths above.
            print(f"[qbit] add: {info['release']!r} queued but no bundle id resolved — "
                  f"reporting as failed", flush=True)
            _track(h, {"name": info["release"], "category": category or "",
                       "size": info["size"], "save_path": target or "",
                       "added_on": int(time.time()), "bundle_id": None,
                       "failed": True})
            continue
        _track(h, {"name": info["release"], "category": category or "",
                   "size": info["size"], "save_path": target,
                   "added_on": int(time.time()), "bundle_id": bundle_id})
        print(f"[qbit] add: {info['release']!r} -> bundle {bundle_id} @ {target}", flush=True)


def _state(bundle: dict | None):
    if not bundle:
        return "downloading", 0.0
    sid = (bundle.get("status") or {}).get("id")
    # "downloaded" counts: the data is on disk, validation/sharing is
    # post-processing, so Radarr can import already.
    if sid in FulDCClient.DONE_ON_DISK:
        return "pausedUP", 1.0          # complete -> Radarr imports
    if sid in FulDCClient.DONE_BAD:
        return "error", 0.0
    m = _PCT.search((bundle.get("status") or {}).get("str", ""))
    return "downloading", (float(m.group(1)) / 100 if m else 0.0)


def properties(h: str) -> dict:
    """Minimal /torrents/properties body. Radarr calls this to confirm an add
    landed, and uses save_path from it if content_path was empty."""
    with _lock:
        t = dict(_torrents.get(h) or {})
    save_path, _ = _paths(t, None)
    return {"save_path": save_path, "piece_size": 0, "pieces_num": 0,
            "total_size": t.get("size", 0), "addition_date": t.get("added_on", 0),
            "completion_date": 0, "created_by": "fuldc-arr-bridge",
            "seeding_time": 0, "share_ratio": 0.0}


def _paths(t: dict, bundle: dict | None) -> tuple[str, str]:
    """(save_path, content_path) in the shape Radarr requires.

    content_path must be the full path of the downloaded item — save_path plus
    the release name — and must be *strictly different* from save_path. When
    they are equal Radarr refuses the import outright: "Path matches client
    base download directory, it's possible 'Keep top-level folder' is
    disabled".

    FulDC++'s bundle `target` is the target *directory* (trailing backslash),
    not the downloaded folder, so reporting it as content_path made the two
    identical for every completed download — i.e. every import was blocked.
    """
    target = ((bundle or {}).get("target") or t.get("save_path") or "").rstrip("\\/")
    name = t.get("name") or ""
    if not target:
        return "", ""
    # If the target already ends in the release name, it *is* the item.
    if name and target.rsplit("\\", 1)[-1] == name:
        parent = target.rsplit("\\", 1)[0]
        return (parent or target), target
    return target, (f"{target}\\{name}" if name else target)


def _bundle_for(client: FulDCClient, t: dict, by_id: dict | None) -> dict | None:
    """Bundle for one tracked torrent, tolerating a partial failure.

    A single unreachable bundle must not fail the whole /torrents/info
    response: Radarr reads an empty list as "every download disappeared" and
    clears its queue."""
    bid = t.get("bundle_id")
    if bid is None:
        return None
    if by_id is not None:
        return by_id.get(bid)
    try:
        return client.get_bundle(bid)
    except Exception as e:  # noqa: BLE001
        print(f"[qbit] bundle {bid} unavailable: {e}", flush=True)
        return None


def info(client: FulDCClient, category: str | None = None) -> list[dict]:
    out = []
    # One call for the whole queue rather than one per tracked torrent: Radarr
    # polls this every minute, and the old shape made N serial round trips
    # inside the request thread, each with a 25s client timeout.
    try:
        by_id = {b.get("id"): b for b in client.list_bundles() if isinstance(b, dict)}
    except Exception as e:  # noqa: BLE001 - fall back to per-item lookups
        print(f"[qbit] bundle list unavailable ({e}); falling back", flush=True)
        by_id = None
    with _lock:
        tracked = list(_torrents.items())
    for h, t in tracked:
        if category and t["category"] != category:
            continue
        if t.get("failed"):
            # Radarr maps qBittorrent's "error" to Warning, never to Failed, so
            # a failed grab would sit in the queue forever: not blocklisted, not
            # retried, no alternative sought. Report the error briefly so it is
            # visible, then stop reporting the item — a grabbed download that
            # vanishes from the client is what makes Radarr search again.
            if time.time() - t.get("added_on", 0) > FAILED_GRACE_SECONDS:
                with _lock:
                    _torrents.pop(h, None)
                print(f"[qbit] dropping failed {t['name']!r} so Radarr retries",
                      flush=True)
                continue
            state, progress, bundle = "error", 0.0, None
        else:
            bundle = _bundle_for(client, t, by_id)
            state, progress = _state(bundle)
        save_path, content_path = _paths(t, bundle)
        out.append({
            "hash": h, "name": t["name"], "size": t["size"],
            "progress": progress, "state": state,
            "save_path": save_path, "content_path": content_path,
            "category": t["category"], "dlspeed": 0, "upspeed": 0,
            "eta": 0 if progress >= 1 else 8640000, "added_on": t["added_on"],
            "amount_left": int(t["size"] * (1 - progress)),
            "completion_on": t["added_on"] if progress >= 1 else 0,
            # -2 means "use the global limit". Omitting these makes them
            # deserialize to 0, and Radarr's HasReachedSeedLimit then reads a
            # non-negative limit as an explicit one already met — so with
            # "Remove Completed Downloads" enabled it deletes every download
            # the instant it finishes.
            "ratio": 0.0, "ratio_limit": -2,
            "seeding_time_limit": -2, "inactive_seeding_time_limit": -2,
            "seeding_time": 0, "last_activity": t["added_on"],
            "num_seeds": 0, "num_leechs": 0, "priority": 0, "tags": "",
        })
    return out


def delete(client: FulDCClient, hashes: list[str], delete_files: bool = False) -> None:
    for h in hashes:
        with _lock:
            t = _torrents.pop(h, None)
        if t and delete_files and t.get("bundle_id"):
            if not client.remove_bundle(t["bundle_id"]):
                print(f"[qbit] delete: bundle {t['bundle_id']} could not be removed; "
                      f"it is now orphaned in the FulDC++ queue", flush=True)
