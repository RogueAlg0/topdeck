"""Tests for the portfolio: lots, valuation, and the CLI. All network is faked."""

from __future__ import annotations

import json
import os

import pytest

from topdeck import adapters as game_adapters
from topdeck.adapters.base import CardHit, GameAdapter, Price
from topdeck.cli import main
from topdeck.net import SourceError
from topdeck.output import (
    holdings_table,
    portfolio_list_json,
    portfolio_lot_json,
    portfolio_lots_table,
    portfolio_summary_lines,
)
from topdeck.portfolio import (
    Holding,
    Lot,
    PortfolioStore,
    _default_path,
    join_key_for,
    price_holding,
    summarize,
)


class FakeAdapter(GameAdapter):
    game_key = "mtg"
    display_name = "Magic: The Gathering"
    trust_tier = "solid"
    source_name = "fake"

    def __init__(self):
        self.hits: list[CardHit] = []
        self.prices: dict[str, list[Price]] = {}
        self.fail_search: str | None = None
        self.fail_prices: str | None = None

    def search(self, query: str) -> list[CardHit]:
        if self.fail_search:
            raise SourceError(self.fail_search)
        return self.hits

    def get_prices(self, hit: CardHit) -> list[Price]:
        if self.fail_prices:
            raise SourceError(self.fail_prices)
        return self.prices.get(hit.card_id, [])


def _price(
    value: float | None,
    market: str = "tcgplayer",
    currency: str = "USD",
    printing: str = "normal",
) -> Price:
    return Price(
        market=market,
        currency=currency,
        condition="near-mint",
        printing=printing,
        price=value,
        as_of="2026-09-30T00:00:00Z",
        source="fake",
    )


def _hit(
    card_id: str = "card-1",
    name: str = "Lightning Bolt",
    set_name: str = "Alpha",
    extra: dict | None = None,
) -> CardHit:
    return CardHit(
        card_id=card_id,
        name=name,
        set_code="LEA",
        set_name=set_name,
        collector_number="161",
        extra=extra or {},
    )


def _lot(**kwargs) -> Lot:
    defaults = dict(
        id=1,
        game="mtg",
        card_id="card-1",
        join_key=123,
        name="Lightning Bolt",
        set_name="Alpha",
        qty=4,
        purchase_price=1.0,
        added_at=0.0,
    )
    defaults.update(kwargs)
    return Lot(**defaults)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPDECK_DATA_DIR", str(tmp_path))
    return PortfolioStore()


@pytest.fixture
def fake(monkeypatch):
    adapter = FakeAdapter()
    monkeypatch.setitem(game_adapters.REGISTRY, "mtg", adapter)
    return adapter


@pytest.fixture
def fresh_sync(monkeypatch):
    monkeypatch.setattr("topdeck.backbone.sync_status", lambda game: "fresh")


@pytest.fixture
def stale_sync(monkeypatch):
    monkeypatch.setattr("topdeck.backbone.sync_status", lambda game: "stale")


# ---------------------------------------------------------------------------
# Join keys


def test_join_key_prefers_tcgplayer_id():
    hit = _hit(extra={"tcgplayer_id": 42, "product_id": 7})
    assert join_key_for(hit) == 42


def test_join_key_falls_back_to_product_id():
    assert join_key_for(_hit(extra={"product_id": 7})) == 7


def test_join_key_falls_back_to_numeric_card_id():
    assert join_key_for(_hit(card_id="987")) == 987


def test_join_key_none_when_unparseable():
    hit = _hit(card_id="card-1", extra={"tcgplayer_id": "not-an-int"})
    assert join_key_for(hit) is None


# ---------------------------------------------------------------------------
# Store


def test_add_and_round_trip(store):
    lot = store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 4, 1.25)
    assert lot.id > 0
    assert lot.qty == 4
    assert lot.purchase_price == 1.25
    assert lot.join_key == 123
    found = store.get(lot.id)
    assert found is not None
    assert found.name == "Lightning Bolt"


