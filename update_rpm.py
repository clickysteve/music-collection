#!/usr/bin/env python3
"""
update_rpm.py — Fetch RPM (33/45) for vinyl albums from Discogs and write back to Notion.

For each vinyl album with a Discogs master URL:
1. Check rpm_cache.json — skip if already cached
2. Hit Discogs /masters/{id}/versions?format=Vinyl to get format strings
3. Extract RPM from format descriptions (explicit first, then heuristic)
4. Write RPM back to Notion "RPM" column (Select property)
5. Cache the result

Environment variables:
    NOTION_TOKEN        - Notion integration token
    DISCOGS_KEY         - Discogs consumer key
    DISCOGS_SECRET      - Discogs consumer secret

Usage:
    python update_rpm.py                    # update missing RPMs only
    python update_rpm.py --force            # re-fetch all RPMs from Discogs
    python update_rpm.py --dry-run          # show what would be updated without writing
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

VINYL_DATABASE_ID = "43a34f8c6c6c46c780ddac4697e36b0b"
NOTION_VERSION = "2022-06-28"
CACHE_FILE = Path(__file__).parent / "rpm_cache.json"

DISCOGS_BASE = "https://api.discogs.com"
DISCOGS_RATE_LIMIT = 1.0  # seconds between Discogs requests (60/min authenticated)
NOTION_WRITE_PAUSE = 0.25

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def notion_headers():
    return {
        "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def discogs_params():
    return {
        "key": os.environ["DISCOGS_KEY"],
        "secret": os.environ["DISCOGS_SECRET"],
    }

def discogs_headers():
    return {"User-Agent": "MusicCollectionRPM/1.0"}


def load_cache():
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def extract_rpm_from_formats(format_strings):
    """
    Given a list of format strings from Discogs versions, determine RPM.
    Returns "33", "45", or None.
    """
    rpm_votes = {"33": 0, "45": 0}

    for fmt in format_strings:
        fmt_lower = fmt.lower()

        # Explicit RPM mentioned
        if "45 rpm" in fmt_lower:
            rpm_votes["45"] += 1
            continue
        if "33" in fmt_lower and "rpm" in fmt_lower:
            rpm_votes["33"] += 1
            continue

        # Heuristic based on format type
        parts = [p.strip().lower() for p in fmt.split(",")]

        has_lp = any(p == "lp" for p in parts)
        has_7 = any(p.startswith('7"') or p == '7"' for p in parts)
        has_10 = any(p.startswith('10"') or p == '10"' for p in parts)
        has_12 = any(p.startswith('12"') or p == '12"' for p in parts)

        if has_lp:
            rpm_votes["33"] += 1
        elif has_7:
            rpm_votes["45"] += 1
        elif has_12:
            # 12" EPs/singles are often 45, but 12" LPs are 33
            is_single_or_ep = any(p in ("single", "ep", "maxi-single") for p in parts)
            if is_single_or_ep:
                rpm_votes["45"] += 1
            else:
                rpm_votes["33"] += 1
        elif has_10:
            rpm_votes["33"] += 1  # 10" more commonly 33 for albums

    if rpm_votes["33"] > 0 or rpm_votes["45"] > 0:
        return "33" if rpm_votes["33"] >= rpm_votes["45"] else "45"
    return None


def fetch_rpm_from_discogs(master_id):
    """Fetch vinyl versions of a master and determine RPM."""
    url = f"{DISCOGS_BASE}/masters/{master_id}/versions"
    params = {**discogs_params(), "format": "Vinyl", "per_page": 10}

    r = requests.get(url, headers=discogs_headers(), params=params, timeout=30)
    if r.status_code == 429:
        print("    Rate limited, waiting 60s...")
        time.sleep(60)
        r = requests.get(url, headers=discogs_headers(), params=params, timeout=30)

    if r.status_code != 200:
        print(f"    Discogs API error: {r.status_code}")
        return None

    data = r.json()
    versions = data.get("versions", [])
    if not versions:
        return None

    format_strings = [v.get("format", "") for v in versions if v.get("format")]
    return extract_rpm_from_formats(format_strings)


def query_all_vinyl_pages():
    """Get all pages from the vinyl Notion database."""
    pages = []
    url = f"https://api.notion.com/v1/databases/{VINYL_DATABASE_ID}/query"
    start_cursor = None

    while True:
        payload = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor

        r = requests.post(url, headers=notion_headers(), json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        pages.extend(data.get("results", []))

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    return pages


def get_page_info(page):
    """Extract artist, title, discogs master ID, and current RPM from a page."""
    props = page["properties"]

    # Artist (title property)
    artist_parts = props.get("Artist", {}).get("title", [])
    artist = "".join(t.get("plain_text", "") for t in artist_parts).strip()

    # Title (rich text)
    title_parts = props.get("Title", {}).get("rich_text", [])
    title = "".join(t.get("plain_text", "") for t in title_parts).strip()

    # Discogs URL
    discogs_url = props.get("Discogs URL", {}).get("url", "") or ""
    master_match = re.search(r"/master/(\d+)", discogs_url)
    master_id = master_match.group(1) if master_match else None

    # Current RPM value
    rpm_prop = props.get("RPM", {})
    if rpm_prop.get("type") == "select":
        current_rpm = (rpm_prop.get("select") or {}).get("name", "")
    elif rpm_prop.get("type") == "rich_text":
        rpm_parts = rpm_prop.get("rich_text", [])
        current_rpm = "".join(t.get("plain_text", "") for t in rpm_parts).strip()
    else:
        current_rpm = ""

    return {
        "page_id": page["id"],
        "artist": artist,
        "title": title,
        "master_id": master_id,
        "current_rpm": current_rpm,
    }


def update_notion_rpm(page_id, rpm_value):
    """Write RPM to Notion page as a Select property."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {
        "properties": {
            "RPM": {
                "select": {"name": rpm_value}
            }
        }
    }
    r = requests.patch(url, headers=notion_headers(), json=payload, timeout=30)
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    force = "--force" in sys.argv
    dry_run = "--dry-run" in sys.argv

    for var in ("NOTION_TOKEN", "DISCOGS_KEY", "DISCOGS_SECRET"):
        if not os.environ.get(var):
            print(f"Error: {var} not set")
            sys.exit(1)

    cache = load_cache()

    print("Fetching vinyl albums from Notion...")
    pages = query_all_vinyl_pages()
    print(f"  Found {len(pages)} vinyl albums")

    updated = 0
    skipped = 0
    failed = 0
    no_discogs = 0

    for i, page in enumerate(pages):
        info = get_page_info(page)
        label = f"{info['artist']} - {info['title']}"

        if not info["master_id"]:
            no_discogs += 1
            continue

        mid = info["master_id"]

        # Check cache (unless forcing)
        if not force and mid in cache:
            # Still write to Notion if the page doesn't have RPM yet
            if not info["current_rpm"] and cache[mid]:
                print(f"  [{i+1}/{len(pages)}] {label} -> {cache[mid]} (from cache, writing to Notion)")
                if not dry_run:
                    try:
                        update_notion_rpm(info["page_id"], cache[mid])
                        time.sleep(NOTION_WRITE_PAUSE)
                        updated += 1
                    except Exception as e:
                        print(f"    Notion write failed: {e}")
                        failed += 1
            else:
                skipped += 1
            continue

        # Fetch from Discogs
        print(f"  [{i+1}/{len(pages)}] {label} ... ", end="", flush=True)
        rpm = fetch_rpm_from_discogs(mid)
        time.sleep(DISCOGS_RATE_LIMIT)

        if rpm:
            print(f"{rpm} RPM")
            cache[mid] = rpm
            if not dry_run:
                try:
                    update_notion_rpm(info["page_id"], rpm)
                    time.sleep(NOTION_WRITE_PAUSE)
                    updated += 1
                except Exception as e:
                    print(f"    Notion write failed: {e}")
                    failed += 1
        else:
            print("unknown")
            cache[mid] = ""
            failed += 1

        # Save cache periodically
        if (i + 1) % 20 == 0 and not dry_run:
            save_cache(cache)

    if not dry_run:
        save_cache(cache)

    print(f"\n{'='*60}")
    print(f"Done! Updated: {updated}, Skipped: {skipped}, No Discogs: {no_discogs}, Unknown: {failed}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
