"""Rendering for the price command: rich tables and JSON.

Card names and source labels become terminal hyperlinks (OSC 8) via
rich's link style. Rich degrades gracefully on terminals without
hyperlink support; we never emit escape codes ourselves.
"""

from __future__ import annotations

import json

from rich.console import Console
from rich.table import Table
from rich.text import Text

from topdeck.adapters.base import CardHit, Price


def linked(text: str, url: str) -> Text:
    """Text that becomes a clickable hyperlink where the terminal allows."""
    if url:
        return Text(text, style=f"link {url}")
    return Text(text)


def _money(price: Price) -> str:
    if price.price is None:
        return "n/a"
    symbols = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "JPY": "\u00a5"}
    symbol = symbols.get(price.currency, price.currency + " ")
    return f"{symbol}{price.price:,.2f}"


def price_table(hit: CardHit, prices: list[Price]) -> Table:
    table = Table(
        title=hit.name,
        title_style="bold gold1",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Set")
    table.add_column("Collector #")
    table.add_column("Finish")
    table.add_column("Market")
    table.add_column("Price", justify="right")
    table.add_column("As of")
    table.add_column("Source")
    for price in prices:
        table.add_row(
            hit.set_name or hit.set_code,
            hit.collector_number,
            price.printing,
            price.market,
            _money(price),
            price.as_of,
            linked(price.source, price.source_url),
        )
    return table


def card_line(hit: CardHit) -> Text:
    """One-line card identity with a hyperlink on the name when possible."""
    line = linked(hit.name, hit.url)
    detail = f"  {hit.set_name or hit.set_code} #{hit.collector_number}".rstrip(" #")
    line.append(detail, style="dim")
    return line


def candidate_table(
    hits: list[CardHit],
    numbers: list[int] | None = None,
    recommended: int | None = 0,
) -> Table:
    """Numbered candidate list. `numbers` are the labels shown; they should
    match the positions --pick expects. `recommended` marks one row, or
    None for no marker."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Card")
    table.add_column("Set")
    table.add_column("Collector #")
    table.add_column("Finish")
    labels = numbers if numbers is not None else list(range(1, len(hits) + 1))
    for label, hit in zip(labels, hits):
        name = linked(hit.name, hit.url)
        if recommended is not None and label == recommended:
            name.append("  [recommended]", style="bold green")
        table.add_row(
            str(label),
            name,
            hit.set_name or hit.set_code,
            hit.collector_number,
            hit.finish,
        )
    return table


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


def _hit_dict(hit: CardHit) -> dict:
    return {
        "name": hit.name,
        "set_code": hit.set_code,
        "set_name": hit.set_name,
        "collector_number": hit.collector_number,
        "finish": hit.finish,
        "url": hit.url,
    }


def json_payload(
    *,
    game: str,
    query: str,
    chosen: CardHit,
    prices: list[Price],
    alternatives: list[CardHit],
    recommended: bool,
) -> str:
    payload: dict = {
        "game": game,
        "query": query,
        "matches": 1 + len(alternatives),
        "result": {
            "card": _hit_dict(chosen),
            "recommended": recommended,
            "prices": [_price_dict(p) for p in prices],
        },
        "alternatives": [{"card": _hit_dict(h), "recommended": False} for h in alternatives],
    }
    if alternatives:
        payload["hint"] = (
            f"{len(alternatives) + 1} cards matched. Showing the recommended "
            "one; re-run with --pick N to choose another."
        )
    return json.dumps(payload, indent=2)


def print_result(
    console: Console,
    *,
    adapter_name: str,
    trust_tier: str,
    hit: CardHit,
    prices: list[Price],
) -> None:
    console.print()
    console.print(card_line(hit))
    if trust_tier in ("beta", "experimental"):
        tier_word = "beta" if trust_tier == "beta" else "experimental"
        console.print(
            f"[yellow]Heads up: {adapter_name} prices are {tier_word}. "
            "Coverage comes from community sources, so treat numbers as "
            "a guide, not gospel.[/yellow]"
        )
    if not prices:
        console.print(
            "[dim]No prices found for this printing right now. "
            "The source may not track it yet.[/dim]"
        )
        return
    console.print(price_table(hit, prices))
