"""Tests for the history view: sparklines and `topdeck history`. No network."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console

import topdeck.adapters
import topdeck.cli
from topdeck import backbone, output
from topdeck.adapters.base import CardHit, GameAdapter, Price
from topdeck.adapters.lorcana import LorcastAdapter
from topdeck.adapters.mtg import ScryfallAdapter
from topdeck.adapters.pokemon import TcgdexAdapter
from topdeck.adapters.tcgcsv import TcgcsvBulkAdapter
from topdeck.cli import main
from topdeck.net import SourceError


@pytest.fixture
def cache_home(tmp_path, monkeypatch):
    """Backbone reads and writes only under this fake XDG cache dir."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def _seed(game="fake", key=7, cents=(100, 120, 110, 140, 130), source="tcgcsv"):
    """One snapshot per day, oldest first, ending today."""
    total = len(cents)
    for index, value in enumerate(cents):
        backbone.record_history(game, key, value, None, source, date=_days_ago(total - 1 - index))


def _hit(name="Bolt", card_id="7", **extra):
    return CardHit(
        card_id=card_id,
        name=name,
        set_code="LEA",
        set_name="Alpha",
        collector_number="161",
        url="https://example.com/card",
        extra=extra,
    )


def _price(value=1.30):
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


def _points(*prices):
    return [
        {"date": f"2026-09-{index + 1:02d}", "price": price, "source": "tcgcsv"}
        for index, price in enumerate(prices)
    ]


class _HistoryAdapter:
    game_key = "fake"
    display_name = "Fake Game"
    trust_tier = "solid"
    source_name = "fakesource"

    def __init__(self, hits, key=7):
        self._hits = hits
        self._key = key

    def search(self, query):
        if query == "boom":
            raise SourceError("the price source is down.")
        return list(self._hits)

    def get_prices(self, hit):
        return [_price()]

    def history_key(self, hit):
        return self._key


@pytest.fixture
def fake_game(monkeypatch):
    hits = [_hit("Bolt"), _hit("Bolt")]
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": _HistoryAdapter(hits)})
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: False)
    return hits


# ---------------------------------------------------------------------------
# render_sparkline


def test_sparkline_rises():
    assert output.render_sparkline([1.0, 2.0, 3.0, 4.0]) == "▁▃▆█"


def test_sparkline_falls():
    assert output.render_sparkline([4.0, 3.0, 2.0, 1.0]) == "█▆▃▁"


def test_sparkline_flat_series_uses_middle_block():
    assert output.render_sparkline([2.0, 2.0, 2.0, 2.0]) == "▄▄▄▄"


@pytest.mark.parametrize("values", [[], [1.0], [1.0, 2.0], [None, None, 1.0]])
def test_sparkline_needs_three_usable_points(values):
    assert output.render_sparkline(values) is None


def test_sparkline_skips_missing_values():
    assert output.render_sparkline([None, 1.0, None, 2.0, 3.0]) == "▁▅█"


# ---------------------------------------------------------------------------
# history_points / history_values


def test_history_points_prefers_market_over_mid():
    rows = [("2026-09-30", 150, 120, "tcgcsv")]
    assert output.history_points(rows) == [
        {"date": "2026-09-30", "price": 1.50, "source": "tcgcsv"}
    ]


def test_history_points_falls_back_to_mid():
    rows = [("2026-09-30", None, 120, "tcgcsv")]
    assert output.history_points(rows)[0]["price"] == 1.20


def test_history_points_without_any_price():
    rows = [("2026-09-30", None, None, "tcgcsv")]
    assert output.history_points(rows)[0]["price"] is None


def test_history_values_in_dollars_oldest_first():
    rows = [
        ("2026-09-29", 100, None, "tcgcsv"),
        ("2026-09-30", None, 250, "scryfall"),
    ]
    assert output.history_values(rows) == [1.0, 2.5]


# ---------------------------------------------------------------------------
# history_stats


def test_history_stats_full():
    stats = output.history_stats(_points(1.0, 1.2, 1.1))
    assert stats["low"] == 1.0
    assert stats["high"] == 1.2
    assert stats["current"] == 1.1
    assert stats["change_pct"] == pytest.approx(10.0)
    assert stats["sparkline"] == "▁█▅"


def test_history_stats_skips_unpriced_rows():
    stats = output.history_stats(_points(1.0, None, 1.5))
    assert (stats["low"], stats["high"], stats["current"]) == (1.0, 1.5, 1.5)
    assert stats["change_pct"] == pytest.approx(50.0)


def test_history_stats_single_point_has_zero_change():
    stats = output.history_stats(_points(1.25))
    assert stats["change_pct"] == 0.0
    assert stats["sparkline"] is None


def test_history_stats_zero_first_price_has_no_change_pct():
    stats = output.history_stats(_points(0.0, 1.0, 2.0))
    assert stats["change_pct"] is None
    assert stats["low"] == 0.0


