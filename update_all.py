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


def inject_into_html(cd_albums, vinyl_albums, html_path):
    html = html_path.read_text(encoding="utf-8")
    for marker, data in [("CD", cd_albums), ("VINYL", vinyl_albums)]:
        data = clean_album_data(data)
        json_str = json.dumps(data, ensure_ascii=False)
        pattern = rf'/\* __{marker}_DATA__ \*/.*?/\* __END_{marker}_DATA__ \*/'
        html, count = re.subn(pattern, f'/* __{marker}_DATA__ */\n{json_str}\n/* __END_{marker}_DATA__ */', html, flags=re.DOTALL)
        if count == 0: print(f"  Warning: __{marker}_DATA__ markers not found")
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


def resolve_cover_urls(albums, label=""):
    """Resolve Cover Art Archive redirects to final archive.org URLs.

    coverartarchive.org/release-group/{mbid}/front-250 redirects (302) to
    an archive.org URL. Resolving at export time saves the browser a round-trip
    per image and lets CDN/browser caching work much better.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    to_resolve = [(i, a) for i, a in enumerate(albums)
                  if a.get("cover_url", "").startswith("https://coverartarchive.org/")]

    if not to_resolve:
        return albums

    print(f"  Resolving {len(to_resolve)} cover art URLs for {label}...")
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

    # Use 6 threads to be polite to Cover Art Archive
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(resolve_one, i, a) for i, a in to_resolve]
        for future in as_completed(futures):
            idx, final_url = future.result()
            if final_url:
                albums[idx]["cover_url"] = final_url
                resolved_count += 1

    print(f"  Resolved {resolved_count}/{len(to_resolve)} cover URLs to archive.org")
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
    cd = extract_dominant_colors(cd, "CDs")
    vinyl = extract_dominant_colors(vinyl, "Vinyl")
    inject_into_html(cd, vinyl, INDEX_HTML)
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