def test_get_missing_returns_none(store):
    assert store.get(999) is None


def test_same_card_twice_is_two_lots(store):
    store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 4, 1.25)
    store.add("mtg", "card-1", 123, "Lightning Bolt", "Beta", 2, 2.00)
    assert store.count() == 2
    assert len(store.list()) == 2


def test_find_by_name_is_case_insensitive(store):
    store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 4, 1.25)
    assert len(store.find_by_name("lightning bolt")) == 1
    assert store.find_by_name("Black Lotus") == []


def test_remove_returns_lot_and_deletes(store):
    lot = store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 4, 1.25)
    removed = store.remove(lot.id)
    assert removed is not None
    assert removed.name == "Lightning Bolt"
    assert store.list() == []


def test_remove_missing_returns_none(store):
    assert store.remove(999) is None


# ---------------------------------------------------------------------------
# Store location: XDG data dir with honest fallbacks


def test_default_path_honors_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TOPDECK_DATA_DIR", str(tmp_path))
    assert _default_path() == os.path.join(str(tmp_path), "portfolio.sqlite")


def test_default_path_honors_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("TOPDECK_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _default_path() == os.path.join(str(tmp_path), "topdeck", "portfolio.sqlite")


def test_default_path_falls_back_to_dot_topdeck(monkeypatch, tmp_path):
    monkeypatch.delenv("TOPDECK_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    real_makedirs = os.makedirs

    def boom(path, exist_ok=False):
        if "topdeck" in path and ".topdeck" not in path:
            raise OSError("read-only")
        return real_makedirs(path, exist_ok=exist_ok)

    monkeypatch.setattr(os, "makedirs", boom)
    assert _default_path() == os.path.join(str(tmp_path), ".topdeck", "portfolio.sqlite")


def test_default_path_returns_first_when_nothing_writable(monkeypatch, tmp_path):
    monkeypatch.delenv("TOPDECK_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    def boom(path, exist_ok=False):
        raise OSError("read-only")

    monkeypatch.setattr(os, "makedirs", boom)
    assert _default_path().endswith(os.path.join("topdeck", "portfolio.sqlite"))


# ---------------------------------------------------------------------------
# Re-pricing one holding


def test_price_holding_success(fake, fresh_sync):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.20)]}
    holding = price_holding(_lot())
    assert holding.error is None
    assert holding.price.price == 1.20
    assert holding.stale_sidecar is False
    assert holding.value == pytest.approx(4.80)
    assert holding.cost == pytest.approx(4.00)


def test_price_holding_marks_stale_sidecar(fake, stale_sync):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.20)]}
    holding = price_holding(_lot())
    assert holding.stale_sidecar is True
    assert holding.price.price == 1.20  # live path still prices it


def test_price_holding_unknown_game(stale_sync):
    holding = price_holding(_lot(game="bogus"))
    assert "unknown game" in holding.error
    assert holding.value is None


def test_price_holding_search_error(fake, fresh_sync):
    fake.fail_search = "source is down"
    holding = price_holding(_lot())
    assert holding.error == "source is down"


def test_price_holding_no_hits(fake, fresh_sync):
    fake.hits = []
    holding = price_holding(_lot())
    assert "no longer listed" in holding.error


def test_price_holding_prices_error(fake, fresh_sync):
    fake.hits = [_hit()]
    fake.fail_prices = "prices exploded"
    holding = price_holding(_lot())
    assert holding.error == "prices exploded"


def test_price_holding_no_prices_is_error(fake, fresh_sync):
    fake.hits = [_hit()]
    fake.prices = {}
    holding = price_holding(_lot())
    assert holding.error == "no prices right now"


def test_price_holding_prefers_exact_card_id_but_falls_back(fake, fresh_sync):
    fake.hits = [_hit("card-9", "Lightning Bolt")]
    fake.prices = {"card-9": [_price(2.00)]}
    holding = price_holding(_lot(card_id="card-1"))
    assert holding.price.price == 2.00
    assert holding.note == "exact printing no longer listed; showing closest match"


