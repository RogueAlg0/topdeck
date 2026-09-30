"""Tests for decklist batch pricing (`topdeck price --file` / stdin). No network."""

from __future__ import annotations

import io
import json
import sys

import pytest

import topdeck.adapters
import topdeck.cli
from topdeck.adapters.base import CardHit, Price
from topdeck.cli import BatchLine, main, parse_decklist
from topdeck.net import SourceError


def _hit(name, set_name="Alpha", collector_number="161", price=2.5):
    return CardHit(
        card_id=name,
        name=name,
        set_code="S",
        set_name=set_name,
        collector_number=collector_number,
        url="https://example.com/card",
        extra={"price": price},
    )


def _price(value=2.5, currency="USD", provenance="market"):
    return Price(
        market="tcgplayer",
        currency=currency,
        condition="near-mint",
        printing="normal",
        price=value,
        as_of="2026-09-30T00:00:00",
        source="fakesource",
        source_url="https://example.com/source",
        provenance=provenance,
    )


class BatchAdapter:
    """Fake game adapter with per-name hits and controllable prices."""

    game_key = "fake"
    display_name = "Fake Game"
    trust_tier = "solid"
    source_name = "fakesource"

    def __init__(self, hits_by_query):
        self._hits = hits_by_query
        self.searches: list[str] = []
        self.price_map: dict[str, list[Price]] = {}

    def search(self, query):
        self.searches.append(query)
        result = self._hits.get(query, [])
        if result == "boom":
            raise SourceError("the price source is down.")
        return list(result)

    def get_prices(self, hit):
        if hit.name == "Priceless":
            raise SourceError("prices exploded")
        return self.price_map.get(hit.name, [_price(hit.extra.get("price", 2.5))])


@pytest.fixture
def batch_game(monkeypatch):
    adapter = BatchAdapter(
        {
            "Lightning Bolt": [_hit("Lightning Bolt", price=3.0)],
            "Giant Growth": [_hit("Giant Growth", price=0.5)],
            "Ambiguous": [
                _hit("Ambiguous One", set_name="New", price=1.0),
                _hit("Ambiguous Two", set_name="Old", price=2.0),
            ],
            "Missing": [],
            "Boom": "boom",
            "Priceless": [_hit("Priceless")],
            "Empty": [_hit("Empty")],
        }
    )
    adapter.price_map = {
        "Giant Growth": [_price(0.4, currency="EUR", provenance="market")],
        "Empty": [],
    }
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": adapter})
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: False)
    return adapter


def _deck(tmp_path, text):
    deck = tmp_path / "deck.txt"
    deck.write_text(text)
    return deck


# ---------------------------------------------------------------------------
# Decklist parsing


def test_parse_decklist_quantities():
    entries, warnings = parse_decklist(
        ["4 Lightning Bolt", "4x Giant Growth", "2X Dark Ritual", "Counterspell"]
    )
    assert entries == [
        (4, "Lightning Bolt"),
        (4, "Giant Growth"),
        (2, "Dark Ritual"),
        (1, "Counterspell"),
    ]
    assert warnings == []


def test_parse_decklist_skips_blanks_and_comments():
    entries, warnings = parse_decklist(["", "   ", "# sideboard", "  # indented", "4 Bolt"])
    assert entries == [(4, "Bolt")]
    assert warnings == []


def test_parse_decklist_broken_lines_warn_with_line_numbers():
    entries, warnings = parse_decklist(["4 Bolt", "4x", "0 Growth", "2 Ritual"])
    assert entries == [(4, "Bolt"), (2, "Ritual")]
    assert len(warnings) == 2
    assert warnings[0].startswith("line 2:")
    assert "(skipped)" in warnings[0]
    assert warnings[1].startswith("line 3:")


