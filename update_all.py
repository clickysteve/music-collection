#!/usr/bin/env python3
"""
update_all.py — One script to update everything.

1. Updates Notion databases (CD + Vinyl) with MusicBrainz/Discogs metadata
   by calling your existing notion_covers.py for each database.
2. Exports both collections from Notion into index.html.
3. Commits and pushes to GitHub.

Usage:
    python update_all.py

Options:
    --notion-only   Just update Notion, skip export/push
    --export-only   Just export + push, skip MusicBrainz lookups

Environment variables:
    NOTION_TOKEN    - Your Notion integration token
    MB_USER_AGENT   - MusicBrainz user agent (must include contact email)
"""

import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CD_DATABASE_ID = "2f39e578d459801689dec91e5a424282"
VINYL_DATABASE_ID = "43a34f8c6c6c46c780ddac4697e36b0b"

NOTION_COVERS_SCRIPT = Path(__file__).parent / "notion_covers.py"
SITE_DIR = Path(__file__).parent
INDEX_HTML = SITE_DIR / "index.html"

NOTION_API_VERSION = "2022-06-28"


# ---------------------------------------------------------------------------
# Step 1: Update Notion via notion_covers.py
# ---------------------------------------------------------------------------

def update_notion_databases():
    token = os.environ.get("NOTION_TOKEN", "").strip()
    mb_agent = os.environ.get("MB_USER_AGENT", "").strip()

    if not token:
        print("Error: Set NOTION_TOKEN"); sys.exit(1)
    if not mb_agent:
        print("Error: Set MB_USER_AGENT"); sys.exit(1)
    if not NOTION_COVERS_SCRIPT.exists():
        print(f"Error: notion_covers.py not found at {NOTION_COVERS_SCRIPT}"); sys.exit(1)

    for label, db_id in [("CD Collection", CD_DATABASE_ID), ("Vinyl Collection", VINYL_DATABASE_ID)]:
        print(f"\n{'='*60}")
        print(f"Updating {label}")
        print(f"{'='*60}\n")

        env = os.environ.copy()
        env["NOTION_TOKEN"] = token
        env["NOTION_DATABASE_ID"] = db_id
        env["MB_USER_AGENT"] = mb_agent

        result = subprocess.run(
            [sys.executable, str(NOTION_COVERS_SCRIPT)],
            env=env, cwd=str(NOTION_COVERS_SCRIPT.parent),
        )
        if result.returncode != 0:
            print(f"\nWarning: notion_covers.py exited with code {result.returncode} for {label}")


# ---------------------------------------------------------------------------
# Step 2: Export from Notion to index.html
# ---------------------------------------------------------------------------

try:
    import requests
except ImportError:
    print("Please install requests: pip install requests"); sys.exit(1)


def get_notion_headers():
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        print("Error: Set NOTION_TOKEN"); sys.exit(1)
    return {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_API_VERSION, "Content-Type": "application/json"}


def query_all_pages(database_id, headers):
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    pages, payload = [], {"page_size": 100}
    while True:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        pages.extend(data["results"])
        if not data.get("has_more"): break
        payload["start_cursor"] = data["next_cursor"]
    return pages


def get_title(props):
    return "".join(p.get("plain_text", "") for p in props.get("Artist", {}).get("title", []))
def get_rich_text(props, name):
    return "".join(p.get("plain_text", "") for p in props.get(name, {}).get("rich_text", []))
def get_number(props, name):
    return props.get(name, {}).get("number")
def get_select(props, name):
    sel = props.get(name, {}).get("select"); return sel.get("name", "") if sel else ""
def get_multi_select(props, name):
    return [s.get("name", "") for s in props.get(name, {}).get("multi_select", [])]
def get_url(props, name):
    return props.get(name, {}).get("url") or ""
def get_date(props, name):
    d = props.get(name, {}).get("date"); return d.get("start", "") if d else ""
