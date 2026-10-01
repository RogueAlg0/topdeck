"""Portfolios: what you own and what it is worth right now.

Lots live in a small SQLite store under the XDG data dir
(~/.local/share/topdeck), next to the watchlist store. This is user
data, not cache. No telemetry; everything stays on the machine.

Current value reuses the same price path as everything else: the
game's adapter resolves the card and `get_prices` leads with the
fresh sidecar's USD leg when a sync is recent, falling back to the
live path otherwise. The portfolio never prices anything itself.

Realized P&L on sale is out of scope on purpose; the portfolio
tracks unrealized P&L only.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

from topdeck import adapters as game_adapters
from topdeck import backbone
from topdeck.adapters.base import CardHit, Price
from topdeck.net import SourceError
from topdeck.watch import pick_tracked_price


def _default_path() -> str:
    override = os.environ.get("TOPDECK_DATA_DIR")
    if override:
        return os.path.join(override, "portfolio.sqlite")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    candidates = [
        os.path.join(base, "topdeck", "portfolio.sqlite"),
        os.path.join(os.path.expanduser("~"), ".topdeck", "portfolio.sqlite"),
    ]
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            return path
        except OSError:
            continue
    return candidates[0]


@dataclass
class Lot:
    """One purchase: quantity and what was paid per copy, in USD."""

    id: int
    game: str
    card_id: str
    join_key: int | None
    name: str
    set_name: str
    qty: int
    purchase_price: float
    added_at: float


@dataclass
class Holding:
    """One lot re-priced at current prices."""

    lot: Lot
    price: Price | None
    stale_sidecar: bool  # the game's synced data is not fresh, so the price is live
    error: str | None = None
    note: str | None = None

    @property
    def value(self) -> float | None:
        if self.price is None or self.price.price is None:
            return None
        return self.lot.qty * self.price.price

    @property
    def cost(self) -> float:
        return self.lot.qty * self.lot.purchase_price


def join_key_for(hit: CardHit) -> int | None:
    """The backbone join key for a resolved hit.

    Adapters carry it under different extra keys; the TCGCSV-backed
    games use the product id itself as the card id.
    """
    for key in ("tcgplayer_id", "product_id"):
        value = hit.extra.get(key)
        if isinstance(value, int):
            return value
    try:
        return int(hit.card_id)
    except (TypeError, ValueError):
        return None


class PortfolioStore:
    """SQLite-backed portfolio. One writer at a time; the CLI is the writer."""

    def __init__(self, path: str | None = None):
        self.path = path or _default_path()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lots ("
                "id INTEGER PRIMARY KEY, "
                "game TEXT NOT NULL, "
                "card_id TEXT NOT NULL, "
                "join_key INTEGER, "
                "name TEXT NOT NULL, "
                "set_name TEXT NOT NULL DEFAULT '', "
                "qty INTEGER NOT NULL, "
                "purchase_price REAL NOT NULL, "
                "added_at REAL NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_lot(row: sqlite3.Row) -> Lot:
        return Lot(
            id=row["id"],
            game=row["game"],
            card_id=row["card_id"],
            join_key=row["join_key"],
            name=row["name"],
            set_name=row["set_name"],
            qty=row["qty"],
            purchase_price=row["purchase_price"],
            added_at=row["added_at"],
        )

    def add(
        self,
        game: str,
        card_id: str,
        join_key: int | None,
        name: str,
        set_name: str,
        qty: int,
        purchase_price: float,
    ) -> Lot:
        """Record one purchase. Every add is a new lot, even for a card
        already held: buying more at a different price is a new lot."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO lots (game, card_id, join_key, name, set_name,"
                " qty, purchase_price, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (game, card_id, join_key, name, set_name, qty, purchase_price, time.time()),
            )
            conn.commit()
            lot_id = cur.lastrowid
        finally:
            conn.close()
        found = self.get(lot_id)
        assert found is not None
        return found

    def get(self, lot_id: int) -> Lot | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
        finally:
            conn.close()
        return self._row_to_lot(row) if row else None

    def list(self) -> list[Lot]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM lots ORDER BY id").fetchall()
        finally:
            conn.close()
        return [self._row_to_lot(r) for r in rows]

    def find_by_name(self, name: str) -> list[Lot]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM lots WHERE lower(name) = lower(?) ORDER BY id",
                (name,),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_lot(r) for r in rows]

    def remove(self, lot_id: int) -> Lot | None:
        lot = self.get(lot_id)
        if lot is None:
            return None
        conn = self._connect()
        try:
            conn.execute("DELETE FROM lots WHERE id = ?", (lot_id,))
            conn.commit()
        finally:
            conn.close()
        return lot

    def count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM lots").fetchone()
        finally:
            conn.close()
        return row["n"]


def price_holding(lot: Lot) -> Holding:
    """Re-price one lot through the game's normal price path.

    Resolution mirrors the check command: search by name, prefer the
    exact stored card id, then take the tracked (headline USD) price
    leg. Never raises; a dead source is an error on the holding.
    """
    stale_sidecar = backbone.sync_status(lot.game) != "fresh"
    adapter = game_adapters.resolve_game(lot.game)
    if adapter is None:
        return Holding(lot, None, stale_sidecar, error=f'unknown game "{lot.game}"')
    try:
        hits = adapter.search(lot.name)
    except SourceError as exc:
        return Holding(lot, None, stale_sidecar, error=str(exc))
    if not hits:
        return Holding(lot, None, stale_sidecar, error="no longer listed by the source")
    ranked = game_adapters.rank_candidates(hits, lot.name)
    hit = next((h for h in ranked if h.card_id == lot.card_id), ranked[0])
    note = None
    if hit.card_id != lot.card_id:
        note = "exact printing no longer listed; showing closest match"
    try:
        prices = adapter.get_prices(hit)
    except SourceError as exc:
        return Holding(lot, None, stale_sidecar, error=str(exc))
    tracked = pick_tracked_price(prices)
    if tracked is None:
        return Holding(lot, None, stale_sidecar, error="no prices right now", note=note)
    return Holding(lot, tracked, stale_sidecar, note=note)


@dataclass
class PortfolioSummary:
    """Totals over holdings with a current USD price.

    Holdings without a USD price are excluded from the totals and
    counted in `excluded`, so the P&L always compares like with like.
    """

    holdings: list[Holding]
    value: float
    cost: float
    pnl: float
    pnl_pct: float | None  # None when the cost basis is zero
    excluded: int
    total_lots: int
    total_qty: int


def summarize(holdings: list[Holding]) -> PortfolioSummary:
    """Add up value, cost, and unrealized P&L over priced-in-USD holdings."""
    priced = [
        holding
        for holding in holdings
        if holding.price is not None
        and holding.price.currency == "USD"
        and holding.value is not None
    ]
    value = sum(holding.value for holding in priced)
    cost = sum(holding.cost for holding in priced)
    pnl = value - cost
    pnl_pct = pnl / cost * 100 if cost else None
    return PortfolioSummary(
        holdings=holdings,
        value=value,
        cost=cost,
        pnl=pnl,
        pnl_pct=pnl_pct,
        excluded=len(holdings) - len(priced),
        total_lots=len(holdings),
        total_qty=sum(holding.lot.qty for holding in holdings),
    )
