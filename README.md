# Music Collection Gallery

A static gallery site for browsing a physical music collection (CDs + vinyl), powered by Notion as the database and hosted for free on GitHub Pages.

![Dark themed gallery with album art grid, search, and filtering](https://img.shields.io/badge/albums-600%2B-1db954?style=flat-square) ![GitHub Pages](https://img.shields.io/badge/hosted-GitHub%20Pages-blue?style=flat-square)

## What it does

- Pulls album data from two Notion databases (CD + Vinyl)
- Resolves cover art from Cover Art Archive (with iTunes fallback)
- Fetches genre tags from MusicBrainz
- Pulls listening stats + full monthly listening history from Last.fm
  (with fuzzy album matching: pressing variants, EP/promo suffixes, roman
  numerals, near-miss titles)
- Generates missing album suggestions for the artists you own and play most
- Exports everything to static JSON (`albums.json`, `heatmap_data.json`)
  fetched by a single-page site on GitHub Pages
- A GitHub Action keeps play counts, last-played dates, and listening history
  fresh every 15 minutes

### Features

- Grid and list views with adjustable tile sizes
- Filter by collection (CD/Vinyl), type (Album/EP/Single), genre (searchable,
  alphabetical), release year/decade, and runtime ("I have time...")
- Time Machine — filter to what you actually listened to in any year, plus
  "Never played" and "Gathering dust (1 yr+)" views
- Sort by artist, title, year, recently played, most/least scrobbled, or
  colour (hue-bucketed rainbow grid)
- Shareable URLs — filters, sort, view, and open album all live in the hash
- Now Spinning — live banner when something's playing on Last.fm, with a
  spinning cover that opens the album if it's in the collection
- Year in Review — any year's plays, month-by-month chart, top albums/artists,
  and discoveries
- This Month in History — what this month sounded like in past years, plus
  first-listen anniversaries
- One-Month Wonders, Comebacks, and a Taste Over Time genre stream graph
- Wishlist — heart missing albums in the suggestions panel for record shopping
- Search across artists and titles (diacritic- and punctuation-insensitive)
- Album detail modal with metadata, description/bio, and links
- Stats dashboard with year chart, decade breakdown, top artists, genre breakdown, collection split, and collection vs listening compatibility
- Scrobble heatmap — cards glow by listening intensity
- Ambient background — subtle color shifts based on visible album art
- "Pick One" — pure-wildcard random album selector
- "I Have Time..." — pick albums that fit your available listening time
- Collector's indicator — shows when you own an album on both CD and vinyl
- "What am I missing?" gap identifier showing gaps in your top artists' discographies
- "Artists to Explore" — genre-based recommendations for expanding your collection
- "Who Influenced Who" — interactive force-directed graph showing artist connections based on shared genres, with drag-to-explore and click-to-filter
- "The Shelf" — 3D CSS spine view rendering your collection as album spines on wooden shelves, colour-matched from cover art, with pull-to-preview interaction
- Album descriptions/bios from Last.fm shown on cards and in the detail modal
- Clear all filters button and clickable logo to reset everything
- Collapsible toolbar for mobile browsing
- Scrobble links (direct or via OpenScrobbler)
- Scroll-to-top floating button
- Installable PWA — works offline (network-first data, cache-first cover art)
- Fully responsive dark theme
- Test suite (Python matcher unit tests + 42 jsdom site integration tests) run
  in CI on every push

## Setting up your own

### Prerequisites

- A [Notion](https://notion.so) account with two databases (one for CDs, one for vinyl — or just one if you prefer)
- Python 3.8+
- A [MusicBrainz](https://musicbrainz.org) user agent string (just your app name + contact email)
- A GitHub account for hosting

### 1. Set up your Notion databases

Each database needs these properties:

| Property | Type | Description |
|----------|------|-------------|
| Artist | Title | Artist name |
| Title | Rich text | Album title |
| Year | Number | Release year |
| Type | Select | Album, EP, Single, Compilation |
| Runtime | Number | Length in minutes |
| MBID | Rich text | MusicBrainz release-group ID |
| MB URL | URL | MusicBrainz link |
| Discogs URL | URL | Discogs link |
| Played! | Multi-select or Select | Whether you've played it |
| Last Played | Date | When you last listened |
| Direct Scrobble | URL | Direct scrobble link (optional) |

Create a [Notion integration](https://www.notion.so/my-integrations) and share both databases with it.

### 2. Configure the script

Open `update_all.py` and replace the database IDs near the top:

```python
CD_DATABASE_ID = "your_cd_database_id_here"
VINYL_DATABASE_ID = "your_vinyl_database_id_here"
```

You can find these in each database's Notion URL — it's the long hex string after the workspace name.

### 3. Install dependencies

```bash
pip install requests colorthief
```

### 4. Set environment variables

```bash
export NOTION_TOKEN="your_notion_integration_token"
export MB_USER_AGENT="YourAppName/1.0 (your@email.com)"

# Optional: Last.fm integration
export LASTFM_API_KEY="your_lastfm_api_key"    # Free at https://www.last.fm/api/account/create
export LASTFM_USER="your_lastfm_username"
```

### 5. Run it

```bash
python update_all.py
```

This will:
1. Update your Notion databases with MusicBrainz/Discogs metadata
2. Export everything, resolve cover art, fetch genres, extract colors
3. Fetch Last.fm stats and album descriptions (if API key set)
4. Write the data to `albums.json`
5. Commit and push to GitHub

Options:
- `--fast` — daily refresh: skips the Notion metadata pass, RPM pass, and all
  Last.fm fetches (listening data is reused from `albums.json`, which the
  GitHub Action keeps fresh). Runs in a few seconds.
- `--collection=cd|vinyl|both` — limit the Notion metadata/RPM passes to one
  database
- `--export-only` / `--notion-only` — as they sound

For the full listening-history features, also run
`python update_lastplayed.py --backfill` once to build `heatmap_data.json`
from your complete scrobble history.

### 6. Enable GitHub Pages

In your repo settings, go to **Pages** and set the source to the `main` branch. Your gallery will be live at `https://yourusername.github.io/your-repo-name/`.

## How it works

Album data lives in `albums.json`, which `index.html` fetches on load — no server, no database, just two static files. Listening history (monthly play counts per album/artist) lives in `heatmap_data.json`, fetched lazily for the "This Month in History" and "Year in Review" features. Because the data is separate from the markup, the 15-minute last-played cron only ever rewrites the JSON files.

Note: since data is fetched at runtime, preview locally over HTTP (`python3 -m http.server`) rather than opening `index.html` from `file://`.

The export script maintains several cache files to keep subsequent runs fast:

- `cover_cache.json` — resolved Cover Art Archive URLs (permanent)
- `genre_cache.json` — MusicBrainz genre tags (permanent)
- `color_cache.json` — dominant colors extracted from covers (permanent)
- `suggestions_cache.json` — artist discography data (30-day TTL)
- `lastfm_cache.json` — Last.fm listening stats (24-hour TTL)
- `description_cache.json` — Last.fm album descriptions (permanent)

After the first run, only newly added albums trigger network requests.

## File overview

| File | Purpose |
|------|---------|
| `index.html` | The gallery — HTML, CSS, and JS in one file |
| `albums.json` | All album + suggestions data, fetched by the site at runtime |
| `heatmap_data.json` | Monthly listening history per album/artist (Last.fm) |
| `sw.js` | Service Worker for caching cover art images |
| `update_all.py` | Main script — updates Notion, exports, pushes |
| `update_lastplayed.py` | Cron script — last played, play counts, listening history |
| `notion_covers.py` | Updates Notion with MusicBrainz/Discogs metadata |
| `export_notion.py` | Standalone export (used by update_all.py) |
| `manifest.webmanifest`, `icon-*.png` | PWA install metadata + icons |
| `tests/` | AlbumMatcher unit tests + jsdom site integration tests (run in CI) |
| `*_cache.json` | Local caches to speed up repeated runs (gitignored) |

## License

Do whatever you want with it.