def get_formula_string(props, name):
    f = props.get(name, {}).get("formula", {})
    if f.get("type") == "string": return f.get("string", "")
    if f.get("type") == "number":
        v = f.get("number"); return str(v) if v is not None else ""
    return ""


def page_to_album(page):
    props = page["properties"]
    artist = get_title(props)
    title = get_rich_text(props, "Title")
    mbid = get_rich_text(props, "MBID")
    runtime = get_number(props, "Runtime")

    played_prop = props.get("Played!", {})
    if played_prop.get("type") == "multi_select":
        played = ", ".join(get_multi_select(props, "Played!"))
    elif played_prop.get("type") == "select":
        played = get_select(props, "Played!")
    else:
        played = ""

    length_prop = props.get("Length", {})
    length = get_formula_string(props, "Length") if length_prop.get("type") == "formula" else get_rich_text(props, "Length")
    if not length and runtime: length = f"{int(runtime)} min"

    return {
        "artist": artist, "title": title,
        "year": get_number(props, "Year"), "type": get_select(props, "Type"),
        "runtime": runtime, "length": length,
        "cover_url": f"https://coverartarchive.org/release-group/{mbid}/front-250" if mbid else "",
        "mbid": mbid, "mb_url": get_url(props, "MB URL"),
        "discogs_url": get_url(props, "Discogs URL"),
        "scrobble": get_select(props, "Scrobble") or get_rich_text(props, "Scrobble"),
        "played": played, "last_played": get_date(props, "Last Played"),
        "direct_scrobble_url": get_url(props, "Direct Scrobble"),
    }


def export_database(db_id, label, headers):
    print(f"  Exporting {label}...")
    pages = query_all_pages(db_id, headers)
    albums = []
    for page in pages:
        try:
            album = page_to_album(page)
            if album["artist"] and album["title"]: albums.append(album)
        except Exception as e:
            print(f"  Warning: {page.get('id', '?')}: {e}")
    albums.sort(key=lambda a: (a["artist"].lower(), a["title"].lower()))
    print(f"  Got {len(albums)} {label} records.")
    return albums


def clean_album_data(albums):
    cleaned = []
    for album in albums:
        clean = {}
        for k, v in album.items():
            if isinstance(v, str):
                v = "".join(c for c in v if unicodedata.category(c)[0] != "C" or c in " \t").strip()
            clean[k] = v
        cleaned.append(clean)
    return cleaned


GENRE_CACHE_FILE = SITE_DIR / "genre_cache.json"


