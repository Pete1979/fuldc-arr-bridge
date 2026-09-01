#!/usr/bin/env python3
"""fuldc-arr-bridge — movies MVP CLI.

Search FulDC++ for a movie, rank the results, and (optionally) queue the best
one into a target library folder. Read-only by default; downloading requires
--grab. Nothing leaves your network.

Config via env:
  FULDC_URL   (default http://mgmt:5600)
  FULDC_USER  (default admin)
  FULDC_PASS  (required)
  DC_ROOT     your DC share root, a Windows path (e.g. S:\\dc, D:\\Media)
  MOVIES_DIR / SERIES_DIR  optional full-path overrides for non-standard layouts

Examples:
  FULDC_PASS=... ./bridge.py search "Dune" --year 2021
  FULDC_PASS=... DC_ROOT="S:\\dc" ./bridge.py grab "Dune" --year 2021 --grab
"""

from __future__ import annotations

import argparse
import os
import sys

from fuldc_client import FulDCClient, FulDCError
from ranker import Prefs, rank
from core import hybrid_grab, resolve_target, run_search, searched


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def client_from_env() -> FulDCClient:
    pw = os.environ.get("FULDC_PASS")
    if not pw:
        sys.exit("FULDC_PASS not set")
    return FulDCClient(os.environ.get("FULDC_URL", "http://host.docker.internal:5600"),
                       os.environ.get("FULDC_USER", "admin"), pw)


def prefs_from_args(a) -> Prefs:
    p = Prefs()
    if a.lang:
        p.prefer_lang = [s.strip().lower() for s in a.lang.split(",") if s.strip()]
    if a.quality:
        p.prefer_quality = [s.strip().lower() for s in a.quality.split(",") if s.strip()]
    return p


def do_search(a) -> list:
    c = client_from_env()
    iid, results = run_search(c, a.title, a.year, a.wait)
    if iid is None:
        print("# no results for any query variant")
        return []
    try:
        cands = rank(results, a.title, a.year, prefs_from_args(a), include_dupes=a.show_dupes)
        print(f"# {len(results)} raw results, {len(cands)} candidates (dupes "
              f"{'shown' if a.show_dupes else 'hidden'})\n")
        for i, cand in enumerate(cands[:a.top], 1):
            r = cand.result
            t = r.get("type", {})
            kind = t.get("id")
            extra = t.get("str") if kind == "directory" else ""
            print(f"{i:2}. [{cand.score:6.1f}] {cand.release[:70]}")
            print(f"      {human(r.get('size') or 0):>8}  users={r.get('users',{}).get('count')}"
                  f"  slots={r.get('slots',{}).get('str','')}  {kind} {extra}")
            print(f"      {' · '.join(cand.reasons)}")
            print(f"      path: {r.get('path','')}")
        return cands
    finally:
        c.close(iid)


def do_grab(a) -> None:
    c = client_from_env()
    prefs = prefs_from_args(a)
    dc_root = os.environ.get("DC_ROOT", "S:\\dc")
    movies_dir = os.environ.get("MOVIES_DIR")
    series_dir = os.environ.get("SERIES_DIR")
    season = getattr(a, "season", None)
    # a.year is passed. Without it this call left year=None, so the CLI built the series folder
    # without the year that resolve_target appends (Show.Name rather than Show.Name.2025), while
    # the webhook path -- which goes through hybrid_grab and does pass it -- built one with. The
    # same show grabbed from the two front ends landed in two different directories, and --year
    # did nothing to change that.
    target = resolve_target(a.kind, a.title, a.series, dc_root, a.target,
                            season, movies_dir, series_dir, a.year)
    if not a.grab:
        with searched(c, a.title, a.year, wait=a.wait,
                      kind=a.kind, season=season) as (iid, results):
            if iid is None:
                print(f"# nothing shared now — would create an AutoSearch item → {target}")
                return
            cands = rank(results, a.title, a.year, prefs, kind=a.kind)
        if cands:
            best = cands[0]
            print(f"# best: [{best.score}] {best.release}")
            print(f"#   {' · '.join(best.reasons)}")
            print(f"#   target: {target}")
        print("\n# DRY RUN — re-run with --grab to actually queue this download")
        return
    res = hybrid_grab(c, a.title, a.year, kind=a.kind, series=a.series,
                      season=season, prefs=prefs, dc_root=dc_root,
                      movies_dir=movies_dir, series_dir=series_dir,
                      target=target, wait=a.wait)
    if res["mode"] == "autosearch":
        print(f"# not shared right now → created AutoSearch item id={res['autosearch_id']} "
              f"matcher={res['matcher']!r} target={res['target']}")
        return
    bundle_id = res["bundle_id"]
    print(f"# grabbed {res['release']} → bundle {bundle_id} target {res['target']}")
    if bundle_id is None:
        print("# (could not resolve bundle id; check the FulDC++ queue)")
        return
    if not (a.monitor or a.notify):
        return
    print("# monitoring bundle (download continues even if you stop watching)...")

    def on_status(sid, b):
        print(f"#   status: {(b.get('status') or {}).get('str', sid)}")

    final = c.wait_bundle(bundle_id, on_status=on_status)
    fsid = (final or {}).get("status", {}).get("id")
    print(f"# final status: {fsid}")
    if a.notify and fsid in c.DONE_ON_DISK:
        from notify import refresh
        refresh(a.kind)


def main() -> None:
    ap = argparse.ArgumentParser(description="FulDC++ movies bridge (MVP)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("search", "grab"):
        s = sub.add_parser(name)
        s.add_argument("title")
        s.add_argument("--year", type=int)
        s.add_argument("--wait", type=float, default=10.0, help="result collection seconds")
        s.add_argument("--top", type=int, default=10)
        s.add_argument("--quality", help="comma list, best first (e.g. 1080p,720p)")
        s.add_argument("--lang", help="preferred language tokens (e.g. swesub)")
        if name == "search":
            s.add_argument("--show-dupes", action="store_true")
        else:
            s.add_argument("--kind", choices=["movie", "series"], default="movie")
            s.add_argument("--season", type=int,
                           help="season number (series only) — searches S<NN> and "
                                "saves into <Show>\\S<NN>\\")
            s.add_argument("--series", help="series folder name (defaults to title)")
            s.add_argument("--target", help="explicit Windows target dir (overrides DC_ROOT layout)")
            s.add_argument("--grab", action="store_true", help="actually queue the download")
            s.add_argument("--monitor", action="store_true", help="wait for the download to finish")
            s.add_argument("--notify", action="store_true",
                           help="refresh your media server after completion (MEDIASERVER env: plex|jellyfin|webhook)")
    a = ap.parse_args()
    try:
        if a.cmd == "search":
            a.show_dupes = getattr(a, "show_dupes", False)
            do_search(a)
        else:
            do_grab(a)
    except FulDCError as e:
        sys.exit(f"FulDC++ API error: {e}")


if __name__ == "__main__":
    main()
