"""Tests for the topdeck-mcp tools, with a fake game. No network."""

import pytest

import topdeck.adapters
import topdeck.mcp_server as mcp_server
from topdeck.adapters.base import CardHit, Price
from topdeck.net import SourceError


def _hit(name, set_name="New Set", collector_number="10"):
    return CardHit(
        card_id=name,
        name=name,
        set_code="S",
        set_name=set_name,
        collector_number=collector_number,
        released_at="2024-01-01",
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
        self.search_calls = []

    def search(self, query):
        self.search_calls.append(query)
        if query == "boom":
            raise SourceError("the price source is down.")
        return list(self._hits)

    def get_prices(self, hit):
        return [_price()]


@pytest.fixture
def fake_game(monkeypatch):
    adapter = FakeAdapter(
        [
            _hit("Exact Card", "New Set", "10"),
            _hit("Exact Card", "Old Set", "1"),
            _hit("Exact Cardamom", "New Set", "5"),
        ]
    )
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": adapter})
    return adapter


def test_search_cards_ranks_and_recommends(fake_game):
    out = mcp_server.search_cards(game="fake", query="exact card")
    assert out["matches"] == 3
    assert out["recommended"] == 0
    assert out["candidates"][0]["set_name"] == "New Set"
    assert out["candidates"][0]["collector_number"] == "10"


def test_search_cards_unknown_game(fake_game):
    out = mcp_server.search_cards(game="yugioh", query="x")
    assert "error" in out
    assert "fake" in out["valid_games"]


def test_search_cards_no_matches(monkeypatch):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": FakeAdapter([])})
    out = mcp_server.search_cards(game="fake", query="nothing")
    assert out["matches"] == 0
    assert out["candidates"] == []
    assert out["recommended"] is None


def test_search_cards_source_error(fake_game):
    out = mcp_server.search_cards(game="fake", query="boom")
    assert "error" in out


def test_price_lookup_single_match_shape(monkeypatch):
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": FakeAdapter([_hit("Solo")])})
    out = mcp_server.price_lookup(game="fake", query="solo")
    assert out["matches"] == 1
    assert out["result"]["card"]["name"] == "Solo"
    assert out["result"]["recommended"] is True
    assert out["alternatives"] == []
    price = out["result"]["prices"][0]
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


def test_price_lookup_multi_returns_recommended_plus_alternatives(fake_game):
    out = mcp_server.price_lookup(game="fake", query="exact")
    assert out["matches"] == 3
    assert out["result"]["card"]["set_name"] == "New Set"
    assert out["result"]["recommended"] is True
    assert len(out["alternatives"]) == 2


def test_price_lookup_pick_selects(fake_game):
    out = mcp_server.price_lookup(game="fake", query="exact", pick=3)
    assert out["result"]["card"]["name"] == "Exact Cardamom"
    assert out["result"]["recommended"] is False


def test_price_lookup_pick_out_of_range(fake_game):
    out = mcp_server.price_lookup(game="fake", query="exact", pick=99)
    assert "error" in out


def test_price_lookup_unknown_game(fake_game):
    out = mcp_server.price_lookup(game="yugioh", query="x")
    assert "error" in out


def test_game_isolation(fake_game, monkeypatch):
    """A lookup for one game never touches another game's adapter."""
    other = FakeAdapter([_hit("Other")])
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": fake_game, "other": other})
    mcp_server.price_lookup(game="fake", query="exact")
    mcp_server.search_cards(game="fake", query="exact")
    assert other.search_calls == []
    assert fake_game.search_calls == ["exact", "exact"]


def test_entry_point_exists():
    assert callable(mcp_server.main)


# ---------------------------------------------------------------------------
# Error paths: the tools answer with error dicts, never tracebacks


def test_price_lookup_search_error(fake_game):
    out = mcp_server.price_lookup("fake", "boom")
    assert "down" in out["error"]
    assert out["game"] == "fake"


def test_price_lookup_no_matches(monkeypatch, fake_game):
    monkeypatch.setattr(fake_game, "_hits", [])
    out = mcp_server.price_lookup("fake", "nothing matches this")
    assert out["matches"] == 0


def test_price_lookup_prices_error(monkeypatch):
    adapter = FakeAdapter([_hit("Exact Card")])

    def boom(hit):
        raise SourceError("prices exploded")

    monkeypatch.setattr(adapter, "get_prices", boom)
    monkeypatch.setattr(topdeck.adapters, "REGISTRY", {"fake": adapter})
    out = mcp_server.price_lookup("fake", "Exact Card")
    assert "exploded" in out["error"]


def test_main_runs_the_server(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: calls.append(1))
    mcp_server.main()
    assert calls == [1]


def test_module_main_guard_runs_server(monkeypatch):
    """The `python -m topdeck.mcp_server` entry point calls main()."""
    import runpy

    from mcp.server.mcpserver import MCPServer

    calls = []
    monkeypatch.setattr(MCPServer, "run", lambda self: calls.append(1))
    runpy.run_module("topdeck.mcp_server", run_name="__main__", alter_sys=True)
    assert calls == [1]


# ---------------------------------------------------------------------------
# Missing extra: the module must load without the mcp package, the tools stay
# plain callables, and main() exits non-zero with a one-line hint, no traceback


@pytest.fixture
def mcp_server_without_extra(monkeypatch):
    """Re-import topdeck.mcp_server with every `mcp` import blocked, as if
    the optional extra were not installed. monkeypatch restores sys.modules."""
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError(f"no module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    for mod in list(sys.modules):
        if mod == "mcp" or mod.startswith("mcp."):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.delitem(sys.modules, "topdeck.mcp_server", raising=False)
    return importlib.import_module("topdeck.mcp_server")


def test_missing_extra_server_is_none(mcp_server_without_extra):
    assert mcp_server_without_extra.MCPServer is None
    assert mcp_server_without_extra.mcp is None


def test_missing_extra_tools_stay_callable(mcp_server_without_extra, fake_game):
    """Without the extra the tool functions are plain callables, not MCP Tools."""
    out = mcp_server_without_extra.search_cards(game="fake", query="exact card")
    assert out["matches"] == 3
    assert out["candidates"][0]["set_name"] == "New Set"
    out = mcp_server_without_extra.price_lookup(game="fake", query="exact")
    assert out["matches"] == 3
    assert out["result"]["card"]["set_name"] == "New Set"


def test_missing_extra_main_exits_nonzero_with_hint(mcp_server_without_extra, capsys):
    """No traceback, no stdout: one install hint on stderr, non-zero exit."""
    with pytest.raises(SystemExit) as excinfo:
        mcp_server_without_extra.main()
    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert captured.err.strip() == mcp_server_without_extra.EXTRA_HINT
    assert "pip install topdeck[mcp]" in captured.err


def test_missing_extra_cli_imports_cleanly(monkeypatch):
    """Blocking the mcp import must not break the base CLI: it never imports mcp."""
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError(f"no module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    for mod in list(sys.modules):
        if mod == "mcp" or mod.startswith("mcp.") or mod.startswith("topdeck"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    cli = importlib.import_module("topdeck.cli")
    assert callable(cli.main)
    assert "mcp" not in sys.modules
