"""Tests for `topdeck price`, with a fake game so no network is needed."""

import argparse
import json
from datetime import date, timedelta

import pytest

import topdeck.cli
from topdeck.adapters.base import CardHit, Price
from topdeck.cli import main
from topdeck.net import SourceError


def _hit(name, set_name="Set", collector_number="1"):
    return CardHit(
        card_id=name,
        name=name,
        set_code="S",
        set_name=set_name,
        collector_number=collector_number,
        url="https://example.com/card",
    )


def _price(value=1.23):
    return Price(
        market="tcgplayer",
        currency="USD",
        condition="near-mint",
        printing="normal",
        price=value,
        as_of="2026-09-30T00:00:00",
        source="fakesource",
        source_url="https://example.com/source",
    )


class FakeAdapter:
    game_key = "fake"
    display_name = "Fake Game"
    trust_tier = "solid"
    source_name = "fakesource"

    def __init__(self, hits):
        self._hits = hits

    def search(self, query):
        if query == "boom":
            raise SourceError("the price source is down.")
        return list(self._hits)

    def get_prices(self, hit):
        return [_price()]

    def history_key(self, hit):
        return None


@pytest.fixture
def fake_game(monkeypatch):
    hits = [
        _hit("Exact Card", "New Set", "10"),
        _hit("Exact Card", "Old Set", "1"),
        _hit("Exact Cardamom", "New Set", "5"),
    ]
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": FakeAdapter(hits)})
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: False)
    return hits


@pytest.fixture(autouse=True)
def _no_auto_sync(monkeypatch):
    """Auto-sync is covered by its own tests in test_backbone.py.

    These tests use a fake game with no sync history, so without this
    every price lookup here would attempt a real network sync.
    """
    monkeypatch.setattr(topdeck.cli, "_maybe_auto_sync", lambda adapter, as_json: None)


def test_unknown_game_lists_valid_games(capsys, fake_game):
    assert main(["price", "yugioh", "x"]) == 2
    out = capsys.readouterr().out
    assert "Unknown game" in out
    assert "fake" in out


def test_no_matches_is_friendly(capsys, monkeypatch):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": FakeAdapter([])})
    assert main(["price", "fake", "nothing"]) == 0
    assert "No matches" in capsys.readouterr().out


def test_source_error_exits_one(capsys, fake_game):
    assert main(["price", "fake", "boom"]) == 1
    assert "Could not look that up" in capsys.readouterr().out


