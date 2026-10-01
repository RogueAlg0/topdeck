"""Tests for terminal and JSON rendering. No network."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
from rich.console import Console

from topdeck.adapters.base import CardHit, Price
from topdeck.output import (
    _amount,
    _change_text,
    _check_amount,
    _money,
    _relative_as_of,
    _signal_text,
    batch_json,
    batch_table,
    candidate_table,
    change_pct,
    check_table,
    grand_total_lines,
    json_payload,
    linked,
    price_table,
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


def _price(value=1.23, currency="USD", provenance="market", source="fakesource"):
    return Price(
        market="tcgplayer",
        currency=currency,
        condition="near-mint",
        printing="normal",
        price=value,
        as_of="2026-09-30T00:00:00",
        source=source,
        source_url="https://example.com/source",
        provenance=provenance,
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


# ---------------------------------------------------------------------------
# Price table: 80-column friendly


def test_relative_as_of_renders_human_times():
    from datetime import datetime, timezone

    now = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
    cases = [
        ("2026-09-30T11:59:30+00:00", "just now"),
        ("2026-09-30T12:00:30+00:00", "just now"),  # future stamp: clock skew
        ("2026-09-30T12:00:00", "just now"),  # naive stamp read as UTC
        ("2026-09-30T11:55:00+00:00", "5m ago"),
        ("2026-09-30T11:55:00Z", "5m ago"),  # Zulu parses on every supported Python
        ("2026-09-30T10:00:00+00:00", "2h ago"),
        ("2026-09-27T12:00:00+00:00", "3d ago"),
        ("2026-08-01T12:00:00+00:00", "2026-08-01"),
        ("not-a-date", "not-a-date"),  # garbage passes through, never crashes
    ]
    for raw, expected in cases:
        assert _relative_as_of(raw, now=now) == expected


def test_relative_as_of_defaults_to_now():
    # Far enough in the past that any real "now" renders the plain date.
    assert _relative_as_of("2000-01-01T00:00:00+00:00") == "2000-01-01"


def test_price_table_shows_source_as_its_own_column():
    table = price_table(_hit(), [_price(), _price()])
    assert [c.header for c in table.columns] == [
        "Set",
        "Collector #",
        "Finish",
        "Market",
        "Source",
        "Price",
        "As of",
    ]


def test_price_table_source_column_names_each_leg():
    prices = [_price(source="tcgcsv"), _price(source="scryfall")]
    console = Console(width=100, record=True)
    console.print(price_table(_hit(), prices))
    out = console.export_text()
    assert "tcgcsv" in out
    assert "scryfall" in out


def test_price_table_caps_long_set_names():
    console = Console(width=80, record=True)
    long_name = "A Very Long Set Name That Would Wrap Across Many Lines Here"
    console.print(price_table(_hit(set_name=long_name), [_price()]))
    out = console.export_text()
    assert long_name not in out  # capped with an ellipsis, never wrapped
    assert "Market" in out and "As of" in out


# ---------------------------------------------------------------------------
# Check table: split set column, quiet rows dimmed, currencies labeled


def test_check_table_splits_set_into_own_column():
    console = _console()
    console.print(check_table([_row()]))
    out = console.export_text()
    assert "Set" in out
    assert "Alpha" in out


def test_check_amount_labels_non_usd_currency():
    assert _check_amount(1.50, "USD").plain == "$1.50"
    assert _check_amount(1.90, "EUR").plain == "\u20ac1.90 EUR"
    assert _check_amount(None, "EUR").plain == "n/a"


def test_change_text_negative_move():
    row = _row(delta_abs=-0.2, delta_pct=-20.0, spike=False, drop=True)
    assert _change_text(row).plain == "-$0.20 (-20.0%)"


def test_change_text_zero_move_is_quiet():
    row = _row(previous=1.0, current=1.0, delta_abs=0.0, delta_pct=0.0, spike=False)
    assert _change_text(row).plain == "no change"


def test_change_text_labels_eur():
    row = _row(currency="EUR", delta_abs=0.9, delta_pct=90.0)
    assert _change_text(row).plain == "+\u20ac0.90 (+90.0%) EUR"


def test_check_table_dims_unchanged_rows():
    row = _row(previous=1.0, current=1.0, delta_abs=0.0, delta_pct=0.0, spike=False)
    assert not row.alert
    console = _console()
    console.print(check_table([row]))
    out = console.export_text()
    assert "no change" in out
    assert "+$0.00" not in out


def test_check_table_error_row():
    row = _row(error="source is down", current=None, delta_abs=None, delta_pct=None)
    console = _console()
    console.print(check_table([row]))
    out = console.export_text()
    assert "error" in out
    assert "n/a" in out


# ---------------------------------------------------------------------------
# Price provenance and decklist batch rendering


_UNSET = object()


def _batch_line(query="Bolt", quantity=4, hit=_UNSET, unit=_UNSET, error=None, note=None):
    hit = _hit() if hit is _UNSET else hit
    unit = _price(2.5) if unit is _UNSET else unit
    total = quantity * unit.price if unit is not None and unit.price is not None else None
    return SimpleNamespace(
        query=query,
        quantity=quantity,
        hit=hit,
        prices=[unit] if unit is not None else [],
        unit=unit,
        line_total=total,
        error=error,
        note=note,
    )


def test_price_table_labels_mid_provenance():
    console = _console()
    console.print(price_table(_hit(), [_price(provenance="mid")]))
    out = console.export_text()
    assert "tcgplayer mid" in out


def test_price_table_market_provenance_has_no_suffix():
    console = _console()
    console.print(price_table(_hit(), [_price()]))
    out = console.export_text()
    assert "tcgplayer mid" not in out
    assert "tcgplayer" in out


def test_json_price_dict_carries_provenance():
    payload = json.loads(
        json_payload(
            game="fake",
            query="bolt",
            chosen=_hit(),
            prices=[_price(provenance="mid")],
            alternatives=[],
            recommended=True,
        )
    )
    assert payload["result"]["prices"][0]["provenance"] == "mid"


def test_batch_table_rows_and_totals():
    console = _console()
    lines = [
        _batch_line(),
        _batch_line(
            query="Missing",
            quantity=1,
            hit=None,
            unit=None,
            error="No matches. Try a shorter query or check the spelling.",
        ),
    ]
    console.print(batch_table(lines))
    out = console.export_text()
    assert "4x" in out
    assert "Bolt" in out
    assert "$2.50" in out
    assert "$10.00" in out
    assert "No matches" in out
    assert "n/a" in out


def test_batch_table_no_prices_row():
    console = _console()
    line = _batch_line(query="Empty", quantity=2, hit=_hit("Empty"), unit=None)
    console.print(batch_table([line]))
    assert "no prices right now" in console.export_text()


def test_batch_table_note_and_bare_hit():
    console = _console()
    bare = CardHit(card_id="c9", name="Bare", set_code="", set_name="", collector_number="", url="")
    line = _batch_line(query="Bare", quantity=1, hit=bare, note='2 matches; using top hit "Bare".')
    console.print(batch_table([line]))
    out = console.export_text()
    assert "top hit" in out


def test_batch_table_mid_unit_label():
    console = _console()
    console.print(batch_table([_batch_line(unit=_price(2.5, provenance="mid"))]))
    assert "tcgplayer mid" in console.export_text()


def test_grand_total_lines_per_currency():
    console = _console()
    for text in grand_total_lines({"USD": 12.0, "EUR": 0.8}):
        console.print(text)
    out = console.export_text()
    assert "Grand total: $12.00" in out
    assert "Grand total: \u20ac0.80" in out


def test_batch_json_shape():
    payload = json.loads(
        batch_json(
            game="fake",
            lines=[
                _batch_line(),
                _batch_line(query="Missing", quantity=1, hit=None, unit=None, error="No matches."),
            ],
            grand_total={"USD": 10.0},
            warnings=["line 2: skipped"],
        )
    )
    assert payload["command"] == "price"
    assert payload["mode"] == "batch"
    assert payload["game"] == "fake"
    assert len(payload["lines"]) == 2
    first = payload["lines"][0]
    assert first["query"] == "Bolt"
    assert first["quantity"] == 4
    assert first["card"]["name"] == "Bolt"
    assert first["unit_price"]["price"] == 2.5
    assert first["unit_price"]["provenance"] == "market"
    assert len(first["prices"]) == 1
    assert first["line_total"] == 10.0
    assert first["line_currency"] == "USD"
    assert first["error"] is None
    second = payload["lines"][1]
    assert second["card"] is None
    assert second["unit_price"] is None
    assert second["line_total"] is None
    assert second["line_currency"] is None
    assert payload["grand_total"] == {"USD": 10.0}
    assert payload["warnings"] == ["line 2: skipped"]


# --- % change inline on lookups ---


def _history_rows():
    # (date, market_cents, mid_cents, source), oldest first.
    return [
        ("2026-09-01", 100, 90, "tcgcsv"),
        ("2026-09-20", 120, 110, "tcgcsv"),
        ("2026-09-25", 140, 130, "tcgcsv"),
        ("2026-09-30", 150, 140, "tcgcsv"),
    ]


def test_change_pct_full_window():
    assert change_pct(_history_rows(), 30, today=date(2026, 9, 30)) == pytest.approx(50.0)


def test_change_pct_short_window_uses_window_oldest():
    # The 7-day window opens at 140, not at the 30-day oldest of 100.
    assert change_pct(_history_rows(), 7, today=date(2026, 9, 30)) == pytest.approx(
        (150 - 140) / 140 * 100
    )


def test_change_pct_thin_history_is_none():
    rows = _history_rows()
    assert change_pct(rows, 1, today=date(2026, 9, 30)) is None
    assert change_pct([], 30, today=date(2026, 9, 30)) is None
    assert change_pct(rows[:1], 30, today=date(2026, 9, 30)) is None


def test_change_pct_skips_unpriced_bad_and_future_rows():
    rows = [
        ("2026-09-25", None, None, "tcgcsv"),  # no price at all
        ("not-a-date", 100, 100, "tcgcsv"),  # unparseable
        ("2026-10-05", 999, 999, "tcgcsv"),  # in the future
        ("2026-09-28", 100, 90, "tcgcsv"),
        ("2026-09-30", 110, 100, "tcgcsv"),
    ]
    assert change_pct(rows, 30, today=date(2026, 9, 30)) == pytest.approx(10.0)


def test_change_pct_zero_oldest_is_none():
    rows = [("2026-09-25", 0, 0, "tcgcsv"), ("2026-09-30", 150, 140, "tcgcsv")]
    assert change_pct(rows, 30, today=date(2026, 9, 30)) is None


def test_change_pct_falls_back_to_mid():
    rows = [("2026-09-25", None, 100, "tcgcsv"), ("2026-09-30", None, 150, "tcgcsv")]
    assert change_pct(rows, 30, today=date(2026, 9, 30)) == pytest.approx(50.0)


def test_price_table_shows_change_columns_with_history():
    console = _console()
    console.print(
        price_table(_hit(), [_price(), _price(value=2.0)], changes={"7d": 7.14, "30d": 50.0})
    )
    out = console.export_text()
    assert "7d %" in out
    assert "30d %" in out
    assert "+7.1%" in out
    assert "+50.0%" in out


def test_price_table_changes_belong_to_headline_row_only():
    console = _console()
    console.print(
        price_table(_hit(), [_price(), _price(value=2.0)], changes={"7d": -12.35, "30d": 0.0})
    )
    out = console.export_text()
    assert out.count("-12.3%") == 1
    assert "+0.0%" in out


def test_price_table_thin_window_reads_na():
    console = _console()
    console.print(price_table(_hit(), [_price()], changes={"7d": None, "30d": 50.0}))
    out = console.export_text()
    assert "n/a" in out
    assert "+50.0%" in out


def test_price_table_without_history_has_no_change_columns():
    console = _console()
    console.print(price_table(_hit(), [_price()]))
    out = console.export_text()
    assert "7d %" not in out
    assert "30d %" not in out


def test_print_result_passes_changes_to_table():
    console = _console()
    print_result(
        console,
        adapter_name="Fake",
        trust_tier="solid",
        hit=_hit(),
        prices=[_price()],
        changes={"7d": 5.0, "30d": None},
    )
    out = console.export_text()
    assert "7d %" in out
    assert "+5.0%" in out


def test_json_payload_includes_changes_when_given():
    payload = json.loads(
        json_payload(
            game="fake",
            query="Bolt",
            chosen=_hit(),
            prices=[_price()],
            alternatives=[],
            recommended=True,
            changes={"7d": 7.142, "30d": None},
        )
    )
    assert payload["result"]["change_7d_pct"] == 7.14
    assert payload["result"]["change_30d_pct"] is None


def test_json_payload_omits_changes_without_history():
    payload = json.loads(
        json_payload(
            game="fake",
            query="Bolt",
            chosen=_hit(),
            prices=[_price()],
            alternatives=[],
            recommended=True,
        )
    )
    assert "change_7d_pct" not in payload["result"]
    assert "change_30d_pct" not in payload["result"]
