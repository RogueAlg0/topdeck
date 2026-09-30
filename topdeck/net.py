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

import tenacity

APP_UA = "topdeck/0.1.0 (https://github.com/RogueAlg0/topdeck)"
# TCGCSV rejects Python's default User-Agent with a 401, so it gets a
# browser-style one. Nothing sneaky, just an honest client string.
BROWSER_UA = "Mozilla/5.0 (compatible; topdeck/0.1.0; +https://github.com/RogueAlg0/topdeck)"

# Retry budget for one logical fetch: the first try plus three retries.
_MAX_ATTEMPTS = 4


class _TokenBucket:
    """Per-host rate limiter: capacity 1, refilled at `rate` tokens/sec.

    At most one request per 1/rate seconds goes out; the first request
    is immediate. Each bucket has its own lock, so threads waiting on
    one host never block threads fetching from another.
    """

    def __init__(self, rate: float) -> None:
        self.rate = rate
        self._tokens = 1.0
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(1.0, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait = (1.0 - self._tokens) / self.rate
            # Sleep holding this bucket's lock: same-host requests stay
            # strictly paced, and no other host is affected.
            time.sleep(wait)
            self._updated = time.monotonic()
            self._tokens = max(0.0, self._tokens - 1.0)


_buckets: dict[str, _TokenBucket] = {}
_buckets_lock = threading.Lock()


def _cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "topdeck", "http_cache.sqlite")


def _db() -> sqlite3.Connection | None:
    try:
        path = _cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        # Bulk fetches run on threads now; wait briefly on a locked
        # database instead of failing the whole fetch.
        conn.execute("PRAGMA busy_timeout = 5000")
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
    """Block until `host`'s token bucket has room for one request."""
    if min_interval <= 0:
        return
    rate = 1.0 / min_interval
    with _buckets_lock:
        bucket = _buckets.get(host)
        if bucket is None or bucket.rate != rate:
            bucket = _TokenBucket(rate=rate)
            _buckets[host] = bucket
    bucket.take()


class SourceError(Exception):
    """A price source failed in a way the caller should report kindly.

    `status` carries the HTTP status when the failure was an HTTP
    error, so callers can special-case codes like 404 without parsing
    the message text.
    """

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def _transient(exc: BaseException) -> bool:
    """Retry only what a retry can fix: 429, 5xx, timeouts, dead routes.

    4xx responses are answers, not outages. A 404 in particular means
    "no such thing", and retrying it would just be impolite.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


@tenacity.retry(
    retry=tenacity.retry_if_exception(_transient),
    # Exponential backoff with jitter, capped: polite against free APIs,
    # and a retry storm can never build.
    wait=tenacity.wait_exponential_jitter(initial=1, max=10),
    stop=tenacity.stop_after_attempt(_MAX_ATTEMPTS),
    reraise=True,
)
def _get(url: str, user_agent: str, timeout: float) -> str:
    """One raw GET. Raises urllib errors; tenacity retries the transient ones."""
    req = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


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
    try:
        body = _get(url, user_agent, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise SourceError(
                "the price source asked us to slow down (HTTP 429). Try again in a minute.",
                status=429,
            ) from exc
        raise SourceError(f"the price source returned HTTP {exc.code}.", status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"could not reach the price source ({exc}).") from exc
    _write_cache(url, body)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SourceError("the price source returned garbled data.") from exc