def test_price_holding_picks_headline_usd_leg(fake, fresh_sync):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(9.0, currency="EUR"), _price(1.50)]}
    holding = price_holding(_lot())
    assert holding.price.currency == "USD"
    assert holding.price.price == 1.50


def test_holding_value_none_without_price():
    holding = Holding(_lot(), None, False, error="boom")
    assert holding.value is None


def test_holding_value_none_when_price_is_none():
    holding = Holding(_lot(), _price(None), False)
    assert holding.value is None


# ---------------------------------------------------------------------------
# Summaries


def test_summarize_empty():
    summary = summarize([])
    assert summary.value == 0
    assert summary.cost == 0
    assert summary.pnl == 0
    assert summary.pnl_pct is None
    assert summary.excluded == 0
    assert summary.total_lots == 0
    assert summary.total_qty == 0


def test_summarize_adds_value_cost_pnl():
    holdings = [
        Holding(_lot(qty=4, purchase_price=1.00), _price(1.50), False),
        Holding(_lot(id=2, qty=1, purchase_price=10.00), _price(8.00), False),
    ]
    summary = summarize(holdings)
    assert summary.value == pytest.approx(14.00)
    assert summary.cost == pytest.approx(14.00)
    assert summary.pnl == pytest.approx(0.00)
    assert summary.pnl_pct == pytest.approx(0.0)
    assert summary.excluded == 0
    assert summary.total_lots == 2
    assert summary.total_qty == 5


def test_summarize_profit_and_pct():
    holding = Holding(_lot(qty=2, purchase_price=5.00), _price(7.50), False)
    summary = summarize([holding])
    assert summary.pnl == pytest.approx(5.00)
    assert summary.pnl_pct == pytest.approx(50.0)


def test_summarize_zero_cost_has_no_pct():
    holding = Holding(_lot(qty=2, purchase_price=0.00), _price(7.50), False)
    summary = summarize([holding])
    assert summary.pnl == pytest.approx(15.00)
    assert summary.pnl_pct is None


def test_summarize_excludes_unpriced_and_non_usd():
    holdings = [
        Holding(_lot(), _price(1.50), False),
        Holding(_lot(id=2), None, False, error="boom"),
        Holding(_lot(id=3), _price(9.0, currency="EUR"), False),
        Holding(_lot(id=4), _price(None), False),
    ]
    summary = summarize(holdings)
    assert summary.value == pytest.approx(6.00)
    assert summary.excluded == 3
    assert summary.total_lots == 4


# ---------------------------------------------------------------------------
# CLI: portfolio add


def test_portfolio_add_records_lot(store, fake, capsys):
    fake.hits = [_hit(extra={"tcgplayer_id": 42})]
    assert main(["portfolio", "add", "mtg", "Lightning Bolt", "4", "1.25", "--first"]) == 0
    out = capsys.readouterr().out
    assert 'Added 4x "Lightning Bolt"' in out
    assert "$1.25 per copy" in out
    lots = store.list()
    assert len(lots) == 1
    assert lots[0].qty == 4
    assert lots[0].purchase_price == 1.25
    assert lots[0].join_key == 42
    assert lots[0].game == "mtg"


