"""Adapter tests with mocked HTTP. No real network calls here."""

import pytest

import topdeck.net
from topdeck.adapters import (
    GAME_ALIASES,
    REGISTRY,
    resolve_game,
)


@pytest.fixture
def no_net(monkeypatch):
    """Every test here decides exactly what the network returns."""
    calls = []

    def fake(url, **kwargs):
        calls.append(url)
        handler = fake.routes.get(url)
        assert handler is not None, f"unexpected URL: {url}"
        return handler()

    fake.routes = {}
    fake.calls = calls
    monkeypatch.setattr(topdeck.net, "fetch_json", fake)
    return fake


def test_registry_covers_backbone_roster_with_trust_tiers():
    from topdeck import backbone

    assert set(REGISTRY) == set(backbone.GAMES)
    assert REGISTRY["mtg"].trust_tier == "solid"
    assert REGISTRY["pokemon"].trust_tier == "solid"
    assert REGISTRY["lorcana"].trust_tier == "beta"
    assert REGISTRY["onepiece"].trust_tier == "solid"
    assert REGISTRY["riftbound"].trust_tier == "experimental"
    dedicated = {"mtg", "pokemon", "lorcana", "onepiece", "riftbound"}
    for key in set(REGISTRY) - dedicated:
        assert REGISTRY[key].trust_tier == "beta"
        assert REGISTRY[key].source_name == "tcgcsv"


def test_resolve_game_aliases_and_unknown():
    assert resolve_game("mtg").game_key == "mtg"
    assert resolve_game("MTG").game_key == "mtg"
    assert resolve_game("magic").game_key == "mtg"
    assert GAME_ALIASES["magic"] == "mtg"
    assert resolve_game("ygo").game_key == "yugioh"
    assert resolve_game("atlantis") is None


def test_scryfall_search_and_prices(no_net):
    no_net.routes["https://api.scryfall.com/cards/search?q=Bolt"] = lambda: {
        "data": [
            {
                "id": "abc",
                "name": "Lightning Bolt",
                "set": "lea",
                "set_name": "Limited Edition Alpha",
                "collector_number": "161",
                "finishes": ["normal"],
                "released_at": "1993-08-05",
                "scryfall_uri": "https://scryfall.com/card/lea/161/lightning-bolt",
                "prices": {
                    "usd": "12.50",
                    "usd_foil": None,
                    "usd_etched": None,
                    "eur": "9.99",
                    "eur_foil": None,
                },
            }
        ]
    }
    adapter = REGISTRY["mtg"]
    hits = adapter.search("Bolt")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.name == "Lightning Bolt"
    assert hit.set_code == "lea"
    assert hit.collector_number == "161"
    assert hit.url.startswith("https://scryfall.com/card/")
    prices = adapter.get_prices(hit)
    by_printing = {(p.market, p.printing): p for p in prices}
    assert by_printing[("tcgplayer", "normal")].price == 12.50
    assert by_printing[("tcgplayer", "normal")].currency == "USD"
    assert by_printing[("cardmarket", "normal")].price == 9.99
    assert by_printing[("cardmarket", "normal")].currency == "EUR"
    assert len(prices) == 2  # nulls are skipped, never emitted
    for p in prices:
        assert p.condition and p.as_of and p.source == "scryfall"


