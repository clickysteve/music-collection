#!/usr/bin/env python3
"""
update_lastplayed.py — Lightweight script for GitHub Action.

Reads album data from index.html, fetches recent Last.fm scrobbles,
calculates last-played dates using the 50% track threshold, and
injects updated dates back into index.html.

Does NOT require Notion, MusicBrainz, or Anthropic API keys.
Only needs: LASTFM_API_KEY and LASTFM_USER environment variables.

Usage:
    LASTFM_API_KEY=xxx LASTFM_USER=xxx python update_lastplayed.py
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SITE_DIR = Path(__file__).parent
INDEX_HTML = SITE_DIR / "index.html"
TRACKCOUNT_CACHE_FILE = SITE_DIR / "trackcount_cache.json"
LASTPLAYED_CACHE_FILE = SITE_DIR / "lastplayed_cache.json"
LASTFM_CACHE_FILE = SITE_DIR / "lastfm_cache.json"

try:
    import requests
except ImportError:
    print("Please install requests: pip install requests")
    sys.exit(1)


def _normalize_for_match(s):
    s = s.lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_albums(html, marker):
    pattern = rf'/\* __{marker}_DATA__ \*/\s*(.*?)\s*/\* __END_{marker}_DATA__ \*/'
    m = re.search(pattern, html, re.DOTALL)
    if not m:
        return []
    return json.loads(m.group(1))


def inject_into_html(cd_albums, vinyl_albums, html_path):
    html = html_path.read_text(encoding="utf-8")
    for marker, data in [("CD", cd_albums), ("VINYL", vinyl_albums)]:
        json_str = json.dumps(data, ensure_ascii=False)
        pattern = rf'/\* __{marker}_DATA__ \*/.*?/\* __END_{marker}_DATA__ \*/'
        html, count = re.subn(pattern, f'/* __{marker}_DATA__ */\n{json_str}\n/* __END_{marker}_DATA__ */', html, flags=re.DOTALL)
        if count == 0:
            print(f"  Warning: __{marker}_DATA__ markers not found")
    html_path.write_text(html, encoding="utf-8")


def fetch_top_albums(api_key, username):
    """Fetch play counts from Last.fm user.getTopAlbums."""
    # Check cache (24h)
    cache = {}
    if LASTFM_CACHE_FILE.exists():
        try:
            cache = json.loads(LASTFM_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - cache.get("_ts", 0) < 24 * 3600:
                print("  Using cached Last.fm play counts (< 24h old)")
                return cache.get("plays", {})
        except Exception:
            pass

    print(f"  Fetching Last.fm play counts for '{username}'...")
    lastfm_albums = {}
    page = 1
    total_pages = 1

    while page <= total_pages:
        try:
            resp = requests.get("https://ws.audioscrobbler.com/2.0/", params={
                "method": "user.getTopAlbums",
                "user": username,
                "api_key": api_key,
                "format": "json",
                "limit": 200,
                "page": page,
            }, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            top = data.get("topalbums", {})
            total_pages = int(top.get("@attr", {}).get("totalPages", 1))
            for a in top.get("album", []):
                artist = a.get("artist", {}).get("name", "").lower()
                name = a.get("name", "").lower()
                plays = int(a.get("playcount", 0))
                lastfm_albums[f"{artist}|||{name}"] = plays
            page += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"    Error on page {page}: {e}")
            break

    cache = {"_ts": time.time(), "plays": lastfm_albums}
    LASTFM_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"  Last.fm: {len(lastfm_albums)} albums with play counts")
    return lastfm_albums


def fetch_scrobbles_and_calculate(api_key, username, all_albums):
    """Fetch recent scrobbles and calculate last-played dates using 50% threshold."""

    # Load track count cache
    track_counts = {}
    if TRACKCOUNT_CACHE_FILE.exists():
        try:
            track_counts = json.loads(TRACKCOUNT_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Build lookups
    album_track_counts = {}  # norm_key -> track count
    album_keys = set()
    for a in all_albums:
        norm_key = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        album_keys.add(norm_key)
        mbid = a.get("mbid", "")
        tc = a.get("track_count") or (track_counts.get(mbid, 0) if mbid else 0)
        if tc:
            album_track_counts[norm_key] = tc

    # Load existing cache
    lp_cache = {}
    if LASTPLAYED_CACHE_FILE.exists():
        try:
            lp_cache = json.loads(LASTPLAYED_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Determine scan range
    last_scan_ts = lp_cache.get("_last_scan_ts", 0)
    scan_from = int(last_scan_ts) - 86400 if last_scan_ts else None

    print(f"  Fetching scrobbles for last-played calculation...")
    if scan_from:
        print(f"    Scanning from {datetime.utcfromtimestamp(scan_from).strftime('%Y-%m-%d')}")

    # Paginate through getRecentTracks
    all_scrobbles = []
    page = 1
    total_pages = 1
    max_pages = 500

    while page <= total_pages and page <= max_pages:
        try:
            params = {
                "method": "user.getRecentTracks",
                "user": username,
                "api_key": api_key,
                "format": "json",
                "limit": 200,
                "page": page,
            }
            if scan_from:
                params["from"] = scan_from

            resp = requests.get("https://ws.audioscrobbler.com/2.0/",
                                params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            rt = data.get("recenttracks", {})
            total_pages = int(rt.get("@attr", {}).get("totalPages", 1))

            for t in rt.get("track", []):
                if t.get("@attr", {}).get("nowplaying"):
                    continue
                artist = t.get("artist", {}).get("#text", "")
                album_name = t.get("album", {}).get("#text", "")
                track_name = t.get("name", "")
                date_uts = t.get("date", {}).get("uts", "")

                if artist and album_name and date_uts:
                    norm_key = f"{_normalize_for_match(artist)}|||{_normalize_for_match(album_name)}"
                    if norm_key in album_keys:
                        all_scrobbles.append((int(date_uts), norm_key, _normalize_for_match(track_name)))

            if page % 10 == 0:
                print(f"    Page {page}/{total_pages} ({len(all_scrobbles)} relevant scrobbles)")
            page += 1
            time.sleep(0.25)

        except Exception as e:
            print(f"    Error on page {page}: {e}")
            break

    print(f"  Collected {len(all_scrobbles)} relevant scrobbles across {page-1} pages")

    # Group into sessions and apply 50% threshold
    SESSION_GAP = 4 * 3600
    scrobbles_by_album = defaultdict(list)
    for ts, norm_key, track_name in all_scrobbles:
        scrobbles_by_album[norm_key].append((ts, track_name))

    new_last_played = {}
    for norm_key, scrobbles in scrobbles_by_album.items():
        track_count = album_track_counts.get(norm_key, 0)
        threshold = max(1, (track_count + 1) // 2) if track_count else 1

        scrobbles.sort(key=lambda x: -x[0])
        sessions = []
        current_session = []

        for ts, track_name in scrobbles:
            if not current_session:
                current_session = [(ts, track_name)]
            elif current_session[-1][0] - ts <= SESSION_GAP:
                current_session.append((ts, track_name))
            else:
                sessions.append(current_session)
                current_session = [(ts, track_name)]
        if current_session:
            sessions.append(current_session)

        for session in sessions:
            unique_tracks = len(set(tn for _, tn in session))
            if unique_tracks >= threshold:
                session_date = datetime.utcfromtimestamp(session[0][0]).strftime("%Y-%m-%d")
                new_last_played[norm_key] = session_date
                break

    print(f"  Found qualifying listens for {len(new_last_played)} albums (50%+ threshold)")

    # Merge into cache
    existing_dates = lp_cache.get("dates", {})
    for norm_key, date_str in new_last_played.items():
        existing = existing_dates.get(norm_key, "")
        if not existing or date_str > existing:
            existing_dates[norm_key] = date_str

    lp_cache["dates"] = existing_dates
    lp_cache["_last_scan_ts"] = time.time()
    LASTPLAYED_CACHE_FILE.write_text(json.dumps(lp_cache, ensure_ascii=False), encoding="utf-8")

    return existing_dates


def main():
    api_key = os.environ.get("LASTFM_API_KEY", "").strip()
    username = os.environ.get("LASTFM_USER", "").strip()

    if not api_key or not username:
        print("Error: Set LASTFM_API_KEY and LASTFM_USER")
        sys.exit(1)

    if not INDEX_HTML.exists():
        print(f"Error: {INDEX_HTML} not found")
        sys.exit(1)

    html = INDEX_HTML.read_text(encoding="utf-8")
    cd_albums = extract_albums(html, "CD")
    vinyl_albums = extract_albums(html, "VINYL")
    all_albums = cd_albums + vinyl_albums
    print(f"Loaded {len(all_albums)} albums ({len(cd_albums)} CD + {len(vinyl_albums)} vinyl)")

    # Fetch play counts
    play_counts = fetch_top_albums(api_key, username)

    # Apply play counts
    norm_plays = {}
    for key, val in play_counts.items():
        parts = key.split("|||")
        if len(parts) == 2:
            norm_key = f"{_normalize_for_match(parts[0])}|||{_normalize_for_match(parts[1])}"
            if norm_key not in norm_plays or val > norm_plays[norm_key]:
                norm_plays[norm_key] = val

    for a in all_albums:
        norm_key = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        plays = norm_plays.get(norm_key, 0)
        if plays > 0:
            a["lastfm_plays"] = plays

    # Calculate last-played dates
    lp_dates = fetch_scrobbles_and_calculate(api_key, username, all_albums)

    # Apply last-played dates (merge with Notion dates)
    applied = 0
    for a in all_albums:
        norm_key = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        scrobble_date = lp_dates.get(norm_key, "")
        notion_date = a.get("last_played", "")

        best_date = max(scrobble_date, notion_date) if scrobble_date and notion_date else (scrobble_date or notion_date)
        if best_date:
            a["last_played"] = best_date
            applied += 1

    print(f"  Applied last-played dates to {applied}/{len(all_albums)} albums")

    # Re-split and inject
    cd_count = len(cd_albums)
    cd_albums = all_albums[:cd_count]
    vinyl_albums = all_albums[cd_count:]

    inject_into_html(cd_albums, vinyl_albums, INDEX_HTML)
    print(f"  Updated index.html")
    print("Done!")


if __name__ == "__main__":
    main()
