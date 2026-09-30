"""Watchlists: track cards, catch the spikes.

Watches and their price history live in a small SQLite store under the
XDG data dir (~/.local/share/topdeck), separate from the HTTP cache.
No telemetry; everything stays on the machine.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

from topdeck.adapters.base import Price

#: A move counts as a spike or a drop at this percent change.
SPIKE_PCT = 10.0


def _default_path() -> str:
    override = os.environ.get("TOPDECK_DATA_DIR")
    if override:
        return os.path.join(override, "watchlist.sqlite")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    candidates = [
        os.path.join(base, "topdeck", "watchlist.sqlite"),
        os.path.join(os.path.expanduser("~"), ".topdeck", "watchlist.sqlite"),
    ]
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            return path
        except OSError:
            continue
    return candidates[0]


@dataclass
class Watch:
    id: int
    game: str
    card_id: str
    name: str
    set_name: str
    target_price: float | None
    added_at: float


@dataclass
class CheckRow:
    """One watched card after a check: prices then and now, and the verdict."""

    watch: Watch
    previous: float | None
    current: float | None
    currency: str
    source: str
    delta_abs: float | None
    delta_pct: float | None
    spike: bool
    drop: bool
    target_hit: bool
    error: str | None = None
    note: str | None = None

    @property
    def alert(self) -> bool:
        return bool(self.spike or self.drop or self.target_hit or self.error)


class WatchStore:
    """SQLite-backed watchlist. One writer at a time; the CLI is the writer."""

    def __init__(self, path: str | None = None):
        self.path = path or _default_path()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        # check runs watches on threads; wait briefly on a locked
        # database instead of failing the whole run.
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS watches ("
                "id INTEGER PRIMARY KEY, "
                "game TEXT NOT NULL, "
                "card_id TEXT NOT NULL, "
                "name TEXT NOT NULL, "
                "set_name TEXT NOT NULL DEFAULT '', "
                "target_price REAL, "
                "added_at REAL NOT NULL, "
                "UNIQUE (game, card_id))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS snapshots ("
                "id INTEGER PRIMARY KEY, "
                "watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE, "
                "checked_at REAL NOT NULL, "
                "price REAL, "
                "currency TEXT NOT NULL DEFAULT 'USD', "
                "source TEXT NOT NULL DEFAULT '')"
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_watch(row: sqlite3.Row) -> Watch:
        return Watch(
            id=row["id"],
            game=row["game"],
            card_id=row["card_id"],
            name=row["name"],
            set_name=row["set_name"],
            target_price=row["target_price"],
            added_at=row["added_at"],
        )

    def add(
        self,
        game: str,
        card_id: str,
        name: str,
        set_name: str,
        target_price: float | None = None,
    ) -> Watch:
        """Add a watch. Raises sqlite3.IntegrityError if it is already watched."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO watches (game, card_id, name, set_name, target_price, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (game, card_id, name, set_name, target_price, time.time()),
            )
            conn.commit()
            watch_id = cur.lastrowid
        finally:
            conn.close()
        found = self.get(watch_id)
        assert found is not None
        return found

    def get(self, watch_id: int) -> Watch | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        finally:
            conn.close()
        return self._row_to_watch(row) if row else None

    def find(self, game: str, card_id: str) -> Watch | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM watches WHERE game = ? AND card_id = ?",
                (game, card_id),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_watch(row) if row else None

    def list(self) -> list[Watch]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM watches ORDER BY id").fetchall()
        finally:
            conn.close()
        return [self._row_to_watch(r) for r in rows]

    def find_by_name(self, name: str) -> list[Watch]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM watches WHERE lower(name) = lower(?) ORDER BY id",
                (name,),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_watch(r) for r in rows]

    def set_target(self, watch_id: int, target_price: float | None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE watches SET target_price = ? WHERE id = ?",
                (target_price, watch_id),
            )
            conn.commit()
        finally:
            conn.close()

    def remove(self, watch_id: int) -> Watch | None:
        watch = self.get(watch_id)
        if watch is None:
            return None
        conn = self._connect()
        try:
            conn.execute("DELETE FROM snapshots WHERE watch_id = ?", (watch_id,))
            conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
            conn.commit()
        finally:
            conn.close()
        return watch

    def record_snapshot(
        self,
        watch_id: int,
        price: float | None,
        currency: str,
        source: str,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO snapshots (watch_id, checked_at, price, currency, source)"
                " VALUES (?, ?, ?, ?, ?)",
                (watch_id, time.time(), price, currency, source),
            )
            conn.commit()
        finally:
            conn.close()

    def previous_price(self, watch_id: int) -> float | None:
        """The most recent non-null checked price, or None if never priced."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT price FROM snapshots"
                " WHERE watch_id = ? AND price IS NOT NULL"
                " ORDER BY checked_at DESC LIMIT 1",
                (watch_id,),
            ).fetchone()
        finally:
            conn.close()
        return row["price"] if row else None

    def count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM watches").fetchone()
        finally:
            conn.close()
        return row["n"]


def pick_tracked_price(prices: list[Price]) -> Price | None:
    """The one price a watch tracks.

    Deterministic, so checks compare like for like: USD first, then
    tcgplayer, then the normal printing.
    """
    candidates = [p for p in prices if p.price is not None]
    if not candidates:
        return None

    def key(p: Price) -> tuple[int, int, int, str, str]:
        return (
            0 if p.currency == "USD" else 1,
            0 if p.market == "tcgplayer" else 1,
            0 if p.printing == "normal" else 1,
            p.market,
            p.printing,
        )

    return sorted(candidates, key=key)[0]


def describe_move(
    previous: float | None, current: float | None
) -> tuple[float | None, float | None, bool, bool]:
    """Absolute delta, percent delta, spike, drop. Nones when unmeasurable."""
    if previous is None or current is None:
        return None, None, False, False
    delta_abs = current - previous
    if previous == 0:
        delta_pct = 0.0 if current == 0 else None
    else:
        delta_pct = delta_abs / previous * 100
    # Round before comparing: float noise must not decide a 10% boundary.
    delta_abs = round(delta_abs, 4) if delta_abs is not None else None
    delta_pct = round(delta_pct, 4) if delta_pct is not None else None
    spike = delta_pct is not None and delta_pct >= SPIKE_PCT
    drop = delta_pct is not None and delta_pct <= -SPIKE_PCT
    return delta_abs, delta_pct, spike, drop


def build_row(
    watch: Watch,
    previous: float | None,
    current: float | None,
    currency: str,
    source: str,
    error: str | None = None,
    note: str | None = None,
) -> CheckRow:
    delta_abs, delta_pct, spike, drop = describe_move(previous, current)
    target_hit = (
        watch.target_price is not None and current is not None and current <= watch.target_price
    )
    return CheckRow(
        watch=watch,
        previous=previous,
        current=current,
        currency=currency,
        source=source,
        delta_abs=delta_abs,
        delta_pct=delta_pct,
        spike=spike,
        drop=drop,
        target_hit=target_hit,
        error=error,
        note=note,
    )