def test_history_stats_without_priced_points():
    stats = output.history_stats(_points(None, None))
    assert stats == {
        "low": None,
        "high": None,
        "current": None,
        "change_pct": None,
        "sparkline": None,
    }


# ---------------------------------------------------------------------------
# history_table / history_summary


def test_history_table_shows_series_oldest_first():
    console = Console(width=80, record=True)
    console.print(output.history_table(_points(1.0, 1.5)))
    out = console.export_text()
    assert out.index("2026-09-01") < out.index("2026-09-02")
    assert "$1.00" in out and "$1.50" in out


def test_history_table_marks_unpriced_rows():
    console = Console(width=80, record=True)
    console.print(output.history_table(_points(None)))
    assert "n/a" in console.export_text()


def test_history_summary_line():
    stats = output.history_stats(_points(1.0, 1.2, 1.1))
    text = output.history_summary(stats, 30).plain
    assert "Low $1.00" in text
    assert "High $1.20" in text
    assert "Now $1.10" in text
    assert "+10.0% over 30d" in text
    assert "▁█▅" in text


def test_history_summary_negative_change():
    stats = output.history_stats(_points(2.0, 1.0, 1.5))
    assert "-25.0%" in output.history_summary(stats, 30).plain


def test_history_summary_zero_change():
    stats = output.history_stats(_points(1.0, 1.0, 1.0))
    text = output.history_summary(stats, 30).plain
    assert "+0.0% over 30d" in text
    assert "▄▄▄" in text


def test_history_summary_without_change_pct():
    stats = output.history_stats(_points(0.0, 1.0, 2.0))
    assert "change n/a" in output.history_summary(stats, 30).plain


def test_history_summary_without_priced_points():
    text = output.history_summary(output.history_stats(_points(None)), 30).plain
    assert "No priced snapshots" in text


# ---------------------------------------------------------------------------
# price table: Trend column


def test_price_table_sparkline_column_only_with_sparkline():
    table = output.price_table(_hit(), [_price(), _price()], sparkline="▁█")
    assert [column.header for column in table.columns][-1] == "Trend"


def test_price_table_sparkline_on_headline_row_only():
    console = Console(width=80, record=True)
    console.print(output.price_table(_hit(), [_price(), _price()], sparkline="▁█"))
    out = console.export_text()
    assert out.count("▁") == 1
    assert out.count("█") == 1


def test_price_table_sparkline_fits_80_columns():
    console = Console(width=80, record=True)
    console.print(output.price_table(_hit(), [_price()], sparkline="▁▂▃▄▅▆▇█▁▂▃▄"))
    out = console.export_text()
    assert max(len(line) for line in out.splitlines()) <= 80
    assert "Trend" in out
    assert "$1.30" in out  # the headline price survives the new column


def test_price_table_sparkline_truncates_on_narrow_terminal():
    console = Console(width=40, record=True)
    console.print(output.price_table(_hit(), [_price()], sparkline="▁▂▃▄▅▆▇█▁▂▃▄"))
    out = console.export_text()
    assert max(len(line) for line in out.splitlines()) <= 40
    assert "Trend" in out


def test_print_result_shows_sparkline():
    console = Console(width=80, record=True)
    output.print_result(
        console,
        adapter_name="Fake",
        trust_tier="solid",
        hit=_hit(),
        prices=[_price()],
        sparkline="▁█",
    )
    out = console.export_text()
    assert "Trend" in out
    assert "▁█" in out


# ---------------------------------------------------------------------------
# adapter history keys


def test_base_history_key_defaults_to_none():
    assert GameAdapter().history_key(_hit()) is None


def test_mtg_history_key():
    adapter = ScryfallAdapter()
    assert adapter.history_key(_hit(tcgplayer_id=123)) == 123
    assert adapter.history_key(_hit()) is None
    assert adapter.history_key(_hit(tcgplayer_id="123")) is None


def test_lorcana_history_key():
    adapter = LorcastAdapter()
    assert adapter.history_key(_hit(tcgplayer_id=9)) == 9
    assert adapter.history_key(_hit()) is None


def test_pokemon_history_key():
    adapter = TcgdexAdapter()
    assert adapter.history_key(_hit(product_id=42)) == 42
    assert adapter.history_key(_hit()) is None


def test_tcgcsv_history_key():
    adapter = TcgcsvBulkAdapter()
    assert adapter.history_key(_hit(card_id="456")) == 456
    assert adapter.history_key(_hit(card_id="abc")) is None
    assert adapter.history_key(_hit(card_id=None)) is None


# ---------------------------------------------------------------------------
# `topdeck history`


def test_history_unknown_game(capsys, fake_game):
    assert main(["history", "nope", "Bolt"]) == 2
    assert "Unknown game" in capsys.readouterr().out


def test_history_no_matches(capsys, monkeypatch):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": _HistoryAdapter([])})
    assert main(["history", "fake", "nothing"]) == 0
    assert "No matches" in capsys.readouterr().out


