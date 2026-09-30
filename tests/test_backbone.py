"""Tests for the TCGCSV price backbone. Network is faked; the clock is frozen."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import topdeck.net
from topdeck import backbone
from topdeck.adapters import REGISTRY, resolve_game
from topdeck.adapters.base import CardHit, Price, prices_from_cents, with_sidecar_price
from topdeck.adapters.tcgcsv import TcgcsvBulkAdapter
from topdeck.backbone import SyncResult
from topdeck.cli import main
from topdeck.doctor import _backbone_summary, local_checks
from topdeck.output import sync_json, sync_table


@pytest.fixture
def cache_home(tmp_path, monkeypatch):
    """Backbone reads and writes only under this fake XDG cache dir."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_net(monkeypatch):
    """Every test decides exactly what the network returns."""
    calls = []
    routes = {}

    def fake(url, **kwargs):
        calls.append(url)
        handler = routes.get(url)
        assert handler is not None, f"unexpected URL: {url}"
        return handler()

    fake.calls = calls
    fake.routes = routes
    monkeypatch.setattr(topdeck.net, "fetch_json", fake)
    return fake


def _riftbound_routes(fake):
    """Small but nasty fixture: every skip branch has a row."""
    fake.routes["https://tcgcsv.com/tcgplayer/89/groups"] = lambda: {
        "results": [
            {
                "groupId": 1,
                "name": "Set One",
                "abbreviation": "S1",
                "publishedOn": "2024-01-01",
            },
            {"groupId": 2, "name": "Set Two", "abbreviation": "S2"},
            "not a dict",
            {"name": "No Group Id"},
        ]
    }
    fake.routes["https://tcgcsv.com/tcgplayer/89/1/products"] = lambda: {
        "results": [
            {"productId": 101, "cleanName": "Test Card Alpha", "name": "Test Card Alpha"},
            {"productId": 102, "name": "Test Card Beta"},
            {"productId": 103, "cleanName": "100% Real_Card"},
            {"productId": 104, "cleanName": "Priceless Card"},
            {"productId": 105, "cleanName": "Foil Only Card"},
            {"productId": 107, "cleanName": "Worthless Card"},
            "not a dict",
            {"name": "No Product Id"},
        ]
    }
    fake.routes["https://tcgcsv.com/tcgplayer/89/1/prices"] = lambda: {
        "results": [
            {"productId": 101, "marketPrice": 1.50, "midPrice": 1.20, "subTypeName": "Normal"},
            {"productId": 101, "marketPrice": 5.00, "midPrice": 4.50, "subTypeName": "Foil"},
            {"productId": 102, "marketPrice": None, "midPrice": 0.75, "subTypeName": "Normal"},
            {"productId": 103, "marketPrice": "garbage", "midPrice": 0.10, "subTypeName": "Normal"},
            {"productId": 105, "marketPrice": 3.00, "midPrice": 2.50, "subTypeName": "Foil"},
            {"productId": 107, "marketPrice": None, "midPrice": None, "subTypeName": "Normal"},
            {"productId": 106, "marketPrice": 9.99, "midPrice": 9.00, "subTypeName": "Normal"},
            {"marketPrice": 1.00},
            "not a dict",
        ]
    }
    fake.routes["https://tcgcsv.com/tcgplayer/89/2/products"] = lambda: {
        "results": [
            {"productId": 201, "cleanName": "Second Set Card", "name": "Second Set Card"},
        ]
    }
    fake.routes["https://tcgcsv.com/tcgplayer/89/2/prices"] = lambda: {
        "results": [
            {"productId": 201, "marketPrice": 2.00, "midPrice": None, "subTypeName": "Normal"},
        ]
    }


def _db_rows(game="riftbound"):
    conn = sqlite3.connect(backbone.db_path())
    try:
        return conn.execute(
            "SELECT join_key, market_cents, mid_cents FROM prices WHERE game = ? ORDER BY join_key",
            (game,),
        ).fetchall()
    finally:
        conn.close()