def test_json_single_match_shape(capsys, monkeypatch):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": FakeAdapter([_hit("Solo")])})
    assert main(["--json", "price", "fake", "solo"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["game"] == "fake"
    assert payload["query"] == "solo"
    assert payload["matches"] == 1
    assert payload["result"]["card"]["name"] == "Solo"
    assert payload["result"]["recommended"] is True
    assert payload["alternatives"] == []
    price = payload["result"]["prices"][0]
    for key in (
        "market",
        "currency",
        "condition",
        "printing",
        "price",
        "as_of",
        "source",
    ):
        assert price[key], f"missing {key}"


def test_first_takes_recommended(capsys, fake_game):
    assert main(["--json", "price", "fake", "exact card", "--first"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["card"]["set_name"] == "New Set"
    assert payload["result"]["card"]["collector_number"] == "10"
    assert payload["result"]["recommended"] is True


def test_pick_selects_numbered_match(capsys, fake_game):
    assert main(["--json", "price", "fake", "exact card", "--pick", "2"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["card"]["set_name"] == "Old Set"
    assert payload["result"]["recommended"] is False
    assert len(payload["alternatives"]) == 2


def test_pick_out_of_range(capsys, fake_game):
    assert main(["price", "fake", "exact card", "--pick", "99"]) == 2
    assert "out of range" in capsys.readouterr().out


def test_json_multiple_matches_lists_alternatives(capsys, fake_game):
    assert main(["--json", "price", "fake", "exact"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["matches"] == 3
    assert len(payload["alternatives"]) == 2
    assert "hint" in payload
    assert "--pick" in payload["hint"]


def test_human_multiple_matches_lists_alternatives(capsys, fake_game):
    assert main(["price", "fake", "exact"]) == 0
    out = capsys.readouterr().out
    assert "2 other matches" in out
    assert "--pick" in out


def test_alternative_numbers_match_pick_positions(capsys, fake_game):
    """The # column in the alternatives table must agree with --pick N."""
    assert main(["price", "fake", "exact"]) == 0
    capsys.readouterr()
    # recommended match (position 1) is shown as the result; the other two
    # keep their ranked positions 2 and 3, so --pick 3 finds the third row.
    assert main(["--json", "price", "fake", "exact", "--pick", "3"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["card"]["name"] == "Exact Cardamom"


def test_interactive_pick_uses_picker(monkeypatch, capsys, fake_game):
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(topdeck.cli, "interactive_pick", lambda console, hits, query: hits[2])
    assert main(["price", "fake", "exact"]) == 0
    assert "Exact Cardamom" in capsys.readouterr().out


def test_interactive_walkaway_exits_one(monkeypatch, capsys, fake_game):
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(topdeck.cli, "interactive_pick", lambda c, h, q: None)
    assert main(["price", "fake", "exact"]) == 1


def test_query_words_are_joined(monkeypatch, capsys):
    seen = {}

    class Spy(FakeAdapter):
        def search(self, query):
            seen["query"] = query
            return [_hit("Solo")]

    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": Spy([_hit("Solo")])})
    assert main(["--json", "price", "fake", "black", "lotus"]) == 0
    assert seen["query"] == "black lotus"
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "black lotus"


# The remaining subcommands are still stubs.


def test_help_lists_price(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "price" in capsys.readouterr().out


def test_ev_stub_is_gone(capsys):
    """The unimplemented `ev` subcommand is not registered: argparse
    rejects it, and the help's command list names no `ev`."""
    with pytest.raises(SystemExit) as exc:
        main(["ev"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    first_words = {line.split()[0] for line in capsys.readouterr().out.splitlines() if line.split()}
    assert "ev" not in first_words


def test_dispatch_backstop_for_unknown_command(monkeypatch):
    """argparse rejects unknown subcommands before dispatch, so the
    fallthrough is unreachable in practice; it exists so a subcommand
    added to the parser but forgotten in dispatch fails loudly."""
    parser = topdeck.cli.build_parser()
    monkeypatch.setattr(
        parser,
        "parse_args",
        lambda argv=None: argparse.Namespace(command="nope", json=False),
    )
    monkeypatch.setattr(topdeck.cli, "build_parser", lambda: parser)
    with pytest.raises(AssertionError, match="dispatch fell through"):
        main([])


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Price-fetch error path and interactivity check


def test_price_get_prices_error(capsys, monkeypatch):
    adapter = FakeAdapter([_hit("Solo")])

    def boom(hit):
        raise SourceError("prices exploded")

    monkeypatch.setattr(adapter, "get_prices", boom)
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": adapter})
    assert main(["price", "fake", "solo", "--first"]) == 1
    assert "Could not fetch prices" in capsys.readouterr().out


def test_is_interactive_needs_both_streams(monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert topdeck.cli._is_interactive() is False
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert topdeck.cli._is_interactive() is True


def test_cli_module_main_guard(monkeypatch):
    """The `python -m topdeck.cli` entry point exits via main()."""
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["topdeck", "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("topdeck.cli", run_name="__main__", alter_sys=True)
    assert exc.value.code == 0


def _index_for_fake(tmp_path, monkeypatch):
    """A synced trigram index for the fake game, under a fake cache dir."""
    from topdeck import backbone, trigrams

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    conn = backbone._connect()
    try:
        trigrams.build_index(conn, "fake", [(1, "Lightning Bolt", "Fake Set", "FS")])
        conn.commit()
    finally:
        conn.close()


def test_no_matches_suggests_spelling(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": FakeAdapter([])})
    _index_for_fake(tmp_path, monkeypatch)
    assert main(["price", "fake", "Lighnting Bolt"]) == 0
    out = capsys.readouterr().out
    assert "No matches" in out
    assert 'Did you mean: "Lightning Bolt"?' in out


def test_no_matches_without_index_has_no_suggestion(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": FakeAdapter([])})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert main(["price", "fake", "Lighnting Bolt"]) == 0
    assert "Did you mean" not in capsys.readouterr().out


def test_watch_add_no_matches_suggests_spelling(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": FakeAdapter([])})
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: False)
    monkeypatch.setenv("TOPDECK_DATA_DIR", str(tmp_path / "data"))
    _index_for_fake(tmp_path, monkeypatch)
    assert main(["watch", "add", "fake", "Lighnting Bolt"]) == 0
    assert 'Did you mean: "Lightning Bolt"?' in capsys.readouterr().out


def test_batch_no_matches_suggests_spelling(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": FakeAdapter([])})
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: False)
    monkeypatch.setenv("COLUMNS", "120")  # hermetic table width, no mid-word wraps
    _index_for_fake(tmp_path, monkeypatch)
    decklist = tmp_path / "deck.txt"
    decklist.write_text("1 Lighnting Bolt\n")
    assert main(["price", "fake", "--file", str(decklist)]) == 0
    out = capsys.readouterr().out
    assert 'Did you mean: "Lightning Bolt"?' in out


# --- % change inline on lookups ---


class _KeyedAdapter(FakeAdapter):
    def history_key(self, hit):
        return 42


def _seed_history(monkeypatch, tmp_path):
    """Three snapshots for the fake game: 100 a month ago, 140 five days
    ago, 150 today."""
    from topdeck import backbone

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    today = date.today()

    def days_ago(n):
        return (today - timedelta(days=n)).strftime("%Y-%m-%d")

    backbone.record_history("fake", 42, 100, 90, "tcgcsv", date=days_ago(30))
    backbone.record_history("fake", 42, 140, 130, "tcgcsv", date=days_ago(5))
    backbone.record_history("fake", 42, 150, 140, "tcgcsv", date=days_ago(0))


def test_price_changes_none_without_join_key(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert topdeck.cli._price_changes(FakeAdapter([]), _hit("X")) is None


def test_price_changes_none_without_history(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert topdeck.cli._price_changes(_KeyedAdapter([]), _hit("X")) is None


def test_price_changes_from_history(monkeypatch, tmp_path):
    _seed_history(monkeypatch, tmp_path)
    changes = topdeck.cli._price_changes(_KeyedAdapter([]), _hit("X"))
    assert changes["30d"] == pytest.approx(50.0)
    assert changes["7d"] == pytest.approx((150 - 140) / 140 * 100)


def test_price_table_shows_change_columns(capsys, tmp_path, monkeypatch):
    _seed_history(monkeypatch, tmp_path)
    hits = [
        _hit("Exact Card", "New Set", "10"),
        _hit("Exact Card", "Old Set", "1"),
        _hit("Exact Cardamom", "New Set", "5"),
    ]
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": _KeyedAdapter(hits)})
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: False)
    monkeypatch.setenv("COLUMNS", "120")  # hermetic table width, no mid-word wraps
    assert main(["price", "fake", "Exact Card"]) == 0
    out = capsys.readouterr().out
    assert "7d %" in out
    assert "30d %" in out
    assert "+7.1%" in out
    assert "+50.0%" in out


def test_price_json_includes_changes(capsys, tmp_path, monkeypatch):
    _seed_history(monkeypatch, tmp_path)
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": _KeyedAdapter([_hit("Solo")])})
    assert main(["--json", "price", "fake", "Solo"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["change_30d_pct"] == 50.0
    assert payload["result"]["change_7d_pct"] == pytest.approx(7.14)


def test_price_json_omits_changes_without_history(capsys, fake_game):
    assert main(["--json", "price", "fake", "Exact Card"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "change_7d_pct" not in payload["result"]
    assert "change_30d_pct" not in payload["result"]
