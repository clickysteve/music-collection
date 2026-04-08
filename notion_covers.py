#!/usr/bin/env python3
"""
notion_covers.py

Incremental updater for a Notion music library database.

Fills missing fields (never overwrites existing values):
- Cover (Files & media) + sets page cover
- Year (Number)
- Type (Select)
- MBID (Rich text)  [MusicBrainz release-group MBID]
- MB URL (URL)      [MusicBrainz release-group URL]
- Discogs URL (URL) [from MusicBrainz url relations]
- Runtime (Number)  [minutes, derived from a representative release tracklist]

Environment variables required:
- NOTION_TOKEN
- NOTION_DATABASE_ID
- MB_USER_AGENT   (must include contact email per MusicBrainz etiquette)

Notion property mapping for your DB:
- Artist = the database Title (page name) property
- Title  = album title column

Performance improvements (incremental, no feature removals):
- If MBID already exists on a row, we use it directly (no MB search).
- MusicBrainz rate-limit throttling applies only to MusicBrainz API calls,
  not to Cover Art Archive image URL resolution (which is a separate service).
- No delay is added for rows that are skipped or have nothing to update.
"""

from __future__ import annotations

import os
import time
import json
import re
import difflib
from typing import Any, Dict, Optional, List, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------
# Property names (edit only if your Notion columns differ)
# ----------------------------
PROP_ARTIST = "Artist"          # Notion TITLE (page name)
PROP_TITLE = "Title"            # album name column
PROP_COVER = "Cover"
PROP_YEAR = "Year"
PROP_TYPE = "Type"
PROP_MBID = "MBID"
PROP_MB_URL = "MB URL"
PROP_DISCOGS_URL = "Discogs URL"
PROP_RUNTIME = "Runtime"        # minutes (Number)

# ----------------------------
# Tuning knobs
# ----------------------------
NOTION_VERSION = "2022-06-28"
NOTION_PAGE_SIZE = 100

MB_MIN_INTERVAL_SECONDS = 1.15   # MusicBrainz etiquette: be gentle (API calls only)
NOTION_WRITE_PAUSE = 0.20        # pause between Notion updates (only after a PATCH)

TOP_CANDIDATES = 14
MIN_SCORE_RELEASE = 0.56
MIN_SCORE_RG = 0.54

RUNTIME_ROUND_DECIMALS = 2

MB_TYPE_MAP = {
    "album": "Album",
    "ep": "EP",
    "single": "Single",
    "compilation": "Compilation",
    "soundtrack": "Soundtrack",
    "live": "Live",
    "remix": "Remix",
}

# ----------------------------
# Env
# ----------------------------
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()
MB_USER_AGENT = os.environ.get("MB_USER_AGENT", "").strip()

if not NOTION_TOKEN or not NOTION_DATABASE_ID:
    raise RuntimeError("Set NOTION_TOKEN and NOTION_DATABASE_ID environment variables.")
if not MB_USER_AGENT:
    raise RuntimeError("Set MB_USER_AGENT (must include contact email), e.g. NotionCoverBot/2.0 (you@example.com)")

# ----------------------------
# HTTP sessions with retries
# ----------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        status=6,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PATCH", "HEAD"],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


NOTION = make_session()
MB = make_session()   # MusicBrainz API
CAA = make_session()  # Cover Art Archive (separate service)

_last_mb_request_at = 0.0


def mb_throttle() -> None:
    """Throttle *only* MusicBrainz API calls (not CAA image redirects)."""
    global _last_mb_request_at
    now = time.time()
    wait = (_last_mb_request_at + MB_MIN_INTERVAL_SECONDS) - now
    if wait > 0:
        time.sleep(wait)
    _last_mb_request_at = time.time()


def notion_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def mb_headers() -> Dict[str, str]:
    return {"User-Agent": MB_USER_AGENT, "Accept": "application/json"}

# ----------------------------
# Notion property helpers
# ----------------------------

def get_text(prop: Optional[Dict[str, Any]]) -> str:
    """Reads Notion title or rich_text (and string formula) properties."""
    if not prop:
        return ""
    t = prop.get("type")
    if t == "title":
        parts = prop.get("title", []) or []
        return "".join(p.get("plain_text", "") for p in parts).strip()
    if t == "rich_text":
        parts = prop.get("rich_text", []) or []
        return "".join(p.get("plain_text", "") for p in parts).strip()
    if t == "formula":
        f = prop.get("formula", {}) or {}
        if f.get("type") == "string":
            return (f.get("string") or "").strip()
    return ""