def _stamp_meta(game, moment):
    conn = backbone._connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"synced_at:{game}", moment.isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def test_sync_game_stores_price_rows_and_meta(cache_home, fake_net):
    _riftbound_routes(fake_net)
    result = backbone.sync_game("riftbound")
    assert result.ok, result.error
    assert result.game == "riftbound"
    assert result.groups == 2
    assert result.products == 5
    by_id = {r[0]: r for r in _db_rows()}
    # Normal row wins over the Foil row for the same product; money in cents.
    assert by_id[101][1] == 150
    assert by_id[101][2] == 120
    # Missing market price: mid survives.
    assert by_id[102][1] is None
    assert by_id[102][2] == 75
    # Garbage market price is dropped, mid survives.
    assert by_id[103][1] is None
    assert by_id[103][2] == 10
    # No Normal row: the Foil row is better than nothing.
    assert by_id[105][1] == 300
    assert by_id[201][1] == 200
    assert by_id[201][2] is None
    # Products with no usable prices never make it in.
    assert 104 not in by_id
    assert 106 not in by_id
    assert 107 not in by_id
    stamp = backbone.synced_at("riftbound")
    assert stamp is not None
    datetime.fromisoformat(stamp)  # parses, or the test fails


def test_sync_game_never_raises(cache_home, fake_net):
    def boom():
        raise topdeck.net.SourceError("source is down")

    fake_net.routes["https://tcgcsv.com/tcgplayer/89/groups"] = boom
    result = backbone.sync_game("riftbound")
    assert not result.ok
    assert "source is down" in result.error
    assert backbone.sync_status("riftbound") == "never"


def test_sync_respects_xdg_cache_home(cache_home, fake_net):
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    assert backbone.db_path().startswith(str(cache_home))
    assert "topdeck" in backbone.db_path()


def test_sync_status_lifecycle(cache_home, fake_net):
    assert backbone.sync_status("riftbound") == "never"
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    synced = datetime.fromisoformat(backbone.synced_at("riftbound"))
    assert backbone.sync_status("riftbound", now=synced) == "fresh"
    assert backbone.sync_status("riftbound", now=synced + timedelta(hours=36)) == "fresh"
    assert backbone.sync_status("riftbound", now=synced + timedelta(hours=36, seconds=1)) == "stale"


def test_sync_status_naive_stamp_treated_as_utc(cache_home):
    conn = backbone._connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES "
            "('synced_at:riftbound', '2026-09-30T12:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    now = datetime(2026, 9, 30, 13, 0, 0, tzinfo=timezone.utc)
    assert backbone.sync_status("riftbound", now=now) == "fresh"


def test_sync_status_corrupt_stamp_is_stale(cache_home):
    conn = backbone._connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('synced_at:riftbound', 'not-a-time')"
        )
        conn.commit()
    finally:
        conn.close()
    assert backbone.sync_status("riftbound") == "stale"


def test_schema_rebuild_on_version_mismatch(cache_home):
    conn = backbone._connect()
    try:
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
        conn.execute(
            "INSERT INTO prices (game, join_key, market_cents, mid_cents) "
            "VALUES ('riftbound', 1, 100, 90)"
        )
        conn.commit()
    finally:
        conn.close()
    assert backbone.sync_status("riftbound") == "never"
    assert _db_rows() == []
    conn = sqlite3.connect(backbone.db_path())
    try:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    finally:
        conn.close()
    assert version == str(backbone.SCHEMA_VERSION)


def test_connect_rebuilds_corrupt_database(cache_home):
    os.makedirs(os.path.dirname(backbone.db_path()), exist_ok=True)
    with open(backbone.db_path(), "w") as fh:
        fh.write("this is not a database")
    conn = backbone._connect()
    try:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    finally:
        conn.close()
    assert version == str(backbone.SCHEMA_VERSION)


def test_synced_at_without_database_is_none(cache_home):
    assert backbone.synced_at("riftbound") is None


def test_to_cents_conversions():
    assert backbone._to_cents(None) is None
    assert backbone._to_cents(1.50) == 150
    assert backbone._to_cents(0) == 0
    assert backbone._to_cents("garbage") is None


def test_pick_price_row():
    assert backbone._pick_price_row([]) is None
    foil = {"subTypeName": "Foil", "marketPrice": 5.0}
    normal = {"subTypeName": "Normal", "marketPrice": 1.5}
    assert backbone._pick_price_row([foil, normal]) is normal
    assert backbone._pick_price_row([foil]) is foil


def test_lookup_price_serves_fresh_row(cache_home, fake_net):
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    stamp = backbone.synced_at("riftbound")
    row = backbone.lookup_price("riftbound", 101)
    assert row is not None
    assert row["market_cents"] == 150
    assert row["mid_cents"] == 120
    assert row["as_of"] == stamp