def test_portfolio_add_json(store, fake, capsys):
    fake.hits = [_hit()]
    assert main(["--json", "portfolio", "add", "mtg", "Bolt", "2", "3.50", "--first"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "portfolio"
    assert payload["action"] == "added"
    assert payload["lot"]["qty"] == 2
    assert payload["lot"]["purchase_price"] == 3.5


def test_portfolio_add_same_card_twice_is_two_lots(store, fake):
    fake.hits = [_hit()]
    assert main(["portfolio", "add", "mtg", "Bolt", "4", "1.00", "--first"]) == 0
    assert main(["portfolio", "add", "mtg", "Bolt", "2", "2.00", "--first"]) == 0
    assert store.count() == 2


def test_portfolio_add_unknown_game(store, capsys):
    assert main(["portfolio", "add", "atlantis", "Blue-Eyes", "1", "1.00"]) == 2
    assert store.list() == []


@pytest.mark.parametrize("bad", ["0", "-3", "2.5", "abc", ""])
def test_portfolio_add_rejects_bad_qty(store, fake, capsys, bad):
    fake.hits = [_hit()]
    assert main(["portfolio", "add", "mtg", "Bolt", bad, "1.00", "--first"]) == 2
    assert "not a usable quantity" in capsys.readouterr().out
    assert store.list() == []


@pytest.mark.parametrize("bad", ["-1", "abc", "inf", "nan", ""])
def test_portfolio_add_rejects_bad_price(store, fake, capsys, bad):
    fake.hits = [_hit()]
    assert main(["portfolio", "add", "mtg", "Bolt", "4", bad, "--first"]) == 2
    assert "not a usable price" in capsys.readouterr().out
    assert store.list() == []


def test_portfolio_add_allows_zero_price(store, fake):
    fake.hits = [_hit()]
    assert main(["portfolio", "add", "mtg", "Bolt", "1", "0", "--first"]) == 0
    assert store.list()[0].purchase_price == 0.0


def test_portfolio_add_search_source_error(store, fake, capsys):
    fake.fail_search = "source is down"
    assert main(["portfolio", "add", "mtg", "Bolt", "1", "1.00", "--first"]) == 1
    assert "Could not look that up" in capsys.readouterr().out
    assert store.list() == []


def test_portfolio_add_search_empty(store, fake, capsys):
    fake.hits = []
    assert main(["portfolio", "add", "mtg", "zzz", "1", "1.00", "--first"]) == 0
    assert "No matches" in capsys.readouterr().out
    assert store.list() == []


def test_portfolio_add_pick_out_of_range(store, fake, capsys):
    fake.hits = [_hit("card-1", "Bolt", "Alpha"), _hit("card-2", "Bolt", "Beta")]
    assert main(["portfolio", "add", "mtg", "Bolt", "1", "1.00", "--pick", "9"]) == 2
    assert "out of range" in capsys.readouterr().out
    assert store.list() == []


def test_portfolio_add_pick_selects_numbered_match(store, fake):
    fake.hits = [_hit("card-1", "Bolt", "Alpha"), _hit("card-2", "Bolt", "Beta")]
    assert main(["portfolio", "add", "mtg", "Bolt", "1", "1.00", "--pick", "2"]) == 0
    assert store.list()[0].card_id == "card-2"


# ---------------------------------------------------------------------------
# CLI: portfolio list


def test_portfolio_list_empty_is_friendly(store, capsys):
    assert main(["portfolio"]) == 0
    assert "portfolio is empty" in capsys.readouterr().out


def test_portfolio_list_explicit_list(store, capsys):
    assert main(["portfolio", "list"]) == 0
    assert "portfolio is empty" in capsys.readouterr().out


def test_portfolio_list_empty_json(store, capsys):
    assert main(["--json", "portfolio"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "list"
    assert payload["holdings"] == []
    assert payload["summary"]["total_lots"] == 0


def test_portfolio_list_shows_totals_and_holdings(store, fake, fresh_sync, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.50)]}
    store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 4, 1.00)
    assert main(["portfolio"]) == 0
    out = capsys.readouterr().out
    assert "Total value:" in out and "$6.00" in out
    assert "Total cost:" in out and "$4.00" in out
    assert "Unrealized P&L:" in out and "+$2.00" in out and "+50.0%" in out
    assert "Lightning Bolt" in out
    assert "sidecar stale" not in out


def test_portfolio_list_marks_stale_sidecar(store, fake, stale_sync, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.50)]}
    store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 4, 1.00)
    assert main(["portfolio"]) == 0
    # The note wraps in narrow terminals; the marker itself must be shown.
    assert "sidecar stale" in capsys.readouterr().out


