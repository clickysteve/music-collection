#!/usr/bin/env python3
"""
mb_client.py — one shared, well-behaved MusicBrainz API client.

MusicBrainz throttles by source IP at (on average) 1 request/second, and the
penalty is all-or-nothing: go over and *every* request is refused with HTTP 503
until the measured rate drops again. See
https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting

Three things follow from that, and this module exists so every caller gets all
three instead of each script reinventing its own `time.sleep()`:

1. One throttle for the whole process. Separate per-phase sleeps in different
   functions can't see each other, so back-to-back phases end up stacking.
2. Real headroom. 1.1s of sleep is ~0.9 req/s on paper, but request setup and
   MusicBrainz's own averaging window leave no margin — a long run drifts over.
3. Backing *off* on 503. Retrying at the same cadence keeps the measured rate
   above the limit, so the block never lifts. We pause hard instead, and slow
   the baseline down for the rest of the run.
"""

from __future__ import annotations

import atexit
import json
import os
import random
import time
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Seconds between requests. 1.5s (~0.67 req/s) leaves margin under the 1 req/s
# IP limit; override with MB_MIN_INTERVAL for a one-off faster/slower run.
BASE_INTERVAL = float(os.environ.get("MB_MIN_INTERVAL", "1.5"))

# When we do get throttled, the interval ratchets up towards this ceiling and
# decays back down again after a run of clean responses.
MAX_INTERVAL = 6.0
INTERVAL_GROWTH = 1.4
DECAY_AFTER_OK = 25          # consecutive good responses before easing off

# A 503 means "you are over the limit right now" — stop for long enough that the
# rate actually drops, rather than retrying into the same block.
COOLDOWN_SECONDS = [5.0, 15.0, 45.0]

DEFAULT_USER_AGENT = "MusicCollectionGallery/1.0 ( steve.blythe@a8c.com )"

# The pipeline spans several processes (update_all.py shells out to
# notion_covers.py once per database), and MusicBrainz measures our rate per IP,
# not per process. Without this, each new process starts with a clear throttle
# clock and fires immediately after the previous one finished — which is how a
# single genre lookup ended up 503ing on its very first request. State older
# than this is stale enough to ignore.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mb_state.json")
STATE_MAX_AGE = 300.0

# Counters for the end-of-run summary.
throttle_events = 0          # number of 503/429 responses seen
requests_made = 0

_interval = BASE_INTERVAL
_last_request_at = 0.0
_consecutive_ok = 0


def _load_state() -> None:
    """Inherit the previous process's pace, if it ran recently."""
    global _interval, _last_request_at
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return
    last = float(state.get("last_request_at", 0.0))
    if time.time() - last > STATE_MAX_AGE:
        return
    _last_request_at = last
    _interval = min(max(float(state.get("interval", BASE_INTERVAL)), BASE_INTERVAL),
                    MAX_INTERVAL)


def _save_state() -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"last_request_at": _last_request_at, "interval": _interval}, fh)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


_load_state()
atexit.register(_save_state)


def user_agent() -> str:
    """MusicBrainz wants `App/version ( contact )` so they can reach the author.

    A bare email or a library default gets lumped in with "anonymous" clients,
    which share one small global allowance. Anything that already looks like the
    documented shape is passed through untouched.
    """
    ua = os.environ.get("MB_USER_AGENT", "").strip()
    if not ua:
        return DEFAULT_USER_AGENT
    if "/" in ua and "(" in ua:
        return ua
    # Bare contact details ("me@example.com", "me@example.com/1.0") — wrap them
    # in an application name so the request identifies the app, not just a user.
    contact = ua.split("/")[0].strip()
    return f"MusicCollectionGallery/1.0 ( {contact} )"


def headers() -> Dict[str, str]:
    return {"User-Agent": user_agent(), "Accept": "application/json"}


def _make_session() -> requests.Session:
    """Retry transport hiccups only.

    Status retries are deliberately *not* configured here: urllib3 replays a
    failed request without going through our throttle (and its first retry has
    no delay at all), which is exactly how a single 503 turns into a burst.
    Rate-limit responses are handled in `get()` instead.
    """
    retry = Retry(
        total=3, connect=3, read=3, status=0,
        backoff_factor=1.0,
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _make_session()


def _throttle() -> None:
    global _last_request_at
    wait = (_last_request_at + _interval) - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.time()


def _note_throttled() -> None:
    """Slow the baseline down — being throttled means our pace is too fast."""
    global _interval, _consecutive_ok, throttle_events
    throttle_events += 1
    _consecutive_ok = 0
    _interval = min(_interval * INTERVAL_GROWTH, MAX_INTERVAL)
    _save_state()   # a later process should inherit the slower pace immediately


def _note_ok() -> None:
    global _interval, _consecutive_ok
    _consecutive_ok += 1
    if _consecutive_ok >= DECAY_AFTER_OK and _interval > BASE_INTERVAL:
        _interval = max(_interval / INTERVAL_GROWTH, BASE_INTERVAL)
        _consecutive_ok = 0


def _cooldown(attempt: int, resp: Optional[requests.Response]) -> float:
    """How long to wait after a 503/429, honouring Retry-After when sent."""
    wait = COOLDOWN_SECONDS[min(attempt, len(COOLDOWN_SECONDS) - 1)]
    if resp is not None:
        try:
            wait = max(wait, float(resp.headers.get("Retry-After", 0)))
        except (TypeError, ValueError):
            pass
    # Jitter so a retried batch doesn't re-synchronise into another burst.
    return wait + random.uniform(0, 1.0)


def get(url: str, params: Optional[Dict[str, Any]] = None,
        timeout: int = 30) -> Optional[requests.Response]:
    """Throttled GET against the MusicBrainz API.

    Returns the final response (which may still be an error), or None if the
    request never completed. 503/429 are retried after a real cooldown.
    """
    global requests_made
    resp = None
    for attempt in range(len(COOLDOWN_SECONDS) + 1):
        _throttle()
        try:
            requests_made += 1
            resp = SESSION.get(url, headers=headers(), params=params, timeout=timeout)
        except requests.RequestException:
            resp = None
            if attempt >= len(COOLDOWN_SECONDS):
                return None
            time.sleep(_cooldown(attempt, None))
            continue

        if resp.status_code in (429, 503):
            _note_throttled()
            if attempt >= len(COOLDOWN_SECONDS):
                return resp
            time.sleep(_cooldown(attempt, resp))
            continue

        _note_ok()
        return resp
    return resp


def get_json(url: str, params: Optional[Dict[str, Any]] = None,
             timeout: int = 30) -> Optional[Dict[str, Any]]:
    """As `get()`, but returns the decoded body or None on any failure."""
    resp = get(url, params, timeout)
    if resp is None or resp.status_code >= 400:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def status_line() -> str:
    """One-line summary for the end of a run."""
    if not requests_made:
        return "MusicBrainz: no requests made"
    if not throttle_events:
        return f"MusicBrainz: {requests_made} requests, no rate limiting"
    return (f"MusicBrainz: {requests_made} requests, rate limited {throttle_events}x "
            f"(interval now {_interval:.2f}s)")