def test_tcgdex_search_and_prices(no_net):
    no_net.routes["https://api.tcgdex.net/v2/en/cards?name=Pikachu"] = lambda: [
        {"id": "swsh3-44", "localId": "44", "name": "Pikachu"},
        {"id": "base1-58", "localId": "58", "name": "Pikachu"},
    ]

    def detail_swsh():
        return {
            "id": "swsh3-44",
            "name": "Pikachu",
            "localId": "44",
            "set": {
                "id": "swsh3",
                "name": "Darkness Ablaze",
                "releaseDate": "2020-08-14",
            },
            "pricing": {
                "cardmarket": {
                    "updated": "2026-09-29T22:54:32.921Z",
                    "avg": 0.22,
                    "avg-holo": 0.58,
                },
                "tcgplayer": {
                    "updated": "2026-09-29T22:55:01.985Z",
                    "normal": {"productId": 219345, "marketPrice": 0.27},
                    "reverse-holofoil": {"productId": 219346, "marketPrice": 0.44},
                },
            },
        }

    def detail_base():
        return {
            "id": "base1-58",
            "name": "Pikachu",
            "localId": "58",
            "set": {"id": "base1", "name": "Base Set", "releaseDate": "1999-01-09"},
            "pricing": {},
        }

    no_net.routes["https://api.tcgdex.net/v2/en/cards/swsh3-44"] = detail_swsh
    no_net.routes["https://api.tcgdex.net/v2/en/cards/base1-58"] = detail_base
    adapter = REGISTRY["pokemon"]
    hits = adapter.search("Pikachu")
    assert len(hits) == 2
    assert hits[0].set_name == "Darkness Ablaze"
    assert hits[0].url == "https://www.tcgplayer.com/product/219345"
    prices = adapter.get_prices(hits[0])
    by_key = {(p.market, p.printing): p for p in prices}
    assert by_key[("cardmarket", "normal")].price == 0.22
    assert by_key[("cardmarket", "holo")].price == 0.58
    assert by_key[("tcgplayer", "normal")].price == 0.27
    assert by_key[("tcgplayer", "reverse-holo")].price == 0.44
    assert by_key[("cardmarket", "normal")].as_of == "2026-09-29T22:54:32.921Z"
    assert by_key[("tcgplayer", "normal")].as_of == "2026-09-29T22:55:01.985Z"
    # empty pricing block means no prices, not a crash
    assert adapter.get_prices(hits[1]) == []


def test_tcgdex_prefers_priced_card_when_dates_missing(no_net):
    """Promo sets often lack releaseDate; the priced card should rank first."""
    no_net.routes["https://api.tcgdex.net/v2/en/cards?name=Pika"] = lambda: [
        {"id": "a", "localId": "1", "name": "Pika"},
        {"id": "b", "localId": "2", "name": "Pika"},
    ]
    no_net.routes["https://api.tcgdex.net/v2/en/cards/a"] = lambda: {
        "id": "a",
        "name": "Pika",
        "localId": "1",
        "set": {"id": "s1", "name": "Old Promo", "releaseDate": None},
        "pricing": {},
    }
    no_net.routes["https://api.tcgdex.net/v2/en/cards/b"] = lambda: {
        "id": "b",
        "name": "Pika",
        "localId": "2",
        "set": {"id": "s2", "name": "New Promo", "releaseDate": None},
        "pricing": {"cardmarket": {"avg": 1.0, "updated": "2026-09-30T00:00:00Z"}},
    }
    from topdeck.adapters import rank_candidates

    hits = rank_candidates(REGISTRY["pokemon"].search("Pika"), "Pika")
    assert hits[0].card_id == "b"


def test_lorcast_search_and_prices(no_net):
    no_net.routes["https://api.lorcast.com/v0/sets"] = lambda: {
        "results": [{"code": "TFC", "name": "The First Chapter"}]
    }
    no_net.routes["https://api.lorcast.com/v0/sets/TFC/cards"] = lambda: [
        {
            "id": "crd_1",
            "name": "Mickey Mouse",
            "version": "Brave Little Tailor",
            "collector_number": 1,
            "released_at": "2023-08-18",
            "prices": {"usd": "1142.8"},
        },
        {
            "id": "crd_2",
            "name": "Elsa",
            "version": "",
            "collector_number": 2,
            "released_at": "2023-08-18",
            "prices": {"usd": None},
        },
    ]
    adapter = REGISTRY["lorcana"]
    hits = adapter.search("mickey")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.name == "Mickey Mouse - Brave Little Tailor"
    assert hit.set_code == "TFC"
    assert hit.collector_number == "1"
    prices = adapter.get_prices(hit)
    assert len(prices) == 1
    assert prices[0].price == 1142.8
    assert prices[0].currency == "USD"
    assert prices[0].source == "lorcast"


