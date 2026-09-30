"""Disney Lorcana adapter, powered by Lorcast.

Lorcast has no search endpoint, so we pull the per-set card lists (cached
for a day) and match names locally. Lorcast asks only for a small courtesy
delay between requests. Foil price granularity is unverified on this
source, so only the standard price is reported.
"""

from __future__ import annotations

from datetime import datetime, timezone

from topdeck import net
from topdeck.adapters.base import CardHit, GameAdapter, Price


class LorcastAdapter(GameAdapter):
    game_key = "lorcana"
    display_name = "Disney Lorcana"
    trust_tier = "beta"
    source_name = "lorcast"
    min_interval = 0.15
    cache_ttl = 24 * 3600

    def _all_cards(self) -> list[dict]:
        payload = net.fetch_json(
            "https://api.lorcast.com/v0/sets",
            ttl=self.cache_ttl,
            min_interval=self.min_interval,
        )
        sets = payload.get("results", []) if isinstance(payload, dict) else []
        cards: list[dict] = []
        for s in sets:
            code = s.get("code")
            if not code:
                continue
            batch = net.fetch_json(
                f"https://api.lorcast.com/v0/sets/{code}/cards",
                ttl=self.cache_ttl,
                min_interval=self.min_interval,
            )
            items = batch if isinstance(batch, list) else []
            for card in items:
                card["_set"] = s
                cards.append(card)
        return cards

    def search(self, query: str) -> list[CardHit]:
        wanted = query.strip().lower()
        hits: list[CardHit] = []
        for card in self._all_cards():
            name = str(card.get("name", ""))
            version = str(card.get("version", "") or "")
            display = f"{name} - {version}" if version else name
            if wanted not in display.lower():
                continue
            set_info = card.get("_set") or {}
            prices = card.get("prices") or {}
            hits.append(
                CardHit(
                    card_id=str(card.get("id", "")),
                    name=display,
                    set_code=str(set_info.get("code", "")),
                    set_name=str(set_info.get("name", "")),
                    collector_number=str(card.get("collector_number", "")),
                    released_at=str(card.get("released_at", "") or ""),
                    extra={"usd": prices.get("usd")},
                )
            )
            if len(hits) >= 15:
                break
        return hits

    def get_prices(self, hit: CardHit) -> list[Price]:
        raw = hit.extra.get("usd")
        if raw is None:
            return []
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return []
        as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return [
            Price(
                market="tcgplayer",
                currency="USD",
                condition="near-mint",
                printing="normal",
                price=value,
                as_of=as_of,
                source="lorcast",
                source_url="https://lorcast.com",
            )
        ]
