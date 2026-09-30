"""topdeck doctor: an honest health report for every price source.

Each source gets one small live request with a short timeout, so a slow
or dead source cannot stall the whole run. A source that fails is
reported as down, never raised. Local state (HTTP cache, watchlist
store) is reported alongside.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from topdeck import backbone, net
from topdeck.watch import WatchStore

_TIMEOUT = 5.0
_SLOW_AFTER = 2.0
_UA = net.APP_UA

# One probe per price source. tcgcsv serves two games, so one probe
# covers both; the per-game rows share its result.
_PROBES: tuple[tuple[str, str, str, str], ...] = (
    (
        "mtg",
        "Magic: The Gathering",
        "scryfall",
        "https://api.scryfall.com/cards/named?exact=Lightning+Bolt",
    ),
    ("pokemon", "Pokemon", "tcgdex", "https://api.tcgdex.net/v2/en/cards?name=pikachu"),
    ("lorcana", "Disney Lorcana", "lorcast", "https://api.lorcast.com/v0/sets"),
    ("onepiece", "One Piece Card Game", "tcgcsv", "https://tcgcsv.com/tcgplayer/68/groups"),
    ("riftbound", "Riftbound", "tcgcsv", "https://tcgcsv.com/tcgplayer/89/groups"),
)


@dataclass
class SourceHealth:
    game: str
    display_name: str
    source: str
    status: str  # ok | slow | down
    latency_ms: int | None
    detail: str


def _probe(url: str, user_agent: str = _UA) -> tuple[bool, int | None, str]:
    """One live GET. Returns (healthy, latency_ms, detail). Never raises."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            resp.read(4096)
    except urllib.error.HTTPError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return False, elapsed_ms, f"HTTP {exc.code}"
    except Exception as exc:  # timeouts, DNS, refused connections
        elapsed_ms = int((time.monotonic() - started) * 1000)
        reason = "timed out" if "timed out" in str(exc).lower() else "unreachable"
        return False, elapsed_ms, reason
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return True, elapsed_ms, "responding"


def _check_one_probe(
    probe: tuple[str, str, str, str],
) -> SourceHealth:
    """Probe one source. Never raises; a dead source is a row, not an error."""
    game, display_name, source, url = probe
    user_agent = net.BROWSER_UA if "tcgcsv.com" in url else _UA
    healthy, latency_ms, detail = _probe(url, user_agent)
    if not healthy:
        status = "down"
    elif latency_ms is not None and latency_ms >= _SLOW_AFTER * 1000:
        status = "slow"
    else:
        status = "ok"
    return SourceHealth(
        game=game,
        display_name=display_name,
        source=source,
        status=status,
        latency_ms=latency_ms,
        detail=detail,
    )


def check_sources() -> list[SourceHealth]:
    """Probe every game's price source. Slow or dead sources are rows, not errors.

    Probes run on threads so one slow source cannot stall the rest.
    pool.map preserves _PROBES order, so the report reads the same
    every time.
    """
    with ThreadPoolExecutor(max_workers=len(_PROBES)) as pool:
        return list(pool.map(_check_one_probe, _PROBES))


def _cache_summary() -> str:
    path = net._cache_path()
    if not os.path.exists(path):
        return "no cache yet"
    try:
        size_kb = os.path.getsize(path) // 1024
        conn = net._db()
        if conn is None:
            return f"{size_kb} KB on disk"
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM http_cache").fetchone()[0]
        finally:
            conn.close()
        return f"{count} entries, {size_kb} KB on disk"
    except OSError:
        return "unreadable"


def _watch_summary() -> str:
    try:
        store = WatchStore()
    except OSError:
        return "unreadable"
    try:
        n = store.count()
    except Exception:
        return "unreadable"
    noun = "card" if n == 1 else "cards"
    return f"{n} {noun} watched"


def _backbone_summary() -> str:
    """Compact per-game sync state: counts plus any games needing a sync."""
    states = [(game, backbone.sync_status(game)) for game in backbone.GAMES]
    fresh = sum(1 for _, status in states if status == "fresh")
    problems = [
        f"{game}: {'never synced' if status == 'never' else status}"
        for game, status in states
        if status != "fresh"
    ]
    summary = f"{fresh}/{len(states)} games fresh"
    if problems:
        shown = "; ".join(problems[:5])
        if len(problems) > 5:
            shown += f"; +{len(problems) - 5} more"
        summary += f" ({shown}). Run `topdeck sync` to refresh prices."
    return summary


def local_checks() -> list[tuple[str, str]]:
    """(label, detail) rows for on-machine state."""
    return [
        ("HTTP cache", _cache_summary()),
        ("Watchlist", _watch_summary()),
        ("TCGCSV sync", _backbone_summary()),
    ]
