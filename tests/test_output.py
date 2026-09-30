"""Tests for terminal and JSON rendering. No network."""

from __future__ import annotations

import json

from rich.console import Console

from topdeck.adapters.base import CardHit, Price
from topdeck.output import (
    _amount,
    _money,
    _signal_text,
    candidate_table,
    check_table,
    linked,
    print_result,
    watch_json,
)
from topdeck.watch import CheckRow, Watch


def _console():
    return Console(width=120, record=True)


def _hit(name="Bolt", set_name="Alpha"):
    return CardHit(
        card_id="c1",
        name=name,
        set_code="LEA",
        set_name=set_name,
        collector_number="161",
        url="https://example.com/card",
    )


def _price(value=1.23, currency="USD"):
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


def _watch(name="Bolt"):
    return Watch(
        id=1,
        game="mtg",
        card_id="c1",
        name=name,
        set_name="Alpha",
        target_price=None,
        added_at=0.0,
    )


def _row(**kwargs):
    base = dict(
        watch=_watch(),
        previous=1.0,
        current=1.2,
        currency="USD",
        source="fake",
        delta_abs=0.2,
        delta_pct=20.0,
        spike=True,
        drop=False,
        target_hit=False,
    )
    base.update(kwargs)
    return CheckRow(**base)


def test_linked_without_url_is_plain_text():
    text = linked("Bolt", "")
    assert text.plain == "Bolt"
    assert "link" not in str(text.style or "")


def test_linked_with_url_is_hyperlink():
    text = linked("Bolt", "https://example.com")
    assert "https://example.com" in str(text.style)


def test_money_none_price_is_na():
    assert _money(_price(value=None)) == "n/a"


def test_amount_unknown_currency_uses_code():
    assert _amount(5.0, "CHF") == "CHF 5.00"
    assert _amount(None, "USD") == "n/a"


def test_candidate_table_marks_recommended_row():
    console = _console()
    console.print(candidate_table([_hit("A"), _hit("B")], numbers=[1, 2], recommended=2))
    out = console.export_text()
    assert out.count("recommended") == 1
    assert "B" in out


def test_print_result_warns_on_beta_trust_tier():
    console = _console()
    print_result(
        console, adapter_name="Fake Game", trust_tier="beta", hit=_hit(), prices=[_price()]
    )
    out = console.export_text()
    assert "Heads up" in out and "beta" in out


def test_print_result_warns_on_experimental_trust_tier():
    console = _console()
    print_result(
        console, adapter_name="Fake Game", trust_tier="experimental", hit=_hit(), prices=[_price()]
    )
    assert "experimental" in console.export_text()


def test_print_result_says_so_when_no_prices():
    console = _console()
    print_result(console, adapter_name="Fake Game", trust_tier="solid", hit=_hit(), prices=[])
    assert "No prices found" in console.export_text()


def test_signal_text_drop():
    assert "drop" in _signal_text(_row(spike=False, drop=True))


def test_signal_text_new_card():
    assert "new" in _signal_text(_row(previous=None, delta_abs=None, delta_pct=None, spike=False))


def test_signal_text_error():
    assert "error" in _signal_text(_row(error="source is down"))


def test_check_table_shows_note():
    console = _console()
    console.print(check_table([_row(note="closest match shown")]))
    assert "closest match shown" in console.export_text()


def test_watch_json_merges_extra():
    payload = json.loads(watch_json("list", [], extra={"count": 3}))
    assert payload["count"] == 3
    assert payload["action"] == "list"
