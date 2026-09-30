"""Rendering for the price command: rich tables and JSON.

Card names and source labels become terminal hyperlinks (OSC 8) via
rich's link style. Rich degrades gracefully on terminals without
hyperlink support; we never emit escape codes ourselves.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

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
    return _amount(price.price, price.currency)


def _amount(value: float | None, currency: str) -> str:
    if value is None:
        return "n/a"
    symbols = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "JPY": "\u00a5"}
    symbol = symbols.get(currency, currency + " ")
    return f"{symbol}{value:,.2f}"


def _relative_as_of(raw: str, now: datetime | None = None) -> str:
    """Render an ISO timestamp as "2h ago".

    Falls back to the raw value when it cannot be parsed, so an odd
    source format never breaks the table.
    """
    try:
        stamp = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return raw
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    seconds = max(0, int((now - stamp).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 30 * 86400:
        return f"{seconds // 86400}d ago"
    return stamp.date().isoformat()


def price_table(hit: CardHit, prices: list[Price]) -> Table:
    # No title: print_result already shows the card name above the table.
    # Market doubles as the source link, so Source folds into it.
    table = Table(show_header=True, header_style="bold")
    table.add_column("Set", max_width=20, overflow="ellipsis")
    table.add_column("Collector #", no_wrap=True)
    table.add_column("Finish")
    table.add_column("Market")
    table.add_column("Price", justify="right")
    table.add_column("As of", no_wrap=True)
    for price in prices:
        table.add_row(
            hit.set_name or hit.set_code,
            hit.collector_number,
            price.printing,
            linked(price.market, price.source_url),
            _money(price),
            _relative_as_of(price.as_of),
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


# ---------------------------------------------------------------------------
# Watchlists and check


def _watch_dict(watch) -> dict:
    return {
        "id": watch.id,
        "game": watch.game,
        "card_id": watch.card_id,
        "name": watch.name,
        "set_name": watch.set_name,
        "target_price": watch.target_price,
    }


def watch_table(watches) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Game")
    table.add_column("Card")
    table.add_column("Set")
    table.add_column("Target", justify="right")
    for watch in watches:
        target = _amount(watch.target_price, "USD") if watch.target_price else "-"
        table.add_row(
            str(watch.id),
            watch.game,
            watch.name,
            watch.set_name or "-",
            target,
        )
    return table


def _check_amount(value: float | None, currency: str) -> Text:
    """An amount for the check table. Non-USD amounts get their currency
    code spelled out, so a euro price never sits next to a dollar price
    unlabeled."""
    text = Text(_amount(value, currency))
    if value is not None and currency != "USD":
        text.append(f" {currency}", style="dim")
    return text


def _change_text(row) -> Text:
    if row.delta_abs is None or row.delta_pct is None:
        return Text("n/a", style="dim")
    if row.delta_abs == 0:
        return Text("no change", style="dim")
    sign = "+" if row.delta_abs >= 0 else "-"
    core = f"{sign}{_amount(abs(row.delta_abs), row.currency)} ({sign}{abs(row.delta_pct):.1f}%)"
    if row.currency != "USD":
        core += f" {row.currency}"
    return Text(core)


def _signal_text(row) -> str:
    if row.error:
        return "[red]error[/red]"
    parts = []
    if row.target_hit:
        parts.append("[bold green]TARGET HIT[/bold green]")
    if row.spike:
        parts.append("[red]spike[/red]")
    if row.drop:
        parts.append("[green]drop[/green]")
    if not parts and row.previous is None and row.current is not None:
        parts.append("[dim]new[/dim]")
    return ", ".join(parts)


def check_table(rows) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Card")
    table.add_column("Set")
    table.add_column("Prev", justify="right")
    table.add_column("Now", justify="right")
    table.add_column("Change", justify="right")
    table.add_column("Signal")
    for row in rows:
        watch = row.watch
        card = Text(watch.name)
        if row.note:
            card.append(f"\n{row.note}", style="dim")
        now = _check_amount(row.current, row.currency) if not row.error else Text("n/a")
        table.add_row(
            card,
            watch.set_name or "-",
            _check_amount(row.previous, row.currency),
            now,
            _change_text(row),
            _signal_text(row),
            # Quiet rows dim out; only movers and target hits demand eyes.
            style="dim" if not row.alert else None,
        )
    return table


def doctor_table(sources, local) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    styles = {"ok": "green", "slow": "yellow", "down": "red"}
    for src in sources:
        style = styles.get(src.status, "")
        latency = f"{src.latency_ms} ms" if src.latency_ms is not None else "-"
        table.add_row(
            f"{src.display_name} ({src.source})",
            f"[{style}]{src.status}[/{style}]",
            f"{src.detail}, {latency}",
        )
    for label, detail in local:
        table.add_row(label, "[green]ok[/green]", detail)
    return table


def watch_json(action: str, watches, extra: dict | None = None) -> str:
    payload: dict = {
        "command": "watch",
        "action": action,
        "watches": [_watch_dict(w) for w in watches],
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, indent=2)


def check_row_dict(row) -> dict:
    return {
        "watch": _watch_dict(row.watch),
        "previous": row.previous,
        "current": row.current,
        "currency": row.currency,
        "source": row.source,
        "delta_abs": row.delta_abs,
        "delta_pct": row.delta_pct,
        "spike": row.spike,
        "drop": row.drop,
        "target_hit": row.target_hit,
        "alert": row.alert,
        "error": row.error,
        "note": row.note,
    }


def check_json(rows, alert_only: bool, checked_at: str) -> str:
    return json.dumps(
        {
            "command": "check",
            "checked_at": checked_at,
            "alert_only": alert_only,
            "rows": [check_row_dict(r) for r in rows],
        },
        indent=2,
    )


def doctor_json(sources, local) -> str:
    return json.dumps(
        {
            "command": "doctor",
            "sources": [
                {
                    "game": s.game,
                    "display_name": s.display_name,
                    "source": s.source,
                    "status": s.status,
                    "latency_ms": s.latency_ms,
                    "detail": s.detail,
                }
                for s in sources
            ],
            "local": [{"label": label, "detail": detail} for label, detail in local],
        },
        indent=2,
    )