def test_lookup_price_miss_returns_none(cache_home, fake_net):
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    assert backbone.lookup_price("riftbound", 999999) is None
    # Other games never leak in.
    assert backbone.lookup_price("pokemon", 101) is None


def test_lookup_price_stale_or_never_returns_none(cache_home, fake_net):
    assert backbone.lookup_price("riftbound", 101) is None
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    old = datetime.now(timezone.utc) - timedelta(hours=40)
    _stamp_meta("riftbound", old)
    assert backbone.lookup_price("riftbound", 101) is None


def test_prices_from_cents_market_wins():
    prices = prices_from_cents(150, 120, as_of="2026-09-30T12:00:00+00:00", source="tcgcsv")
    assert len(prices) == 1
    price = prices[0]
    assert price.price == 1.50
    assert price.currency == "USD"
    assert price.market == "tcgplayer"
    assert price.provenance == "market"
    assert price.source == "tcgcsv"


def test_prices_from_cents_mid_fallback_labeled():
    prices = prices_from_cents(None, 75, as_of="s", source="tcgcsv")
    assert len(prices) == 1
    assert prices[0].price == 0.75
    assert prices[0].provenance == "mid"


def test_prices_from_cents_empty_when_no_price():
    assert prices_from_cents(None, None, as_of="s", source="tcgcsv") == []


def _leg(market, currency, printing, value):
    return Price(
        market=market,
        currency=currency,
        condition="near-mint",
        printing=printing,
        price=value,
        as_of="t",
        source="live",
    )


def test_with_sidecar_price_drops_only_the_exact_duplicate():
    sidecar = prices_from_cents(250, None, as_of="s", source="tcgcsv")
    live = [
        _leg("tcgplayer", "USD", "normal", 0.10),
        _leg("tcgplayer", "USD", "foil", 0.50),
        _leg("cardmarket", "EUR", "normal", 0.08),
    ]
    out = with_sidecar_price(sidecar, live)
    assert [p.price for p in out] == [2.50, 0.50, 0.08]
    assert out[0].source == "tcgcsv"


def test_with_sidecar_price_empty_sidecar_returns_live_untouched():
    live = [_leg("tcgplayer", "USD", "normal", 0.10)]
    assert with_sidecar_price([], live) == live


def test_with_sidecar_price_keeps_everything_when_no_duplicate():
    sidecar = prices_from_cents(250, None, as_of="s", source="tcgcsv")
    live = [_leg("cardmarket", "EUR", "normal", 0.08)]
    out = with_sidecar_price(sidecar, live)
    assert [p.price for p in out] == [2.50, 0.08]


def _hit(card_id, **extra):
    return CardHit(
        card_id=card_id,
        name="Test Card",
        set_code="S1",
        set_name="Set One",
        collector_number="1",
        extra=extra,
    )


def test_tcgcsv_adapter_supplements_live_legs_when_fresh(cache_home, fake_net, monkeypatch):
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    stamp = backbone.synced_at("riftbound")

    def no_network(url, **kwargs):
        raise AssertionError(f"network hit during backbone lookup: {url}")

    monkeypatch.setattr(topdeck.net, "fetch_json", no_network)
    adapter = REGISTRY["riftbound"]
    live = [
        {"marketPrice": 99.0, "subTypeName": "Normal"},
        {"marketPrice": 5.00, "subTypeName": "Foil"},
    ]
    prices = adapter.get_prices(_hit("101", prices=live))
    # Sidecar USD row first; the live Normal leg it supersedes is dropped;
    # the Foil leg survives with its own source label.
    assert [(p.price, p.printing, p.source) for p in prices] == [
        (1.50, "normal", "tcgcsv"),
        (5.00, "foil", "tcgcsv"),
    ]
    assert prices[0].provenance == "market"
    assert prices[0].as_of == stamp
    mid = adapter.get_prices(_hit("102", prices=[]))
    assert mid[0].provenance == "mid"
    assert mid[0].price == 0.75


def test_tcgcsv_adapter_falls_back_to_live_on_miss(cache_home, fake_net):
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    adapter = REGISTRY["riftbound"]
    live = [{"marketPrice": 7.5, "midPrice": 7.0, "subTypeName": "Normal"}]
    prices = adapter.get_prices(_hit("999999", prices=live))
    # 7.5 is the live row; the backbone has no row 999999.
    assert [p.price for p in prices] == [7.5]


