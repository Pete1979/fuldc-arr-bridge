"""Release-name parsing and ranking for FulDC++ search results.

DC results are raw folder names with no metadata, so this is heuristic. The
release name lives in the `path` (usually the parent of a quality subfolder),
not in `name`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

YEAR_RE = re.compile(r"(19\d\d|20\d\d)")
SEASON_EP_RE = re.compile(r"s(\d{1,2})e(\d{1,3})", re.IGNORECASE)   # S01E03 single episode
SEASON_RE = re.compile(r"(?:^|[^a-z])s(\d{1,2})(?![a-z0-9])", re.IGNORECASE)  # S01 (no Exx)
COMPLETE_RE = re.compile(r"\b(complete|hela\s+serien|full\s+season)\b", re.IGNORECASE)
QUALITY_ONLY = {"1080p", "720p", "480p", "2160p", "4k", "uhd", "1080", "720", "2160"}
CODEC_TOKENS = {"x264", "x265", "h264", "h265", "hevc", "avc", "xvid"}
LANG_TOKENS = {"swesub", "swedish", "nordic", "multi", "sv", "en"}
QUALITY_RANK = {"2160p": 4, "4k": 4, "uhd": 4, "1080p": 3, "720p": 2, "480p": 1}
BAD_TOKENS = {"cam", "camrip", "ts", "telesync", "tc", "telecine", "hdcam",
              "screener", "scr", "sample", "workprint"}

_norm = re.compile(r"[.\s_\-]+")
ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


# Scandinavian letters that are conventionally transliterated rather than
# stripped. NFKD would turn å into "a", which is what most scene releases do,
# but "ö" -> "oe" also appears; both spellings must match the same title.
_FOLD_MAP = str.maketrans({
    "ß": "ss", "æ": "ae", "ø": "o", "đ": "d", "ð": "d", "þ": "th", "ł": "l",
})


def fold(s: str) -> str:
    """Strip accents so a title matches however the release spells it.

    'Kärlek' and 'Karlek' are the same film; without folding the title tokens
    score zero against each other and an unrelated result wins. Also collapses
    NFD (decomposed) input, which arrives from macOS-originated names and
    compares unequal to the NFC form byte-for-byte.
    """
    s = unicodedata.normalize("NFKD", s.translate(_FOLD_MAP))
    return "".join(c for c in s if not unicodedata.combining(c))


def normalize(s: str) -> str:
    return _norm.sub(" ", fold(s).replace("&", " and ")).strip().lower()


def _token_in(token: str, haystack: str) -> bool:
    """Does a title word appear in a normalized release name?

    Short words must match whole: a plain substring test lets 'a', 'up' or
    'it' hit almost any release, so a one-word title scored 1/1 against
    unrelated content and rode the size/user bonuses to the top.
    """
    if len(token) <= 3:
        return token in haystack.split()
    return token in haystack


def strip_leading_article(title: str) -> str:
    return ARTICLE_RE.sub("", title).strip()


_SCENE_DROP = re.compile(r"[^\w\s.\-]+")


def scene_title(title: str) -> str:
    """DC releases are named scene-style: dotted, punctuation stripped
    (e.g. 'Lord of the Rings: The Rings of Power' ->
    'Lord.of.the.Rings.The.Rings.of.Power'). The hub search ANDs every term, so
    a token like 'Rings:' (colon attached) matches nothing in a dotted filename.
    Dropping punctuation and using dots is what real releases look like."""
    t = title.replace("&", " and ")         # scene convention: '&' -> 'and', not dropped
    t = _SCENE_DROP.sub("", t)               # drop : ' , ! ? ( ) etc.
    t = re.sub(r"\s+", ".", t.strip())       # spaces -> dots
    t = re.sub(r"\.{2,}", ".", t)            # collapse repeated dots
    return t.strip("._-")


def search_queries(title: str, year: int | None) -> list[str]:
    """Ordered hub-search patterns to try. Leading articles are dropped because
    DC hub search ANDs every term and a stopword like 'The' can zero out an
    otherwise-valid query (verified: 'The Matrix 1999' -> 0, 'Matrix 1999' -> ok).
    Titles are scene-formatted (dotted, no punctuation) to match DC filenames.
    """
    base = scene_title(strip_leading_article(title))
    out = [f"{base} {year}", base] if year else [base]
    seen: set[str] = set()
    ordered: list[str] = []
    for q in out:
        key = q.lower().strip()
        if key and key not in seen:
            seen.add(key)
            ordered.append(q)
    return ordered


def parse_release_folder(path: str) -> str:
    """Pick the release folder from a virtual path. Skip bare quality/codec
    segments and prefer the deepest segment that looks like a release."""
    segs = [s for s in path.strip("/").split("/") if s]
    if not segs:
        return ""
    for seg in reversed(segs):
        low = seg.lower().strip()
        if low in QUALITY_ONLY or low in CODEC_TOKENS:
            continue
        if YEAR_RE.search(seg) or any(t in low for t in CODEC_TOKENS) or " " in normalize(seg):
            return seg
    return segs[-1]


def extract_year(name: str) -> int | None:
    m = YEAR_RE.findall(name)
    return int(m[-1]) if m else None


@dataclass
class Prefs:
    prefer_quality: list[str] = field(default_factory=lambda: ["1080p", "720p", "2160p"])
    prefer_codec: list[str] = field(default_factory=lambda: ["x265", "x264"])
    prefer_lang: list[str] = field(default_factory=list)   # e.g. ["swesub"]
    require_quality: list[str] = field(default_factory=list)  # e.g. ["1080p"]; empty = any
    min_size: int = 700 * 1024**2          # 700 MB (movies / season packs)
    min_size_episode: int = 100 * 1024**2  # 100 MB (a single TV episode is small)
    max_size: int = 100 * 1024**3          # 100 GB
    min_users: int = 1


@dataclass
class Candidate:
    result: dict
    release: str
    year: int | None
    score: float
    reasons: list[str]
    path: str = ""

    def quality_haystack(self) -> str:
        """Where a quality token may legitimately appear.

        Quality often lives in a *subfolder* of the release dir
        (…/Release.Name.2021/1080p/), which parse_release_folder deliberately
        skips — so the name alone is not enough. But matching the whole path
        lets a hub root called "/1080p-Releases/" satisfy require_quality for a
        480p release, the same false-positive class already fixed for
        BAD_TOKENS. Look only at the release folder and what sits beneath it.
        """
        segs = [s for s in self.path.strip("/").split("/") if s]
        if self.release in segs:
            segs = segs[segs.index(self.release):]
        return normalize(" ".join(segs) + " " + self.release)


def score_result(result: dict, title: str, year: int | None, prefs: Prefs,
                 kind: str = "movie") -> Candidate:
    path = result.get("path", "") or result.get("name", "")
    release = parse_release_folder(path)
    rel_norm = normalize(release)
    ryear = extract_year(release)
    size = result.get("size") or 0
    users = (result.get("users") or {}).get("count", 0)
    slots_free = (result.get("slots") or {}).get("free", 0)
    dupe = result.get("dupe")

    score, reasons = 0.0, []
    want = normalize(title)
    want_tokens = [t for t in want.split() if t]
    hit = sum(1 for t in want_tokens if _token_in(t, rel_norm))
    if want_tokens:
        frac = hit / len(want_tokens)
        score += 40 * frac
        reasons.append(f"title {hit}/{len(want_tokens)}")
    if year and ryear == year:
        score += 25; reasons.append(f"year {year}")
    elif year and ryear and abs(ryear - year) <= 1:
        score += 8; reasons.append(f"year~{ryear}")
    elif year and ryear and ryear != year:
        score -= 20; reasons.append(f"year!={ryear}")

    low = rel_norm
    # quality/codec/language may sit in a subfolder of the release dir, so look
    # at the full path as well as the release name
    hay = normalize(f"{path} {release}")
    for i, q in enumerate(prefs.prefer_quality):
        if q in hay:
            score += 12 - i * 2; reasons.append(q); break
    for i, c in enumerate(prefs.prefer_codec):
        if c in hay:
            score += 6 - i; reasons.append(c); break
    for lg in prefs.prefer_lang:
        if lg in hay:
            score += 10; reasons.append(lg); break

    # bad-source tokens stay scoped to the release name — hub root folders
    # ("/-x264-Kids/…") would otherwise poison every result
    if any(b in low.split() for b in BAD_TOKENS):
        score -= 60; reasons.append("BAD-source")
    floor = (prefs.min_size_episode
             if kind == "series" and SEASON_EP_RE.search(release) else prefs.min_size)
    if size < floor:
        score -= 40; reasons.append("too-small")
    if size > prefs.max_size:
        score -= 20; reasons.append("too-big")
    if users < prefs.min_users:
        score -= 30; reasons.append("no-users")
    score += min(users, 10) * 1.5
    score += min(slots_free, 10) * 0.5
    if dupe:
        reasons.append("DUPE(already-have)")

    if kind == "series":
        if SEASON_EP_RE.search(release):
            score -= 15; reasons.append("single-ep")
        elif COMPLETE_RE.search(low):
            score += 28; reasons.append("complete")
        elif SEASON_RE.search(release):
            score += 20; reasons.append("season-pack")

    score += (result.get("relevance") or 0) * 2
    return Candidate(result, release, ryear, round(score, 1), reasons, path)


def rank(results: list[dict], title: str, year: int | None, prefs: Prefs,
         include_dupes: bool = False, kind: str = "movie") -> list[Candidate]:
    cands = [score_result(r, title, year, prefs, kind) for r in results]
    if not include_dupes:
        cands = [c for c in cands if not c.result.get("dupe")]
    if prefs.require_quality:
        want = [q.lower() for q in prefs.require_quality]
        cands = [c for c in cands
                 if any(q in c.quality_haystack() for q in want)]
    # Tie-break deliberately rather than keeping arbitrary API order: more
    # sources first, then the larger file (usually the better encode).
    cands.sort(key=lambda c: (c.score,
                              (c.result.get("users") or {}).get("count", 0),
                              c.result.get("size") or 0),
               reverse=True)
    return cands