def test_history_source_error(capsys, fake_game):
    assert main(["history", "fake", "boom"]) == 1
    assert "Could not look that up" in capsys.readouterr().out


def test_history_pick_out_of_range(capsys, fake_game):
    assert main(["history", "fake", "Bolt", "--pick", "9"]) == 2
    assert "out of range" in capsys.readouterr().out


def test_history_without_any_history_is_honest(capsys, fake_game, cache_home):
    assert main(["history", "fake", "Bolt"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "No price history" in out
    assert "topdeck check" in out


def test_history_without_join_key_is_honest(capsys, monkeypatch, cache_home):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": _HistoryAdapter([_hit()], key=None)})
    _seed()
    assert main(["history", "fake", "Bolt"]) == 0
    assert "No price history" in capsys.readouterr().out


def test_history_shows_series_and_summary(capsys, fake_game, cache_home):
    _seed(cents=(100, 120, 110, 140, 130))
    assert main(["history", "fake", "Bolt"]) == 0
    out = capsys.readouterr().out
    assert out.index(_days_ago(4)) < out.index(_days_ago(0))
    assert "Low $1.00" in out
    assert "High $1.40" in out
    assert "Now $1.30" in out
    assert "+30.0% over 30d" in out
    assert "▁▅▃█▇" in out


def test_history_thin_history_has_no_sparkline(capsys, fake_game, cache_home):
    _seed(cents=(100, 130))
    assert main(["history", "fake", "Bolt"]) == 0
    out = capsys.readouterr().out
    assert "Low $1.00" in out
    assert "High $1.30" in out
    assert "+30.0% over 30d" in out
    assert "▁" not in out
    assert "█" not in out


def test_history_json_shape(capsys, fake_game, cache_home):
    _seed(cents=(100, 120, 110, 140, 130))
    assert main(["--json", "history", "fake", "Bolt"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "history"
    assert payload["game"] == "fake"
    assert payload["query"] == "Bolt"
    assert payload["window_days"] == 30
    assert [point["date"] for point in payload["points"]] == [
        _days_ago(4),
        _days_ago(3),
        _days_ago(2),
        _days_ago(1),
        _days_ago(0),
    ]
    assert [point["price"] for point in payload["points"]] == [1.0, 1.2, 1.1, 1.4, 1.3]
    assert payload["low"] == 1.0
    assert payload["high"] == 1.4
    assert payload["current"] == 1.3
    assert payload["change_pct"] == pytest.approx(30.0)
    assert payload["sparkline"] == "▁▅▃█▇"


def test_history_json_without_history(capsys, fake_game, cache_home):
    assert main(["--json", "history", "fake", "Bolt"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["points"] == []
    assert payload["low"] is None
    assert payload["sparkline"] is None


# ---------------------------------------------------------------------------
# price command: sparkline wiring


def test_price_shows_trend_column(capsys, fake_game, cache_home):
    _seed(cents=(100, 110, 105, 120, 130))
    assert main(["price", "fake", "Bolt"]) == 0
    out = capsys.readouterr().out
    assert "Trend" in out
    assert "▁▃▂▆█" in out


def test_price_hides_trend_column_when_history_thin(capsys, fake_game, cache_home):
    _seed(cents=(100, 130))
    assert main(["price", "fake", "Bolt"]) == 0
    out = capsys.readouterr().out
    assert "Trend" not in out
    assert "$1.30" in out


def test_price_hides_trend_column_without_join_key(capsys, monkeypatch, cache_home):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": _HistoryAdapter([_hit()], key=None)})
    _seed()
    assert main(["price", "fake", "Bolt"]) == 0
    assert "Trend" not in capsys.readouterr().out


def test_price_trend_column_shows_recent_end(capsys, fake_game, cache_home):
    _seed(cents=tuple(range(100, 130)))  # 30 rising days
    assert main(["price", "fake", "Bolt"]) == 0
    out = capsys.readouterr().out
    assert "Trend" in out
    # The table shows the recent 12 blocks, not the whole 30-day run.
    assert "▅▆▆▆▇▇▇▇████" in out
    assert "▁▁▁▁" not in out
    assert max(len(line) for line in out.splitlines()) <= 80


def test_price_without_prices_skips_history_lookup(capsys, monkeypatch, cache_home):
    class _NoPrices(_HistoryAdapter):
        def get_prices(self, hit):
            return []

    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": _NoPrices([_hit()])})
    monkeypatch.setattr(topdeck.cli, "_is_interactive", lambda: False)
    _seed()
    assert main(["price", "fake", "Bolt"]) == 0
    out = capsys.readouterr().out
    assert "No prices found" in out
    assert "Trend" not in out


def test_price_json_has_no_sparkline(capsys, fake_game, cache_home):
    _seed()
    assert main(["--json", "price", "fake", "Bolt"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "sparkline" not in payload["result"]
    assert "Trend" not in capsys.readouterr().out
