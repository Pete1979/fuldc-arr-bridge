"""Minimal FulDC++ / AirDC++ Web API client (stdlib only).

Verified against FulDC++ 1.08 (api_feature_level 10) over HTTP basic auth.
Covers exactly what the movies MVP needs: search -> results -> download to a
target folder -> track/remove the resulting bundle.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from typing import Any


def _decode(raw: bytes) -> str:
    """Decode an API response body without ever raising.

    A bare .decode() assumes UTF-8. FulDC++ is a Windows application serving
    filenames that came off the hubs, so a single cp1252 byte — an å, ä or ö
    from a Swedish release — produced UnicodeDecodeError. That is not
    FulDCError, so it escaped every caller's handler and killed the request
    thread outright. Replacing undecodable bytes degrades one title rather
    than losing the whole request.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


class FulDCError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# Priority enum values, per airdcpp/core/types/Priority.h:23-33. The API
# defaults an absent priority to LOW (FileSearchParser.cpp:34-37), which is both
# the slowest per-hub interval (15s vs 5s) and the first class rejected by the
# search-queue overflow guard (SearchEntity.cpp:184 sheds priority <= NORMAL).
# Always send one explicitly.
PRIO_LOW = 3        # background/RSS-shaped polling: shed me first, that's correct
PRIO_NORMAL = 4
PRIO_HIGH = 5       # someone is waiting on this: interactive grabs