def test_tcgcsv_adapter_falls_back_to_live_when_stale(cache_home, fake_net):
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    _stamp_meta("riftbound", datetime.now(timezone.utc) - timedelta(hours=40))
    adapter = REGISTRY["riftbound"]
    live = [{"marketPrice": 7.5, "subTypeName": "Normal"}]
    prices = adapter.get_prices(_hit("101", prices=live))
    # Stale sync: the live row (7.5) wins over the synced 1.50.
    assert [p.price for p in prices] == [7.5]


def test_tcgcsv_adapter_bad_card_id_uses_live_path(cache_home, fake_net):
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    adapter = REGISTRY["riftbound"]
    live = [{"marketPrice": 7.5, "subTypeName": "Normal"}]
    prices = adapter.get_prices(_hit("not-a-number", prices=live))
    assert [p.price for p in prices] == [7.5]


def test_mtg_adapter_supplements_scryfall_legs_when_fresh(cache_home, fake_net):
    _mtg_routes(fake_net)
    backbone.sync_game("mtg")
    stamp = backbone.synced_at("mtg")
    adapter = REGISTRY["mtg"]
    hit = _hit(
        "scryfall-id",
        tcgplayer_id=697344,
        prices={"usd": "0.10", "usd_foil": "0.50", "eur": "0.08"},
    )
    prices = adapter.get_prices(hit)
    # Sidecar USD row first; the plain-usd Scryfall leg it supersedes is
    # dropped; foil and EUR legs survive.
    assert [(p.price, p.printing, p.currency, p.source) for p in prices] == [
        (2.50, "normal", "USD", "tcgcsv"),
        (0.50, "foil", "USD", "scryfall"),
        (0.08, "normal", "EUR", "scryfall"),
    ]
    assert prices[0].provenance == "market"
    assert prices[0].as_of == stamp


def test_mtg_adapter_falls_back_to_scryfall(cache_home, fake_net):
    _mtg_routes(fake_net)
    backbone.sync_game("mtg")
    adapter = REGISTRY["mtg"]
    # Card the backbone never saw: Scryfall's own prices answer.
    hit = _hit("scryfall-id", tcgplayer_id=123456, prices={"usd": "0.10"})
    prices = adapter.get_prices(hit)
    assert [p.price for p in prices] == [0.10]
    assert prices[0].source == "scryfall"
    # Card with no TCGplayer id at all: same fallback.
    hit2 = _hit("scryfall-id", tcgplayer_id=None, prices={"usd": "0.20"})
    assert adapter.get_prices(hit2)[0].price == 0.20


def test_mtg_adapter_stale_sync_uses_scryfall(cache_home, fake_net):
    _mtg_routes(fake_net)
    backbone.sync_game("mtg")
    _stamp_meta("mtg", datetime.now(timezone.utc) - timedelta(hours=40))
    adapter = REGISTRY["mtg"]
    hit = _hit("scryfall-id", tcgplayer_id=697344, prices={"usd": "0.10"})
    prices = adapter.get_prices(hit)
    assert prices[0].source == "scryfall"


def _mtg_routes(fake):
    fake.routes["https://tcgcsv.com/tcgplayer/1/groups"] = lambda: {
        "results": [{"groupId": 7, "name": "Set Seven", "abbreviation": "S7"}]
    }
    fake.routes["https://tcgcsv.com/tcgplayer/1/7/products"] = lambda: {
        "results": [{"productId": 697344, "cleanName": "Lightning Bolt"}]
    }
    fake.routes["https://tcgcsv.com/tcgplayer/1/7/prices"] = lambda: {
        "results": [
            {"productId": 697344, "marketPrice": 2.50, "midPrice": 2.00, "subTypeName": "Normal"},
        ]
    }


def test_pokemon_adapter_supplements_tcgdex_legs_when_fresh(cache_home, fake_net):
    _pokemon_routes(fake_net)
    backbone.sync_game("pokemon")
    adapter = REGISTRY["pokemon"]
    pricing = {
        "cardmarket": {"avg": 1.00, "updated": "cm"},
        "tcgplayer": {
            "normal": {"marketPrice": 1.25, "updated": "tcg"},
            "reverse-holofoil": {"marketPrice": 2.00, "updated": "tcg"},
        },
    }
    hit = _hit("pl4-1", product_id=84191, pricing=pricing)
    prices = adapter.get_prices(hit)
    # Sidecar USD row first; the plain tcgplayer leg it supersedes is
    # dropped; Cardmarket and reverse-holo legs survive.
    assert [(p.price, p.market, p.printing) for p in prices] == [
        (3.25, "tcgplayer", "normal"),
        (1.00, "cardmarket", "normal"),
        (2.00, "tcgplayer", "reverse-holo"),
    ]
    assert prices[0].source == "tcgcsv"
    assert prices[0].provenance == "market"
    assert prices[1].source == "tcgdex"


