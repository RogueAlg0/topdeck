"""topdeck-mcp: the price adapters as an MCP server (stdio, JSON-RPC).

Same data as `topdeck price`, for assistants and scripts. Non-interactive
by nature: the tools never prompt. When a query matches several cards,
price_lookup returns the recommended match's prices plus the ranked
alternatives, and the caller decides what to do next.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from topdeck import adapters as game_adapters
from topdeck.adapters.base import CardHit, Price
from topdeck.net import SourceError

mcp = MCPServer("topdeck")


def _hit_dict(hit: CardHit) -> dict:
    return {
        "name": hit.name,
        "set_code": hit.set_code,
        "set_name": hit.set_name,
        "collector_number": hit.collector_number,
        "finish": hit.finish,
        "url": hit.url,
    }


def _price_dict(price: Price) -> dict:
    return {
        "market": price.market,
        "currency": price.currency,
        "condition": price.condition,
        "printing": price.printing,
        "price": price.price,
        "as_of": price.as_of,
        "source": price.source,
        "source_url": price.source_url,
    }


def _game_error(game: str) -> dict:
    return {
        "error": f'Unknown game "{game}".',
        "valid_games": sorted(game_adapters.REGISTRY),
    }


@mcp.tool()
def search_cards(game: str, query: str) -> dict:
    """Find cards matching a name. Returns the ranked candidate list with
    the recommended match first (exact name, then most recent set)."""
    adapter = game_adapters.resolve_game(game)
    if adapter is None:
        return _game_error(game)
    try:
        hits = adapter.search(query)
    except SourceError as exc:
        return {"game": adapter.game_key, "query": query, "error": str(exc)}
    ranked = game_adapters.rank_candidates(hits, query)
    return {
        "game": adapter.game_key,
        "query": query,
        "matches": len(ranked),
        "recommended": 0 if ranked else None,
        "candidates": [_hit_dict(h) for h in ranked],
    }


@mcp.tool()
def price_lookup(game: str, query: str, pick: int | None = None) -> dict:
    """Look up market prices for a card. Every price carries its market,
    currency, condition, printing, as-of timestamp, and source.

    When several cards match and pick is not given, returns the
    recommended match's prices plus the ranked alternatives."""
    adapter = game_adapters.resolve_game(game)
    if adapter is None:
        return _game_error(game)
    try:
        hits = adapter.search(query)
    except SourceError as exc:
        return {"game": adapter.game_key, "query": query, "error": str(exc)}
    ranked = game_adapters.rank_candidates(hits, query)
    if not ranked:
        return {"game": adapter.game_key, "query": query, "matches": 0}
    if pick is not None:
        if not 1 <= pick <= len(ranked):
            return {
                "game": adapter.game_key,
                "query": query,
                "error": (
                    f"pick {pick} is out of range: {len(ranked)} matches, pick 1 to {len(ranked)}."
                ),
            }
        chosen = ranked[pick - 1]
    else:
        chosen = ranked[0]
    try:
        prices = adapter.get_prices(chosen)
    except SourceError as exc:
        return {"game": adapter.game_key, "query": query, "error": str(exc)}
    return {
        "game": adapter.game_key,
        "query": query,
        "matches": len(ranked),
        "result": {
            "card": _hit_dict(chosen),
            "recommended": chosen is ranked[0],
            "prices": [_price_dict(p) for p in prices],
        },
        "alternatives": [{"card": _hit_dict(h)} for h in ranked if h is not chosen],
    }


def main() -> None:
    """Entry point for the `topdeck-mcp` script. Speaks stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
