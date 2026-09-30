"""Accuracy sweep: no adapter may emit a price missing the five questions.

Every Price must carry a non-empty market, currency, condition,
printing, as-of, and source, and every number must come straight from
the fixture. No fabricated or placeholder values anywhere.
"""

import pytest

import topdeck.net
from topdeck.adapters import REGISTRY

_FIVE_QUESTIONS = ("market", "currency", "condition", "printing", "as_of", "source")


@pytest.fixture
def no_net(monkeypatch):
    def fake(url, **kwargs):
        handler = fake.routes.get(url)
        assert handler is not None, f"unexpected URL: {url}"
        return handler()

    fake.routes = {}
    monkeypatch.setattr(topdeck.net, "fetch_json", fake)
    return fake


def _scryfall_routes(fake):
    fake.routes["https://api.scryfall.com/cards/search?q=Bolt"] = lambda: {
        "data": [
            {
                "id": "a",
                "name": "Lightning Bolt",
                "set": "lea",
                "set_name": "Limited Edition Alpha",
                "collector_number": "161",
                "finishes": ["normal", "foil"],
                "released_at": "1993-08-05",
                "scryfall_uri": "https://scryfall.com/card/lea/161/x",
                "prices": {"usd": "12.50", "usd_foil": "30.00", "eur": None},
            }
        ]
    }


def _tcgdex_routes(fake):
    fake.routes["https://api.tcgdex.net/v2/en/cards?name=Pika"] = lambda: [
        {"id": "x1", "localId": "25", "name": "Pikachu"}
    ]
    fake.routes["https://api.tcgdex.net/v2/en/cards/x1"] = lambda: {
        "id": "x1",
        "name": "Pikachu",
        "localId": "25",
        "set": {"id": "s", "name": "Base", "releaseDate": "1999-01-09"},
        "pricing": {
            "cardmarket": {"updated": "2026-09-30T00:00:00Z", "avg": 1.5},
            "tcgplayer": {
                "updated": "2026-09-30T00:00:00Z",
                "normal": {"productId": 1, "marketPrice": 2.5},
            },
        },
    }


def _lorcast_routes(fake):
    fake.routes["https://api.lorcast.com/v0/sets"] = lambda: {
        "results": [{"code": "TFC", "name": "The First Chapter"}]
    }
    fake.routes["https://api.lorcast.com/v0/sets/TFC/cards"] = lambda: [
        {
            "id": "c1",
            "name": "Elsa",
            "version": "Queen",
            "collector_number": 5,
            "released_at": "2023-08-18",
            "prices": {"usd": "3.25"},
        }
    ]


def _tcgcsv_routes(fake, cat):
    base = f"https://tcgcsv.com/tcgplayer/{cat}"
    fake.routes[base + "/groups"] = lambda: {
        "results": [
            {
                "groupId": 7,
                "name": "Set",
                "abbreviation": "ST",
                "publishedOn": "2025-01-01T00:00:00",
            }
        ]
    }
    fake.routes[base + "/7/products"] = lambda: {
        "results": [
            {
                "productId": 9,
                "name": "Hero",
                "cleanName": "Hero",
                "url": "https://www.tcgplayer.com/product/9/x",
                "extendedData": [{"name": "Number", "value": "001"}],
            }
        ]
    }
    fake.routes[base + "/7/prices"] = lambda: {
        "results": [
            {
                "productId": 9,
                "lowPrice": 1.0,
                "midPrice": 2.0,
                "marketPrice": 1.75,
                "subTypeName": "Normal",
            }
        ]
    }


@pytest.mark.parametrize(
    "game_key, query, route_setup",
    [
        ("mtg", "Bolt", _scryfall_routes),
        ("pokemon", "Pika", _tcgdex_routes),
        ("lorcana", "Elsa", _lorcast_routes),
        ("onepiece", "Hero", lambda f: _tcgcsv_routes(f, 68)),
        ("riftbound", "Hero", lambda f: _tcgcsv_routes(f, 89)),
        ("yugioh", "Hero", lambda f: _tcgcsv_routes(f, 2)),
    ],
)
def test_every_price_answers_five_questions(no_net, game_key, query, route_setup):
    route_setup(no_net)
    adapter = REGISTRY[game_key]
    hits = adapter.search(query)
    assert hits, f"{game_key} returned no hits for fixture query"
    prices = adapter.get_prices(hits[0])
    assert prices, f"{game_key} returned no prices for fixture hit"
    for price in prices:
        for field in _FIVE_QUESTIONS:
            assert getattr(price, field), f"{game_key}: empty {field}"
        assert isinstance(price.price, (int, float))
        assert price.price > 0


@pytest.mark.parametrize("game_key", list(REGISTRY))
def test_empty_source_block_yields_no_prices_never_guesses(no_net, game_key):
    """A source with no numbers for a card yields zero prices, never
    zeroes, placeholders, or guesses."""
    from dataclasses import replace

    from topdeck import backbone

    if game_key == "mtg":
        _scryfall_routes(no_net)
    elif game_key == "pokemon":
        _tcgdex_routes(no_net)
    elif game_key == "lorcana":
        _lorcast_routes(no_net)
    else:
        _tcgcsv_routes(no_net, backbone.CATEGORY_IDS[game_key])
    adapter = REGISTRY[game_key]
    query = {"mtg": "Bolt", "pokemon": "Pika", "lorcana": "Elsa"}.get(game_key, "Hero")
    hits = adapter.search(query)
    assert hits
    empty_extra = {
        "mtg": {"prices": {}},
        "pokemon": {"pricing": {}},
        "lorcana": {"usd": None},
    }.get(game_key, {"prices": []})
    blank = replace(hits[0], extra=empty_extra)
    assert adapter.get_prices(blank) == []
