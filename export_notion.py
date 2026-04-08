#!/usr/bin/env python3
"""
Export CD & Vinyl Collections from Notion to the GitHub Pages gallery.

Usage:
    python export_notion.py

Requires:
    pip install requests

Environment variables:
    NOTION_TOKEN  - Your Notion integration token (starts with ntn_)
"""

import json
import os
import re
import sys
import unicodedata
from pathlib import Path

try:
    import requests
except ImportError:
    print("Please install requests: pip install requests")
    sys.exit(1)

DATABASES = {
    "cd": "2f39e578d459801689dec91e5a424282",
    "vinyl": "43a34f8c6c6c46c780ddac4697e36b0b",
}

NOTION_API_VERSION = "2022-06-28"
OUTPUT_DIR = Path(__file__).parent
OUTPUT_HTML = OUTPUT_DIR / "index.html"


def get_notion_headers():
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        print("Error: Set the NOTION_TOKEN environment variable.")
        sys.exit(1)
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def query_all_pages(database_id, headers):
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    pages, payload = [], {"page_size": 100}
    while True:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        pages.extend(data["results"])
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return pages


def get_title(props):
    return "".join(p.get("plain_text", "") for p in props.get("Artist", {}).get("title", []))

def get_rich_text(props, name):
    return "".join(p.get("plain_text", "") for p in props.get(name, {}).get("rich_text", []))

def get_number(props, name):
    return props.get(name, {}).get("number")

def get_select(props, name):
    sel = props.get(name, {}).get("select")
    return sel.get("name", "") if sel else ""

def get_multi_select(props, name):
    return [s.get("name", "") for s in props.get(name, {}).get("multi_select", [])]

def get_url(props, name):
    return props.get(name, {}).get("url") or ""

def get_date(props, name):
    d = props.get(name, {}).get("date")
    return d.get("start", "") if d else ""

def get_formula_string(props, name):
    f = props.get(name, {}).get("formula", {})
    if f.get("type") == "string": return f.get("string", "")
    if f.get("type") == "number":
        v = f.get("number")
        return str(v) if v is not None else ""
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
    if not length and runtime:
        length = f"{int(runtime)} min"

    return {
        "artist": artist, "title": title,
        "year": get_number(props, "Year"),
        "type": get_select(props, "Type"),
        "runtime": runtime, "length": length,
        "cover_url": f"https://coverartarchive.org/release-group/{mbid}/front-250" if mbid else "",
        "mbid": mbid,
        "mb_url": get_url(props, "MB URL"),
        "discogs_url": get_url(props, "Discogs URL"),
        "scrobble": get_select(props, "Scrobble") or get_rich_text(props, "Scrobble"),
        "played": played,
        "last_played": get_date(props, "Last Played"),
        "direct_scrobble_url": get_url(props, "Direct Scrobble"),
    }


def export_database(db_id, label, headers):
    print(f"  Exporting {label}...")
    pages = query_all_pages(db_id, headers)
    albums = []
    for page in pages:
        try:
            album = page_to_album(page)
            if album["artist"] and album["title"]:
                albums.append(album)
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
        if count == 0:
            print(f"  Warning: __{marker}_DATA__ markers not found")
    html_path.write_text(html, encoding="utf-8")
    print(f"  Injected {len(cd_albums)} CDs + {len(vinyl_albums)} vinyl into {html_path.name}")


def main():
    headers = get_notion_headers()
    cd = export_database(DATABASES["cd"], "CD Collection", headers)
    vinyl = export_database(DATABASES["vinyl"], "Vinyl Collection", headers)
    print(f"\n  Total: {len(cd)} CDs + {len(vinyl)} vinyl = {len(cd) + len(vinyl)} albums")
    if OUTPUT_HTML.exists():
        inject_into_html(cd, vinyl, OUTPUT_HTML)
    else:
        print(f"  Warning: {OUTPUT_HTML} not found")
    print("\nDone! Upload index.html to GitHub or use update_all.py for auto-push.")


if __name__ == "__main__":
    main()
