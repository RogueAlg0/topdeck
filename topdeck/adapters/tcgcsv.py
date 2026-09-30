"""Shared TCGCSV bulk adapter for games without a live search API.

TCGCSV publishes daily TCGplayer dumps: category -> groups (sets) ->
products (cards) -> prices. We download each level once a day into the
HTTP cache and match names locally. marketPrice is preferred; midPrice
is the fallback when marketPrice is null.
"""

from __future__ import annotations

from datetime import datetime, timezone

from topdeck import net
from topdeck.adapters.base import CardHit, GameAdapter, Price

_PRINTING_BY_SUBTYPE = {"Normal": "normal", "Foil": "foil"}


class TcgcsvBulkAdapter(GameAdapter):
    category_id: int = 0
    min_interval = 0.3
    cache_ttl = 24 * 3600

    def _groups(self) -> list[dict]:
        url = f"https://tcgcsv.com/tcgplayer/{self.category_id}/groups"
        data = net.fetch_json(
            url,
            ttl=self.cache_ttl,
            min_interval=self.min_interval,
            user_agent=net.BROWSER_UA,
        )
        results = data.get("results", []) if isinstance(data, dict) else []
        return [g for g in results if isinstance(g, dict)]

    def _catalog(self) -> list[dict]:
        """Every product joined with its price rows, across all groups."""
        catalog: list[dict] = []
        for group in self._groups():
            group_id = group.get("groupId")
            if group_id is None:
                continue
            base = f"https://tcgcsv.com/tcgplayer/{self.category_id}/{group_id}"
            products = net.fetch_json(
                base + "/products",
                ttl=self.cache_ttl,
                min_interval=self.min_interval,
                user_agent=net.BROWSER_UA,
            )
            prices = net.fetch_json(
                base + "/prices",
                ttl=self.cache_ttl,
                min_interval=self.min_interval,
                user_agent=net.BROWSER_UA,
            )
            by_product: dict[int, list[dict]] = {}
            price_rows = prices.get("results", []) if isinstance(prices, dict) else []
            for row in price_rows:
                if isinstance(row, dict) and row.get("productId") is not None:
                    by_product.setdefault(row["productId"], []).append(row)
            prod_rows = products.get("results", []) if isinstance(products, dict) else []
            for prod in prod_rows:
                if not isinstance(prod, dict):
                    continue
                prod["_group"] = group
                prod["_prices"] = by_product.get(prod.get("productId"), [])
                catalog.append(prod)
        return catalog

    @staticmethod
    def _extended(prod: dict) -> dict:
        out: dict[str, str] = {}
        for item in prod.get("extendedData") or []:
            if isinstance(item, dict) and item.get("name"):
                out[str(item["name"])] = str(item.get("value", ""))
        return out

    def search(self, query: str) -> list[CardHit]:
        wanted = query.strip().lower()
        hits: list[CardHit] = []
        for prod in self._catalog():
            name = str(prod.get("cleanName") or prod.get("name") or "")
            if wanted not in name.lower():
                continue
            group = prod.get("_group") or {}
            ext = self._extended(prod)
            hits.append(
                CardHit(
                    card_id=str(prod.get("productId", "")),
                    name=name,
                    set_code=str(group.get("abbreviation", "")),
                    set_name=str(group.get("name", "")),
                    collector_number=ext.get("Number", ""),
                    released_at=str(group.get("publishedOn", "") or "")[:10],
                    url=str(prod.get("url", "") or ""),
                    extra={"prices": prod.get("_prices", [])},
                )
            )
            if len(hits) >= 15:
                break
        return hits

    def get_prices(self, hit: CardHit) -> list[Price]:
        as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out: list[Price] = []
        for row in hit.extra.get("prices", []):
            raw = row.get("marketPrice")
            if raw is None:
                raw = row.get("midPrice")
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            printing = _PRINTING_BY_SUBTYPE.get(str(row.get("subTypeName", "")), "normal")
            out.append(
                Price(
                    market="tcgplayer",
                    currency="USD",
                    condition="near-mint",
                    printing=printing,
                    price=value,
                    as_of=as_of,
                    source="tcgcsv",
                    source_url="https://tcgcsv.com",
                )
            )
        return out