def test_pokemon_adapter_falls_back_to_tcgdex(cache_home, fake_net):
    _pokemon_routes(fake_net)
    backbone.sync_game("pokemon")
    adapter = REGISTRY["pokemon"]
    pricing = {"tcgplayer": {"normal": {"marketPrice": 1.25, "updated": "today"}}}
    hit = _hit("swsh3-1", product_id=None, pricing=pricing)
    prices = adapter.get_prices(hit)
    assert [p.price for p in prices] == [1.25]
    assert prices[0].source == "tcgdex"


def _pokemon_routes(fake):
    fake.routes["https://tcgcsv.com/tcgplayer/3/groups"] = lambda: {
        "results": [{"groupId": 9, "name": "Set Nine", "abbreviation": "S9"}]
    }
    fake.routes["https://tcgcsv.com/tcgplayer/3/9/products"] = lambda: {
        "results": [{"productId": 84191, "cleanName": "Charizard"}]
    }
    fake.routes["https://tcgcsv.com/tcgplayer/3/9/prices"] = lambda: {
        "results": [
            {"productId": 84191, "marketPrice": 3.25, "midPrice": 3.00, "subTypeName": "Normal"},
        ]
    }


def test_lorcana_adapter_supersedes_lorcast_leg_when_fresh(cache_home, fake_net):
    _lorcana_routes(fake_net)
    backbone.sync_game("lorcana")
    adapter = REGISTRY["lorcana"]
    hit = _hit("lorcana-id", tcgplayer_id=454233, usd="9.99")
    prices = adapter.get_prices(hit)
    # Lorcast's only leg is the exact USD leg the sidecar supersedes,
    # so the sidecar row stands alone: no duplicate, nothing lost.
    assert len(prices) == 1
    assert prices[0].price == 4.75
    assert prices[0].source == "tcgcsv"


def test_lorcana_adapter_falls_back_to_lorcast(cache_home, fake_net):
    _lorcana_routes(fake_net)
    backbone.sync_game("lorcana")
    adapter = REGISTRY["lorcana"]
    hit = _hit("lorcana-id", tcgplayer_id=None, usd="9.99")
    prices = adapter.get_prices(hit)
    assert [p.price for p in prices] == [9.99]
    assert prices[0].source == "lorcast"


def _lorcana_routes(fake):
    fake.routes["https://tcgcsv.com/tcgplayer/71/groups"] = lambda: {
        "results": [{"groupId": 11, "name": "Set Eleven", "abbreviation": "S11"}]
    }
    fake.routes["https://tcgcsv.com/tcgplayer/71/11/products"] = lambda: {
        "results": [{"productId": 454233, "cleanName": "Elsa"}]
    }
    fake.routes["https://tcgcsv.com/tcgplayer/71/11/prices"] = lambda: {
        "results": [
            {"productId": 454233, "marketPrice": 4.75, "midPrice": 4.50, "subTypeName": "Normal"},
        ]
    }


def test_registry_covers_every_backbone_game():
    assert set(REGISTRY) == set(backbone.GAMES)
    assert len(REGISTRY) == len(backbone.CATEGORY_IDS) == len(backbone.GAME_NAMES)


def test_registry_new_games_are_parameterized_tcgcsv():
    adapter = REGISTRY["yugioh"]
    assert isinstance(adapter, TcgcsvBulkAdapter)
    assert adapter.category_id == 2
    assert adapter.display_name == "YuGiOh"
    assert adapter.trust_tier == "beta"
    assert adapter.source_name == "tcgcsv"
    # The original five keep their own adapters and tiers.
    assert REGISTRY["onepiece"].trust_tier == "solid"
    assert REGISTRY["riftbound"].trust_tier == "experimental"
    assert REGISTRY["mtg"].game_key == "mtg"