def test_batch_line_total_none_without_unit():
    bare = BatchLine(
        query="X", quantity=4, hit=None, prices=[], unit=None, error="No matches.", note=None
    )
    assert bare.line_total is None
    no_price = BatchLine(
        query="X", quantity=4, hit=None, prices=[], unit=_price(None), error=None, note=None
    )
    assert no_price.line_total is None
    priced = BatchLine(
        query="X", quantity=4, hit=None, prices=[], unit=_price(3.0), error=None, note=None
    )
    assert priced.line_total == 12.0


# ---------------------------------------------------------------------------
# Batch flows


def test_batch_file_terminal_output(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "4 Lightning Bolt\n2x Giant Growth\n# comment\n\nMissing\n")
    assert main(["price", "fake", "--file", str(deck)]) == 0
    out = capsys.readouterr().out
    assert "Lightning Bolt" in out
    assert "4x" in out
    assert "$12.00" in out  # 4 x $3.00
    assert "Giant Growth" in out
    assert "\u20ac0.80" in out  # 2 x EUR 0.40
    assert "No matches" in out  # per-card errors render inline, not fatal
    assert "Grand total: $12.00" in out
    assert "Grand total: \u20ac0.80" in out


def test_batch_json_shape(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "4 Lightning Bolt\n3x\n")
    assert main(["--json", "price", "fake", "--file", str(deck)]) == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["command"] == "price"
    assert payload["mode"] == "batch"
    assert payload["game"] == "fake"
    line = payload["lines"][0]
    assert line["query"] == "Lightning Bolt"
    assert line["quantity"] == 4
    assert line["card"]["name"] == "Lightning Bolt"
    assert line["unit_price"]["price"] == 3.0
    assert line["unit_price"]["provenance"] == "market"
    assert line["line_total"] == 12.0
    assert line["line_currency"] == "USD"
    assert line["error"] is None
    assert line["note"] is None
    assert payload["grand_total"] == {"USD": 12.0}
    assert len(payload["warnings"]) == 1
    assert payload["warnings"][0].startswith("line 2:")
    # parse warnings go to stderr so stdout stays pure JSON
    assert "line 2" in out.err


def test_batch_stdin(capsys, monkeypatch, batch_game):
    monkeypatch.setattr(sys, "stdin", io.StringIO("4 Lightning Bolt\n"))
    assert main(["price", "fake"]) == 0
    assert "Grand total: $12.00" in capsys.readouterr().out


def test_batch_stdin_tty_needs_input(capsys, monkeypatch, batch_game):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty(""))
    assert main(["price", "fake"]) == 2
    assert "decklist" in capsys.readouterr().out


def test_batch_file_and_query_conflict(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "4 Bolt\n")
    assert main(["price", "fake", "Bolt", "--file", str(deck)]) == 2
    assert "not both" in capsys.readouterr().out


def test_batch_missing_file(capsys, batch_game):
    assert main(["price", "fake", "--file", "/nonexistent/deck.txt"]) == 1
    assert "Could not read" in capsys.readouterr().out


def test_batch_empty_decklist(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "# nothing here\n\n")
    assert main(["price", "fake", "--file", str(deck)]) == 0
    assert "No card names found" in capsys.readouterr().out