def prop_is_empty(prop: Optional[Dict[str, Any]]) -> bool:
    if not prop:
        return True
    t = prop.get("type")
    if t == "files":
        return len(prop.get("files", []) or []) == 0
    if t == "number":
        return prop.get("number") is None
    if t == "select":
        return prop.get("select") is None
    if t == "url":
        return not (prop.get("url") or "").strip()
    if t == "rich_text":
        return len(prop.get("rich_text", []) or []) == 0
    if t == "title":
        return len(prop.get("title", []) or []) == 0
    return True

# ----------------------------
# Notion API
# ----------------------------

def notion_get_database() -> Dict[str, Any]:
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}"
    r = NOTION.get(url, headers=notion_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def notion_query_database(or_filter: Optional[Dict[str, Any]], start_cursor: Optional[str] = None) -> Dict[str, Any]:
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    payload: Dict[str, Any] = {"page_size": NOTION_PAGE_SIZE}
    if or_filter:
        payload["filter"] = or_filter
    if start_cursor:
        payload["start_cursor"] = start_cursor
    r = NOTION.post(url, headers=notion_headers(), data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json()


def notion_update_page(page_id: str, properties: Dict[str, Any], page_cover_url: Optional[str] = None) -> None:
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload: Dict[str, Any] = {"properties": properties}
    if page_cover_url:
        payload["cover"] = {"type": "external", "external": {"url": page_cover_url}}
    r = NOTION.patch(url, headers=notion_headers(), data=json.dumps(payload), timeout=30)
    r.raise_for_status()

# ----------------------------
# Matching helpers
# ----------------------------

def normalise(s: str) -> str:
    s = (s or "").lower().strip()
    s = s.replace("’", "'")
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\[.*?\]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def similarity(a: str, b: str) -> float:
    a_n, b_n = normalise(a), normalise(b)
    if not a_n or not b_n:
        return 0.0
    return difflib.SequenceMatcher(None, a_n, b_n).ratio()


def extract_artist_credit(obj: Dict[str, Any]) -> str:
    ac = obj.get("artist-credit", []) or []
    names = []
    for item in ac:
        if isinstance(item, dict) and item.get("name"):
            names.append(item["name"])
    return " ".join(names).strip()

# ----------------------------
# MusicBrainz
# ----------------------------

def mb_get_json(url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        mb_throttle()
        r = MB.get(url, headers=mb_headers(), params=params, timeout=30)
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:
        return None


def mb_search_release(artist: str, title: str) -> List[Dict[str, Any]]:
    queries = [
        f'release:"{title}" AND artist:"{artist}"',
        f'release:"{title}"',
    ]
    url = "https://musicbrainz.org/ws/2/release/"
    for q in queries:
        data = mb_get_json(url, {"query": q, "fmt": "json", "limit": TOP_CANDIDATES})
        if data and data.get("releases"):
            return data["releases"] or []
    return []


def mb_search_release_group(artist: str, title: str) -> List[Dict[str, Any]]:
    queries = [
        f'releasegroup:"{title}" AND artist:"{artist}"',
        f'releasegroup:"{title}"',
    ]
    url = "https://musicbrainz.org/ws/2/release-group/"
    for q in queries:
        data = mb_get_json(url, {"query": q, "fmt": "json", "limit": TOP_CANDIDATES})
        if data and data.get("release-groups"):
            return data["release-groups"] or []
    return []


def pick_best_release(artist: str, title: str, releases: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str], float]:
    best_rel_id, best_rg_id, best_score = None, None, -1.0
    for rel in releases:
        rel_title = rel.get("title", "")
        rel_artist = extract_artist_credit(rel)

        score = 0.62 * similarity(title, rel_title) + 0.38 * similarity(artist, rel_artist)

        if (rel.get("status") or "").lower() == "official":
            score += 0.02
        caa = rel.get("cover-art-archive", {}) or {}
        if caa.get("front") is True:
            score += 0.02

        if score > best_score:
            best_score = score
            best_rel_id = rel.get("id")
            rg = rel.get("release-group") or {}
            best_rg_id = rg.get("id") if isinstance(rg, dict) else None

    if best_score < MIN_SCORE_RELEASE:
        return None, None, best_score
    return best_rel_id, best_rg_id, best_score


def pick_best_release_group(artist: str, title: str, rgs: List[Dict[str, Any]]) -> Tuple[Optional[str], float]:
    best_rg_id, best_score = None, -1.0
    for rg in rgs:
        rg_title = rg.get("title", "")
        rg_artist = extract_artist_credit(rg)

        score = 0.62 * similarity(title, rg_title) + 0.38 * similarity(artist, rg_artist)
        if (rg.get("primary-type") or "").lower() == "album":
            score += 0.01

        if score > best_score:
            best_score = score
            best_rg_id = rg.get("id")

    if best_score < MIN_SCORE_RG:
        return None, best_score
    return best_rg_id, best_score


def lookup_release_group(rg_id: str) -> Tuple[Optional[int], Optional[str], str, Optional[str]]:
    url = f"https://musicbrainz.org/ws/2/release-group/{rg_id}"
    data = mb_get_json(url, {"fmt": "json", "inc": "url-rels"})

    year = None
    type_label = None
    discogs_url = None
    mb_url = f"https://musicbrainz.org/release-group/{rg_id}"

    if not data:
        return None, None, mb_url, None

    frd = data.get("first-release-date") or ""
    if isinstance(frd, str) and len(frd) >= 4 and frd[:4].isdigit():
        year = int(frd[:4])

    pt = (data.get("primary-type") or "").strip()
    if pt:
        type_label = MB_TYPE_MAP.get(pt.lower(), pt)

    for rel in data.get("relations", []) or []:
        if (rel.get("type") or "").lower() == "discogs":
            u = (rel.get("url") or {}).get("resource")
            if u:
                discogs_url = u
                break

    return year, type_label, mb_url, discogs_url


def resolve_cover_url(rg_id: str) -> Optional[str]:
    """Resolve Cover Art Archive URL to a direct image URL (no MB API throttling)."""
    front = f"https://coverartarchive.org/release-group/{rg_id}/front"
    try:
        r = CAA.get(front, headers=mb_headers(), allow_redirects=True, timeout=30, stream=True)
        if r.status_code >= 400:
            return None
        return r.url
    except Exception:
        return None


def pick_release_for_runtime(rg_id: str) -> Optional[str]:
    url = f"https://musicbrainz.org/ws/2/release-group/{rg_id}"
    data = mb_get_json(url, {"fmt": "json", "inc": "releases"})
    if not data:
        return None

    releases = data.get("releases", []) or []
    if not releases:
        return None

    releases_sorted = sorted(
        releases,
        key=lambda r: (
            (r.get("status") or "") != "Official",
            r.get("date") or "9999",
        ),
    )
    return releases_sorted[0].get("id")


def runtime_minutes_for_release(release_id: str) -> Optional[float]:
    url = f"https://musicbrainz.org/ws/2/release/{release_id}"
    data = mb_get_json(url, {"fmt": "json", "inc": "recordings"})
    if not data:
        return None

    total_ms = 0
    have_any_lengths = False

    for medium in data.get("media", []) or []:
        for track in medium.get("tracks", []) or []:
            length = track.get("length")
            if length is not None:
                have_any_lengths = True
            if length:
                total_ms += int(length)

    if not have_any_lengths or total_ms == 0:
        return None

    return round(total_ms / 60000.0, RUNTIME_ROUND_DECIMALS)

# ----------------------------
# Main
# ----------------------------

def main() -> None:
    db = notion_get_database()
    schema_props = db.get("properties", {}) or {}

    def ptype(name: str) -> Optional[str]:
        return (schema_props.get(name) or {}).get("type")

    cover_ok = ptype(PROP_COVER) == "files"
    year_ok = ptype(PROP_YEAR) == "number"
    type_ok = ptype(PROP_TYPE) == "select"
    mbid_ok = ptype(PROP_MBID) == "rich_text"
    mburl_ok = ptype(PROP_MB_URL) == "url"
    discogs_ok = ptype(PROP_DISCOGS_URL) == "url"
    runtime_ok = ptype(PROP_RUNTIME) == "number"

    if ptype(PROP_ARTIST) != "title":
        print(f"⚠️  Note: '{PROP_ARTIST}' is not a title property in this DB. Current type: {ptype(PROP_ARTIST)}")

    # OR filter so we fetch pages missing *any* updatable property (including Runtime)
    or_conditions: List[Dict[str, Any]] = []
    if cover_ok:
        or_conditions.append({"property": PROP_COVER, "files": {"is_empty": True}})
    if year_ok:
        or_conditions.append({"property": PROP_YEAR, "number": {"is_empty": True}})
    if type_ok:
        or_conditions.append({"property": PROP_TYPE, "select": {"is_empty": True}})
    if mbid_ok:
        or_conditions.append({"property": PROP_MBID, "rich_text": {"is_empty": True}})
    if mburl_ok:
        or_conditions.append({"property": PROP_MB_URL, "url": {"is_empty": True}})
    if discogs_ok:
        or_conditions.append({"property": PROP_DISCOGS_URL, "url": {"is_empty": True}})
    if runtime_ok:
        or_conditions.append({"property": PROP_RUNTIME, "number": {"is_empty": True}})

    or_filter = {"or": or_conditions} if or_conditions else None

    cursor = None
    updated = 0
    skipped = 0

    while True:
        data = notion_query_database(or_filter=or_filter, start_cursor=cursor)
        results = data.get("results", []) or []

        for page in results:
            page_id = page["id"]
            props = page.get("properties", {}) or {}

            artist = get_text(props.get(PROP_ARTIST))
            album_title = get_text(props.get(PROP_TITLE))
            label = f"{artist} — {album_title}".strip(" —")

            if not artist or not album_title:
                print(f"⚠️  Skipped: missing Artist/Title :: {label}")
                skipped += 1
                continue

            need_cover = cover_ok and prop_is_empty(props.get(PROP_COVER))
            need_year = year_ok and prop_is_empty(props.get(PROP_YEAR))
            need_type = type_ok and prop_is_empty(props.get(PROP_TYPE))
            need_mbid = mbid_ok and prop_is_empty(props.get(PROP_MBID))
            need_mburl = mburl_ok and prop_is_empty(props.get(PROP_MB_URL))
            need_discogs = discogs_ok and prop_is_empty(props.get(PROP_DISCOGS_URL))
            need_runtime = runtime_ok and prop_is_empty(props.get(PROP_RUNTIME))

            if not any([need_cover, need_year, need_type, need_mbid, need_mburl, need_discogs, need_runtime]):
                continue

            # If MBID exists already, use it directly to avoid MB search calls.
            existing_mbid = get_text(props.get(PROP_MBID)) if mbid_ok else ""
            rg_id = existing_mbid.strip() if existing_mbid else None

            # Only search MusicBrainz if we don't already have rg_id
            if not rg_id:
                releases = mb_search_release(artist, album_title)
                if releases:
                    _, rg_id, _ = pick_best_release(artist, album_title, releases)

                if not rg_id:
                    rgs = mb_search_release_group(artist, album_title)
                    if rgs:
                        rg_id, _ = pick_best_release_group(artist, album_title, rgs)

            if not rg_id:
                print(f"⚠️  Skipped: no confident MB match :: {label}")
                skipped += 1
                continue

            # Only hit the release-group lookup if we need any fields derived from it.
            year = None
            type_label = None
            mb_url = None
            discogs_url = None
            if need_year or need_type or need_mburl or need_discogs:
                year, type_label, mb_url, discogs_url = lookup_release_group(rg_id)

            cover_url = resolve_cover_url(rg_id) if need_cover else None

            runtime_minutes = None
            if need_runtime:
                rid = pick_release_for_runtime(rg_id)
                if rid:
                    runtime_minutes = runtime_minutes_for_release(rid)

            updates: Dict[str, Any] = {}
            page_cover_url = None

            if need_cover and cover_url:
                updates[PROP_COVER] = {"files": [{"name": "cover", "type": "external", "external": {"url": cover_url}}]}
                page_cover_url = cover_url

            if need_year and (year is not None):
                updates[PROP_YEAR] = {"number": int(year)}

            if need_type and type_label:
                updates[PROP_TYPE] = {"select": {"name": type_label}}

            if need_mbid and rg_id:
                updates[PROP_MBID] = {"rich_text": [{"type": "text", "text": {"content": rg_id}}]}

            if need_mburl and mb_url:
                updates[PROP_MB_URL] = {"url": mb_url}

            if need_discogs and discogs_url:
                updates[PROP_DISCOGS_URL] = {"url": discogs_url}

            if need_runtime and (runtime_minutes is not None):
                updates[PROP_RUNTIME] = {"number": float(runtime_minutes)}

            if not updates:
                if need_runtime and runtime_minutes is None:
                    print(f"⚠️  Skipped (no track lengths): {label}")
                elif need_cover and not cover_url:
                    print(f"⚠️  Skipped (no cover art): {label}")
                else:
                    print(f"⚠️  Skipped (nothing to update): {label}")
                skipped += 1
                continue

            notion_update_page(page_id, updates, page_cover_url=page_cover_url)
            updated += 1

            extras = []
            if PROP_COVER in updates: extras.append("cover")
            if PROP_YEAR in updates: extras.append(f"year={year}")
            if PROP_TYPE in updates: extras.append(f"type={type_label}")
            if PROP_MBID in updates: extras.append(f"mbid={rg_id}")
            if PROP_MB_URL in updates: extras.append("mb_url")
            if PROP_DISCOGS_URL in updates: extras.append("discogs_url")
            if PROP_RUNTIME in updates: extras.append(f"runtime={runtime_minutes}min")
            print(f"✅ Updated: {label} ({', '.join(extras)})")

            time.sleep(NOTION_WRITE_PAUSE)

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    print(f"\nDone. Updated {updated}. Skipped {skipped}.")


if __name__ == "__main__":
    main()