def test_portfolio_list_excludes_unpriced_with_note(store, fake, fresh_sync, capsys):
    fake.hits = [_hit()]
    fake.prices = {}
    store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 4, 1.00)
    assert main(["portfolio"]) == 0
    out = capsys.readouterr().out
    assert "1 holding has no current USD price" in out
    assert "excluded from the totals" in out
    assert "no prices right now" in out


def test_portfolio_list_plural_excluded_note(store, fake, fresh_sync, capsys):
    fake.hits = [_hit()]
    fake.prices = {}
    store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 4, 1.00)
    store.add("mtg", "card-2", 124, "Black Lotus", "Alpha", 1, 100.00)
    assert main(["portfolio"]) == 0
    assert "2 holdings have no current USD price" in capsys.readouterr().out


def test_portfolio_list_json(store, fake, fresh_sync, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.50)]}
    store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 4, 1.00)
    assert main(["--json", "portfolio"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "portfolio"
    holding = payload["holdings"][0]
    assert holding["lot"]["name"] == "Lightning Bolt"
    assert holding["current"]["price"] == 1.50
    assert holding["value"] == pytest.approx(6.00)
    assert holding["cost"] == pytest.approx(4.00)
    assert holding["stale_sidecar"] is False
    assert holding["error"] is None
    summary = payload["summary"]
    assert summary["total_value_usd"] == pytest.approx(6.00)
    assert summary["total_cost_usd"] == pytest.approx(4.00)
    assert summary["pnl_usd"] == pytest.approx(2.00)
    assert summary["pnl_pct"] == pytest.approx(50.0)
    assert summary["excluded_holdings"] == 0


def test_portfolio_list_json_stale_flag(store, fake, stale_sync, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.50)]}
    store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 4, 1.00)
    assert main(["--json", "portfolio"]) == 0
    assert json.loads(capsys.readouterr().out)["holdings"][0]["stale_sidecar"] is True


def test_portfolio_list_negative_pnl(store, fake, fresh_sync, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(0.50)]}
    store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 4, 1.00)
    assert main(["portfolio"]) == 0
    out = capsys.readouterr().out
    assert "-$2.00" in out
    assert "-50.0%" in out


def test_portfolio_list_zero_cost_shows_na_pct(store, fake, fresh_sync, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.50)]}
    store.add("mtg", "card-1", 123, "Lightning Bolt", "Alpha", 2, 0.00)
    assert main(["portfolio"]) == 0
    assert "(n/a)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI: portfolio remove


def test_portfolio_remove_by_id(store, fake, capsys):
    fake.hits = [_hit()]
    main(["portfolio", "add", "mtg", "Bolt", "4", "1.00", "--first"])
    capsys.readouterr()
    lot_id = store.list()[0].id
    assert main(["portfolio", "remove", str(lot_id)]) == 0
    assert "Removed 4x" in capsys.readouterr().out
    assert store.list() == []


def test_portfolio_remove_by_name(store, fake, capsys):
    fake.hits = [_hit()]
    main(["portfolio", "add", "mtg", "Bolt", "4", "1.00", "--first"])
    capsys.readouterr()
    assert main(["portfolio", "remove", "lightning", "bolt"]) == 0
    assert store.list() == []


