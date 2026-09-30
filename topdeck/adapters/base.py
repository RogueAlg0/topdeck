"""Game adapters: one small class per game behind a shared interface.

Every adapter answers the same two questions: which cards match this
query, and what do they cost right now. Prices follow the five-question
rule: market, currency, condition, printing, and as-of travel with every
number. No bare prices, ever.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CardHit:
    """One card that matched a search query."""

    card_id: str
    name: str
    set_code: str
    set_name: str
    collector_number: str
    finish: str = "normal"
    released_at: str = ""  # YYYY-MM-DD when known, else ""
    url: str = ""  # card page on the source site, for hyperlinks
    extra: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Price:
    """One observed price. The five questions are fields, not footnotes."""

    market: str  # e.g. tcgplayer, cardmarket
    currency: str  # ISO code, e.g. USD
    condition: str  # e.g. near-mint
    printing: str  # e.g. normal, foil, etched
    price: float | None
    as_of: str  # ISO 8601 timestamp
    source: str  # adapter's source name, e.g. scryfall
    source_url: str = ""
    provenance: str = "market"  # "market", or "mid" when the number is a fallback


class GameAdapter:
    """Interface every game implements. Keep it small on purpose."""

    game_key: str = ""
    display_name: str = ""
    trust_tier: str = "solid"  # solid | beta | experimental
    source_name: str = ""
    min_interval: float = 0.5  # seconds between requests to this source
    cache_ttl: float = 12 * 3600  # how long HTTP responses stay cached

    def search(self, query: str) -> list[CardHit]:
        """Return cards matching the query, best guesses first."""
        raise NotImplementedError

    def get_prices(self, hit: CardHit) -> list[Price]:
        """Return current prices for one resolved card."""
        raise NotImplementedError

    def history_key(self, hit: CardHit) -> int | None:
        """The integer join key into price history, or None when this
        adapter files none for the hit."""
        return None


def prices_from_cents(
    market_cents: int | None,
    mid_cents: int | None,
    *,
    as_of: str,
    source: str,
    source_url: str = "",
) -> list[Price]:
    """Build Price rows from backbone integer cents.

    Market cents win; mid cents are the labeled fallback. Both missing
    means no price at all, never a zero.
    """
    raw = market_cents
    provenance = "market"
    if raw is None:
        raw = mid_cents
        provenance = "mid"
    if raw is None:
        return []
    return [
        Price(
            market="tcgplayer",
            currency="USD",
            condition="near-mint",
            printing="normal",
            price=raw / 100,
            as_of=as_of,
            source=source,
            source_url=source_url,
            provenance=provenance,
        )
    ]


def with_sidecar_price(sidecar: list[Price], live: list[Price]) -> list[Price]:
    """Supplement live price legs with the backbone sidecar's USD row.

    The sidecar stores the headline (tcgplayer, USD, normal) price, so it
    goes first and the live leg it supersedes is dropped. Every other leg
    (EUR, foils) is kept as-is, with its own as_of untouched.
    """
    if not sidecar:
        return live
    return sidecar + [
        leg
        for leg in live
        if (leg.market, leg.currency, leg.printing) != ("tcgplayer", "USD", "normal")
    ]


def _recency_key(released_at: str) -> float:
    """Most recent first. Unknown dates sort last, never crash."""
    try:
        year, month, day = (int(p) for p in released_at.split("-")[:3])
        return -(year * 10000 + month * 100 + day)
    except (ValueError, AttributeError):
        return 0.0


def rank_candidates(hits: list[CardHit], query: str) -> list[CardHit]:
    """Rank matches: exact name first, then most recent set.

    Exact means the whole name matches case-insensitively. Recency uses
    the set release date when the source provides one.
    """
    wanted = query.strip().lower()

    def key(hit: CardHit) -> tuple[int, float]:
        exact = 0 if hit.name.strip().lower() == wanted else 1
        return (exact, _recency_key(hit.released_at))

    return sorted(hits, key=key)