def test_registry_aliases():
    assert resolve_game("ygo") is REGISTRY["yugioh"]
    assert resolve_game("yu-gi-oh") is REGISTRY["yugioh"]
    assert resolve_game("magic") is REGISTRY["mtg"]
    assert resolve_game("no-such-game") is None


def test_doctor_summary_reports_never_synced_and_suggests_sync(cache_home):
    summary = _backbone_summary()
    assert f"0/{len(backbone.GAMES)} games fresh" in summary
    assert "mtg: never synced" in summary
    assert f"+{len(backbone.GAMES) - 5} more" in summary
    assert "Run `topdeck sync` to refresh prices." in summary
    local = dict(local_checks())
    assert "TCGCSV sync" in local


def test_doctor_summary_all_fresh_has_no_suggestion(cache_home):
    now = datetime.now(timezone.utc)
    for game in backbone.GAMES:
        _stamp_meta(game, now)
    summary = _backbone_summary()
    assert f"{len(backbone.GAMES)}/{len(backbone.GAMES)} games fresh" in summary
    assert "never synced" not in summary
    assert "stale" not in summary
    assert "Run `topdeck sync`" not in summary


def test_doctor_summary_flags_stale(cache_home):
    now = datetime.now(timezone.utc)
    _stamp_meta("mtg", now - timedelta(hours=40))
    summary = _backbone_summary()
    assert "mtg: stale" in summary
    assert "Run `topdeck sync` to refresh prices." in summary


def test_sync_cli_one_game_table(cache_home, fake_net, capsys):
    _riftbound_routes(fake_net)
    assert main(["sync", "riftbound"]) == 0
    out = capsys.readouterr().out
    assert "riftbound" in out
    assert "synced" in out
    assert "2 groups" in out