def test_portfolio_remove_json(store, fake, capsys):
    fake.hits = [_hit()]
    main(["portfolio", "add", "mtg", "Bolt", "4", "1.00", "--first"])
    capsys.readouterr()
    assert main(["--json", "portfolio", "remove", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "removed"
    assert payload["lot"]["name"] == "Lightning Bolt"


def test_portfolio_remove_missing_exits_one(store, capsys):
    assert main(["portfolio", "remove", "Black Lotus"]) == 1
    assert "not in your portfolio" in capsys.readouterr().out


def test_portfolio_remove_ambiguous_name_exits_two(store, capsys):
    store.add("mtg", "card-1", 123, "Bolt", "Alpha", 4, 1.00)
    store.add("mtg", "card-2", 124, "Bolt", "Beta", 2, 2.00)
    assert main(["portfolio", "remove", "Bolt"]) == 2
    assert "by ID" in capsys.readouterr().out
    assert len(store.list()) == 2


# ---------------------------------------------------------------------------
# Output units


def test_holdings_table_sorts_biggest_first_and_unpriced_last():
    import io

    from rich.console import Console

    holdings = [
        Holding(_lot(id=1, qty=1, purchase_price=1.0), None, False, error="boom"),
        Holding(_lot(id=2, qty=1, purchase_price=1.0), _price(2.00), False),
        Holding(_lot(id=3, qty=4, purchase_price=1.0), _price(1.50), False),
    ]
    buf = io.StringIO()
    Console(file=buf, width=120).print(holdings_table(holdings))
    out = buf.getvalue()
    assert out.index("$6.00") < out.index("$2.00") < out.index("n/a")


def test_holdings_table_marks_stale_and_notes():
    import io

    from rich.console import Console

    holdings = [
        Holding(
            _lot(set_name="Alpha"),
            _price(1.50),
            True,
            note="exact printing no longer listed; showing closest match",
        ),
    ]
    buf = io.StringIO()
    Console(file=buf, width=120).print(holdings_table(holdings))
    out = buf.getvalue()
    assert "sidecar stale; live price" in out
    assert "closest match" in out


def test_holdings_table_labels_non_usd():
    import io

    from rich.console import Console

    holdings = [Holding(_lot(qty=2, purchase_price=1.0), _price(3.00, currency="EUR"), False)]
    buf = io.StringIO()
    Console(file=buf, width=120).print(holdings_table(holdings))
    out = buf.getvalue()
    assert "EUR" in out
    assert "n/a" in out  # P&L is undefined across currencies


def test_portfolio_lots_table_renders():
    console_out = portfolio_lots_table([_lot()])
    assert console_out.row_count == 1


def test_portfolio_summary_lines_colors():
    import io

    from rich.console import Console

    def rendered(summary):
        buf = io.StringIO()
        console = Console(file=buf, width=80, force_terminal=True, color_system="truecolor")
        for line in portfolio_summary_lines(summary):
            console.print(line)
        return buf.getvalue()

    up = summarize([Holding(_lot(qty=2, purchase_price=5.0), _price(7.50), False)])
    out = rendered(up)
    assert "+$5.00" in out and "+50.0%" in out
    assert "32m" in out  # green (bold green renders as 1;32)

    down = summarize([Holding(_lot(qty=2, purchase_price=5.0), _price(2.50), False)])
    out = rendered(down)
    assert "-$5.00" in out and "-50.0%" in out
    assert "31m" in out  # red (bold red renders as 1;31)

    flat = summarize([Holding(_lot(qty=2, purchase_price=5.0), _price(5.00), False)])
    assert "+$0.00" in rendered(flat)

    free = summarize([Holding(_lot(qty=2, purchase_price=0.0), _price(5.00), False)])
    assert "(n/a)" in rendered(free)


def test_portfolio_lot_json_shape():
    payload = json.loads(portfolio_lot_json("removed", _lot()))
    assert payload["command"] == "portfolio"
    assert payload["action"] == "removed"
    assert payload["lot"]["join_key"] == 123


def test_portfolio_list_json_shape():
    holdings = [Holding(_lot(), _price(1.50), True)]
    payload = json.loads(portfolio_list_json(holdings, summarize(holdings)))
    assert payload["action"] == "list"
    assert payload["holdings"][0]["stale_sidecar"] is True
    assert payload["holdings"][0]["current"]["market"] == "tcgplayer"
    assert payload["summary"]["excluded_holdings"] == 0
