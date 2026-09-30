"""Pokemon adapter, powered by TCGdex v2 (REST).

Search returns slim records, so we fetch details for the top handful of
name matches. TCGdex can be slow, so responses are cached and detail
fetches are capped.
"""

from __future__ import annotations

import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from topdeck import net
from topdeck.adapters.base import CardHit, GameAdapter, Price, rank_candidates
from topdeck.progress import Progress

_DETAIL_CAP = 12


class TcgdexAdapter(GameAdapter):
    game_key = "pokemon"
    display_name = "Pokemon"
    trust_tier = "solid"
    source_name = "tcgdex"
    min_interval = 1.0
    cache_ttl = 6 * 3600

    def _detail(self, card_id: str) -> dict:
        url = f"https://api.tcgdex.net/v2/en/cards/{card_id}"
        data = net.fetch_json(url, ttl=self.cache_ttl, min_interval=self.min_interval)
        return data if isinstance(data, dict) else {}

    def search(self, query: str) -> list[CardHit]:
        url = "https://api.tcgdex.net/v2/en/cards?name=" + urllib.parse.quote(query)
        data = net.fetch_json(url, ttl=self.cache_ttl, min_interval=self.min_interval)
        items = data if isinstance(data, list) else []
        prelim = [
            CardHit(
                card_id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                set_code="",
                set_name="",
                collector_number=str(item.get("localId", "")),
            )
            for item in items
            if item.get("id")
        ]
        hits: list[CardHit] = []
        # Detail fetches run on threads; the per-host throttle in
        # net.fetch_json still spaces every request, so this overlaps
        # TCGdex's latency without getting pushy. Results stay in
        # ranked order because pool.map preserves input order.
        shortlist = rank_candidates(prelim, query)[:_DETAIL_CAP]
        progress = Progress("Fetching card details", len(shortlist))

        def _one(cand: CardHit) -> tuple[CardHit, dict]:
            detail = self._detail(cand.card_id)
            progress.tick()
            return cand, detail

        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(_one, shortlist))
        progress.finish()
        for cand, detail in resolved:
            if not detail:
                continue
            set_info = detail.get("set") or {}
            pricing = detail.get("pricing") or {}
            tcgplayer = (pricing.get("tcgplayer") or {}).get("normal") or {}
            product_id = tcgplayer.get("productId")
            has_prices = bool(
                (pricing.get("cardmarket") or {}).get("avg") or tcgplayer.get("marketPrice")
            )
            hits.append(
                CardHit(
                    card_id=cand.card_id,
                    name=str(detail.get("name", cand.name)),
                    set_code=str(set_info.get("id", "")),
                    set_name=str(set_info.get("name", "")),
                    collector_number=str(detail.get("localId", cand.collector_number)),
                    released_at=str(set_info.get("releaseDate", "") or ""),
                    url=f"https://www.tcgplayer.com/product/{product_id}" if product_id else "",
                    extra={"pricing": pricing, "has_prices": has_prices},
                )
            )
        # Promo sets often have no releaseDate, so recency cannot rank them.
        # Among otherwise tied matches, prefer a card we can actually price.
        # The shared ranker is stable, so this order survives as the tiebreak.
        hits.sort(key=lambda h: 0 if h.extra.get("has_prices") else 1)
        return hits

    def get_prices(self, hit: CardHit) -> list[Price]:
        pricing = hit.extra.get("pricing", {})
        out: list[Price] = []
        cardmarket = pricing.get("cardmarket") or {}
        as_of_cm = str(cardmarket.get("updated", ""))
        for field, printing in (("avg", "normal"), ("avg-holo", "holo")):
            raw = cardmarket.get(field)
            if raw is None:
                continue
            out.append(
                Price(
                    market="cardmarket",
                    currency="EUR",
                    condition="near-mint",
                    printing=printing,
                    price=float(raw),
                    as_of=as_of_cm,
                    source="tcgdex",
                    source_url="https://tcgdex.net",
                )
            )
        tcgplayer = pricing.get("tcgplayer") or {}
        as_of_tcg = str(tcgplayer.get("updated", ""))
        for field, printing in (("normal", "normal"), ("reverse-holofoil", "reverse-holo")):
            variant = tcgplayer.get(field) or {}
            raw = variant.get("marketPrice")
            if raw is None:
                continue
            out.append(
                Price(
                    market="tcgplayer",
                    currency="USD",
                    condition="near-mint",
                    printing=printing,
                    price=float(raw),
                    as_of=as_of_tcg,
                    source="tcgdex",
                    source_url="https://tcgdex.net",
                )
            )
        return out