def load_genre_cache():
    if GENRE_CACHE_FILE.exists():
        try:
            return json.loads(GENRE_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_genre_cache(cache):
    GENRE_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def fetch_genres(albums, label=""):
    """Fetch genre tags from MusicBrainz for each album's release-group MBID.

    Uses the ?inc=genres parameter on the release-group endpoint.
    Picks the top genre by vote count. Caches permanently by MBID.
    """
    import time

    mb_agent = os.environ.get("MB_USER_AGENT", "").strip()
    if not mb_agent:
        mb_agent = "MusicCollectionGallery/1.0 (steve.blythe@a8c.com)"

    mb_headers = {"User-Agent": mb_agent, "Accept": "application/json"}
    cache = load_genre_cache()

    to_fetch = []
    for i, a in enumerate(albums):
        mbid = a.get("mbid", "")
        if mbid and mbid in cache:
            albums[i]["genres"] = cache[mbid]
        elif mbid:
            to_fetch.append((i, a))
        else:
            albums[i]["genres"] = []

    if not to_fetch:
        print(f"  All {label} genres cached.")
        return albums

    print(f"  Fetching genres for {len(to_fetch)} {label} albums...")
    fetched = 0

    for idx, album in to_fetch:
        mbid = album["mbid"]
        time.sleep(1.1)  # MusicBrainz rate limit: 1 req/sec
        try:
            resp = requests.get(
                f"https://musicbrainz.org/ws/2/release-group/{mbid}",
                params={"fmt": "json", "inc": "genres"},
                headers=mb_headers, timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                genres_raw = data.get("genres", [])
                # Sort by vote count, take top 3 genre names
                genres_raw.sort(key=lambda g: -g.get("count", 0))
                genre_names = [g["name"] for g in genres_raw[:3]]
                albums[idx]["genres"] = genre_names
                cache[mbid] = genre_names
                fetched += 1
                if fetched % 20 == 0:
                    print(f"    ...{fetched}/{len(to_fetch)}")
            else:
                albums[idx]["genres"] = []
        except Exception:
            albums[idx]["genres"] = []

    save_genre_cache(cache)
    print(f"  Fetched genres for {fetched}/{len(to_fetch)} {label} albums")
    return albums


SUGGESTIONS_CACHE_FILE = SITE_DIR / "suggestions_cache.json"


def load_suggestions_cache():
    if SUGGESTIONS_CACHE_FILE.exists():
        try:
            return json.loads(SUGGESTIONS_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_suggestions_cache(cache):
    SUGGESTIONS_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def find_missing_albums(all_albums):
    """Query MusicBrainz for top artists' discographies and find albums not in the collection."""
    import time

    mb_agent = os.environ.get("MB_USER_AGENT", "").strip()
    if not mb_agent:
        print("  Skipping suggestions: MB_USER_AGENT not set")
        return []

    mb_headers = {"User-Agent": mb_agent, "Accept": "application/json"}

    # Count albums per artist
    artist_counts = {}
    owned_titles = {}  # artist_lower -> set of title_lower
    for a in all_albums:
        artist = a["artist"]
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
        key = artist.lower()
        if key not in owned_titles:
            owned_titles[key] = set()
        owned_titles[key].add(a["title"].lower())

    # Top 15 artists by album count
    top_artists = sorted(artist_counts.items(), key=lambda x: -x[1])[:15]

    cache = load_suggestions_cache()
    suggestions = []

    for artist_name, owned_count in top_artists:
        cache_key = artist_name.lower()

        # Check cache (valid for 30 days)
        if cache_key in cache:
            cached = cache[cache_key]
            if time.time() - cached.get("ts", 0) < 30 * 86400:
                # Use cached discography
                discog = cached["albums"]
                owned = owned_titles.get(cache_key, set())
                missing = [a for a in discog if a["title"].lower() not in owned]
                if missing:
                    suggestions.append({
                        "artist": artist_name,
                        "owned": owned_count,
                        "total": len(discog),
                        "missing": missing[:8],
                    })
                continue

        # Query MusicBrainz for artist
        time.sleep(1.2)  # Rate limit
        try:
            search_url = "https://musicbrainz.org/ws/2/artist"
            resp = requests.get(search_url, params={"query": artist_name, "fmt": "json", "limit": 1},
                                headers=mb_headers, timeout=10)
            resp.raise_for_status()
            artists = resp.json().get("artists", [])
            if not artists:
                continue

            artist_id = artists[0]["id"]

            # Get release groups (albums + EPs)
            time.sleep(1.2)
            rg_url = f"https://musicbrainz.org/ws/2/release-group"
            resp = requests.get(rg_url, params={
                "artist": artist_id, "type": "album", "fmt": "json", "limit": 100
            }, headers=mb_headers, timeout=10)
            resp.raise_for_status()

            release_groups = resp.json().get("release-groups", [])
            discog = []
            for rg in release_groups:
                title = rg.get("title", "")
                year = rg.get("first-release-date", "")[:4]
                mbid = rg.get("id", "")
                if title:
                    discog.append({
                        "title": title,
                        "year": int(year) if year.isdigit() else None,
                        "mbid": mbid,
                    })

            # Cache the discography
            cache[cache_key] = {"ts": time.time(), "albums": discog}

            # Find missing
            owned = owned_titles.get(cache_key, set())
            missing = [a for a in discog if a["title"].lower() not in owned]
            if missing:
                # Sort missing by year (newest first), limit to 8
                missing.sort(key=lambda x: -(x.get("year") or 0))
                suggestions.append({
                    "artist": artist_name,
                    "owned": owned_count,
                    "total": len(discog),
                    "missing": missing[:8],
                })

            print(f"    {artist_name}: {len(discog)} total, {len(missing)} missing")

        except Exception as e:
            print(f"    {artist_name}: error - {e}")

    save_suggestions_cache(cache)
    print(f"  Found suggestions for {len(suggestions)} artists")
    return suggestions


def inject_into_html(cd_albums, vinyl_albums, html_path, suggestions=None):
    html = html_path.read_text(encoding="utf-8")
    for marker, data in [("CD", cd_albums), ("VINYL", vinyl_albums)]:
        data = clean_album_data(data)
        json_str = json.dumps(data, ensure_ascii=False)
        pattern = rf'/\* __{marker}_DATA__ \*/.*?/\* __END_{marker}_DATA__ \*/'
        html, count = re.subn(pattern, f'/* __{marker}_DATA__ */\n{json_str}\n/* __END_{marker}_DATA__ */', html, flags=re.DOTALL)
        if count == 0: print(f"  Warning: __{marker}_DATA__ markers not found")

    # Inject suggestions data
    if suggestions is not None:
        json_str = json.dumps(suggestions, ensure_ascii=False)
        pattern = r'/\* __SUGGESTIONS_DATA__ \*/.*?/\* __END_SUGGESTIONS_DATA__ \*/'
        html, count = re.subn(pattern, f'/* __SUGGESTIONS_DATA__ */\n{json_str}\n/* __END_SUGGESTIONS_DATA__ */', html, flags=re.DOTALL)
        if count == 0:
            print(f"  Warning: __SUGGESTIONS_DATA__ markers not found")
        else:
            print(f"  Injected {len(suggestions)} artist suggestions")

    html_path.write_text(html, encoding="utf-8")
    print(f"  Injected {len(cd_albums)} CDs + {len(vinyl_albums)} vinyl into {html_path.name}")


COLOR_CACHE_FILE = SITE_DIR / "color_cache.json"


def load_color_cache():
    if COLOR_CACHE_FILE.exists():
        try:
            return json.loads(COLOR_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_color_cache(cache):
    COLOR_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def extract_dominant_colors(albums, label=""):
    """Extract dominant color from each album's cover art using colorthief.

    Caches results in color_cache.json keyed by MBID to avoid re-downloading.
    """
    try:
        from colorthief import ColorThief
        from io import BytesIO
    except ImportError:
        print("  colorthief not installed, skipping color extraction.")
        print("  Install with: pip install colorthief")
        return albums

    from concurrent.futures import ThreadPoolExecutor, as_completed

    cache = load_color_cache()
    to_extract = []
    for i, a in enumerate(albums):
        mbid = a.get("mbid", "")
        if mbid and mbid in cache:
            albums[i]["color"] = cache[mbid]
        elif a.get("cover_url"):
            to_extract.append((i, a))

    if not to_extract:
        print(f"  All {label} colors cached.")
        return albums

    print(f"  Extracting colors for {len(to_extract)} {label} albums...")
    extracted = 0

    def extract_one(idx, album):
        url = album["cover_url"]
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                ct = ColorThief(BytesIO(resp.content))
                r, g, b = ct.get_color(quality=5)
                return idx, f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            pass
        return idx, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(extract_one, i, a) for i, a in to_extract]
        for future in as_completed(futures):
            idx, color = future.result()
            if color:
                albums[idx]["color"] = color
                mbid = albums[idx].get("mbid", "")
                if mbid:
                    cache[mbid] = color
                extracted += 1

    save_color_cache(cache)
    print(f"  Extracted {extracted}/{len(to_extract)} colors for {label}")
    return albums


COVER_CACHE_FILE = SITE_DIR / "cover_cache.json"


def load_cover_cache():
    if COVER_CACHE_FILE.exists():
        try:
            return json.loads(COVER_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cover_cache(cache):
    COVER_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def resolve_cover_urls(albums, label=""):
    """Resolve Cover Art Archive redirects to final archive.org URLs.

    Caches resolved URLs by MBID so subsequent runs skip already-resolved covers.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cache = load_cover_cache()

    to_resolve = []
    cached_count = 0
    for i, a in enumerate(albums):
        mbid = a.get("mbid", "")
        if mbid and mbid in cache:
            albums[i]["cover_url"] = cache[mbid]
            cached_count += 1
        elif a.get("cover_url", "").startswith("https://coverartarchive.org/"):
            to_resolve.append((i, a))

    if cached_count:
        print(f"  {label}: {cached_count} cover URLs from cache")

    if not to_resolve:
        print(f"  {label}: nothing new to resolve")
        return albums

    print(f"  Resolving {len(to_resolve)} new cover art URLs for {label}...")
    resolved_count = 0

    def resolve_one(idx, album):
        url = album["cover_url"]
        try:
            resp = requests.head(url, allow_redirects=True, timeout=10)
            if resp.status_code == 200 and "archive.org" in resp.url:
                return idx, resp.url
        except Exception:
            pass
        return idx, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(resolve_one, i, a) for i, a in to_resolve]
        for future in as_completed(futures):
            idx, final_url = future.result()
            if final_url:
                albums[idx]["cover_url"] = final_url
                mbid = albums[idx].get("mbid", "")
                if mbid:
                    cache[mbid] = final_url
                resolved_count += 1

    save_cover_cache(cache)
    print(f"  Resolved {resolved_count}/{len(to_resolve)} cover URLs for {label}")
    return albums


def itunes_cover_fallback(albums, label=""):
    """For albums still missing cover art, try the iTunes Search API.

    Only queries for albums that don't already have a resolved URL.
    Caches results in cover_cache.json alongside the CAA results.
    """
    import time

    cache = load_cover_cache()

    missing = [(i, a) for i, a in enumerate(albums)
               if not a.get("cover_url") or a["cover_url"].startswith("https://coverartarchive.org/")]

    # Skip any already tried via iTunes (cached as mbid with itunes URL or as _itunes_miss)
    truly_missing = []
    for i, a in missing:
        mbid = a.get("mbid", "")
        itunes_key = f"_itunes_{mbid}" if mbid else ""
        if itunes_key and itunes_key in cache:
            url = cache[itunes_key]
            if url:
                albums[i]["cover_url"] = url
        elif mbid:
            truly_missing.append((i, a))

    if not truly_missing:
        if missing:
            print(f"  {label}: iTunes results all cached")
        return albums

    print(f"  iTunes fallback: looking up {len(truly_missing)} {label} albums...")
    found = 0

    for idx, album in truly_missing:
        query = f"{album['artist']} {album['title']}"
        mbid = album.get("mbid", "")
        try:
            resp = requests.get(
                "https://itunes.apple.com/search",
                params={"term": query, "media": "music", "entity": "album", "limit": 1},
                timeout=10,
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                if results:
                    art_url = results[0].get("artworkUrl100", "")
                    if art_url:
                        art_url = art_url.replace("100x100bb", "250x250bb")
                        albums[idx]["cover_url"] = art_url
                        if mbid:
                            cache[f"_itunes_{mbid}"] = art_url
                        found += 1
                        continue
            # Mark as miss so we don't retry
            if mbid:
                cache[f"_itunes_{mbid}"] = ""
        except Exception:
            pass
        time.sleep(0.3)

    save_cover_cache(cache)
    print(f"  iTunes fallback: found {found}/{len(truly_missing)} covers for {label}")
    return albums


LASTFM_CACHE_FILE = SITE_DIR / "lastfm_cache.json"


def fetch_lastfm_data(all_albums):
    """Fetch listening data from Last.fm and match against collection.

    Requires LASTFM_API_KEY and LASTFM_USER environment variables.
    Uses user.getTopAlbums (paginated) to get play counts for all albums.
    Caches for 24 hours.
    """
    import time

    api_key = os.environ.get("LASTFM_API_KEY", "").strip()
    username = os.environ.get("LASTFM_USER", "").strip()

    if not api_key or not username:
        print("  Skipping Last.fm: set LASTFM_API_KEY and LASTFM_USER")
        return all_albums

    # Check cache
    cache = {}
    if LASTFM_CACHE_FILE.exists():
        try:
            cache = json.loads(LASTFM_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - cache.get("_ts", 0) < 24 * 3600:
                print("  Using cached Last.fm data (< 24h old)")
                return _apply_lastfm(all_albums, cache)
        except Exception:
            pass

    print(f"  Fetching Last.fm data for user '{username}'...")

    # Fetch all top albums (paginated)
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
            albums_page = top.get("album", [])

            for a in albums_page:
                artist = a.get("artist", {}).get("name", "").lower()
                name = a.get("name", "").lower()
                plays = int(a.get("playcount", 0))
                key = f"{artist}|||{name}"
                lastfm_albums[key] = plays

            print(f"    Page {page}/{total_pages} ({len(albums_page)} albums)")
            page += 1
            time.sleep(0.3)

        except Exception as e:
            print(f"    Error on page {page}: {e}")
            break

    # Also fetch recent tracks to get last-played timestamps (last 200)
    lastfm_recent = {}
    try:
        resp = requests.get("https://ws.audioscrobbler.com/2.0/", params={
            "method": "user.getRecentTracks",
            "user": username,
            "api_key": api_key,
            "format": "json",
            "limit": 200,
        }, timeout=15)
        resp.raise_for_status()
        tracks = resp.json().get("recenttracks", {}).get("track", [])
        for t in tracks:
            artist = t.get("artist", {}).get("#text", "").lower()
            album_name = t.get("album", {}).get("#text", "").lower()
            date_str = t.get("date", {}).get("#text", "")
            if artist and album_name and date_str:
                key = f"{artist}|||{album_name}"
                if key not in lastfm_recent:
                    lastfm_recent[key] = date_str
        print(f"    Recent tracks: {len(lastfm_recent)} unique albums")
    except Exception as e:
        print(f"    Recent tracks error: {e}")

    cache = {"_ts": time.time(), "plays": lastfm_albums, "recent": lastfm_recent}
    LASTFM_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"  Last.fm: {len(lastfm_albums)} albums with play counts")

    return _apply_lastfm(all_albums, cache)


def _normalize_for_match(s):
    """Normalize a string for fuzzy matching: strip punctuation, normalize whitespace."""
    import re as _re
    s = s.lower()
    s = s.replace("&", "and")
    s = _re.sub(r"[^\w\s]", "", s)  # Strip punctuation
    s = _re.sub(r"\s+", " ", s).strip()
    return s


def _apply_lastfm(albums, cache):
    """Apply cached Last.fm data to album list using fuzzy matching."""
    plays_data = cache.get("plays", {})
    recent_data = cache.get("recent", {})
    matched = 0

    # Build normalized lookup from Last.fm data
    norm_plays = {}
    norm_recent = {}
    for key, val in plays_data.items():
        parts = key.split("|||")
        if len(parts) == 2:
            norm_key = f"{_normalize_for_match(parts[0])}|||{_normalize_for_match(parts[1])}"
            # Keep highest play count if multiple normalizations collide
            if norm_key not in norm_plays or val > norm_plays[norm_key]:
                norm_plays[norm_key] = val
    for key, val in recent_data.items():
        parts = key.split("|||")
        if len(parts) == 2:
            norm_key = f"{_normalize_for_match(parts[0])}|||{_normalize_for_match(parts[1])}"
            if norm_key not in norm_recent:
                norm_recent[norm_key] = val

    for a in albums:
        norm_key = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        plays = norm_plays.get(norm_key, 0)
        recent = norm_recent.get(norm_key, "")
        if plays > 0:
            a["lastfm_plays"] = plays
            matched += 1
        if recent:
            a["lastfm_recent"] = recent

    print(f"  Last.fm matched {matched}/{len(albums)} albums with play counts")
    return albums


DESCRIPTION_CACHE_FILE = SITE_DIR / "description_cache.json"
ARTIST_BIO_CACHE_FILE = SITE_DIR / "artist_bio_cache.json"




AI_DESC_SYSTEM_PROMPT = """You write album and artist descriptions for a record collection gallery.

When given an album, return TWO sections separated by a blank line:

FIRST: A 2-sentence factual artist bio. Who they are, where they're from, when they formed, what they're known for. Plain facts, no opinions.

SECOND: A 3-4 sentence album description. Stick to facts — recording location, producer, notable session details or stories, what instruments or techniques were used. Say where it fits in the artist's discography. Mention what it sounds like in plain terms.

No editorializing. No flowery language. No superlatives. Liner notes style."""


def generate_ai_descriptions(albums):
    """Generate artist bios and album descriptions using Claude API.

    Makes one API call per album. Caches permanently — descriptions in
    description_cache.json (keyed by MBID), artist bios in
    artist_bio_cache.json (keyed by lowercase artist name).

    Requires ANTHROPIC_API_KEY environment variable.
    """
    import time

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("  Skipping AI descriptions: ANTHROPIC_API_KEY not set")
        return albums

    try:
        import anthropic
    except ImportError:
        print("  Skipping AI descriptions: pip install anthropic")
        return albums

    client = anthropic.Anthropic(api_key=api_key)

    # Load caches
    desc_cache = {}
    if DESCRIPTION_CACHE_FILE.exists():
        try:
            desc_cache = json.loads(DESCRIPTION_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    bio_cache = {}
    if ARTIST_BIO_CACHE_FILE.exists():
        try:
            bio_cache = json.loads(ARTIST_BIO_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Figure out what needs generating
    to_generate = []
    desc_cached = 0
    for i, a in enumerate(albums):
        mbid = a.get("mbid", "")
        artist_key = a.get("artist", "").strip().lower()

        # Apply cached values
        if mbid and mbid in desc_cache:
            albums[i]["description"] = desc_cache[mbid]
            desc_cached += 1
        if artist_key and artist_key in bio_cache:
            albums[i]["artist_bio"] = bio_cache[artist_key]

        # Need to generate if missing description
        if mbid and mbid not in desc_cache and a.get("artist") and a.get("title"):
            to_generate.append((i, a))

    if desc_cached:
        print(f"  {desc_cached} descriptions from cache")

    if not to_generate:
        print(f"  No new descriptions to generate")
        return albums

    print(f"  Generating AI descriptions for {len(to_generate)} albums...")
    generated = 0

    for idx, album in to_generate:
        mbid = album.get("mbid", "")
        artist = album.get("artist", "")
        title = album.get("title", "")
        year = album.get("year", "")
        genres = album.get("genres", [])
        genre_str = ", ".join(genres) if genres else "unknown"
        artist_key = artist.strip().lower()

        try:
            msg = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=250,
                system=AI_DESC_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"{artist} — {title} ({year}). Genres: {genre_str}."
                }]
            )
            text = msg.content[0].text.strip()

            # Split into bio and description on blank line
            parts = re.split(r'\n\s*\n', text, maxsplit=1)
            if len(parts) == 2:
                bio_text = parts[0].strip()
                desc_text = parts[1].strip()
            else:
                bio_text = ""
                desc_text = text

            # Cache description by MBID
            if mbid and desc_text:
                desc_cache[mbid] = desc_text
                albums[idx]["description"] = desc_text

            # Cache artist bio (only if we don't already have one)
            if artist_key and bio_text and artist_key not in bio_cache:
                bio_cache[artist_key] = bio_text
            if artist_key and artist_key in bio_cache:
                albums[idx]["artist_bio"] = bio_cache[artist_key]

            generated += 1

        except Exception as e:
            print(f"    Error for {artist} - {title}: {e}")
            if mbid:
                desc_cache[mbid] = ""

        time.sleep(0.3)

        if generated % 25 == 0 and generated > 0:
            print(f"    ...{generated}/{len(to_generate)}")
            DESCRIPTION_CACHE_FILE.write_text(json.dumps(desc_cache, ensure_ascii=False), encoding="utf-8")
            ARTIST_BIO_CACHE_FILE.write_text(json.dumps(bio_cache, ensure_ascii=False), encoding="utf-8")

    # Save caches
    DESCRIPTION_CACHE_FILE.write_text(json.dumps(desc_cache, ensure_ascii=False), encoding="utf-8")
    ARTIST_BIO_CACHE_FILE.write_text(json.dumps(bio_cache, ensure_ascii=False), encoding="utf-8")
    print(f"  Generated {generated}/{len(to_generate)} descriptions")
    return albums


def export_to_site():
    print(f"\n{'='*60}")
    print("Exporting to GitHub Pages site")
    print(f"{'='*60}\n")
    if not INDEX_HTML.exists():
        print(f"Error: {INDEX_HTML} not found."); sys.exit(1)
    headers = get_notion_headers()
    cd = export_database(CD_DATABASE_ID, "CD Collection", headers)
    vinyl = export_database(VINYL_DATABASE_ID, "Vinyl Collection", headers)
    cd = resolve_cover_urls(cd, "CDs")
    vinyl = resolve_cover_urls(vinyl, "Vinyl")
    cd = itunes_cover_fallback(cd, "CDs")
    vinyl = itunes_cover_fallback(vinyl, "Vinyl")
    cd = fetch_genres(cd, "CDs")
    vinyl = fetch_genres(vinyl, "Vinyl")
    cd = extract_dominant_colors(cd, "CDs")
    vinyl = extract_dominant_colors(vinyl, "Vinyl")

    # Fetch Last.fm listening data + album descriptions
    all_albums = cd + vinyl
    all_albums = fetch_lastfm_data(all_albums)
    all_albums = generate_ai_descriptions(all_albums)
    # Re-split after enrichment
    cd_count = len(cd)
    cd = all_albums[:cd_count]
    vinyl = all_albums[cd_count:]

    # Find missing album suggestions for top artists
    print("\n  Finding missing album suggestions...")
    suggestions = find_missing_albums(all_albums)

    inject_into_html(cd, vinyl, INDEX_HTML, suggestions=suggestions)
    print(f"\n  Total: {len(cd)} CDs + {len(vinyl)} vinyl = {len(cd) + len(vinyl)} albums")


# ---------------------------------------------------------------------------
# Step 3: Git push
# ---------------------------------------------------------------------------

def git_push():
    print(f"\n{'='*60}")
    print("Pushing to GitHub")
    print(f"{'='*60}\n")
    try:
        subprocess.run(["git", "add", "index.html"], cwd=SITE_DIR, check=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=SITE_DIR, capture_output=True)
        if result.returncode == 0:
            print("  No changes to push."); return
        subprocess.run(["git", "commit", "-m", "Update album data from Notion"], cwd=SITE_DIR, check=True)
        subprocess.run(["git", "push"], cwd=SITE_DIR, check=True)
        print("  Pushed to GitHub!")
    except subprocess.CalledProcessError as e:
        print(f"  Git failed: {e}\n  You may need to push manually.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    args = set(sys.argv[1:])
    notion_only = "--notion-only" in args
    export_only = "--export-only" in args

    if not export_only:
        update_notion_databases()
    if not notion_only:
        export_to_site()
        git_push()

    print(f"\n{'='*60}")
    print("All done!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
