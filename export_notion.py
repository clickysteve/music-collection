#!/usr/bin/env python3
"""
Export CD Collection from Notion to JSON for the GitHub Pages gallery.

Usage:
    python export_notion.py

Requires:
    pip install requests

Environment variables:
    NOTION_API_KEY  - Your Notion integration token (starts with ntn_)

The script reads your Notion CD Collection database, builds a JSON file
with all album data (including Cover Art Archive URLs from MBIDs), and
optionally commits + pushes to your GitHub Pages repo.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("Please install requests: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Your Notion database ID (extracted from the URL you shared)
NOTION_DATABASE_ID = "2f39e578d459801689dec91e5a424282"

# Notion API version
NOTION_API_VERSION = "2022-06-28"

# Output path (same directory as this script by default)
OUTPUT_DIR = Path(__file__).parent
OUTPUT_JSON = OUTPUT_DIR / "albums.json"
OUTPUT_HTML = OUTPUT_DIR / "index.html"

# Set to True to auto-commit and push after export
AUTO_GIT_PUSH = False

# ---------------------------------------------------------------------------
# Notion API helpers
# ---------------------------------------------------------------------------

def get_notion_headers():
    api_key = os.environ.get("NOTION_API_KEY")
    if not api_key:
        print("Error: Set the NOTION_API_KEY environment variable.")
        print("  1. Go to https://www.notion.so/my-integrations")
        print("  2. Create a new integration")
        print("  3. Copy the token and run:")
        print('     export NOTION_API_KEY="ntn_..."')
        print("  4. Don't forget to connect the integration to your CD Collection database")
        print("     (click ... menu on the database page -> Connections -> your integration)")
        sys.exit(1)
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def query_all_pages(database_id, headers):
    """Paginate through all pages in a Notion database."""
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    pages = []
    payload = {"page_size": 100}

    while True:
        resp = requests.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        pages.extend(data["results"])
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    return pages


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------

def get_title(props):
    """Extract the page title (Artist name)."""
    title_prop = props.get("Artist", {})
    parts = title_prop.get("title", [])
    return "".join(p.get("plain_text", "") for p in parts)


def get_rich_text(props, field_name):
    """Extract a rich_text field as plain text."""
    prop = props.get(field_name, {})
    parts = prop.get("rich_text", [])
    return "".join(p.get("plain_text", "") for p in parts)


def get_number(props, field_name):
    """Extract a number field."""
    prop = props.get(field_name, {})
    return prop.get("number")


def get_select(props, field_name):
    """Extract a select field value."""
    prop = props.get(field_name, {})
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def get_multi_select(props, field_name):
    """Extract multi-select field values as a list."""
    prop = props.get(field_name, {})
    return [s.get("name", "") for s in prop.get("multi_select", [])]


def get_url(props, field_name):
    """Extract a URL field."""
    prop = props.get(field_name, {})
    return prop.get("url") or ""


def get_date(props, field_name):
    """Extract a date field (returns the start date string)."""
    prop = props.get(field_name, {})
    date_obj = prop.get("date")
    if date_obj:
        return date_obj.get("start", "")
    return ""


def get_formula_string(props, field_name):
    """Extract a formula field that returns a string."""
    prop = props.get(field_name, {})
    formula = prop.get("formula", {})
    if formula.get("type") == "string":
        return formula.get("string", "")
    elif formula.get("type") == "number":
        val = formula.get("number")
        return str(val) if val is not None else ""
    return ""


def get_files(props, field_name):
    """Extract a files field (returns the first file URL)."""
    prop = props.get(field_name, {})
    files = prop.get("files", [])
    if files:
        f = files[0]
        if f.get("type") == "external":
            return f.get("external", {}).get("url", "")
        elif f.get("type") == "file":
            return f.get("file", {}).get("url", "")
    return ""


# ---------------------------------------------------------------------------
# Main export logic
# ---------------------------------------------------------------------------

def page_to_album(page):
    """Convert a Notion page to an album dict for the JSON export."""
    props = page["properties"]

    artist = get_title(props)
    title = get_rich_text(props, "Title")
    year = get_number(props, "Year")
    album_type = get_select(props, "Type")
    runtime = get_number(props, "Runtime")
    mbid = get_rich_text(props, "MBID")
    mb_url = get_url(props, "MB URL")
    discogs_url = get_url(props, "Discogs URL")
    direct_scrobble_url = get_url(props, "Direct Scrobble")
    scrobble = get_select(props, "Scrobble") or get_rich_text(props, "Scrobble")
    last_played = get_date(props, "Last Played")

    # Played! - could be select or multi-select
    played_prop = props.get("Played!", {})
    if played_prop.get("type") == "multi_select":
        played_vals = get_multi_select(props, "Played!")
        played = ", ".join(played_vals) if played_vals else ""
    elif played_prop.get("type") == "select":
        played = get_select(props, "Played!")
    else:
        played = ""

    # Cover art: prefer the Notion Cover field, fall back to Cover Art Archive
    cover_url = get_url(props, "Cover") or get_files(props, "Cover")
    if not cover_url and mbid:
        cover_url = f"https://coverartarchive.org/release/{mbid}/front-250"

    # Length (might be a formula)
    length_prop = props.get("Length", {})
    if length_prop.get("type") == "formula":
        length = get_formula_string(props, "Length")
    else:
        length = get_rich_text(props, "Length")

    # If Length is empty, compute from Runtime
    if not length and runtime:
        mins = int(runtime)
        length = f"{mins} min"

    return {
        "artist": artist,
        "title": title,
        "year": year,
        "type": album_type,
        "runtime": runtime,
        "length": length,
        "cover_url": cover_url,
        "mbid": mbid,
        "mb_url": mb_url,
        "discogs_url": discogs_url,
        "scrobble": scrobble,
        "played": played,
        "last_played": last_played,
        "direct_scrobble_url": direct_scrobble_url,
    }


def inject_into_html(albums, html_path):
    """Replace the inline ALBUMS data in index.html with fresh data."""
    import re
    html = html_path.read_text(encoding="utf-8")
    json_str = json.dumps(albums, ensure_ascii=False)
    # Replace everything between the marker comments
    pattern = r'/\* __ALBUM_DATA__ \*/.*?/\* __END_ALBUM_DATA__ \*/'
    replacement = f'/* __ALBUM_DATA__ */\n{json_str}\n/* __END_ALBUM_DATA__ */'
    new_html, count = re.subn(pattern, replacement, html, flags=re.DOTALL)
    if count == 0:
        print("Warning: Could not find __ALBUM_DATA__ markers in index.html")
        print("The JSON file was still written, but index.html was not updated.")
        return
    html_path.write_text(new_html, encoding="utf-8")
    print(f"Injected {len(albums)} albums into {html_path}")


def git_push(output_dir):
    """Commit and push changes to the git repo."""
    try:
        subprocess.run(["git", "add", "albums.json", "index.html"], cwd=output_dir, check=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=output_dir,
            capture_output=True
        )
        if result.returncode == 0:
            print("No changes to albums.json — skipping git push.")
            return
        subprocess.run(
            ["git", "commit", "-m", "Update album data from Notion"],
            cwd=output_dir,
            check=True,
        )
        subprocess.run(["git", "push"], cwd=output_dir, check=True)
        print("Pushed updated albums.json to GitHub.")
    except subprocess.CalledProcessError as e:
        print(f"Git operation failed: {e}")
        print("You may need to commit and push manually.")


def main():
    print(f"Exporting CD Collection from Notion database {NOTION_DATABASE_ID}...")
    headers = get_notion_headers()

    # Fetch all pages
    pages = query_all_pages(NOTION_DATABASE_ID, headers)
    print(f"Fetched {len(pages)} albums from Notion.")

    # Convert to album dicts
    albums = []
    for page in pages:
        try:
            album = page_to_album(page)
            if album["artist"] and album["title"]:  # skip empty rows
                albums.append(album)
        except Exception as e:
            page_id = page.get("id", "unknown")
            print(f"Warning: Failed to process page {page_id}: {e}")

    # Sort by artist, then title
    albums.sort(key=lambda a: (a["artist"].lower(), a["title"].lower()))

    # Write JSON (as a backup / for other tools)
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(albums, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(albums)} albums to {OUTPUT_JSON}")

    # Inject into index.html so the page works as a standalone file
    if OUTPUT_HTML.exists():
        inject_into_html(albums, OUTPUT_HTML)
    else:
        print(f"Warning: {OUTPUT_HTML} not found — skipping HTML injection.")

    # Optionally push to git
    if AUTO_GIT_PUSH:
        git_push(OUTPUT_DIR)
    else:
        print("Tip: Set AUTO_GIT_PUSH = True to auto-commit and push after export.")
        print("Or run: git add albums.json && git commit -m 'Update albums' && git push")


if __name__ == "__main__":
    main()