def test_batch_empty_decklist_json(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "# nothing here\n")
    assert main(["--json", "price", "fake", "--file", str(deck)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lines"] == []
    assert payload["grand_total"] == {}


def test_batch_dedupes_lookups(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "4 Lightning Bolt\n2 lightning bolt\n1 LIGHTNING BOLT\n")
    assert main(["--json", "price", "fake", "--file", str(deck)]) == 0
    assert batch_game.searches == ["Lightning Bolt"]
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["lines"]) == 3
    assert payload["grand_total"] == {"USD": 21.0}


def test_batch_per_card_errors(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "1 Boom\n1 Missing\n4 Lightning Bolt\n")
    assert main(["--json", "price", "fake", "--file", str(deck)]) == 0
    payload = json.loads(capsys.readouterr().out)
    by_query = {line["query"]: line for line in payload["lines"]}
    assert "down" in by_query["Boom"]["error"]
    assert "No matches" in by_query["Missing"]["error"]
    assert by_query["Boom"]["card"] is None
    assert by_query["Boom"]["line_total"] is None
    assert payload["grand_total"] == {"USD": 12.0}


def test_batch_all_errors_no_grand_total(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "1 Boom\n1 Missing\n")
    assert main(["price", "fake", "--file", str(deck)]) == 0
    out = capsys.readouterr().out
    assert "No matches" in out
    assert "Grand total" not in out


def test_batch_get_prices_error_is_per_line(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "1 Priceless\n")
    assert main(["--json", "price", "fake", "--file", str(deck)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "prices exploded" in payload["lines"][0]["error"]
    assert payload["lines"][0]["card"]["name"] == "Priceless"


def test_batch_card_without_prices(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "2 Empty\n")
    assert main(["price", "fake", "--file", str(deck)]) == 0
    assert "no prices right now" in capsys.readouterr().out


def test_batch_ambiguous_json_uses_top_hit_with_note(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "1 Ambiguous\n")
    assert main(["--json", "price", "fake", "--file", str(deck)]) == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    line = payload["lines"][0]
    assert line["card"]["name"] == "Ambiguous One"
    assert "top hit" in line["note"]
    assert "Ambiguous: 2 matches; using top hit" in out.err


def test_batch_ambiguous_piped_terminal_notes_inline(capsys, tmp_path, monkeypatch, batch_game):
    monkeypatch.setattr(sys, "stdin", io.StringIO("1 Ambiguous\n"))
    assert main(["price", "fake"]) == 0
    out = capsys.readouterr()
    assert "Ambiguous One" in out.out
    assert "top hit" in out.out
    assert "top hit" in out.err


def test_batch_pick_applies_per_card(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "1 Ambiguous\n")
    assert main(["--json", "price", "fake", "--file", str(deck), "--pick", "2"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lines"][0]["card"]["name"] == "Ambiguous Two"
    assert payload["lines"][0]["note"] is None


def test_batch_pick_out_of_range_is_per_line_error(capsys, tmp_path, batch_game):
    deck = _deck(tmp_path, "1 Ambiguous\n")
    assert main(["--json", "price", "fake", "--file", str(deck), "--pick", "9"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "out of range" in payload["lines"][0]["error"]


def test_batch_first_never_asks(capsys, tmp_path, monkeypatch, batch_game):
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: True)
    deck = _deck(tmp_path, "1 Ambiguous\n")
    assert main(["price", "fake", "--file", str(deck), "--first"]) == 0
    out = capsys.readouterr()
    assert "Ambiguous One" in out.out
    assert "top hit" not in out.err


def test_batch_interactive_picker_is_sequential(capsys, tmp_path, monkeypatch, batch_game):
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: True)
    calls = []

    def fake_pick(console, ranked, query):
        calls.append(query)
        return ranked[1]

    monkeypatch.setattr(topdeck.cli, "interactive_pick", fake_pick)
    deck = _deck(tmp_path, "1 Ambiguous\n")
    assert main(["price", "fake", "--file", str(deck)]) == 0
    assert calls == ["Ambiguous"]
    assert "Ambiguous Two" in capsys.readouterr().out


def test_batch_picker_abort_skips_card(capsys, tmp_path, monkeypatch, batch_game):
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(topdeck.cli, "interactive_pick", lambda console, ranked, query: None)
    deck = _deck(tmp_path, "1 Ambiguous\n4 Lightning Bolt\n")
    assert main(["price", "fake", "--file", str(deck)]) == 0
    out = capsys.readouterr().out
    assert "Skipped." in out
    assert "Grand total: $12.00" in out


def test_batch_table_mid_provenance_label(capsys, tmp_path, monkeypatch, batch_game):
    batch_game.price_map["Lightning Bolt"] = [_price(3.0, provenance="mid")]
    deck = _deck(tmp_path, "1 Lightning Bolt\n")
    assert main(["price", "fake", "--file", str(deck)]) == 0
    assert "tcgplayer mid" in capsys.readouterr().out
