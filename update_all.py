#!/usr/bin/env python3
"""
update_all.py — One script to update everything.

1. Updates Notion databases (CD + Vinyl) with MusicBrainz/Discogs metadata
   by calling your existing notion_covers.py for each database.
2. Exports both collections from Notion into albums.json (loaded by
   index.html at runtime).
3. Commits and pushes to GitHub.

Usage:
    python update_all.py

Options:
    --fast              Fast daily refresh: skip the Notion metadata pass, the
                        RPM pass, and all Last.fm fetches (play counts, track
                        counts, last-played are reused from albums.json, which
                        the 15-minute GitHub Action keeps fresh). Still exports
                        from Notion, resolves covers/genres/colours for new
                        albums, and pushes.
    --collection=X      Limit the Notion metadata + RPM passes to one database:
                        cd, vinyl, or both (default both). Export always
                        includes both collections.
    --notion-only       Just update Notion, skip export/push
    --export-only       Just export + push, skip MusicBrainz lookups

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
ALBUMS_JSON = SITE_DIR / "albums.json"

NOTION_API_VERSION = "2022-06-28"


# ---------------------------------------------------------------------------
# Shared helpers: crash-safe saves + run-wide failure tracking
# ---------------------------------------------------------------------------

def _save_json_atomic(path, data, indent=None):
    """Write JSON to a temp file then atomically replace the target.

    A crash (or Ctrl-C) mid-write can never leave a half-written, corrupt
    cache file behind: the original stays intact until os.replace swaps it.
    """
    import os as _os
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent)
        _os.replace(tmp, path)
    except Exception as e:
        print(f"  Warning: could not save {getattr(path, 'name', path)}: {e}")
        try:
            _os.path.exists(tmp) and _os.remove(tmp)
        except Exception:
            pass


# Collects "this lookup didn't work and here's why" across the whole run so a
# concise summary can be printed at the end instead of scrolling the log.
FAILURES = {}


def record_failure(category, label, reason):
    """Note a non-fatal lookup failure for the end-of-run summary."""
    FAILURES.setdefault(category, []).append((label, reason))


def print_failure_summary():
    """Print a grouped summary of everything that didn't resolve this run."""
    if not FAILURES:
        print("\nNo lookup failures this run.")
        return
    total = sum(len(v) for v in FAILURES.values())
    print(f"\n{'='*60}")
    print(f"Lookup issues this run ({total} total, not fatal)")
    print(f"{'='*60}")
    for category in sorted(FAILURES):
        items = FAILURES[category]
        print(f"\n  {category} ({len(items)}):")
        for label, reason in items[:25]:
            print(f"    - {label}: {reason}")
        if len(items) > 25:
            print(f"    ... and {len(items) - 25} more")


# ---------------------------------------------------------------------------
# Step 1: Update Notion via notion_covers.py
# ---------------------------------------------------------------------------

def update_notion_databases(collection="both"):
    token = os.environ.get("NOTION_TOKEN", "").strip()
    mb_agent = os.environ.get("MB_USER_AGENT", "").strip()

    if not token:
        print("Error: Set NOTION_TOKEN"); sys.exit(1)
    if not mb_agent:
        print("Error: Set MB_USER_AGENT"); sys.exit(1)
    if not NOTION_COVERS_SCRIPT.exists():
        print(f"Error: notion_covers.py not found at {NOTION_COVERS_SCRIPT}"); sys.exit(1)

    databases = [("CD Collection", CD_DATABASE_ID), ("Vinyl Collection", VINYL_DATABASE_ID)]
    if collection == "cd":
        databases = databases[:1]
    elif collection == "vinyl":
        databases = databases[1:]

    for label, db_id in databases:
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
        "rpm": get_select(props, "RPM") or "",
        "date_added": page.get("created_time", ""),
        "page_id": page.get("id", ""),  # internal only; stripped before HTML injection
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


# Fields used only internally (e.g. for Notion writes) that must never be
# injected into the public HTML.
_INTERNAL_ALBUM_KEYS = {"page_id"}


def clean_album_data(albums):
    cleaned = []
    for album in albums:
        clean = {}
        for k, v in album.items():
            if k in _INTERNAL_ALBUM_KEYS or k.startswith("_"):
                continue
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
    _save_json_atomic(GENRE_CACHE_FILE, cache)


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
                record_failure("Genre lookup (MusicBrainz)",
                               f"{album.get('artist','?')} — {album.get('title','?')}",
                               f"HTTP {resp.status_code}")
        except Exception as e:
            albums[idx]["genres"] = []
            record_failure("Genre lookup (MusicBrainz)",
                           f"{album.get('artist','?')} — {album.get('title','?')}", str(e))

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
    _save_json_atomic(SUGGESTIONS_CACHE_FILE, cache, indent=2)