class FulDCClient:
    def __init__(self, base_url: str, user: str, password: str, timeout: int = 25):
        self.base = base_url.rstrip("/") + "/api/v1"
        self._auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any | None = None) -> tuple[int, Any]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Basic {self._auth}",
                     "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = _decode(r.read())
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            body_txt = _decode(e.read())
            try:
                return e.code, json.loads(body_txt or "{}")
            except json.JSONDecodeError:
                return e.code, {"message": body_txt}
        except (urllib.error.URLError, OSError) as e:
            # connection refused / DNS / timeout — surface as our own error type
            # so callers don't have to catch raw urllib exceptions
            raise FulDCError(f"{method} {path}: cannot reach FulDC++ at "
                             f"{self.base} ({e})") from e

    # --- generic ---------------------------------------------------------
    def system_info(self) -> dict:
        st, data = self._call("GET", "/system/system_info")
        if st != 200:
            raise FulDCError(f"system_info http {st}: {data}")
        return data

    def default_download_dir(self) -> str:
        st, data = self._call("POST", "/settings/get", {"keys": ["download_directory"]})
        return (data or {}).get("download_directory", "") if st == 200 else ""

    # --- search ----------------------------------------------------------
    def search(self, pattern: str, wait: float = 10.0, poll: float = 1.0,
               plateau: float = 3.0, priority: int = PRIO_HIGH) -> tuple[int, list[dict]]:
        """Run a hub search, wait for results to settle, return (instance_id, results).

        Waits up to `wait` seconds, stopping early once result_count has been
        unchanged for `plateau` seconds. Caller is responsible for close().

        `priority` is a top-level field of the hub_search body (sibling of
        `query`), not part of the matcher. Omitting it means LOW — see the
        PRIO_* constants above for why that is the wrong default for us.
        """
        st, inst = self._call("POST", "/search")
        if st != 200:
            raise FulDCError(f"create search instance http {st}: {inst}", st)
        if not isinstance(inst, dict) or inst.get("id") is None:
            raise FulDCError(f"create search instance: unexpected body {inst!r}", st)
        iid = inst["id"]
        # The instance now exists server-side and lives until we DELETE it, so
        # nothing below may escape without closing it first.
        try:
            return iid, self._collect(iid, pattern, wait, poll, plateau, priority)
        except BaseException:
            self.close(iid)
            raise

    def _collect(self, iid: int, pattern: str, wait: float, poll: float,
                 plateau: float, priority: int) -> list[dict]:
        st, data = self._call("POST", f"/search/{iid}/hub_search",
                              {"priority": priority, "query": {"pattern": pattern}})
        if st != 200:
            # 503 = "Search queue overflow": the client's outgoing search queue
            # is backed up past 20 minutes. Caller decides whether to back off.
            raise FulDCError(f"hub_search http {st}: {data}", st)
        deadline = time.time() + wait
        last_count, stable_since = -1, time.time()
        while time.time() < deadline:
            time.sleep(poll)
            _, cur = self._call("GET", f"/search/{iid}")
            count = (cur or {}).get("result_count", 0)
            if count != last_count:
                last_count, stable_since = count, time.time()
            elif count > 0 and (time.time() - stable_since) >= plateau:
                break
        _, results = self._call("GET", f"/search/{iid}/results/0/200")
        return results or []

    def close(self, instance_id: int) -> None:
        self._call("DELETE", f"/search/{instance_id}")

    # --- download / queue ------------------------------------------------
    def download_result(self, instance_id: int, result_id: str,
                        target_directory: str | None = None,
                        name: str | None = None) -> dict:
        """Queue a grouped search result. target_directory is a Windows path
        (trailing backslash added). Returns {'bundle_id':..., 'merged':...}.

        File results return a bundle_info immediately. DIRECTORY results kick off
        a filelist (directory) download first and return `directory_download_ids`,
        so the bundle appears asynchronously — we poll exactly those ids until
        one carries a bundle."""
        body: dict = {}
        if target_directory:
            td = target_directory.replace("/", "\\")
            if not td.endswith("\\"):
                td += "\\"
            body["target_directory"] = td
        if name:
            # The `name` argument was accepted and then never sent. Without target_name the API
            # falls back to the result's own last path segment (SearchResult.cpp -> for a
            # directory, getAdcLastDir), so a release at ".../Movie.2021-GRP/1080p/" landed in a
            # folder called "1080p" while the bridge reported content_path built from the
            # release folder — a path that does not exist, so the import failed and every
            # quality-subfoldered release collided in one directory. It also broke the
            # name-based bundle fallback below, where b["name"] == name could never be true.
            body["target_name"] = name
        st, data = self._call("POST", f"/search/{instance_id}/results/{result_id}/download", body)
        if st != 200:
            raise FulDCError(f"download http {st}: {data}")
        data = data or {}
        bi = data.get("bundle_info") or {}
        if bi.get("id"):
            return {"bundle_id": bi["id"], "merged": bi.get("merged")}
        # Directory result: the API hands back the directory downloads it
        # started. Poll *those* — scanning the global list would happily pick
        # up a concurrent grab's bundle instead of ours. Some builds return bare
        # ids, others full objects; normalise to ids.
        dd_raw = data.get("directory_download_ids") or []
        dd_ids = [d.get("id") if isinstance(d, dict) else d for d in dd_raw]
        for _ in range(15):
            for dd_id in dd_ids:
                qb = (self.get_directory_download(dd_id).get("queue_info") or {}).get("bundle") or {}
                if qb.get("id"):
                    return {"bundle_id": qb["id"], "merged": qb.get("merged")}
            if name:
                for b in self.list_bundles():
                    if b.get("name") == name:
                        return {"bundle_id": b["id"], "merged": True}
            time.sleep(1)
        return {"bundle_id": None, "raw": data}

    def get_directory_download(self, dd_id) -> dict:
        st, data = self._call("GET", f"/filelists/directory_downloads/{dd_id}")
        return data or {} if st == 200 else {}

    def list_bundles(self, start: int = 0, count: int = 200) -> list[dict]:
        _, data = self._call("GET", f"/queue/bundles/{start}/{count}")
        return data or []

    def get_bundle(self, bundle_id: int) -> dict | None:
        st, data = self._call("GET", f"/queue/bundles/{bundle_id}")
        return data if st == 200 else None

    def remove_bundle(self, bundle_id: int, remove_finished: bool = True) -> bool:
        st, _ = self._call("POST", f"/queue/bundles/{bundle_id}/remove",
                            {"remove_finished": remove_finished})
        return st in (200, 204)

    # --- autosearch (FulDC++ core module) -------------------------------
    def list_autosearch(self) -> list[dict]:
        _, data = self._call("GET", "/auto_search/items")
        return data or []

    def create_autosearch(self, search_string: str, target_directory: str | None = None,
                          excluded: str = "", file_type: str = "", min_size: int = 0,
                          remove_after_hit: bool = True, action: str = "download",
                          use_params: bool = False, cur_number: int = 1,
                          max_number: int = 0, number_length: int = 2,
                          expire_days: int = 0, matcher_type: str = "partial",
                          matcher_string: str = "") -> dict:
        """Create (or update, if it already exists) a persistent AutoSearch item.

        The client keeps searching and auto-downloads to target_directory when
        the release appears — for content nobody is sharing right this moment.

        `use_params` is REQUIRED for %[inc] episode monitors. AutoSearch.cpp:207
        returns early from formatParams when useParams is false, and
        usingIncrementation() (:264) gates on it too — so without this flag a
        search string of "Show S01E%[inc]" is sent to hubs verbatim and matches
        nothing, forever.

        The API 409s on a duplicate search_string (AutoSearchApi.cpp:322-325),
        so re-requesting the same title must update the existing item rather
        than fail.
        """
        body: dict = {
            "search_string": search_string,
            "action": action,
            "matcher_type": matcher_type,
            "remove_after_hit": remove_after_hit,
            # let the client skip what we already have instead of re-grabbing it
            "check_already_queued": True,
            "check_already_shared": True,
        }
        if matcher_string:
            # a separate matcher (e.g. a regex) validates results while
            # search_string still drives the wide hub search
            body["matcher_string"] = matcher_string
        if use_params:
            # %[inc] expansion: start at cur_number, zero-pad to number_length,
            # 0 = no upper bound (AutoSearch.cpp:161-175, 190-204)
            body.update({"use_params": True, "cur_number": cur_number,
                         "max_number": max_number, "number_length": number_length})
        if expire_days:
            body["expire_time"] = int(time.time()) + expire_days * 86400
        if excluded:
            body["excluded_string"] = excluded
        if file_type:
            body["file_type"] = file_type
        if min_size:
            body["min_size"] = min_size
        if target_directory:
            td = target_directory.replace("/", "\\")
            if not td.endswith("\\"):
                td += "\\"
            body["target"] = td
        st, data = self._call("POST", "/auto_search/items", body)
        if st == 409:
            existing = self.find_autosearch(search_string)
            if existing:
                return self.update_autosearch(existing["id"], body) or existing
        if st not in (200, 201):
            raise FulDCError(f"create autosearch http {st}: {data}", st)
        return data or {}

    def find_autosearch(self, search_string: str) -> dict | None:
        for item in self.list_autosearch():
            if item.get("search_string") == search_string:
                return item
        return None

    def update_autosearch(self, item_id: int, body: dict) -> dict | None:
        """PATCH an existing item. Re-enables it and refreshes target/expiry —
        a repeat request for the same title should revive a spent item."""
        patch = {k: v for k, v in body.items() if k != "search_string"}
        patch["enabled"] = True
        st, data = self._call("PATCH", f"/auto_search/items/{item_id}", patch)
        return data if st in (200, 201) else None

    def delete_autosearch(self, item_id: int) -> bool:
        st, _ = self._call("DELETE", f"/auto_search/items/{item_id}")
        return st in (200, 204)

    def force_autosearch(self, item_id: int) -> bool:
        st, _ = self._call("POST", f"/auto_search/items/{item_id}/search")
        return st in (200, 204)

    # Bundle status ids, per QueueBundleUtils.cpp: new, queued, recheck,
    # downloaded, download_error, completion_validation_running,
    # completion_validation_error, completed, shared.
    DONE_OK = {"completed", "shared"}           # finished and (re)shared
    DONE_ON_DISK = DONE_OK | {"downloaded"}     # data is on disk; validation may still run
    DONE_BAD = {"download_error", "completion_validation_error"}

    def wait_bundle(self, bundle_id: int, timeout: int = 3600, poll: int = 5,
                    on_status=None) -> dict | None:
        """Poll a bundle until it reaches a terminal status or timeout.
        Calls on_status(status_id, bundle) on each status change. Returns the
        final bundle dict (or None if it vanished)."""
        import time as _t
        deadline = _t.time() + timeout
        last = None
        while _t.time() < deadline:
            b = self.get_bundle(bundle_id)
            if b is None:
                return None
            sid = (b.get("status") or {}).get("id")
            if sid != last:
                if on_status:
                    on_status(sid, b)
                last = sid
            if sid in self.DONE_ON_DISK or sid in self.DONE_BAD:
                return b
            _t.sleep(poll)
        return self.get_bundle(bundle_id)
