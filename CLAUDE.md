# Music Collection — Project Context

Personal music collection web app. Live at `music.clickysteve.com`. GitHub Pages.
Global memory: `/Users/steve/Documents/Scripts/claude-mem/CLAUDE.md`

## Session Start
1. Check for merge conflicts from the 15-min cron before making changes
2. Confirm next action from global memory: `/Users/steve/Documents/Scripts/claude-mem/CLAUDE.md`

---

## Status
Live. 233 CDs + 387 vinyl = 620 albums.
**Next:** Nothing queued.

## Data Flow
Notion (source of truth) → Python scripts → `albums.json` → `index.html` → GitHub Pages

Notion databases:
- CDs: `2f39e578d459801689dec91e5a424282`
- Vinyl: `43a34f8c6c6c46c780ddac4697e36b0b`

## Update Pipeline
```bash
./update.sh   # gitignored — has all API keys
```
Runs `update_rpm.py` (Discogs → Notion RPM column) then `update_all.py` (Notion export + Last.fm + AI descriptions + git push).

GitHub Action: `update_lastplayed.py` runs every 15 minutes via cron.

## Cache Files (all .json, committed)
`albums.json`, `cover_cache`, `color_cache`, `rpm_cache`, `lastfm_cache`, `lastplayed_cache`, `genre_cache`, `trackcount_cache`, `description_cache`, `artist_bio_cache`, `heatmap_data`, `suggestions_cache`

## Deploy Checklist
1. Make changes to `index.html` or Python scripts
2. Run `./update.sh` to sync from Notion + Last.fm + push
3. If merge conflict from cron: `git stash && git pull --rebase && git stash pop`
4. Verify at `music.clickysteve.com`

---

## Key Gotchas
- **15-min cron + manual pushes = merge conflicts** on cache files. Resolve with:
  ```bash
  git stash && git pull --rebase && git stash pop
  ```
- **150 vinyl albums lack Discogs URLs** — RPM must be set manually in Notion, can't be auto-fetched
- **RPM badges:** 33rpm = orange (synaesthesia), 45rpm = yellow. Set via Notion "RPM" select column.

## Features
Album covers, genres, RPM badges, Last.fm scrobble heatmap, In Rotation section, "What Am I Missing" suggestions (filter-aware), mosaic mode, fuzzy search (handles μ/µ→mu, curly quotes, diacritics, strips hyphens/spaces).

## Hosting
Custom domain `music.clickysteve.com` via CNAME on WordPress.com DNS → `clickysteve.github.io`.
