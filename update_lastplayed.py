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


# Trailing parenthetical variants in collection titles: "Swim (blue)",
# "Dookie (green case)", "Nimrod (Australia)" etc.
_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")
# Trailing decorations Last.fm often appends to album names
_SUFFIX_RE = re.compile(
    r"\s+(ep|single|promo|demo|deluxe( edition| version)?|"
    r"remaster(ed)?( \d{4})?|anniversary edition)$")


_ROMAN = {"1": "i", "2": "ii", "3": "iii", "4": "iv", "5": "v",
          "6": "vi", "7": "vii", "8": "viii", "9": "ix", "10": "x"}
_ROMAN_REV = {v: k for k, v in _ROMAN.items()}


def _fold_accents(s):
    """Strip diacritics: 'Voilà' -> 'Voila', 'Röyksopp' -> 'Royksopp'."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _numeral_tokens(s):
    """All numbers in a string, arabic or roman — including romans glued to
    the end of squashed forms ('chinoiseriesptiii' -> 'iii')."""
    toks = re.findall(r"\d+", s) + re.findall(r"\b[ivx]{1,4}\b", s)
    m = re.search(r"[^ivx\W]([ivx]{1,4})$", s)
    if m:
        toks.append(m.group(1))
    return toks


def _digits_differ(a, b):
    """True when two strings contain different numbers — guards fuzzy rules
    from matching 'Chinoiseries Pt.3' against 'Chinoiseries Pt.2'."""
    return _numeral_tokens(a) != _numeral_tokens(b)


_VOLUME_RE = re.compile(
    r"^(?:(?:pt|part|vol|volume|no|number|chapter|book|disc|cd)\s*)?"
    r"(?:\d{1,3}|[ivx]{1,4})$")


def _is_volume_marker(s):
    """'pt 2', 'vol iii', '2' — a sequel marker, not a subtitle. Prefix rules
    must not equate 'Chinoiseries Pt 2' with 'Chinoiseries'."""
    return bool(_VOLUME_RE.match(s.strip()))


def _norm_variants(title):
    """Base normalised forms of a title."""
    out = set()
    for t in (title, _PAREN_RE.sub("", title), _fold_accents(title)):
        n = _normalize_for_match(t)
        if n:
            out.add(n)
        # Punctuation as word separator: "Nu*Med" -> "nu med"
        spaced = re.sub(r"[^\w\s]", " ", t.lower().replace("&", "and")).replace("_", " ")
        spaced = re.sub(r"\s+", " ", spaced).strip()
        spaced = re.sub(r"^(the|a|an)\s+", "", spaced)
        if spaced:
            out.add(spaced)
        # ASCII-only: drops mojibake / translated segments ("Shadoof/ Шадуф")
        n_ascii = _normalize_for_match(re.sub(r"[^\x00-\x7f]+", " ", t))
        if n_ascii and len(n_ascii) >= 6:
            out.add(n_ascii)
    # Spelling variants
    for k in list(out):
        if re.search(r"\bokay\b", k):
            out.add(re.sub(r"\bokay\b", "ok", k))
        if re.search(r"\bn\b", k):     # "Rock n Roll" == "Rock and Roll"
            out.add(re.sub(r"\bn\b", "and", k))
    # Spacing variants: "family" == "f a m i l y"
    for k in list(out):
        squashed = k.replace(" ", "")
        if len(squashed) >= 5:
            out.add(squashed)
    # Word-order variants: "…More Painful and Sad…" == "…More Sad and Painful…"
    for k in list(out):
        words = k.split()
        if len(words) >= 4:
            out.add("\x00sorted:" + " ".join(sorted(words)))
    return out


def _artist_variants(artist):
    """Normalised variants of an artist name, for alias resolution."""
    out = set()
    # Comma-inverted articles: "Pixies, the" -> "Pixies"
    uninverted = re.sub(r",\s*(the|a|an)\s*$", "", artist, flags=re.I)
    for src in (artist, _fold_accents(artist), uninverted, _fold_accents(uninverted)):
        n = _normalize_for_match(src)
        if n:
            out.add(n)
        spaced = re.sub(r"[^\w\s]", " ", src.lower().replace("&", "and")).replace("_", " ")
        spaced = re.sub(r"\s+", " ", spaced).strip()
        spaced = re.sub(r"^(the|a|an)\s+", "", spaced)
        if spaced:
            out.add(spaced)
    # No-space forms: "Zero7" == "Zero 7", "Meltbanana" == "Melt Banana"
    for k in list(out):
        squashed = k.replace(" ", "")
        if len(squashed) >= 5:
            out.add(squashed)
    return out


def _title_keys(title):
    """Normalised variants of an album title, for fuzzy matching."""
    keys = set()
    for k in _norm_variants(title):
        keys.add(k)
        s = _SUFFIX_RE.sub("", k)
        if s:
            keys.add(s)
    # Trailing volume numbers: "vol2" == "vol ii" == "vol 2" ("Vol.2" vs "Vol.II")
    extra = set()
    for k in keys:
        m = re.match(r"^(.*?)\s?(\d{1,2})$", k)
        if m and m.group(2) in _ROMAN:
            base = m.group(1).rstrip()
            for suffix in (_ROMAN[m.group(2)], m.group(2)):
                extra.add((base + " " + suffix).strip())
                extra.add((base + suffix).strip())
        m2 = re.match(r"^(.*?)\s?(ii|iii|iv|vi|vii|viii|ix)$", k)
        if m2:
            base = m2.group(1).rstrip()
            for suffix in (_ROMAN_REV[m2.group(2)], m2.group(2)):
                extra.add((base + " " + suffix).strip())
                extra.add((base + suffix).strip())
    keys |= {e for e in extra if e}
    return keys


class AlbumMatcher:
    """Match Last.fm artist/album names to collection albums.

    Titles: exact match on normalised variants (pressing notes, EP/promo
    suffixes, accents, spacing, word order, roman numerals) → word-boundary
    prefix/suffix rules → digit-guarded near-match for typos.

    Artists: exact normalised match → alias resolution (spacing, accents,
    underscores, "/"-split collaborations) → digit-guarded close match, so
    "Braniac", "Zero7" and "Royksopp" find brainiac, zero 7 and röyksopp.

    Pressing variants are grouped at registration: "Swim (red)", "Swim (blue)"
    and "Stop Making Sense - Tour" all share one canonical key, so plays land
    on every copy.
    """

    MIN_PREFIX = 8  # chars — keeps short titles from prefix-matching wrongly

    def __init__(self, albums):
        self.exact = {}         # (norm_artist, title_key) -> canonical norm_key
        self.by_artist = {}     # norm_artist -> [(title_key, canonical), ...]
        self.artist_alias = {}  # alias variant -> norm_artist
        for a in albums:
            nart = _normalize_for_match(a["artist"])
            tkeys = _title_keys(a["title"])
            # Group with an already-registered album (pressing variants of the
            # same record share a canonical, so plays reach every copy)
            canonical = self._lookup([nart], tkeys) \
                or f"{nart}|||{_normalize_for_match(a['title'])}"
            for tk in tkeys:
                self.exact.setdefault((nart, tk), canonical)
                self.by_artist.setdefault(nart, []).append((tk, canonical))
            for alias in _artist_variants(a["artist"]):
                self.artist_alias.setdefault(alias, [])
                if nart not in self.artist_alias[alias]:
                    self.artist_alias[alias].append(nart)
            # Split collaborations ("A / B", "A x B"): each side is an alias
            # of the row ("Mt Fujitive x Smuv" answers to plain "Smuv")
            for part in re.split(r"\s*/\s*|\s+x\s+", a["artist"], flags=re.I):
                p = _normalize_for_match(part)
                if len(p) >= 4 and p != nart:
                    self.artist_alias.setdefault(p, [])
                    if nart not in self.artist_alias[p]:
                        self.artist_alias[p].append(nart)
        self._memo = {}
        self._artist_memo = {}

    def _resolve_artists(self, artist, nart):
        """Collection norm-artist keys this Last.fm artist name could mean."""
        if artist in self._artist_memo:
            return self._artist_memo[artist]
        # Direct key first, then aliases — both, because "Pixies" must also
        # reach albums filed under "Pixies, the"
        found = [nart] if nart in self.by_artist else []
        for v in _artist_variants(artist):
            for hit in self.artist_alias.get(v, []):
                if hit not in found:
                    found.append(hit)
        if not found and len(nart) >= 5:
            import difflib
            for m in difflib.get_close_matches(nart, list(self.artist_alias.keys()),
                                               n=2, cutoff=0.85):
                if not _digits_differ(nart, m):
                    for hit in self.artist_alias[m]:
                        if hit not in found:
                            found.append(hit)
        if not found:
            # Word-boundary prefix: "Captain Beefheart & His Magic Band" ->
            # "Captain Beefheart"; "Himuro" -> "Himuro v. Koichi". Min 6 chars
            # so "Pre" can never reach "Prefuse 73".
            for coll in self.by_artist:
                if len(coll) >= 6 and nart.startswith(coll + " "):
                    found.append(coll)
                elif len(nart) >= 6 and coll.startswith(nart + " "):
                    found.append(coll)
        self._artist_memo[artist] = found
        return found

    def _lookup(self, narts, tkeys):
        """Match title keys against registered albums for the given artists."""
        def keys_for(na):
            # Also try titles with the artist name stripped off the front:
            # "Frank Zappa Meets the Mothers…" -> "Meets the Mothers…"
            ks = list(tkeys)
            for tk in tkeys:
                if tk.startswith(na + " "):
                    stripped = tk[len(na) + 1:]
                    if len(stripped) >= 4:
                        ks.append(stripped)
            return ks

        for na in narts:
            for tk in keys_for(na):
                result = self.exact.get((na, tk))
                if result:
                    return result
        for na in narts:
            for tk in keys_for(na):
                if tk.startswith("\x00"):
                    continue
                for ctk, canonical in self.by_artist.get(na, []):
                    if ctk.startswith("\x00"):
                        continue
                    if len(ctk) >= self.MIN_PREFIX:
                        if tk.startswith(ctk + " ") and not _is_volume_marker(tk[len(ctk) + 1:]):
                            return canonical
                        if tk.endswith(" " + ctk) and not _is_volume_marker(tk[:-len(ctk) - 1]):
                            return canonical
                    if len(tk) >= self.MIN_PREFIX:
                        if ctk.startswith(tk + " ") and not _is_volume_marker(ctk[len(tk) + 1:]):
                            return canonical
                        if ctk.endswith(" " + tk) and not _is_volume_marker(ctk[:-len(tk) - 1]):
                            return canonical
        # Last resort: near-identical titles (typos like "Juice B Crypt" vs
        # "Juice B Crypts") — digit-guarded so Pt.3 never matches Pt.2.
        # Also truncated titles: "The Peace & Truth of" vs the full (and
        # correctly-spelled) "The Peace and Truce Of Future Of the Left".
        import difflib
        for na in narts:
            for tk in keys_for(na):
                if len(tk) < 6 or tk.startswith("\x00"):
                    continue
                for ctk, canonical in self.by_artist.get(na, []):
                    if len(ctk) < 6 or ctk.startswith("\x00") or _digits_differ(tk, ctk):
                        continue
                    if difflib.SequenceMatcher(None, tk, ctk).ratio() >= 0.9:
                        return canonical
                    lo, hi = (tk, ctk) if len(tk) <= len(ctk) else (ctk, tk)
                    if (len(lo) >= 12
                            and difflib.SequenceMatcher(None, lo, hi[:len(lo)]).ratio() >= 0.85):
                        return canonical
        return None

    def match(self, artist, album):
        """Return the collection album's canonical norm_key, or None."""
        memo_key = (artist, album)
        if memo_key in self._memo:
            return self._memo[memo_key]
        nart = _normalize_for_match(artist)
        narts = self._resolve_artists(artist, nart)
        result = self._lookup(narts, _title_keys(album)) if narts else None
        self._memo[memo_key] = result
        return result


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

