"""Tests for `topdeck trade`, with a fake game so no network is needed."""

import json

import pytest

import topdeck.adapters
import topdeck.cli
from topdeck.adapters.base import CardHit, Price
from topdeck.cli import _trade_verdict, main
from topdeck.net import SourceError


def _hit(name):
    return CardHit(
        card_id=name,
        name=name,
        set_code="S",
        set_name="Set",
        collector_number="1",
        url="https://example.com/card",
    )


def _price(value, currency="USD"):
    return Price(
        market="tcgplayer",
        currency=currency,
        condition="near-mint",
        printing="normal",
        price=value,
        as_of="2026-09-30T00:00:00",
        source="fakesource",
        source_url="https://example.com/source",
    )


class FakeTradeAdapter:
    game_key = "fake"
    display_name = "Fake Game"
    trust_tier = "solid"

    def __init__(self, entries):
        # entries: {query: ([hits], [prices] or "boom")}
        self._entries = entries

    def search(self, query):
        if query == "boom":
            raise SourceError("the price source is down.")
        hits, _prices = self._entries.get(query, ([], []))
        return list(hits)

    def get_prices(self, hit):
        for hits, prices in self._entries.values():
            if hit in hits:
                if prices == "boom":
                    raise SourceError("the price source is down.")
                return list(prices)
        return []

    def history_key(self, hit):
        return None


def _entries():
    return {
        "Bolt": ([_hit("Bolt")], [_price(10.0)]),
        "Shock": ([_hit("Shock")], [_price(10.4)]),
        "Lotus": ([_hit("Lotus")], [_price(12.0)]),
        "Recall": ([_hit("Recall")], [_price(20.0)]),
        "Mox": ([_hit("Mox")], []),
        "Euro": ([_hit("Euro")], [_price(9.0, currency="EUR")]),
        "Bad": ([_hit("Bad")], "boom"),
        "Multi": ([_hit("Multi A"), _hit("Multi B")], [_price(5.0)]),
    }


@pytest.fixture
def trade_game(monkeypatch):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": FakeTradeAdapter(_entries())})
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: False)
    monkeypatch.setattr(topdeck.cli, "_maybe_auto_sync", lambda adapter, as_json: None)


# --- Verdict math ---


def test_trade_verdict_fair_within_dollar():
    assert _trade_verdict(10.0, 10.5) == "Fair trade"


def test_trade_verdict_fair_within_percent():
    assert _trade_verdict(100.0, 104.0) == "Fair trade"


def test_trade_verdict_fair_at_threshold():
    assert _trade_verdict(100.0, 105.0) == "Fair trade"


def test_trade_verdict_side_a_up():
    assert _trade_verdict(100.0, 90.0) == "Side A is up $10.00 (10.0%)"


def test_trade_verdict_side_b_up():
    assert _trade_verdict(90.0, 100.0) == "Side B is up $10.00 (10.0%)"


def test_trade_verdict_nothing_priced():
    assert _trade_verdict(0.0, 0.0) == "Cannot judge: no priced cards."


# --- Terminal output ---