def test_tcgcsv_bulk_search_and_prices(no_net):
    cat = 89
    no_net.routes[f"https://tcgcsv.com/tcgplayer/{cat}/groups"] = lambda: {
        "results": [
            {
                "groupId": 1,
                "name": "Origins",
                "abbreviation": "ORI",
                "publishedOn": "2025-06-01T00:00:00",
            }
        ]
    }
    no_net.routes[f"https://tcgcsv.com/tcgplayer/{cat}/1/products"] = lambda: {
        "results": [
            {
                "productId": 100,
                "name": "Ahri",
                "cleanName": "Ahri",
                "url": "https://www.tcgplayer.com/product/100/x",
                "extendedData": [{"name": "Number", "value": "001"}],
            }
        ]
    }
    no_net.routes[f"https://tcgcsv.com/tcgplayer/{cat}/1/prices"] = lambda: {
        "results": [
            {
                "productId": 100,
                "lowPrice": 1.0,
                "midPrice": 2.0,
                "marketPrice": 1.75,
                "subTypeName": "Normal",
            },
            {
                "productId": 100,
                "lowPrice": 3.0,
                "midPrice": None,
                "marketPrice": None,
                "subTypeName": "Foil",
            },
            {
                "productId": 100,
                "lowPrice": 9.0,
                "midPrice": 10.0,
                "marketPrice": None,
                "subTypeName": "Foil",
            },
        ]
    }
    adapter = REGISTRY["riftbound"]
    hits = adapter.search("ahri")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.set_code == "ORI"
    assert hit.collector_number == "001"
    assert hit.released_at == "2025-06-01"
    assert hit.url == "https://www.tcgplayer.com/product/100/x"
    prices = adapter.get_prices(hit)
    by_printing = {p.printing: p for p in prices}
    # marketPrice preferred ...
    assert by_printing["normal"].price == 1.75
    assert by_printing["normal"].provenance == "market"
    # ... midPrice fallback when marketPrice is null
    assert by_printing["foil"].price == 10.0
    assert by_printing["foil"].provenance == "mid"
    for p in prices:
        assert p.market == "tcgplayer"
        assert p.currency == "USD"
        assert p.source == "tcgcsv"


def test_tcgcsv_categories():
    assert REGISTRY["onepiece"].category_id == 68
    assert REGISTRY["riftbound"].category_id == 89


# ---------------------------------------------------------------------------
# Edge cases: skips, caps, and unparseable data


def test_base_adapter_methods_raise_not_implemented():
    from topdeck.adapters.base import CardHit, GameAdapter

    adapter = GameAdapter()
    with pytest.raises(NotImplementedError):
        adapter.search("bolt")
    with pytest.raises(NotImplementedError):
        adapter.get_prices(
            CardHit(card_id="c", name="n", set_code="s", set_name="s", collector_number="1")
        )


def test_lorcast_skips_sets_without_code(no_net):
    no_net.routes["https://api.lorcast.com/v0/sets"] = lambda: {
        "results": [{"name": "No code"}, {"code": "TFC", "name": "The First Chapter"}]
    }
    no_net.routes["https://api.lorcast.com/v0/sets/TFC/cards"] = lambda: [
        {
            "id": "c1",
            "name": "Mickey",
            "version": "",
            "collector_number": 1,
            "released_at": "2023-08-18",
            "prices": {"usd": "2.5"},
        }
    ]
    hits = REGISTRY["lorcana"].search("mickey")
    assert len(hits) == 1
    assert hits[0].set_code == "TFC"


def test_lorcast_search_caps_at_fifteen(no_net):
    no_net.routes["https://api.lorcast.com/v0/sets"] = lambda: {
        "results": [{"code": "TFC", "name": "The First Chapter"}]
    }
    no_net.routes["https://api.lorcast.com/v0/sets/TFC/cards"] = lambda: [
        {
            "id": f"c{i}",
            "name": "Mickey Clone",
            "version": "",
            "collector_number": i,
            "released_at": "2023-08-18",
            "prices": {"usd": "1.0"},
        }
        for i in range(20)
    ]
    assert len(REGISTRY["lorcana"].search("mickey")) == 15


def test_lorcast_unparseable_price_returns_empty():
    from topdeck.adapters.base import CardHit

    hit = CardHit(
        card_id="c",
        name="M",
        set_code="T",
        set_name="T",
        collector_number="1",
        extra={"usd": "priceless"},
    )
    assert REGISTRY["lorcana"].get_prices(hit) == []


def test_scryfall_skips_unparseable_price_field():
    from topdeck.adapters.base import CardHit

    hit = CardHit(
        card_id="c",
        name="M",
        set_code="T",
        set_name="T",
        collector_number="1",
        extra={"prices": {"usd": "N/A", "usd_foil": "3.50"}},
    )
    prices = REGISTRY["mtg"].get_prices(hit)
    assert len(prices) == 1
    assert prices[0].printing == "foil"
    assert prices[0].price == 3.5