def find_missing_albums(all_albums):
    """Query MusicBrainz for top artists' discographies and find albums not in the collection."""
    import time

    mb_agent = os.environ.get("MB_USER_AGENT", "").strip()
    if not mb_agent:
        print("  Skipping suggestions: MB_USER_AGENT not set")
        return []

    mb_headers = {"User-Agent": mb_agent, "Accept": "application/json"}

    # Count albums per artist; owned titles are normalised so near-miss
    # spellings ("¡Uno!" vs "Uno!") don't show up as "missing"
    artist_counts = {}
    owned_titles = {}  # artist_lower -> set of normalised titles
    for a in all_albums:
        artist = a["artist"]
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
        key = artist.lower()
        if key not in owned_titles:
            owned_titles[key] = set()
        owned_titles[key].add(_normalize_for_match(a["title"]))

    # Candidate artists: top 15 by albums owned + top 15 by listening
    # (from heatmap_data.json) — the artists you play constantly matter more
    # than the ones you happen to own a lot of
    top_owned = [name for name, _ in sorted(artist_counts.items(), key=lambda x: -x[1])[:15]]
    top_played = []
    heatmap_file = SITE_DIR / "heatmap_data.json"
    if heatmap_file.exists():
        try:
            hist = json.loads(heatmap_file.read_text(encoding="utf-8"))
            collection_lower = {a.lower() for a in artist_counts}
            top_played = [e["artist"] for e in hist.get("artists", [])
                          if e.get("artist", "").lower() in collection_lower][:15]
        except Exception:
            pass
    seen = set()
    candidates = []
    for name in top_owned + top_played:
        if name.lower() not in seen:
            seen.add(name.lower())
            candidates.append(name)
    top_artists = [(name, artist_counts.get(name, 0)) for name in candidates[:25]]

    cache = load_suggestions_cache()
    suggestions = []

    for artist_name, owned_count in top_artists:
        cache_key = artist_name.lower()

        # Check cache (valid for 30 days; v2 entries exclude comps/live)
        if cache_key in cache:
            cached = cache[cache_key]
            if cached.get("v") == 2 and time.time() - cached.get("ts", 0) < 30 * 86400:
                discog = cached["albums"]
                owned = owned_titles.get(cache_key, set())
                missing = [a for a in discog if _normalize_for_match(a["title"]) not in owned]
                if missing:
                    missing.sort(key=lambda x: -(x.get("year") or 0))
                    suggestions.append({
                        "artist": artist_name,
                        "owned": owned_count,
                        "total": len(discog),
                        "missing": missing[:10],
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
                # Studio albums only — skip compilations, live albums,
                # soundtracks, remix collections, demos etc.
                if rg.get("secondary-types"):
                    continue
                if title:
                    discog.append({
                        "title": title,
                        "year": int(year) if year.isdigit() else None,
                        "mbid": mbid,
                    })

            # Cache the discography (saved immediately so an interrupted run
            # keeps the progress — these MusicBrainz calls are the slow part)
            cache[cache_key] = {"ts": time.time(), "v": 2, "albums": discog}
            save_suggestions_cache(cache)

            # Find missing
            owned = owned_titles.get(cache_key, set())
            missing = [a for a in discog if _normalize_for_match(a["title"]) not in owned]
            if missing:
                # Sort missing by year (newest first), limit to 10
                missing.sort(key=lambda x: -(x.get("year") or 0))
                suggestions.append({
                    "artist": artist_name,
                    "owned": owned_count,
                    "total": len(discog),
                    "missing": missing[:10],
                })

            print(f"    {artist_name}: {len(discog)} total, {len(missing)} missing")

        except Exception as e:
            print(f"    {artist_name}: error - {e}")

    save_suggestions_cache(cache)
    print(f"  Found suggestions for {len(suggestions)} artists")
    return suggestions


def reuse_listening_data(all_albums):
    """Fast mode: copy lastfm_plays / track_count / last_played from the
    existing albums.json instead of re-fetching from Last.fm.

    The 15-minute GitHub Action keeps those fields fresh, so a manual run only
    needs to carry them over. Anything slightly stale is corrected by the cron
    within 15 minutes of pushing.
    """
    if not ALBUMS_JSON.exists():
        print("  No existing albums.json — cannot reuse listening data (run without --fast)")
        return all_albums
    try:
        prev = json.loads(ALBUMS_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  Could not read existing albums.json ({e}); skipping reuse")
        return all_albums

    prev_albums = (prev.get("cd") or []) + (prev.get("vinyl") or [])
    by_mbid = {}
    by_key = {}
    for p in prev_albums:
        if p.get("mbid"):
            by_mbid.setdefault(p["mbid"], p)
        key = f"{_normalize_for_match(p.get('artist', ''))}|||{_normalize_for_match(p.get('title', ''))}"
        by_key.setdefault(key, p)

    reused = 0
    for a in all_albums:
        key = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        p = by_mbid.get(a.get("mbid") or "") or by_key.get(key)
        if not p:
            continue
        if p.get("lastfm_plays"):
            a["lastfm_plays"] = p["lastfm_plays"]
        if p.get("track_count"):
            a["track_count"] = p["track_count"]
        prev_lp = p.get("last_played") or ""
        cur_lp = a.get("last_played") or ""
        if prev_lp and prev_lp[:10] > cur_lp[:10]:
            a["last_played"] = prev_lp
        reused += 1
    print(f"  Reused listening data for {reused}/{len(all_albums)} albums from albums.json")
    return all_albums


def write_albums_json(cd_albums, vinyl_albums, suggestions=None):
    """Write album + suggestions data to albums.json.

    index.html fetches this file at runtime, so data updates (including the
    15-minute last-played cron) never have to rewrite the HTML file.
    """
    from datetime import datetime, timezone
    data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cd": clean_album_data(cd_albums),
        "vinyl": clean_album_data(vinyl_albums),
        "suggestions": suggestions or [],
    }
    _save_json_atomic(ALBUMS_JSON, data)
    print(f"  Wrote {len(data['cd'])} CDs + {len(data['vinyl'])} vinyl "
          f"+ {len(data['suggestions'])} artist suggestions to {ALBUMS_JSON.name}")


COLOR_CACHE_FILE = SITE_DIR / "color_cache.json"


def load_color_cache():
    if COLOR_CACHE_FILE.exists():
        try:
            return json.loads(COLOR_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_color_cache(cache):
    _save_json_atomic(COLOR_CACHE_FILE, cache)


def extract_dominant_colors(albums, label=""):
    """Extract dominant color from each album's cover art using colorthief.

    Caches results in color_cache.json keyed by MBID to avoid re-downloading.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Always apply cached colors first, even if colorthief isn't installed —
    # otherwise a machine without it would export albums.json with no colors.
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

    try:
        from colorthief import ColorThief
        from io import BytesIO
    except ImportError:
        print(f"  colorthief not installed — {len(to_extract)} {label} colors not extracted.")
        print("  Install with: pip install colorthief")
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
    _save_json_atomic(COVER_CACHE_FILE, cache)


def resolve_cover_urls(albums, label=""):
    """Resolve Cover Art Archive redirects to final archive.org URLs.

    Caches resolved URLs by MBID so subsequent runs skip already-resolved covers.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time

    cache = load_cover_cache()

    # Failed resolutions are remembered ("_caa_miss_<mbid>" -> timestamp) so we
    # don't re-issue slow HEAD requests for the same albums on every run.
    # Retried after this many days (CAA does get new art over time).
    CAA_MISS_RETRY_SECS = 14 * 86400

    to_resolve = []
    cached_count = 0
    skipped_miss = 0
    for i, a in enumerate(albums):
        mbid = a.get("mbid", "")
        if mbid and mbid in cache:
            albums[i]["cover_url"] = cache[mbid]
            cached_count += 1
        elif a.get("cover_url", "").startswith("https://coverartarchive.org/"):
            miss_ts = cache.get(f"_caa_miss_{mbid}") if mbid else None
            if miss_ts and _time.time() - miss_ts < CAA_MISS_RETRY_SECS:
                skipped_miss += 1  # keep the original CAA URL; browser may still load it
            else:
                to_resolve.append((i, a))

    if skipped_miss:
        print(f"  {label}: skipped {skipped_miss} known-unresolvable covers (retried every 14 days)")

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
            resp = requests.head(url, allow_redirects=True, timeout=8)
            if resp.status_code == 200 and "archive.org" in resp.url:
                return idx, resp.url
        except Exception:
            pass
        return idx, None

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(resolve_one, i, a) for i, a in to_resolve]
        for future in as_completed(futures):
            idx, final_url = future.result()
            mbid = albums[idx].get("mbid", "")
            if final_url:
                albums[idx]["cover_url"] = final_url
                if mbid:
                    cache[mbid] = final_url
                    cache.pop(f"_caa_miss_{mbid}", None)
                resolved_count += 1
            elif mbid:
                cache[f"_caa_miss_{mbid}"] = _time.time()

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

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def lookup_one(idx, album):
        query = f"{album['artist']} {album['title']}"
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
                        return idx, album, art_url.replace("100x100bb", "250x250bb"), None
            return idx, album, "", None
        except Exception as e:
            return idx, album, None, str(e)

    # Small pool: iTunes search is unauthenticated and rate-limited, don't hammer it
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(lookup_one, i, a) for i, a in truly_missing]
        for fut in as_completed(futures):
            idx, album, art_url, err = fut.result()
            mbid = album.get("mbid", "")
            if err is not None:
                record_failure("Cover art (lookup error)",
                               f"{album.get('artist','?')} — {album.get('title','?')}", err)
            elif art_url:
                albums[idx]["cover_url"] = art_url
                if mbid:
                    cache[f"_itunes_{mbid}"] = art_url
                found += 1
            else:
                # Mark as miss so we don't retry
                if mbid:
                    cache[f"_itunes_{mbid}"] = ""
                record_failure("Cover art (none found)",
                               f"{album.get('artist','?')} — {album.get('title','?')}",
                               "no match on Cover Art Archive or iTunes")

    save_cover_cache(cache)
    print(f"  iTunes fallback: found {found}/{len(truly_missing)} covers for {label}")
    return albums


LASTFM_CACHE_FILE = SITE_DIR / "lastfm_cache.json"
TRACKCOUNT_CACHE_FILE = SITE_DIR / "trackcount_cache.json"
LASTPLAYED_CACHE_FILE = SITE_DIR / "lastplayed_cache.json"


def load_trackcount_cache():
    if TRACKCOUNT_CACHE_FILE.exists():
        try:
            return json.loads(TRACKCOUNT_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_trackcount_cache(cache):
    _save_json_atomic(TRACKCOUNT_CACHE_FILE, cache)


def fetch_track_counts(albums):
    """Fetch track counts per album from Last.fm album.getInfo.

    Caches permanently by MBID in trackcount_cache.json.
    """
    import time

    api_key = os.environ.get("LASTFM_API_KEY", "").strip()
    if not api_key:
        print("  Skipping track counts: LASTFM_API_KEY not set")
        return albums

    cache = load_trackcount_cache()
    to_fetch = []

    for i, a in enumerate(albums):
        mbid = a.get("mbid", "")
        if mbid and mbid in cache:
            albums[i]["track_count"] = cache[mbid]
        elif mbid:
            to_fetch.append((i, a))

    if not to_fetch:
        has_tc = sum(1 for v in cache.values() if v > 0)
        print(f"  All track counts cached ({has_tc} albums with data)")
        return albums

    print(f"  Fetching track counts from Last.fm for {len(to_fetch)} albums...")
    fetched = 0

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_one(idx, album):
        try:
            resp = requests.get("https://ws.audioscrobbler.com/2.0/", params={
                "method": "album.getInfo",
                "artist": album.get("artist", ""),
                "album": album.get("title", ""),
                "api_key": api_key,
                "format": "json",
            }, timeout=10)
            if resp.status_code == 200:
                tracks = resp.json().get("album", {}).get("tracks", {}).get("track", [])
                return idx, album.get("mbid", ""), len(tracks) if tracks else 0
        except Exception:
            pass
        return idx, album.get("mbid", ""), None

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch_one, i, a) for i, a in to_fetch]
        for fut in as_completed(futures):
            idx, mbid, track_count = fut.result()
            if track_count is None:
                continue
            cache[mbid] = track_count
            if track_count:
                albums[idx]["track_count"] = track_count
                fetched += 1
            if fetched % 50 == 0 and fetched > 0:
                save_trackcount_cache(cache)
                print(f"    ...{fetched}/{len(to_fetch)}")

    save_trackcount_cache(cache)
    print(f"  Fetched track counts for {fetched}/{len(to_fetch)} albums")
    return albums


LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
# Do a full authoritative getTopAlbums pull at most this often; between full
# pulls, play counts are kept current by tallying only new scrobbles. Any gap
# longer than this simply triggers a full pull on the next run.
LASTFM_FULL_REFRESH_SECS = 3 * 86400
# Skip Last.fm entirely if the cache was touched more recently than this
# (avoids redundant work when update.sh is run several times in a row).
LASTFM_FRESH_SECS = 15 * 60


def _lastfm_full_pull(api_key, username):
    """Authoritative all-time play counts via user.getTopAlbums (paginated).

    Returns a dict of normalized "artist|||album" -> playcount.
    """
    import time
    plays = {}
    page, total_pages = 1, 1
    while page <= total_pages:
        try:
            resp = requests.get(LASTFM_API, params={
                "method": "user.getTopAlbums", "user": username,
                "api_key": api_key, "format": "json", "limit": 200, "page": page,
            }, timeout=15)
            resp.raise_for_status()
            top = resp.json().get("topalbums", {})
            total_pages = int(top.get("@attr", {}).get("totalPages", 1))
            for a in top.get("album", []):
                artist = a.get("artist", {}).get("name", "")
                name = a.get("name", "")
                key = f"{_normalize_for_match(artist)}|||{_normalize_for_match(name)}"
                plays[key] = max(plays.get(key, 0), int(a.get("playcount", 0)))
            if page % 10 == 0 or page == total_pages:
                print(f"    Page {page}/{total_pages}")
            page += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"    Error on page {page}: {e}")
            break
    return plays