def test_trade_fair(capsys, trade_game):
    assert main(["trade", "Bolt", "--for", "Shock", "--game", "fake", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Side A (1 card):" in out
    assert "Total: $10.00" in out
    assert "Total: $10.40" in out
    assert "Difference: $0.40 (3.8%)" in out
    assert "Fair trade" in out


def test_trade_side_b_up(capsys, trade_game):
    assert main(["trade", "Bolt", "--for", "Lotus", "--game", "fake", "--yes"]) == 0
    assert "Side B is up $2.00 (16.7%)" in capsys.readouterr().out


def test_trade_side_a_up_multi_card(capsys, trade_game):
    assert main(["trade", "Lotus", "Recall", "--for", "Bolt", "--game", "fake", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Side A (2 cards):" in out
    assert "Side A is up $22.00 (68.8%)" in out


def test_trade_unpriced_excluded_with_yes(capsys, trade_game):
    assert main(["trade", "Bolt", "Mox", "--for", "Shock", "--game", "fake", "--yes"]) == 0
    out = capsys.readouterr().out
    assert 'No price for "Mox": excluded from the total.' in out
    assert "n/a" in out
    assert "Fair trade" in out


def test_trade_non_usd_leg_is_unpriced(capsys, trade_game):
    assert main(["trade", "Euro", "--for", "Bolt", "--game", "fake", "--yes"]) == 0
    out = capsys.readouterr().out
    assert 'No price for "Euro": excluded from the total.' in out
    assert "Side B is up $10.00 (100.0%)" in out


def test_trade_nothing_priced_terminal(capsys, trade_game):
    assert main(["trade", "Mox", "--for", "Mox", "--game", "fake", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Cannot judge: no priced cards." in out
    assert "Difference:" not in out


def test_trade_unpriced_decline_cancels(capsys, trade_game, monkeypatch):
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *args: "n")
    assert main(["trade", "Mox", "--for", "Bolt", "--game", "fake"]) == 1
    out = capsys.readouterr().out
    assert "Trade evaluation cancelled." in out
    assert "Fair trade" not in out


def test_trade_unpriced_accept_proceeds(capsys, trade_game, monkeypatch):
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *args: "y")
    assert main(["trade", "Mox", "--for", "Bolt", "--game", "fake"]) == 0
    assert "Side B is up $10.00 (100.0%)" in capsys.readouterr().out


# --- JSON output ---


def test_trade_json(capsys, trade_game):
    assert main(["--json", "trade", "Bolt", "--for", "Lotus", "--game", "fake"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["game"] == "fake"
    assert payload["side_a"][0]["card"]["name"] == "Bolt"
    assert payload["side_a"][0]["value"] == 10.0
    assert payload["total_a"] == 10.0
    assert payload["total_b"] == 12.0
    assert payload["difference"] == 2.0
    assert payload["difference_pct"] == 16.7
    assert payload["verdict"] == "Side B is up $2.00 (16.7%)"
    assert payload["warnings"] == []


def test_trade_json_unpriced(capsys, trade_game):
    assert main(["--json", "trade", "Mox", "--for", "Bolt", "--game", "fake"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["side_a"][0]["value"] is None
    assert payload["total_a"] == 0
    assert payload["difference_pct"] == 100.0
    assert len(payload["warnings"]) == 1


def test_trade_json_nothing_priced(capsys, trade_game):
    assert main(["--json", "trade", "Mox", "--for", "Mox", "--game", "fake"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["difference_pct"] is None
    assert payload["verdict"] == "Cannot judge: no priced cards."


# --- Error paths ---


def test_trade_unknown_game(capsys, trade_game):
    assert main(["trade", "Bolt", "--for", "Lotus", "--game", "nope"]) == 2
    assert 'Unknown game "nope"' in capsys.readouterr().out


def test_trade_no_match(capsys, trade_game):
    assert main(["trade", "Nobody", "--for", "Bolt", "--game", "fake", "--yes"]) == 0
    assert 'No matches for "Nobody"' in capsys.readouterr().out


def test_trade_search_error(capsys, trade_game):
    assert main(["trade", "boom", "--for", "Bolt", "--game", "fake", "--yes"]) == 1
    assert "Could not look that up" in capsys.readouterr().out


def test_trade_prices_error(capsys, trade_game):
    assert main(["trade", "Bad", "--for", "Bolt", "--game", "fake", "--yes"]) == 1
    assert "Could not fetch prices" in capsys.readouterr().out


def test_trade_pick_out_of_range(capsys, trade_game):
    assert main(["trade", "Multi", "--for", "Bolt", "--game", "fake", "--pick", "5"]) == 2
    assert "--pick 5 is out of range" in capsys.readouterr().out
