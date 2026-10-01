"""Tests for price outlier and new-set volatility flags. No network."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from rich.console import Console

from topdeck.adapters.base import CardHit, Price
from topdeck.output import (
    _median,
    batch_json,
    batch_table,
    is_volatile,
    json_payload,
    outlier_flags,
    price_table,
    print_result,
)


def _price(value, printing="normal"):
    return Price(
        market="tcgplayer",
        currency="USD",
        condition="near-mint",
        printing=printing,
        price=value,
        as_of="2026-09-30T00:00:00",
        source="fakesource",
        source_url="https://example.com/source",
    )


def _hit(name="Bolt", released_at=""):
    return CardHit(
        card_id="c1",
        name=name,
        set_code="LEA",
        set_name="Alpha",
        collector_number="161",
        released_at=released_at,
        url="https://example.com/card",
    )


def _console(width=120):
    return Console(width=width, record=True)


# ---------------------------------------------------------------------------
# Outlier flags


def test_median_handles_odd_and_even_counts():
    assert _median([1.0, 2.0, 3.0]) == 2.0
    assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_outlier_flags_absurd_high_row_with_iqr():
    prices = [_price(1.50), _price(1.75), _price(2.00), _price(39999.50, "manga")]
    assert outlier_flags(prices) == {3: "outlier"}


def test_outlier_flags_absurd_low_row_with_iqr():
    prices = [_price(0.01), _price(1.50), _price(1.75), _price(2.00)]
    assert outlier_flags(prices) == {0: "outlier"}


def test_outlier_flags_clean_printings():
    prices = [_price(1.50), _price(1.75), _price(2.00), _price(2.25)]
    assert outlier_flags(prices) == {}


def test_outlier_flags_identical_prices():
    assert outlier_flags([_price(2.00)] * 4) == {}


def test_outlier_flags_five_rows():
    prices = [_price(1.0), _price(2.0), _price(3.0), _price(4.0), _price(500.0)]
    assert outlier_flags(prices) == {4: "outlier"}


def test_outlier_flags_three_rows_use_median_ratio():
    assert outlier_flags([_price(1.50), _price(2.00), _price(39999.50)]) == {2: "outlier"}
    assert outlier_flags([_price(1.50), _price(2.00), _price(2.50)]) == {}
    assert outlier_flags([_price(0.01), _price(1.50), _price(2.00)]) == {0: "outlier"}


def test_outlier_flags_need_three_priced_rows():
    assert outlier_flags([_price(1.50), _price(39999.50)]) == {}
    assert outlier_flags([_price(1.50)]) == {}
    assert outlier_flags([]) == {}


def test_outlier_flags_skip_unpriced_rows():
    prices = [_price(None), _price(1.50), _price(2.00), _price(39999.50)]
    assert outlier_flags(prices) == {3: "outlier"}


# ---------------------------------------------------------------------------
# Volatility flags


def _now():
    return datetime.now(timezone.utc)


def test_is_volatile_recent_release():
    released = (_now() - timedelta(days=3)).date().isoformat()
    assert is_volatile(released, now=_now())


def test_is_volatile_boundary():
    now = _now()
    assert is_volatile((now - timedelta(days=14)).date().isoformat(), now=now)
    assert not is_volatile((now - timedelta(days=15)).date().isoformat(), now=now)


def test_is_volatile_old_release():
    released = (_now() - timedelta(days=30)).date().isoformat()
    assert not is_volatile(released, now=_now())


def test_is_volatile_future_release_is_not_volatile():
    released = (_now() + timedelta(days=2)).date().isoformat()
    assert not is_volatile(released, now=_now())


def test_is_volatile_bad_dates():
    assert not is_volatile("", now=_now())
    assert not is_volatile("not-a-date", now=_now())
    assert not is_volatile(None, now=_now())


def test_is_volatile_defaults_to_now():
    released = (_now() - timedelta(days=3)).date().isoformat()
    assert is_volatile(released)


# ---------------------------------------------------------------------------
# Table rendering


def test_price_table_marks_outlier_and_fits_80_columns():
    console = _console(width=80)
    prices = [_price(1.50), _price(1.75), _price(2.00), _price(39999.50, "manga")]
    console.print(price_table(_hit(), prices))
    out = console.export_text()
    assert "!" in out
    assert "$39,999.50 !" in out
    assert all(len(line) <= 80 for line in out.splitlines())


def test_price_table_no_marker_when_clean():
    console = _console()
    console.print(price_table(_hit(), [_price(1.50), _price(1.75), _price(2.00)]))
    assert "!" not in console.export_text()


def test_print_result_outlier_legend():
    console = _console()
    print_result(
        console,
        adapter_name="Fake Game",
        trust_tier="solid",
        hit=_hit(),
        prices=[_price(1.50), _price(2.00), _price(39999.50)],
    )
    out = console.export_text()
    assert "flagged as suspect, not removed" in out


def test_print_result_volatility_note():
    console = _console()
    released = (_now() - timedelta(days=3)).date().isoformat()
    print_result(
        console,
        adapter_name="Fake Game",
        trust_tier="solid",
        hit=_hit(released_at=released),
        prices=[_price(1.50)],
    )
    out = console.export_text()
    assert "still settling" in out
    assert released in out


def test_print_result_no_legends_for_old_clean_card():
    console = _console()
    print_result(
        console,
        adapter_name="Fake Game",
        trust_tier="solid",
        hit=_hit(released_at="2020-01-01"),
        prices=[_price(1.50)],
    )
    out = console.export_text()
    assert "flagged as suspect" not in out
    assert "still settling" not in out


def _batch_line(prices, unit, query="Bolt"):
    return SimpleNamespace(
        query=query,
        quantity=1,
        hit=_hit(),
        prices=prices,
        unit=unit,
        line_total=None,
        error=None,
        note=None,
    )


def test_batch_table_marks_outlier_unit():
    console = _console()
    prices = [_price(1.50), _price(2.00), _price(39999.50)]
    console.print(batch_table([_batch_line(prices, prices[2])]))
    out = console.export_text()
    assert "$39,999.50" in out
    assert "tcgplayer !" in out


def test_batch_table_no_marker_for_clean_unit():
    console = _console()
    prices = [_price(1.50), _price(2.00), _price(2.50)]
    console.print(batch_table([_batch_line(prices, prices[0])]))
    assert "!" not in console.export_text()


def test_batch_table_unit_missing_from_prices():
    console = _console()
    console.print(batch_table([_batch_line([_price(1.50)], _price(9.99))]))
    assert "!" not in console.export_text()


# ---------------------------------------------------------------------------
# JSON


def test_json_payload_carries_flags_and_volatility():
    released = (_now() - timedelta(days=3)).date().isoformat()
    payload = json.loads(
        json_payload(
            game="fake",
            query="bolt",
            chosen=_hit(released_at=released),
            prices=[_price(1.50), _price(2.00), _price(39999.50)],
            alternatives=[],
            recommended=True,
        )
    )
    assert payload["volatile"] is True
    assert payload["result"]["card"]["released_at"] == released
    assert [p["flags"] for p in payload["result"]["prices"]] == [[], [], ["outlier"]]


def test_json_payload_clean_card():
    payload = json.loads(
        json_payload(
            game="fake",
            query="bolt",
            chosen=_hit(released_at="2020-01-01"),
            prices=[_price(1.50)],
            alternatives=[],
            recommended=True,
        )
    )
    assert payload["volatile"] is False
    assert payload["result"]["prices"][0]["flags"] == []


def test_batch_json_carries_outlier_flags():
    prices = [_price(1.50), _price(2.00), _price(39999.50)]
    line = _batch_line(prices, prices[2])
    payload = json.loads(batch_json(game="fake", lines=[line], grand_total={}, warnings=[]))
    entry = payload["lines"][0]
    assert entry["unit_price"]["flags"] == ["outlier"]
    assert [p["flags"] for p in entry["prices"]] == [[], [], ["outlier"]]


def test_batch_json_error_line_has_no_flags():
    line = SimpleNamespace(
        query="Missing",
        quantity=1,
        hit=None,
        prices=[],
        unit=None,
        line_total=None,
        error="No matches.",
        note=None,
    )
    payload = json.loads(batch_json(game="fake", lines=[line], grand_total={}, warnings=[]))
    entry = payload["lines"][0]
    assert entry["unit_price"] is None
    assert entry["prices"] == []
