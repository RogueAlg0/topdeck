"""Magic: The Gathering adapter, powered by Scryfall.

Scryfall asks for an identifying User-Agent and a gentle pace: 500ms
between search calls, 30s backoff on 429. We honor both.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone

from topdeck import net
from topdeck.adapters.base import CardHit, GameAdapter, Price

# (scryfall field, market, currency, printing)
_PRICE_FIELDS = (
    ("usd", "tcgplayer", "USD", "normal"),
    ("usd_foil", "tcgplayer", "USD", "foil"),
    ("usd_etched", "tcgplayer", "USD", "etched"),
    ("eur", "cardmarket", "EUR", "normal"),
    ("eur_foil", "cardmarket", "EUR", "foil"),
)


class ScryfallAdapter(GameAdapter):
    game_key = "mtg"
    display_name = "Magic: The Gathering"
    trust_tier = "solid"
    source_name = "scryfall"
    min_interval = 0.5
    cache_ttl = 12 * 3600

    def search(self, query: str) -> list[CardHit]:
        url = "https://api.scryfall.com/cards/search?q=" + urllib.parse.quote(query)
        try:
            data = net.fetch_json(url, ttl=self.cache_ttl, min_interval=self.min_interval)
        except net.SourceError as exc:
            if exc.status == 404:
                # Scryfall answers "no such card" with a 404, not an
                # empty list. That is a no-match, not an outage.
                return []
            raise
        hits: list[CardHit] = []
        cards = data.get("data", []) if isinstance(data, dict) else []
        for card in cards[:15]:
            prices = card.get("prices") or {}
            finishes = card.get("finishes") or ["normal"]
            hits.append(
                CardHit(
                    card_id=card.get("id", ""),
                    name=card.get("name", ""),
                    set_code=card.get("set", ""),
                    set_name=card.get("set_name", ""),
                    collector_number=str(card.get("collector_number", "")),
                    finish=finishes[0],
                    released_at=card.get("released_at", "") or "",
                    url=card.get("scryfall_uri", "") or "",
                    extra={"prices": prices},
                )
            )
        return hits

    def get_prices(self, hit: CardHit) -> list[Price]:
        prices = hit.extra.get("prices", {})
        # Scryfall refreshes prices roughly twice a day, so as-of is the
        # moment we fetched, not a live quote.
        as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out: list[Price] = []
        for field, market, currency, printing in _PRICE_FIELDS:
            raw = prices.get(field)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            out.append(
                Price(
                    market=market,
                    currency=currency,
                    condition="near-mint",
                    printing=printing,
                    price=value,
                    as_of=as_of,
                    source="scryfall",
                    source_url="https://scryfall.com",
                )
            )
        return out
