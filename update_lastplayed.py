#!/usr/bin/env python3
"""
update_lastplayed.py — Lightweight script for GitHub Action.

Reads album data from albums.json, fetches recent Last.fm scrobbles, and:
  1. Updates play counts (lastfm_plays) and last-played dates (50% track
     threshold) in albums.json.
  2. Maintains heatmap_data.json — per-album and per-artist monthly listening
     history for every album/artist in the collection (used by the site's
     "This Month in History" and "Year in Review" features).

Does NOT require Notion, MusicBrainz, or Anthropic API keys.
Only needs: LASTFM_API_KEY and LASTFM_USER environment variables.

Usage:
    LASTFM_API_KEY=xxx LASTFM_USER=xxx python update_lastplayed.py
    LASTFM_API_KEY=xxx LASTFM_USER=xxx python update_lastplayed.py --backfill
        (--backfill rescans the full scrobble history from 2005 and rebuilds
         heatmap_data.json from scratch; takes several minutes)
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SITE_DIR = Path(__file__).parent
ALBUMS_JSON = SITE_DIR / "albums.json"
HEATMAP_FILE = SITE_DIR / "heatmap_data.json"
LASTPLAYED_CACHE_FILE = SITE_DIR / "lastplayed_cache.json"   # local-only accelerator
LASTFM_CACHE_FILE = SITE_DIR / "lastfm_cache.json"           # local-only accelerator

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
    # Strip leading articles (the, a, an)
    s = re.sub(r"^(the|a|an)\s+", "", s)
    return s


def _save_json_atomic(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _utc_now_ts():
    return datetime.now(timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_albums_data():
    data = json.loads(ALBUMS_JSON.read_text(encoding="utf-8"))
    return data


def load_heatmap():
    if HEATMAP_FILE.exists():
        try:
            return json.loads(HEATMAP_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# Last.fm fetches
# ---------------------------------------------------------------------------

def fetch_top_albums(api_key, username):
    """Fetch play counts from Last.fm user.getTopAlbums."""
    # Check cache (24h) — only present on local runs, not in the Action
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

    try:
        _save_json_atomic(LASTFM_CACHE_FILE, {"_ts": time.time(), "plays": lastfm_albums})
    except Exception:
        pass
    print(f"  Last.fm: {len(lastfm_albums)} albums with play counts")
    return lastfm_albums


def fetch_scrobbles(api_key, username, scan_from=None, max_pages=600):
    """Paginate through user.getRecentTracks.

    Returns a list of (uts, artist, album, track) tuples (raw names).
    """
    print("  Fetching scrobbles...")
    if scan_from:
        print(f"    Scanning from {datetime.utcfromtimestamp(scan_from).strftime('%Y-%m-%d %H:%M')}")
    else:
        print("    Full history scan (this takes a while)")

    scrobbles = []
    page = 1
    total_pages = 1

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
                if artist and date_uts:
                    scrobbles.append((int(date_uts), artist, album_name, track_name))

            if page % 25 == 0:
                print(f"    Page {page}/{min(total_pages, max_pages)} ({len(scrobbles)} scrobbles)")
            page += 1
            time.sleep(0.25)

        except Exception as e:
            print(f"    Error on page {page}: {e}")
            break

    print(f"  Collected {len(scrobbles)} scrobbles across {page - 1} pages")
    return scrobbles


# ---------------------------------------------------------------------------
# Last-played calculation (50% track threshold, session-based)
# ---------------------------------------------------------------------------

def calculate_last_played(scrobbles, all_albums):
    """Group collection scrobbles into sessions and apply the 50% threshold."""
    album_keys = set()
    album_track_counts = {}
    for a in all_albums:
        norm_key = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        album_keys.add(norm_key)
        tc = a.get("track_count") or 0
        if tc:
            album_track_counts[norm_key] = tc

    scrobbles_by_album = defaultdict(list)
    for uts, artist, album_name, track_name in scrobbles:
        if not album_name:
            continue
        norm_key = f"{_normalize_for_match(artist)}|||{_normalize_for_match(album_name)}"
        if norm_key in album_keys:
            scrobbles_by_album[norm_key].append((uts, _normalize_for_match(track_name)))

    SESSION_GAP = 4 * 3600
    new_last_played = {}
    for norm_key, entries in scrobbles_by_album.items():
        track_count = album_track_counts.get(norm_key, 0)
        threshold = max(1, (track_count + 1) // 2) if track_count else 1

        entries.sort(key=lambda x: -x[0])
        sessions = []
        current = []
        for uts, track_name in entries:
            if not current:
                current = [(uts, track_name)]
            elif current[-1][0] - uts <= SESSION_GAP:
                current.append((uts, track_name))
            else:
                sessions.append(current)
                current = [(uts, track_name)]
        if current:
            sessions.append(current)

        for session in sessions:
            unique_tracks = len(set(tn for _, tn in session))
            if unique_tracks >= threshold:
                session_date = datetime.utcfromtimestamp(session[0][0]).strftime("%Y-%m-%d")
                new_last_played[norm_key] = session_date
                break

    print(f"  Found qualifying listens for {len(new_last_played)} albums (50%+ threshold)")

    # Merge into local cache (accelerator for local runs; harmless if absent)
    lp_cache = {}
    if LASTPLAYED_CACHE_FILE.exists():
        try:
            lp_cache = json.loads(LASTPLAYED_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing_dates = lp_cache.get("dates", {})
    for norm_key, date_str in new_last_played.items():
        existing = existing_dates.get(norm_key, "")
        if not existing or date_str > existing:
            existing_dates[norm_key] = date_str
    lp_cache["dates"] = existing_dates
    lp_cache["_last_scan_ts"] = time.time()
    try:
        _save_json_atomic(LASTPLAYED_CACHE_FILE, lp_cache)
    except Exception:
        pass

    return new_last_played


# ---------------------------------------------------------------------------
# Listening history (heatmap_data.json)
# ---------------------------------------------------------------------------

def update_history(scrobbles, all_albums, hist, backfill=False):
    """Update per-album and per-artist monthly play counts.

    Album entries count scrobbles matching a collection album (artist+album).
    Artist entries count ALL scrobbles by a collection artist, whatever the
    album. Only scrobbles newer than _hist_last_ts are counted, so overlapping
    scans never double-count.
    """
    hist_ts = 0 if backfill else hist.get("_hist_last_ts", 0)

    # Display-name lookups from the collection
    album_names = {}   # norm album key -> (artist, title)
    artist_names = {}  # norm artist -> artist
    for a in all_albums:
        akey = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        album_names.setdefault(akey, (a["artist"], a["title"]))
        artist_names.setdefault(_normalize_for_match(a["artist"]), a["artist"])

    # Index existing entries by normalised key
    albums_idx = {}
    artists_idx = {}
    if not backfill:
        for e in hist.get("albums", []):
            albums_idx[f"{_normalize_for_match(e['artist'])}|||{_normalize_for_match(e['title'])}"] = e
        for e in hist.get("artists", []):
            artists_idx[_normalize_for_match(e["artist"])] = e

    max_ts = hist_ts
    counted = 0
    for uts, artist, album_name, _track in scrobbles:
        if uts <= hist_ts:
            continue
        max_ts = max(max_ts, uts)
        month = datetime.utcfromtimestamp(uts).strftime("%Y-%m")
        nart = _normalize_for_match(artist)

        if nart in artist_names:
            entry = artists_idx.get(nart)
            if entry is None:
                entry = {"artist": artist_names[nart], "total": 0, "months": {}}
                artists_idx[nart] = entry
            entry["months"][month] = entry["months"].get(month, 0) + 1
            counted += 1

        if album_name:
            akey = f"{nart}|||{_normalize_for_match(album_name)}"
            if akey in album_names:
                entry = albums_idx.get(akey)
                if entry is None:
                    disp = album_names[akey]
                    entry = {"artist": disp[0], "title": disp[1], "total": 0, "months": {}}
                    albums_idx[akey] = entry
                entry["months"][month] = entry["months"].get(month, 0) + 1

    # Recompute totals and the master month list
    all_months = set()
    for entry in list(albums_idx.values()) + list(artists_idx.values()):
        entry["total"] = sum(entry["months"].values())
        all_months.update(entry["months"].keys())

    result = {
        "_hist_last_ts": max_ts,
        "months": sorted(all_months),
        "albums": sorted(albums_idx.values(), key=lambda e: -e["total"]),
        "artists": sorted(artists_idx.values(), key=lambda e: -e["total"]),
    }
    print(f"  History: counted {counted} new artist scrobbles; "
          f"{len(result['albums'])} albums, {len(result['artists'])} artists tracked")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    api_key = os.environ.get("LASTFM_API_KEY", "").strip()
    username = os.environ.get("LASTFM_USER", "").strip()
    backfill = "--backfill" in sys.argv[1:]

    if not api_key or not username:
        print("Error: Set LASTFM_API_KEY and LASTFM_USER")
        sys.exit(1)

    if not ALBUMS_JSON.exists():
        print(f"Error: {ALBUMS_JSON} not found")
        sys.exit(1)

    data = load_albums_data()
    cd_albums = data.get("cd", [])
    vinyl_albums = data.get("vinyl", [])
    all_albums = cd_albums + vinyl_albums
    print(f"Loaded {len(all_albums)} albums ({len(cd_albums)} CD + {len(vinyl_albums)} vinyl)")

    hist = load_heatmap()
    hist_ts = 0 if backfill else hist.get("_hist_last_ts", 0)

    # Fetch play counts
    play_counts = fetch_top_albums(api_key, username)

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

    # Fetch scrobbles: from last history checkpoint (1-day overlap so listening
    # sessions spanning the boundary still qualify), or everything on backfill
    if backfill or not hist_ts:
        scan_from = None
        max_pages = 2000
    else:
        scan_from = int(hist_ts) - 86400
        max_pages = 600
    scrobbles = fetch_scrobbles(api_key, username, scan_from=scan_from, max_pages=max_pages)

    # Last-played dates
    lp_dates = calculate_last_played(scrobbles, all_albums)
    applied = 0
    for a in all_albums:
        norm_key = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        scrobble_date = lp_dates.get(norm_key, "")
        notion_date = (a.get("last_played") or "")[:10]
        best_date = max(scrobble_date, notion_date) if scrobble_date and notion_date else (scrobble_date or notion_date)
        if best_date and best_date != (a.get("last_played") or "")[:10]:
            a["last_played"] = best_date
            applied += 1
    print(f"  Updated last-played dates for {applied} albums")

    # Listening history
    new_hist = update_history(scrobbles, all_albums, hist, backfill=backfill)
    _save_json_atomic(HEATMAP_FILE, new_hist)
    print(f"  Updated {HEATMAP_FILE.name}")

    # Write albums.json back (cd/vinyl lists were mutated in place)
    data["cd"] = cd_albums
    data["vinyl"] = vinyl_albums
    _save_json_atomic(ALBUMS_JSON, data)
    print(f"  Updated {ALBUMS_JSON.name}")
    print("Done!")


if __name__ == "__main__":
    main()