def test_sync_cli_one_game_json(cache_home, fake_net, capsys):
    _riftbound_routes(fake_net)
    assert main(["--json", "sync", "riftbound"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["command"] == "sync"
    assert payload["games"][0]["game"] == "riftbound"
    assert payload["games"][0]["status"] == "ok"
    assert payload["games"][0]["products"] == 5


def test_sync_cli_new_game_resolves(cache_home, fake_net, capsys, monkeypatch):
    seen = []

    def fake_sync(game):
        seen.append(game)
        return SyncResult(game=game, ok=True, groups=1, products=2)

    monkeypatch.setattr(backbone, "sync_game", fake_sync)
    assert main(["sync", "yugioh"]) == 0
    assert seen == ["yugioh"]
    assert main(["sync", "ygo"]) == 0
    assert seen == ["yugioh", "yugioh"]


def test_sync_cli_unknown_game(cache_home, capsys):
    assert main(["sync", "atlantis"]) == 2
    out = capsys.readouterr().out
    assert "Unknown game" in out


def test_sync_cli_alias_resolves(cache_home, fake_net, capsys, monkeypatch):
    seen = []

    def fake_sync(game):
        seen.append(game)
        return SyncResult(game=game, ok=True, groups=1, products=2)

    monkeypatch.setattr(backbone, "sync_game", fake_sync)
    assert main(["sync", "magic"]) == 0
    assert seen == ["mtg"]


def test_core_games_are_all_backbone_games():
    assert set(backbone.CORE_GAMES) <= set(backbone.GAMES)
    assert backbone.CORE_GAMES == ("mtg", "pokemon", "lorcana", "onepiece", "riftbound")


def test_sync_cli_defaults_to_core_games_and_failure_exit_code(cache_home, capsys, monkeypatch):
    seen = []

    def fake_sync(game):
        seen.append(game)
        if game == "pokemon":
            return SyncResult(game=game, ok=False, error="boom")
        return SyncResult(game=game, ok=True, groups=1, products=2)

    monkeypatch.setattr(backbone, "sync_game", fake_sync)
    assert main(["sync"]) == 1
    assert seen == list(backbone.CORE_GAMES)
    out = capsys.readouterr().out
    assert "failed" in out
    assert "boom" in out


def test_sync_cli_all_flag_syncs_every_game(cache_home, capsys, monkeypatch):
    seen = []

    def fake_sync(game):
        seen.append(game)
        return SyncResult(game=game, ok=True, groups=1, products=2)

    monkeypatch.setattr(backbone, "sync_game", fake_sync)
    assert main(["sync", "--all"]) == 0
    assert seen == list(backbone.GAMES)
    out = capsys.readouterr().out
    assert "riftbound" in out


def test_sync_cli_all_with_one_game_is_an_error(cache_home, capsys):
    assert main(["sync", "--all", "pokemon"]) == 2
    out = capsys.readouterr().out
    assert "not both" in out


def test_sync_cli_all_json_shape(cache_home, capsys, monkeypatch):
    def fake_sync(game):
        return SyncResult(game=game, ok=True, groups=1, products=2)

    monkeypatch.setattr(backbone, "sync_game", fake_sync)
    assert main(["--json", "sync", "--all"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["command"] == "sync"
    assert len(payload["games"]) == len(backbone.GAMES)


def test_sync_help_mentions_all_and_keeps_cron(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["sync", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--all" in out
    assert "core games" in out
    assert "cron" in out
    assert "0 6 * * * topdeck sync" in out


def test_sync_game_unknown_game_is_failed_result_not_raise(cache_home):
    result = backbone.sync_game("atlantis")
    assert result.ok is False
    assert result.game == "atlantis"


def test_sync_cli_all_games_json_failure_shape(cache_home, capsys, monkeypatch):
    def fake_sync(game):
        return SyncResult(game=game, ok=False, error="boom")

    monkeypatch.setattr(backbone, "sync_game", fake_sync)
    assert main(["--json", "sync", "pokemon"]) == 1
    payload = json.loads(capsys.readouterr().out)
    entry = payload["games"][0]
    assert entry["status"] == "failed"
    assert entry["error"] == "boom"


def test_sync_table_failed_without_error_text():
    table = sync_table([SyncResult(game="mtg", ok=False, error="")])
    assert table is not None


def test_sync_json_ok_shape():
    payload = json.loads(sync_json([SyncResult(game="mtg", ok=True, groups=3, products=10)]))
    assert payload["games"][0]["status"] == "ok"
    assert "error" not in payload["games"][0]


# ---------------------------------------------------------------------------
# First-run auto-sync on `topdeck price`


class _FirstRunAdapter:
    """One-hit fake game with no sync history, so price lookups auto-sync."""

    game_key = "fake"
    display_name = "Fake Game"
    trust_tier = "solid"
    source_name = "fakesource"

    def history_key(self, hit):
        return None

    def search(self, query):
        return [
            CardHit(
                card_id="1",
                name="Solo",
                set_code="S",
                set_name="Set",
                collector_number="1",
                url="https://example.com/card",
            )
        ]

    def get_prices(self, hit):
        return [
            Price(
                market="tcgplayer",
                currency="USD",
                condition="near-mint",
                printing="normal",
                price=1.23,
                as_of="2026-09-30T00:00:00",
                source="fakesource",
                source_url="https://example.com/source",
            )
        ]


@pytest.fixture
def first_run_game(monkeypatch):
    """Fake game with no sync history: its first price lookup auto-syncs."""
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": _FirstRunAdapter()})


def _fake_sync_ok(monkeypatch, groups=2, products=5):
    seen = []

    def fake_sync(game):
        seen.append(game)
        return SyncResult(game=game, ok=True, groups=groups, products=products)

    monkeypatch.setattr(backbone, "sync_game", fake_sync)
    return seen


def test_price_auto_syncs_never_synced_game(cache_home, first_run_game, capsys, monkeypatch):
    seen = _fake_sync_ok(monkeypatch)
    assert main(["price", "fake", "solo"]) == 0
    assert seen == ["fake"]
    captured = capsys.readouterr()
    assert 'Price data for "fake" was never synced. Syncing it now.' in captured.err
    assert "Synced fake: 2 groups, 5 products with prices." in captured.err
    assert "Solo" in captured.out


def test_price_auto_sync_silent_under_json(cache_home, first_run_game, capsys, monkeypatch):
    seen = _fake_sync_ok(monkeypatch)
    assert main(["--json", "price", "fake", "solo"]) == 0
    assert seen == ["fake"]
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["game"] == "fake"
    assert payload["result"]["card"]["name"] == "Solo"


def test_price_auto_sync_failure_falls_back(cache_home, first_run_game, capsys, monkeypatch):
    monkeypatch.setattr(
        backbone,
        "sync_game",
        lambda game: SyncResult(game=game, ok=False, error="boom"),
    )
    assert main(["price", "fake", "solo"]) == 0
    captured = capsys.readouterr()
    assert "Could not sync fake prices (boom)." in captured.err
    assert "Using live prices instead." in captured.err
    assert "Solo" in captured.out


def test_price_no_auto_sync_when_already_synced(cache_home, first_run_game, capsys, monkeypatch):
    _stamp_meta("fake", datetime.now(timezone.utc))

    def fake_sync(game):
        raise AssertionError("sync must not run for a fresh game")

    monkeypatch.setattr(backbone, "sync_game", fake_sync)
    assert main(["price", "fake", "solo"]) == 0
    assert capsys.readouterr().err == ""


def test_price_no_auto_sync_when_stale(cache_home, first_run_game, capsys, monkeypatch):
    # Only "never" triggers. A stale sync serves the live path; it does
    # not surprise anyone with a bulk download.
    _stamp_meta("fake", datetime.now(timezone.utc) - timedelta(hours=40))

    def fake_sync(game):
        raise AssertionError("sync must not run for a stale game")

    monkeypatch.setattr(backbone, "sync_game", fake_sync)
    assert main(["price", "fake", "solo"]) == 0
    assert "never synced" not in capsys.readouterr().err


def test_price_batch_auto_syncs_never_synced_game(
    cache_home, first_run_game, tmp_path, capsys, monkeypatch
):
    _fake_sync_ok(monkeypatch)
    deck = tmp_path / "deck.txt"
    deck.write_text("1 Solo\n")
    assert main(["price", "fake", "--file", str(deck)]) == 0
    captured = capsys.readouterr()
    assert "never synced" in captured.err
    assert "Solo" in captured.out


def test_sync_builds_trigram_search_index(cache_home, fake_net):
    from topdeck import trigrams

    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    suggestions = trigrams.suggest(backbone.db_path(), "riftbound", "Test Card Alpah")
    assert suggestions
    assert suggestions[0].name == "Test Card Alpha"
    assert suggestions[0].join_key == 101
    assert suggestions[0].set_name == "Set One"
    assert suggestions[0].set_code == "S1"
    # Products with no usable prices never enter the index either.
    conn = sqlite3.connect(backbone.db_path())
    try:
        indexed = {
            row[0] for row in conn.execute("SELECT name FROM names WHERE game = 'riftbound'")
        }
    finally:
        conn.close()
    assert "Priceless Card" not in indexed
    assert "Worthless Card" not in indexed


def test_sync_replaces_trigram_index_without_duplicates(cache_home, fake_net):
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    backbone.sync_game("riftbound")

    def _counts():
        conn = sqlite3.connect(backbone.db_path())
        try:
            names = conn.execute("SELECT COUNT(*) FROM names WHERE game = 'riftbound'").fetchone()[
                0
            ]
            postings = conn.execute(
                "SELECT COUNT(*) FROM postings WHERE game = 'riftbound'"
            ).fetchone()[0]
        finally:
            conn.close()
        return names, postings

    assert _counts() == (5, _counts()[1])
    assert _counts()[1] > 0


def test_schema_v4_has_search_tables(cache_home):
    conn = backbone._connect()
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()
    assert {"prices", "names", "postings"} <= tables


def test_tcgcsv_search_trigram_fallback_on_typo(cache_home, fake_net):
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    adapter = REGISTRY["riftbound"]
    hits = adapter.search("Test Card Alpah")
    assert hits
    assert hits[0].name == "Test Card Alpha"
    assert hits[0].card_id == "101"
    assert hits[0].set_name == "Set One"
    # The fallback hit resolves prices through the normal backbone path.
    prices = adapter.get_prices(hits[0])
    assert [p.price for p in prices] == [1.50]
    assert prices[0].provenance == "market"


def test_tcgcsv_search_exact_path_ignores_trigram_index(cache_home, fake_net):
    _riftbound_routes(fake_net)
    backbone.sync_game("riftbound")
    adapter = REGISTRY["riftbound"]
    hits = adapter.search("Test Card Alpha")
    assert hits
    assert hits[0].card_id == "101"
    # Exact path: the live price rows ride along in extra, which the
    # trigram fallback never provides.
    assert hits[0].extra.get("prices")


def test_tcgcsv_search_trigram_fallback_needs_no_sync(cache_home, fake_net):
    # No sync, no index, no crash: the typo just finds nothing.
    _riftbound_routes(fake_net)
    adapter = REGISTRY["riftbound"]
    assert adapter.search("Test Card Alpah") == []