def test_tcgdex_skips_cards_without_detail(no_net):
    no_net.routes["https://api.tcgdex.net/v2/en/cards?name=pikachu"] = lambda: [
        {"id": "sv1-1", "name": "Pikachu", "localId": "1"},
        {"id": "sv1-2", "name": "Pikachu", "localId": "2"},
    ]
    no_net.routes["https://api.tcgdex.net/v2/en/cards/sv1-1"] = lambda: {}
    no_net.routes["https://api.tcgdex.net/v2/en/cards/sv1-2"] = lambda: {
        "id": "sv1-2",
        "name": "Pikachu",
        "localId": "2",
        "set": {"id": "sv1", "name": "Scarlet", "releaseDate": "2023-01-01"},
        "pricing": {"cardmarket": {"avg": 1.5, "updated": "2026-01-01"}},
    }
    hits = REGISTRY["pokemon"].search("pikachu")
    assert [h.card_id for h in hits] == ["sv1-2"]


def test_tcgcsv_skips_groups_without_id(no_net):
    cat = 89
    no_net.routes[f"https://tcgcsv.com/tcgplayer/{cat}/groups"] = lambda: {
        "results": [
            {"name": "No id"},
            {
                "groupId": 7,
                "name": "Origins",
                "abbreviation": "ORI",
                "publishedOn": "2025-06-01T00:00:00",
            },
        ]
    }
    no_net.routes[f"https://tcgcsv.com/tcgplayer/{cat}/7/products"] = lambda: {
        "results": [
            {
                "productId": 100,
                "name": "Ahri",
                "cleanName": "Ahri",
                "url": "https://www.tcgplayer.com/product/100/x",
                "extendedData": [{"name": "Number", "value": "001"}],
            }
        ]
    }
    no_net.routes[f"https://tcgcsv.com/tcgplayer/{cat}/7/prices"] = lambda: {
        "results": [
            {"productId": 100, "marketPrice": 1.75, "midPrice": 2.0, "subTypeName": "Normal"},
        ]
    }
    hits = REGISTRY["riftbound"].search("ahri")
    assert len(hits) == 1
    assert hits[0].set_code == "ORI"


def test_tcgcsv_search_caps_at_fifteen(no_net):
    cat = 89
    no_net.routes[f"https://tcgcsv.com/tcgplayer/{cat}/groups"] = lambda: {
        "results": [
            {
                "groupId": 7,
                "name": "Origins",
                "abbreviation": "ORI",
                "publishedOn": "2025-06-01T00:00:00",
            }
        ]
    }
    no_net.routes[f"https://tcgcsv.com/tcgplayer/{cat}/7/products"] = lambda: {
        "results": [
            {
                "productId": i,
                "name": f"Ahri Clone {i}",
                "cleanName": f"Ahri Clone {i}",
                "url": "https://www.tcgplayer.com/product/x",
                "extendedData": [],
            }
            for i in range(20)
        ]
    }
    no_net.routes[f"https://tcgcsv.com/tcgplayer/{cat}/7/prices"] = lambda: {"results": []}
    assert len(REGISTRY["riftbound"].search("ahri")) == 15


def test_tcgcsv_skips_unparseable_price_row():
    from topdeck.adapters.base import CardHit

    hit = CardHit(
        card_id="100",
        name="Ahri",
        set_code="ORI",
        set_name="Origins",
        collector_number="001",
        extra={
            "prices": [
                {
                    "productId": 100,
                    "marketPrice": "junk",
                    "midPrice": None,
                    "subTypeName": "Normal",
                },
                {"productId": 100, "marketPrice": 2.5, "midPrice": 2.0, "subTypeName": "Normal"},
            ]
        },
    )
    prices = REGISTRY["riftbound"].get_prices(hit)
    assert len(prices) == 1
    assert prices[0].price == 2.5


# ---------------------------------------------------------------------------
# Scryfall 404 means "no such card", not an outage


def test_scryfall_404_is_no_match(no_net):
    def missing():
        raise topdeck.net.SourceError("the price source returned HTTP 404.", status=404)

    import topdeck.adapters.mtg as mtg_mod

    no_net.routes["https://api.scryfall.com/cards/search?q=Bogus"] = missing
    assert mtg_mod.ScryfallAdapter().search("Bogus") == []


