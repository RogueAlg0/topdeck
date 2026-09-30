"""Polite HTTP for price sources: rate limiting plus a local SQLite cache.

Every source gets a descriptive User-Agent, a per-host minimum gap between
requests, and aggressive caching so live fetches happen only on demand.
No telemetry, no tracking. If the cache cannot be written, we degrade to
plain fetching instead of failing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

APP_UA = "topdeck/0.1.0 (https://github.com/RogueAlg0/topdeck)"
# TCGCSV rejects Python's default User-Agent with a 401, so it gets a
# browser-style one. Nothing sneaky, just an honest client string.
BROWSER_UA = "Mozilla/5.0 (compatible; topdeck/0.1.0; +https://github.com/RogueAlg0/topdeck)"

_last_call: dict[str, float] = {}
_lock = threading.Lock()


def _cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "topdeck", "http_cache.sqlite")


def _db() -> sqlite3.Connection | None:
    try:
        path = _cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS http_cache "
            "(url TEXT PRIMARY KEY, fetched_at REAL, body TEXT)"
        )
        return conn
    except OSError:
        return None


def _read_cache(url: str, ttl: float) -> object | None:
    """Return the cached JSON body, or None on a miss. None is not an error."""
    conn = _db()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT fetched_at, body FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    fetched_at, body = row
    if time.time() - fetched_at > ttl:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None


def _write_cache(url: str, body: str) -> None:
    conn = _db()
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO http_cache (url, fetched_at, body) VALUES (?, ?, ?)",
            (url, time.time(), body),
        )
        conn.commit()
    finally:
        conn.close()


def _polite_wait(host: str, min_interval: float) -> None:
    with _lock:
        now = time.monotonic()
        wait = min_interval - (now - _last_call.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()


class SourceError(Exception):
    """A price source failed in a way the caller should report kindly."""


def fetch_json(
    url: str,
    *,
    ttl: float = 3600,
    min_interval: float = 0.0,
    user_agent: str = APP_UA,
    timeout: float = 30,
) -> object:
    """GET a JSON document with rate limiting and SQLite caching.

    Raises SourceError with a human-readable message on failure, so the
    CLI can report it without a traceback.
    """
    host = urllib.request.urlparse(url).netloc
    cached = _read_cache(url, ttl)
    if cached is not None:
        return cached
    _polite_wait(host, min_interval)
    req = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise SourceError(
                "the price source asked us to slow down (HTTP 429). Try again in a minute."
            ) from exc
        raise SourceError(f"the price source returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"could not reach the price source ({exc}).") from exc
    _write_cache(url, body)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SourceError("the price source returned garbled data.") from exc