def _lastfm_incremental(api_key, username, since_ts):
    """Tally album-level scrobbles since `since_ts` via user.getRecentTracks.

    Returns (increments dict of normalized key -> count, latest_ts seen).
    Only new scrobbles are fetched, so this is normally 1-2 small pages.
    """
    import time
    inc, latest = {}, int(since_ts)
    page, total_pages, max_pages = 1, 1, 500
    while page <= total_pages and page <= max_pages:
        try:
            resp = requests.get(LASTFM_API, params={
                "method": "user.getRecentTracks", "user": username,
                "api_key": api_key, "format": "json", "limit": 200,
                "page": page, "from": int(since_ts),
            }, timeout=15)
            resp.raise_for_status()
            rt = resp.json().get("recenttracks", {})
            total_pages = int(rt.get("@attr", {}).get("totalPages", 1))
            for t in rt.get("track", []):
                if t.get("@attr", {}).get("nowplaying"):
                    continue
                artist = t.get("artist", {}).get("#text", "")
                album = t.get("album", {}).get("#text", "")
                uts = t.get("date", {}).get("uts", "")
                if not (artist and album and uts):
                    continue
                latest = max(latest, int(uts))
                key = f"{_normalize_for_match(artist)}|||{_normalize_for_match(album)}"
                inc[key] = inc.get(key, 0) + 1
            page += 1
            time.sleep(0.25)
        except Exception as e:
            print(f"    Error on page {page}: {e}")
            break
    return inc, latest


