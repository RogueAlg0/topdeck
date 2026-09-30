"""Shared TCGCSV bulk adapter for games without a live search API.

TCGCSV publishes daily TCGplayer dumps: category -> groups (sets) ->
products (cards) -> prices. We download each level once a day into the
HTTP cache and match names locally. marketPrice is preferred; midPrice
is the fallback when marketPrice is null.

When the price backbone has fresh data for this game, get_prices leads
with the local sidecar and the live rows supplement it; the join key is
the TCGCSV productId, which doubles as the card_id.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from topdeck import backbone, net, trigrams
from topdeck.adapters.base import CardHit, GameAdapter, Price, prices_from_cents, with_sidecar_price
from topdeck.progress import Progress

_PRINTING_BY_SUBTYPE = {"Normal": "normal", "Foil": "foil"}


def _trigram_hits(game_key: str, query: str) -> list[CardHit]:
    """Typo-tolerant fallback: CardHits built from the synced name index.

    The productId doubles as the card_id, so get_prices resolves these
    through the normal backbone path, freshness rule included.
    """
    return [
        CardHit(
            card_id=str(suggestion.join_key),
            name=suggestion.name,
            set_code=suggestion.set_code,
            set_name=suggestion.set_name,
            collector_number="",
            released_at="",
            url="",
        )
        for suggestion in trigrams.suggest(backbone.db_path(), game_key, query, limit=10)
    ]


# Threads for bulk catalog downloads. The per-host politeness throttle in
# net.fetch_json still paces every request; threads only overlap latency.
_CATALOG_WORKERS = 8


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

    def _catalog(self) -> tuple[list[dict], dict[int, list[dict]]]:
        """Products matching nothing are cheap: price rows are fetched only
        for groups that actually contain a match, so a query never pays
        for price data it cannot use.

        Group downloads run on threads; the per-host throttle in
        net.fetch_json still spaces every request, so this overlaps
        latency without getting pushy.
        """
        groups = self._groups()
        targets = [g for g in groups if g.get("groupId") is not None]
        progress = Progress(f"Fetching {self.display_name} catalog", len(targets))

        def _one(group: dict) -> tuple[int, list[dict]]:
            group_id = group["groupId"]
            products = net.fetch_json(
                f"https://tcgcsv.com/tcgplayer/{self.category_id}/{group_id}/products",
                ttl=self.cache_ttl,
                min_interval=self.min_interval,
                user_agent=net.BROWSER_UA,
            )
            prod_rows = products.get("results", []) if isinstance(products, dict) else []
            rows = [p for p in prod_rows if isinstance(p, dict)]
            for prod in rows:
                prod["_group"] = group
            progress.tick()
            return group_id, rows

        products_by_group: dict[int, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=_CATALOG_WORKERS) as pool:
            for group_id, rows in pool.map(_one, targets):
                products_by_group[group_id] = rows
        progress.finish()
        return groups, products_by_group

    def _prices_for(self, group_id: int) -> dict[int, list[dict]]:
        prices = net.fetch_json(
            f"https://tcgcsv.com/tcgplayer/{self.category_id}/{group_id}/prices",
            ttl=self.cache_ttl,
            min_interval=self.min_interval,
            user_agent=net.BROWSER_UA,
        )
        by_product: dict[int, list[dict]] = {}
        price_rows = prices.get("results", []) if isinstance(prices, dict) else []
        for row in price_rows:
            if isinstance(row, dict) and row.get("productId") is not None:
                by_product.setdefault(row["productId"], []).append(row)
        return by_product

    @staticmethod
    def _extended(prod: dict) -> dict:
        out: dict[str, str] = {}
        for item in prod.get("extendedData") or []:
            if isinstance(item, dict) and item.get("name"):
                out[str(item["name"])] = str(item.get("value", ""))
        return out

    def search(self, query: str) -> list[CardHit]:
        # Search always runs the live catalog path. The backbone is a
        # price sidecar, not a second catalog; get_prices decides.
        wanted = query.strip().lower()
        _, products_by_group = self._catalog()
        matched_groups: dict[int, list[dict]] = {}
        for group_id, prods in products_by_group.items():
            for prod in prods:
                name = str(prod.get("cleanName") or prod.get("name") or "")
                if wanted in name.lower():
                    matched_groups.setdefault(group_id, []).append(prod)
        hits: list[CardHit] = []
        for group_id, prods in matched_groups.items():
            price_rows = self._prices_for(group_id)
            for prod in prods:
                group = prod.get("_group") or {}
                ext = self._extended(prod)
                hits.append(
                    CardHit(
                        card_id=str(prod.get("productId", "")),
                        name=str(prod.get("cleanName") or prod.get("name") or ""),
                        set_code=str(group.get("abbreviation", "")),
                        set_name=str(group.get("name", "")),
                        collector_number=ext.get("Number", ""),
                        released_at=str(group.get("publishedOn", "") or "")[:10],
                        url=str(prod.get("url", "") or ""),
                        extra={
                            "prices": price_rows.get(prod.get("productId"), []),
                        },
                    )
                )
                if len(hits) >= 15:
                    return hits
        if not hits:
            # Substring matching found nothing: ask the synced name
            # index for typo-tolerant suggestions before giving up.
            # Exact queries never reach this branch, so exact behavior
            # is untouched.
            hits = _trigram_hits(self.game_key, query)
        return hits

    def history_key(self, hit: CardHit) -> int | None:
        """The card ID is the TCGplayer product ID, the history join key."""
        try:
            return int(hit.card_id)
        except (TypeError, ValueError):
            return None

    def get_prices(self, hit: CardHit) -> list[Price]:
        # Backbone rule: a fresh sync leads with the sidecar's USD price
        # and the live legs supplement it; a miss or stale data falls
        # back to the live rows the search already fetched. The threshold
        # lives in backbone.FRESHNESS_HOURS.
        live = self._live_prices(hit)
        try:
            join_key: int | None = int(hit.card_id)
        except (TypeError, ValueError):
            join_key = None
        row = backbone.lookup_price(self.game_key, join_key) if join_key is not None else None
        if row is None:
            prices = live
        else:
            sidecar = prices_from_cents(
                row["market_cents"],
                row["mid_cents"],
                as_of=row["as_of"],
                source="tcgcsv",
                source_url="https://tcgcsv.com",
            )
            prices = with_sidecar_price(sidecar, live)
        # The price is already fetched; filing the snapshot is free.
        backbone.record_lookup(self.game_key, join_key, prices)
        return prices

    def _live_prices(self, hit: CardHit) -> list[Price]:
        as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out: list[Price] = []
        for live in hit.extra.get("prices", []):
            raw = live.get("marketPrice")
            provenance = "market"
            if raw is None:
                # No market price: fall back to the mid price, but say so.
                raw = live.get("midPrice")
                provenance = "mid"
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            printing = _PRINTING_BY_SUBTYPE.get(str(live.get("subTypeName", "")), "normal")
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
                    provenance=provenance,
                )
            )
        return out