def test_scryfall_500_still_raises(no_net):
    def down():
        raise topdeck.net.SourceError("the price source returned HTTP 500.", status=500)

    import topdeck.adapters.mtg as mtg_mod

    no_net.routes["https://api.scryfall.com/cards/search?q=Bolt"] = down
    with pytest.raises(topdeck.net.SourceError):
        mtg_mod.ScryfallAdapter().search("Bolt")


# ---------------------------------------------------------------------------
# Bulk downloads run on threads while the throttle paces every request


def _threaded_fetch(monkeypatch, fake, marker, parties):
    """Wrap the fake fetch so `parties` overlapping calls must use threads.

    A barrier that only `parties` distinct threads can pass proves the
    downloads overlap instead of running one after another.
    """
    import threading

    barrier = threading.Barrier(parties, timeout=10)
    seen = set()
    lock = threading.Lock()

    def wrapped(url, **kwargs):
        with lock:
            seen.add(threading.get_ident())
        if marker in url:
            barrier.wait()
        return fake(url, **kwargs)

    monkeypatch.setattr(topdeck.net, "fetch_json", wrapped)
    return seen


def test_tcgcsv_catalog_downloads_in_parallel(no_net, monkeypatch):
    cat = 89
    no_net.routes[f"https://tcgcsv.com/tcgplayer/{cat}/groups"] = lambda: {
        "results": [{"groupId": i, "name": f"Set {i}"} for i in range(1, 5)]
    }
    for i in range(1, 5):
        no_net.routes[f"https://tcgcsv.com/tcgplayer/{cat}/{i}/products"] = lambda i=i: {
            "results": [{"productId": i, "cleanName": f"Card {i}"}]
        }
    seen = _threaded_fetch(monkeypatch, no_net, "/products", 4)
    groups, products_by_group = REGISTRY["riftbound"]._catalog()
    assert len(products_by_group) == 4
    assert {p["cleanName"] for rows in products_by_group.values() for p in rows} == {
        f"Card {i}" for i in range(1, 5)
    }
    assert groups[0]["name"] == "Set 1"
    # Four overlapping barrier passes need at least four threads.
    assert len(seen) >= 4


def test_lorcast_card_lists_download_in_parallel(no_net, monkeypatch):
    no_net.routes["https://api.lorcast.com/v0/sets"] = lambda: {
        "results": [{"code": f"S{i}", "name": f"Set {i}"} for i in range(1, 5)]
    }
    for i in range(1, 5):
        no_net.routes[f"https://api.lorcast.com/v0/sets/S{i}/cards"] = lambda i=i: [
            {"id": f"c{i}", "name": f"Card {i}", "prices": {"usd": "1.0"}}
        ]
    seen = _threaded_fetch(monkeypatch, no_net, "/cards", 4)
    cards = REGISTRY["lorcana"]._all_cards()
    # pool.map preserves set order, and every card keeps its set tag.
    assert [(c["name"], c["_set"]["code"]) for c in cards] == [
        (f"Card {i}", f"S{i}") for i in range(1, 5)
    ]
    assert len(seen) >= 4


def test_tcgdex_details_download_in_parallel(no_net, monkeypatch):
    no_net.routes["https://api.tcgdex.net/v2/en/cards?name=Pikachu"] = lambda: [
        {"id": f"swsh3-{i}", "localId": str(i), "name": "Pikachu"} for i in range(1, 5)
    ]
    for i in range(1, 5):
        no_net.routes[f"https://api.tcgdex.net/v2/en/cards/swsh3-{i}"] = lambda i=i: {
            "id": f"swsh3-{i}",
            "name": "Pikachu",
            "localId": str(i),
            "set": {"id": "swsh3", "name": "Set", "releaseDate": "2021-01-01"},
            "pricing": {"tcgplayer": {"normal": {"marketPrice": 1.0}}},
        }
    seen = _threaded_fetch(monkeypatch, no_net, "/cards/swsh3-", 4)
    hits = REGISTRY["pokemon"].search("Pikachu")
    assert len(hits) == 4
    # Ranked order survives the threads: pool.map preserves input order.
    assert [h.collector_number for h in hits] == ["1", "2", "3", "4"]
    assert len(seen) >= 4