def fetch_lastfm_data(all_albums):
    """Attach Last.fm play counts to albums, fetching incrementally.

    Requires LASTFM_API_KEY and LASTFM_USER. A full getTopAlbums pull happens
    at most every few days; between those, only new scrobbles are counted and
    added to the cached totals. This turns the old ~55-page pull on every run
    into a 1-2 page fetch most of the time.
    """
    import time

    api_key = os.environ.get("LASTFM_API_KEY", "").strip()
    username = os.environ.get("LASTFM_USER", "").strip()

    if not api_key or not username:
        print("  Skipping Last.fm: set LASTFM_API_KEY and LASTFM_USER")
        return all_albums

    cache = {}
    if LASTFM_CACHE_FILE.exists():
        try:
            cache = json.loads(LASTFM_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    # Migrate legacy cache (raw lowercased keys) to normalized keys once.
    if cache.get("plays") and cache.get("_schema") != 2:
        migrated = {}
        for k, v in cache["plays"].items():
            parts = k.split("|||")
            if len(parts) == 2:
                nk = f"{_normalize_for_match(parts[0])}|||{_normalize_for_match(parts[1])}"
                migrated[nk] = max(migrated.get(nk, 0), int(v))
        cache["plays"] = migrated
        cache["_schema"] = 2

    now = time.time()
    plays = cache.get("plays", {})
    full_ts = cache.get("_ts", 0)
    scan_ts = cache.get("_scan_ts", full_ts or now)

    if plays and now - cache.get("_last_touch", 0) < LASTFM_FRESH_SECS:
        print("  Using cached Last.fm data (refreshed < 15 min ago)")
        return _apply_lastfm(all_albums, cache)

    if not plays or now - full_ts >= LASTFM_FULL_REFRESH_SECS:
        print(f"  Fetching full Last.fm play counts for '{username}'...")
        plays = _lastfm_full_pull(api_key, username)
        if plays:
            cache["plays"] = plays
            cache["_ts"] = now
            cache["_scan_ts"] = now
            print(f"  Last.fm: {len(plays)} albums with play counts (full refresh)")
        else:
            print("  Last.fm full pull returned nothing; keeping existing cache")
    else:
        print("  Updating Last.fm play counts incrementally (new scrobbles only)...")
        inc, latest = _lastfm_incremental(api_key, username, scan_ts)
        added = sum(inc.values())
        for k, n in inc.items():
            plays[k] = plays.get(k, 0) + n
        cache["plays"] = plays
        cache["_scan_ts"] = latest
        print(f"  Last.fm: +{added} new scrobbles across {len(inc)} album(s) (incremental)")

    cache["_schema"] = 2
    cache["_last_touch"] = now
    _save_json_atomic(LASTFM_CACHE_FILE, cache)

    return _apply_lastfm(all_albums, cache)


def _normalize_for_match(s):
    """Normalize a string for fuzzy matching: strip punctuation, articles, normalize whitespace."""
    import re as _re
    s = s.lower()
    s = s.replace("&", "and")
    s = _re.sub(r"[^\w\s]", "", s)  # Strip punctuation
    s = _re.sub(r"\s+", " ", s).strip()
    # Strip leading articles (the, a, an)
    s = _re.sub(r"^(the|a|an)\s+", "", s)
    return s


def _apply_lastfm(albums, cache):
    """Apply cached Last.fm play counts (keyed by normalized artist|||album)."""
    norm_plays = cache.get("plays", {})
    matched = 0

    for a in albums:
        norm_key = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        plays = norm_plays.get(norm_key, 0)
        if plays > 0:
            a["lastfm_plays"] = plays
            matched += 1

    print(f"  Last.fm matched {matched}/{len(albums)} albums with play counts")
    return albums


def calculate_last_played(all_albums):
    """Determine last-played dates from Last.fm scrobbles using 50% threshold.

    Paginates through user.getRecentTracks, groups scrobbles by album into
    sessions (scrobbles within 4 hours of each other), and marks an album as
    "played" only if 50%+ of its tracks were scrobbled in a session.

    Merges with Notion 'last_played' dates — whichever is more recent wins.
    Caches results in lastplayed_cache.json.
    """
    import time
    from datetime import datetime, timedelta

    api_key = os.environ.get("LASTFM_API_KEY", "").strip()
    username = os.environ.get("LASTFM_USER", "").strip()

    if not api_key or not username:
        print("  Skipping last-played calculation: set LASTFM_API_KEY and LASTFM_USER")
        return all_albums

    # Load existing last-played cache
    lp_cache = {}
    if LASTPLAYED_CACHE_FILE.exists():
        try:
            lp_cache = json.loads(LASTPLAYED_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Build lookup: normalized album key -> track_count
    album_track_counts = {}
    album_keys_by_norm = {}  # norm_key -> list of album indices
    for i, a in enumerate(all_albums):
        norm_key = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        tc = a.get("track_count", 0)
        if tc:
            album_track_counts[norm_key] = tc
        if norm_key not in album_keys_by_norm:
            album_keys_by_norm[norm_key] = []
        album_keys_by_norm[norm_key].append(i)

    # Determine how far back to scan — if we have cached data, only go back
    # to 1 day before the last scan timestamp to catch anything new
    last_scan_ts = lp_cache.get("_last_scan_ts", 0)
    scan_from = None
    if last_scan_ts:
        # Go back 1 day before last scan to catch stragglers
        scan_from = int(last_scan_ts) - 86400

    print(f"  Fetching scrobbles for last-played calculation...")
    if scan_from:
        print(f"    Scanning from {datetime.utcfromtimestamp(scan_from).strftime('%Y-%m-%d')}")

    # Paginate through getRecentTracks
    all_scrobbles = []  # list of (timestamp, norm_artist, norm_album, track_name)
    page = 1
    total_pages = 1
    max_pages = 500  # safety cap

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
            tracks = rt.get("track", [])

            for t in tracks:
                # Skip "now playing" entries (no date)
                if t.get("@attr", {}).get("nowplaying"):
                    continue
                artist = t.get("artist", {}).get("#text", "")
                album_name = t.get("album", {}).get("#text", "")
                track_name = t.get("name", "")
                date_uts = t.get("date", {}).get("uts", "")

                if artist and album_name and date_uts:
                    ts = int(date_uts)
                    norm_key = f"{_normalize_for_match(artist)}|||{_normalize_for_match(album_name)}"
                    # Only collect scrobbles for albums in our collection
                    if norm_key in album_keys_by_norm:
                        all_scrobbles.append((ts, norm_key, _normalize_for_match(track_name)))

            if page % 10 == 0:
                print(f"    Page {page}/{total_pages} ({len(all_scrobbles)} relevant scrobbles)")
            page += 1
            time.sleep(0.25)

        except Exception as e:
            print(f"    Error on page {page}: {e}")
            break

    print(f"  Collected {len(all_scrobbles)} relevant scrobbles across {page-1} pages")

    if not all_scrobbles:
        # Still apply any cached + Notion dates
        _apply_last_played(all_albums, lp_cache)
        return all_albums

    # Group scrobbles by album, then into sessions (4-hour window)
    SESSION_GAP = 4 * 3600  # 4 hours

    # Sort by album key then timestamp
    from collections import defaultdict
    scrobbles_by_album = defaultdict(list)
    for ts, norm_key, track_name in all_scrobbles:
        scrobbles_by_album[norm_key].append((ts, track_name))

    # For each album, find sessions where 50%+ tracks were played
    new_last_played = {}  # norm_key -> ISO date string of most recent qualifying session

    for norm_key, scrobbles in scrobbles_by_album.items():
        track_count = album_track_counts.get(norm_key, 0)
        if not track_count:
            # No track count data — fall back to "any scrobble counts"
            threshold = 1
        else:
            threshold = max(1, (track_count + 1) // 2)  # ceil(50%)

        # Sort by timestamp descending (newest first)
        scrobbles.sort(key=lambda x: -x[0])

        # Walk through scrobbles and group into sessions
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

        # Check each session (newest first) for 50% threshold
        for session in sessions:
            unique_tracks = len(set(tn for _, tn in session))
            if unique_tracks >= threshold:
                # This session qualifies — take the newest timestamp
                session_date = datetime.utcfromtimestamp(session[0][0]).strftime("%Y-%m-%d")
                new_last_played[norm_key] = session_date
                break  # Only need the most recent qualifying session

    print(f"  Found qualifying listens for {len(new_last_played)} albums (50%+ threshold)")

    # Merge into cache: keep whichever date is newer
    existing_dates = lp_cache.get("dates", {})
    for norm_key, date_str in new_last_played.items():
        existing = existing_dates.get(norm_key, "")
        if not existing or date_str > existing:
            existing_dates[norm_key] = date_str

    lp_cache["dates"] = existing_dates
    lp_cache["_last_scan_ts"] = time.time()
    _save_json_atomic(LASTPLAYED_CACHE_FILE, lp_cache)

    _apply_last_played(all_albums, lp_cache)
    return all_albums


def _apply_last_played(albums, lp_cache):
    """Apply last-played dates to albums. Uses scrobble data from cache,
    merged with Notion last_played — whichever is more recent wins."""
    dates = lp_cache.get("dates", {})
    applied = 0

    for a in albums:
        norm_key = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        scrobble_date = dates.get(norm_key, "")
        notion_date = a.get("last_played", "")

        # Pick whichever is more recent
        best_date = ""
        if scrobble_date and notion_date:
            best_date = max(scrobble_date, notion_date)
        else:
            best_date = scrobble_date or notion_date

        if best_date:
            a["last_played"] = best_date
            applied += 1

    print(f"  Applied last-played dates to {applied}/{len(albums)} albums")


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
            _save_json_atomic(DESCRIPTION_CACHE_FILE, desc_cache)
            _save_json_atomic(ARTIST_BIO_CACHE_FILE, bio_cache)

    # Save caches
    _save_json_atomic(DESCRIPTION_CACHE_FILE, desc_cache)
    _save_json_atomic(ARTIST_BIO_CACHE_FILE, bio_cache)
    print(f"  Generated {generated}/{len(to_generate)} descriptions")
    return albums


def update_rpm_from_albums(vinyl_albums, force=False):
    """Set the RPM badge (33/45) on vinyl by looking up Discogs and writing it
    back to Notion.

    This reuses the vinyl rows already fetched for the export, so the whole run
    queries Notion for vinyl exactly once. (Previously update_rpm.py ran as a
    separate process and did its own full Notion pass first.) Any album whose
    RPM we set is also updated in memory so the badge appears on this run's site.
    """
    import time

    if not (os.environ.get("DISCOGS_KEY") and os.environ.get("DISCOGS_SECRET")):
        print("  Skipping RPM badges: set DISCOGS_KEY and DISCOGS_SECRET")
        return
    if not os.environ.get("NOTION_TOKEN"):
        print("  Skipping RPM badges: set NOTION_TOKEN")
        return
    try:
        import update_rpm as rpm
    except Exception as e:
        print(f"  Skipping RPM badges: could not import update_rpm ({e})")
        return

    print(f"\n{'='*60}")
    print("Setting RPM badges from Discogs (single Notion pass)")
    print(f"{'='*60}\n")

    try:
        cache = rpm.load_cache()
    except Exception:
        cache = {}

    set_count = had = no_discogs = no_rpm_found = 0
    try:
        for a in vinyl_albums:
            m = re.search(r"/master/(\d+)", a.get("discogs_url") or "")
            if not m:
                no_discogs += 1
                continue
            if a.get("rpm") and not force:
                had += 1
                continue

            master_id = m.group(1)
            label = f"{a.get('artist', '?')} — {a.get('title', '?')}"

            if not force and master_id in cache:
                rpm_val = cache[master_id] or None
            else:
                try:
                    rpm_val = rpm.fetch_rpm_from_discogs(master_id)
                except Exception as e:
                    record_failure("RPM lookup (Discogs)", label, str(e))
                    continue
                cache[master_id] = rpm_val or ""
                time.sleep(rpm.DISCOGS_RATE_LIMIT)

            if not rpm_val:
                no_rpm_found += 1
                continue

            if not a.get("page_id"):
                record_failure("RPM write (Notion)", label, "no page_id on record")
                continue
            try:
                rpm.update_notion_rpm(a["page_id"], rpm_val)
                a["rpm"] = rpm_val
                set_count += 1
                time.sleep(rpm.NOTION_WRITE_PAUSE)
            except Exception as e:
                record_failure("RPM write (Notion)", label, str(e))
    finally:
        try:
            rpm.save_cache(cache)
        except Exception as e:
            print(f"  Warning: could not save rpm_cache.json: {e}")

    print(f"  RPM set: {set_count}  |  already had RPM: {had}  |  "
          f"no Discogs URL: {no_discogs}  |  Discogs had no RPM: {no_rpm_found}")
    print("  (No Discogs URL just means no badge could be auto-fetched; the album is still present.)")


def export_to_site(fast=False, collection="both"):
    print(f"\n{'='*60}")
    print("Exporting to GitHub Pages site" + (" (fast mode)" if fast else ""))
    print(f"{'='*60}\n")
    if not INDEX_HTML.exists():
        print(f"Error: {INDEX_HTML} not found."); sys.exit(1)
    import time as _time
    _t0 = _time.time()
    _last = [_t0]

    def _mark(label):
        now = _time.time()
        print(f"    ⏱  {label}: {now - _last[0]:.1f}s")
        _last[0] = now

    headers = get_notion_headers()
    cd = export_database(CD_DATABASE_ID, "CD Collection", headers)
    vinyl = export_database(VINYL_DATABASE_ID, "Vinyl Collection", headers)
    _mark("Notion export")
    if not fast and collection in ("vinyl", "both"):
        update_rpm_from_albums(vinyl)  # single Notion pass: set RPM badges in place
        _mark("RPM pass")
    cd = resolve_cover_urls(cd, "CDs")
    vinyl = resolve_cover_urls(vinyl, "Vinyl")
    cd = itunes_cover_fallback(cd, "CDs")
    vinyl = itunes_cover_fallback(vinyl, "Vinyl")
    _mark("Cover art")
    cd = fetch_genres(cd, "CDs")
    vinyl = fetch_genres(vinyl, "Vinyl")
    _mark("Genres")
    cd = extract_dominant_colors(cd, "CDs")
    vinyl = extract_dominant_colors(vinyl, "Vinyl")
    _mark("Colours")

    # Listening data: fetched from Last.fm on full runs, reused from the
    # cron-maintained albums.json on --fast runs
    all_albums = cd + vinyl
    if fast:
        all_albums = reuse_listening_data(all_albums)
        _mark("Listening data (reused)")
    else:
        all_albums = fetch_lastfm_data(all_albums)
        _mark("Last.fm play counts")
        all_albums = fetch_track_counts(all_albums)
        _mark("Track counts")
        all_albums = calculate_last_played(all_albums)
        _mark("Last played")
    all_albums = generate_ai_descriptions(all_albums)
    _mark("AI descriptions")
    # Re-split after enrichment
    cd_count = len(cd)
    cd = all_albums[:cd_count]
    vinyl = all_albums[cd_count:]

    # Find missing album suggestions for top artists
    # (MusicBrainz limits to 1 request/second, so this stays serial — but each
    # artist's discography is cached for 30 days, so it's usually instant)
    print("\n  Finding missing album suggestions...")
    suggestions = find_missing_albums(all_albums)
    _mark("Suggestions")

    write_albums_json(cd, vinyl, suggestions=suggestions)
    print(f"\n  Total: {len(cd)} CDs + {len(vinyl)} vinyl = {len(cd) + len(vinyl)} albums")
    print(f"  Export took {_time.time() - _t0:.1f}s")


# ---------------------------------------------------------------------------
# Step 3: Git push
# ---------------------------------------------------------------------------

def _run(cmd, **kwargs):
    """Run a git command and return the CompletedProcess."""
    kwargs.setdefault("cwd", SITE_DIR)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def git_push():
    print(f"\n{'='*60}")
    print("Pushing to GitHub")
    print(f"{'='*60}\n")

    MAX_RETRIES = 3

    # Stage everything we care about
    _run(["git", "add", "index.html", "albums.json", "heatmap_data.json",
          "update_all.py", "update_rpm.py"],
         check=False)

    # Check if there's anything to commit
    result = _run(["git", "diff", "--cached", "--quiet"])
    if result.returncode == 0:
        print("  No changes to push.")
        return

    _run(["git", "commit", "-m", "Update album data from Notion"], check=False)

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"  Push attempt {attempt}/{MAX_RETRIES}...")
        push = _run(["git", "push"])
        if push.returncode == 0:
            print("  Pushed to GitHub!")
            return

        # Push rejected: the GitHub Action (update-lastplayed.yml) advanced origin
        # while we were building. Our freshly-exported tree already has the latest
        # Notion album data AND fresh Last.fm data, so it is authoritative. Keep it
        # wholesale via an "ours" merge, which records origin/main as merged without
        # ever producing a conflict, then push again (now a fast-forward). No rebase,
        # no conflict markers, no manual intervention.
        print("  Push rejected; remote advanced. Reconciling (keep local build)...")
        _run(["git", "fetch", "origin"])
        merge = _run(["git", "merge", "-s", "ours", "--no-edit",
                      "-m", "Merge remote last-played updates (keep local rebuild)",
                      "origin/main"])
        if merge.returncode != 0:
            print(f"  Merge failed:\n{merge.stderr.strip()}")

    print("  Failed to push after retries. Run manually: "
          "git fetch && git merge -s ours --no-edit origin/main && git push")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    notion_only = "--notion-only" in args
    export_only = "--export-only" in args
    fast = "--fast" in args
    collection = "both"
    for a in args:
        if a.startswith("--collection="):
            collection = a.split("=", 1)[1].lower() or "both"
    if collection not in ("cd", "vinyl", "both"):
        print("Error: --collection must be cd, vinyl, or both"); sys.exit(1)

    # --fast implies skipping the Notion metadata pass
    if not export_only and not fast:
        update_notion_databases(collection)
    if not notion_only:
        export_to_site(fast=fast, collection=collection)
        print_failure_summary()
        git_push()

    print(f"\n{'='*60}")
    print("All done!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