def calculate_last_played(scrobbles, all_albums, matcher):
    """Group collection scrobbles into sessions and apply the 50% threshold."""
    album_track_counts = {}
    for a in all_albums:
        norm_key = f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        tc = a.get("track_count") or 0
        if tc:
            album_track_counts[norm_key] = tc

    scrobbles_by_album = defaultdict(list)
    for uts, artist, album_name, track_name in scrobbles:
        if not album_name:
            continue
        norm_key = matcher.match(artist, album_name)
        if norm_key:
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

def update_history(scrobbles, all_albums, hist, backfill=False, matcher=None):
    """Update per-album and per-artist monthly play counts.

    Album entries count scrobbles matching a collection album (artist+album).
    Artist entries count ALL scrobbles by a collection artist, whatever the
    album. Only scrobbles newer than _hist_last_ts are counted, so overlapping
    scans never double-count.
    """
    hist_ts = 0 if backfill else hist.get("_hist_last_ts", 0)
    if matcher is None:
        matcher = AlbumMatcher(all_albums)

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
            akey = matcher.match(artist, album_name)
            if akey and akey in album_names:
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

    matcher = AlbumMatcher(all_albums)

    # Fetch play counts and match them to collection albums (fuzzy: pressing
    # variants, EP/promo suffixes, prefix titles). Multiple Last.fm entries
    # matching one album (e.g. "Swim" + "Swim EP") are summed.
    play_counts = fetch_top_albums(api_key, username)

    matched_plays = defaultdict(int)
    for key, val in play_counts.items():
        parts = key.split("|||")
        if len(parts) == 2:
            norm_key = matcher.match(parts[0], parts[1])
            if norm_key:
                matched_plays[norm_key] += val

    for a in all_albums:
        # Match the album's own title through the matcher too, so pressing
        # variants ("Swim (red)" and "Swim (blue)") share the same plays
        norm_key = matcher.match(a["artist"], a["title"]) \
            or f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        plays = matched_plays.get(norm_key, 0)
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
    lp_dates = calculate_last_played(scrobbles, all_albums, matcher)
    applied = 0
    for a in all_albums:
        norm_key = matcher.match(a["artist"], a["title"]) \
            or f"{_normalize_for_match(a['artist'])}|||{_normalize_for_match(a['title'])}"
        scrobble_date = lp_dates.get(norm_key, "")
        notion_date = (a.get("last_played") or "")[:10]
        best_date = max(scrobble_date, notion_date) if scrobble_date and notion_date else (scrobble_date or notion_date)
        if best_date and best_date != (a.get("last_played") or "")[:10]:
            a["last_played"] = best_date
            applied += 1
    print(f"  Updated last-played dates for {applied} albums")

    # Listening history
    new_hist = update_history(scrobbles, all_albums, hist, backfill=backfill, matcher=matcher)
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
