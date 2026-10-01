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


def _market_label(price: Price) -> str:
    """Market name, with the provenance when the number is not a market price.

    A TCGCSV mid-price fallback renders as "tcgplayer mid" so it is never
    mistaken for a market price. Short enough for the 80-column table.
    """
    if price.provenance and price.provenance != "market":
        return f"{price.market} {price.provenance}"
    return price.market


def _relative_as_of(raw: str, now: datetime | None = None) -> str:
    """Render an ISO timestamp as "2h ago".

    Falls back to the raw value when it cannot be parsed, so an odd
    source format never breaks the table.
    """
    try:
        # fromisoformat() only learned the "Z" suffix in 3.11; translate it
        # so Zulu timestamps parse on every Python we support.
        text = raw[:-1] + "+00:00" if isinstance(raw, str) and raw.endswith("Z") else raw
        stamp = datetime.fromisoformat(text)
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


# ---------------------------------------------------------------------------
# Price quality flags


# A printing is flagged as an outlier when it sits absurdly far from
# the card's other printings: Tukey's 1.5*IQR fences with four or more
# priced rows, or a 100x multiple of the median with exactly three.
# Flags mark, never remove: the row stays in the table with a "!" marker.
_OUTLIER_IQR_K = 1.5
_OUTLIER_MIN_ROWS = 4
_OUTLIER_MEDIAN_RATIO = 100

# A set released within this many days gets the volatility note:
# release-week prices swing hard and should be read with skepticism.
VOLATILITY_DAYS = 14


def _median(sorted_values: list[float]) -> float:
    """Median of an already-sorted list."""
    count = len(sorted_values)
    mid = count // 2
    if count % 2:
        return sorted_values[mid]
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile, the spreadsheet kind.

    Only called with four or more values, so rank always lands between
    two real elements.
    """
    rank = (len(sorted_values) - 1) * pct / 100
    low = int(rank)
    frac = rank - low
    return sorted_values[low] + (sorted_values[low + 1] - sorted_values[low]) * frac


def outlier_flags(prices: list[Price]) -> dict[int, str]:
    """Flag rows whose price looks like bad data, as {row_index: "outlier"}.

    Four or more priced rows use Tukey's 1.5*IQR fences on
    linear-interpolation quartiles; exactly three rows use a 100x
    multiple of the median (either direction). Fewer than three priced
    rows carry no signal, and unpriced rows are skipped. Flagged rows
    stay in the table; the flag only marks them suspect.
    """
    valued = [(index, price.price) for index, price in enumerate(prices) if price.price is not None]
    if len(valued) < 3:
        return {}
    amounts = sorted(value for _, value in valued)
    if len(valued) >= _OUTLIER_MIN_ROWS:
        q1 = _percentile(amounts, 25)
        q3 = _percentile(amounts, 75)
        iqr = q3 - q1
        low = q1 - _OUTLIER_IQR_K * iqr
        high = q3 + _OUTLIER_IQR_K * iqr
        return {index: "outlier" for index, value in valued if value < low or value > high}
    median = _median(amounts)
    ratio = _OUTLIER_MEDIAN_RATIO
    return {
        index: "outlier"
        for index, value in valued
        if value > median * ratio or value < median / ratio
    }


def is_volatile(released_at: str, now: datetime | None = None) -> bool:
    """True when the set released within VOLATILITY_DAYS.

    `now` is injectable so tests never depend on the wall clock.
    Missing or unparseable dates are not volatile: unknown is not new.
    """
    try:
        released = datetime.strptime(released_at[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError, AttributeError):
        return False
    today = (now or datetime.now(timezone.utc)).date()
    age_days = (today - released).days
    return 0 <= age_days <= VOLATILITY_DAYS


def price_table(hit: CardHit, prices: list[Price], sparkline: str | None = None) -> Table:
    # No title: print_result already shows the card name above the table.
    # Market doubles as the source link, so Source folds into it.
    # Trend appears only when there is a real sparkline to show; thin
    # history keeps the classic six columns.
    flags = outlier_flags(prices)
    table = Table(show_header=True, header_style="bold")
    table.add_column("Set", max_width=20, overflow="ellipsis")
    table.add_column("Collector #", no_wrap=True)
    table.add_column("Finish")
    table.add_column("Market")
    table.add_column("Price", justify="right")
    table.add_column("As of", no_wrap=True)
    if sparkline is not None:
        # Capped and ellipsis-truncated, so a long trend never breaks
        # the 80-column layout, even on narrow terminals.
        table.add_column("Trend", no_wrap=True, overflow="ellipsis", max_width=TREND_COLUMN_CHARS)
    for index, price in enumerate(prices):
        money = Text(_money(price))
        if index in flags:
            # Compact marker; the legend under the table says what it means.
            money.append(" !", style="yellow")
        row: list = [
            hit.set_name or hit.set_code,
            hit.collector_number,
            price.printing,
            linked(_market_label(price), price.source_url),
            money,
            _relative_as_of(price.as_of),
        ]
        if sparkline is not None:
            # The sparkline belongs to the headline row only; the other
            # legs have no history behind them.
            row.append(sparkline if index == 0 else "")
        table.add_row(*row)
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


def _price_dict(price: Price, flags: tuple[str, ...] = ()) -> dict:
    return {
        "market": price.market,
        "provenance": price.provenance,
        "currency": price.currency,
        "condition": price.condition,
        "printing": price.printing,
        "price": price.price,
        "as_of": price.as_of,
        "source": price.source,
        "source_url": price.source_url,
        "flags": list(flags),
    }


def _hit_dict(hit: CardHit) -> dict:
    return {
        "name": hit.name,
        "set_code": hit.set_code,
        "set_name": hit.set_name,
        "collector_number": hit.collector_number,
        "finish": hit.finish,
        "released_at": hit.released_at,
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
    flags = outlier_flags(prices)
    payload: dict = {
        "game": game,
        "query": query,
        "matches": 1 + len(alternatives),
        "volatile": is_volatile(chosen.released_at),
        "result": {
            "card": _hit_dict(chosen),
            "recommended": recommended,
            "prices": [
                _price_dict(price, ("outlier",) if index in flags else ())
                for index, price in enumerate(prices)
            ],
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
    sparkline: str | None = None,
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
    console.print(price_table(hit, prices, sparkline=sparkline))
    if outlier_flags(prices):
        console.print(
            "[dim]! marks a price far from the card's other printings: "
            "flagged as suspect, not removed.[/dim]"
        )
    if is_volatile(hit.released_at):
        console.print(
            f"[yellow]New set (released {hit.released_at[:10]}): prices are "
            "still settling, read them with skepticism.[/yellow]"
        )


# ---------------------------------------------------------------------------
# Price history and sparklines


HISTORY_WINDOW_DAYS = 30

# Sparkline width budget for the price table's Trend column. The table
# shows the recent end of the trend; the full sparkline is one
# `topdeck history` away.
TREND_COLUMN_CHARS = 12

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def render_sparkline(values: list[float | None]) -> str | None:
    """Draw a block-character sparkline, one block per value.

    Values scale min-to-max across the series, so the shape shows the
    trend rather than the absolute level. A flat series draws the
    middle block throughout. Fewer than three usable points is not a
    trend, so this returns None and callers show the bare numbers.
    """
    points = [value for value in values if value is not None]
    if len(points) < 3:
        return None
    low, high = min(points), max(points)
    if high == low:
        return "▄" * len(points)
    span = high - low
    levels = len(_SPARK_BLOCKS)
    return "".join(
        _SPARK_BLOCKS[min(levels - 1, int((value - low) / span * levels))] for value in points
    )


def history_points(rows: list[tuple]) -> list[dict]:
    """Machine-friendly history rows: date, price in dollars or None, source.

    The price is the market cents when present, else the mid cents, so a
    mid-price fallback reads as the price that was actually recorded.
    """
    points = []
    for row in rows:
        cents = row[1] if row[1] is not None else row[2]
        points.append(
            {
                "date": row[0],
                "price": cents / 100 if cents is not None else None,
                "source": row[3],
            }
        )
    return points


def history_values(rows: list[tuple]) -> list[float | None]:
    """One price per history row in dollars, oldest first."""
    return [point["price"] for point in history_points(rows)]


def history_stats(points: list[dict]) -> dict:
    """Low, high, current, and % change over the shown points.

    Rows without a price are skipped for the stats but stay in the
    series. With a single priced point the change is 0.0%; with none,
    every stat is None.
    """
    priced = [point["price"] for point in points if point["price"] is not None]
    if not priced:
        return {
            "low": None,
            "high": None,
            "current": None,
            "change_pct": None,
            "sparkline": None,
        }
    first, last = priced[0], priced[-1]
    change = None if first == 0 else (last - first) / first * 100
    return {
        "low": min(priced),
        "high": max(priced),
        "current": last,
        "change_pct": change,
        "sparkline": render_sparkline(priced),
    }


def history_table(points: list[dict]) -> Table:
    """The price series, oldest first: one row per snapshot."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("Date", no_wrap=True)
    table.add_column("Price", justify="right")
    table.add_column("Source")
    for point in points:
        price = _amount(point["price"], "USD") if point["price"] is not None else "n/a"
        table.add_row(point["date"], price, point["source"])
    return table


def history_summary(stats: dict, window_days: int) -> Text:
    """One dense summary line: low, high, current, % change, sparkline."""
    if stats["low"] is None:
        return Text("No priced snapshots in this window.", style="dim")
    line = Text()
    line.append(f"Low {_amount(stats['low'], 'USD')}   ")
    line.append(f"High {_amount(stats['high'], 'USD')}   ")
    line.append(f"Now {_amount(stats['current'], 'USD')}   ")
    change = stats["change_pct"]
    if change is None:
        line.append("change n/a   ", style="dim")
    else:
        style = "green" if change > 0 else "red" if change < 0 else "dim"
        line.append(f"{change:+.1f}% over {window_days}d   ", style=style)
    if stats["sparkline"]:
        line.append(stats["sparkline"])
    return line


def history_json(
    *,
    game: str,
    query: str,
    card: CardHit,
    window_days: int,
    points: list[dict],
    stats: dict,
) -> str:
    """Machine-readable history: the series plus the summary stats."""
    return json.dumps(
        {
            "command": "history",
            "game": game,
            "query": query,
            "card": _hit_dict(card),
            "window_days": window_days,
            "points": points,
            "low": stats["low"],
            "high": stats["high"],
            "current": stats["current"],
            "change_pct": stats["change_pct"],
            "sparkline": stats["sparkline"],
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Decklist batch pricing


def batch_table(lines: list) -> Table:
    """One row per decklist entry: quantity, card, unit price, line total.

    Each line has .quantity, .query, .hit (or None), .unit (a Price or
    None), .line_total (or None), .error (or None), and .note (or None).
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Qty", justify="right", style="dim")
    table.add_column("Card")
    table.add_column("Unit", justify="right")
    table.add_column("Total", justify="right")
    for line in lines:
        qty = f"{line.quantity}x"
        if line.error is not None or line.unit is None:
            card = Text(line.query)
            card.append(f"  {line.error or 'no prices right now'}", style="dim")
            missing = Text("n/a", style="dim")
            table.add_row(qty, card, missing, missing)
            continue
        hit = line.hit
        card = linked(hit.name, hit.url)
        detail = f"{hit.set_name or hit.set_code} #{hit.collector_number}".rstrip(" #")
        if detail:
            card.append(f"  {detail}", style="dim")
        if line.note:
            card.append(f"\n{line.note}", style="dim")
        unit = Text(_money(line.unit))
        unit.append(f" {_market_label(line.unit)}", style="dim")
        flags = outlier_flags(line.prices)
        unit_index = next((i for i, p in enumerate(line.prices) if p is line.unit), None)
        if unit_index is not None and unit_index in flags:
            unit.append(" !", style="yellow")
        table.add_row(
            qty,
            card,
            unit,
            Text(_amount(line.line_total, line.unit.currency)),
        )
    return table


def grand_total_lines(totals: dict[str, float]) -> list[Text]:
    """Bold "Grand total: $X" lines, one per currency present."""
    rendered = []
    for currency, total in totals.items():
        text = Text("Grand total: ", style="bold")
        text.append(_amount(total, currency), style="bold")
        rendered.append(text)
    return rendered


def _line_dict(line) -> dict:
    """One batch line as JSON, with outlier flags on its price rows."""
    flags = outlier_flags(line.prices)
    unit_index = next((i for i, p in enumerate(line.prices) if p is line.unit), None)

    def flagged(price, index):
        return _price_dict(price, ("outlier",) if index in flags else ())

    return {
        "query": line.query,
        "quantity": line.quantity,
        "card": _hit_dict(line.hit) if line.hit else None,
        "unit_price": flagged(line.unit, unit_index) if line.unit else None,
        "prices": [flagged(price, index) for index, price in enumerate(line.prices)],
        "line_total": line.line_total,
        "line_currency": line.unit.currency if line.unit else None,
        "error": line.error,
        "note": line.note,
    }


def batch_json(
    *,
    game: str,
    lines: list,
    grand_total: dict[str, float],
    warnings: list[str],
) -> str:
    """Machine-readable batch result: per-line entries plus grand totals."""
    return json.dumps(
        {
            "command": "price",
            "mode": "batch",
            "game": game,
            "lines": [_line_dict(line) for line in lines],
            "grand_total": grand_total,
            "warnings": warnings,
        },
        indent=2,
    )


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
    for alert in row.smart_alerts:
        parts.append(f"[magenta]{alert.line}[/magenta]")
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
        "smart_alerts": [alert.to_dict() for alert in row.smart_alerts],
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


def sync_table(results) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Game")
    table.add_column("Status")
    table.add_column("Detail")
    for res in results:
        if res.ok:
            table.add_row(
                res.game,
                "[green]synced[/green]",
                f"{res.groups} groups, {res.products} products with prices",
            )
        else:
            table.add_row(res.game, "[red]failed[/red]", res.error or "unknown error")
    return table


def sync_json(results) -> str:
    games = []
    for res in results:
        entry: dict = {
            "game": res.game,
            "status": "ok" if res.ok else "failed",
            "groups": res.groups,
            "products": res.products,
        }
        if not res.ok:
            entry["error"] = res.error
        games.append(entry)
    return json.dumps({"command": "sync", "games": games}, indent=2)


# ---------------------------------------------------------------------------
# Portfolios


def _lot_dict(lot) -> dict:
    return {
        "id": lot.id,
        "game": lot.game,
        "card_id": lot.card_id,
        "join_key": lot.join_key,
        "name": lot.name,
        "set_name": lot.set_name,
        "qty": lot.qty,
        "purchase_price": lot.purchase_price,
    }


def _holding_dict(holding) -> dict:
    return {
        "lot": _lot_dict(holding.lot),
        "current": _price_dict(holding.price) if holding.price else None,
        "value": holding.value,
        "cost": holding.lot.qty * holding.lot.purchase_price,
        "stale_sidecar": holding.stale_sidecar,
        "error": holding.error,
        "note": holding.note,
    }


def _holding_amount(value: float | None, currency: str) -> Text:
    """A portfolio amount. Non-USD amounts keep their currency code, so a
    euro value never sits next to a dollar value unlabeled."""
    text = Text(_amount(value, currency))
    if value is not None and currency != "USD":
        text.append(f" {currency}", style="dim")
    return text


def _holding_card_text(holding) -> Text:
    """Card name with the set and any honesty notes under it."""
    lot = holding.lot
    card = Text(lot.name)
    if lot.set_name:
        card.append(f"  {lot.set_name}", style="dim")
    if holding.stale_sidecar:
        card.append("\nsidecar stale; live price", style="dim")
    if holding.note:
        card.append(f"\n{holding.note}", style="dim")
    if holding.error:
        card.append(f"\n{holding.error}", style="dim")
    return card


def _holding_pnl_text(holding) -> Text:
    """Per-lot unrealized P&L. Only defined for USD-priced holdings."""
    if holding.price is None or holding.price.currency != "USD" or holding.value is None:
        return Text("n/a", style="dim")
    pnl = holding.value - holding.lot.qty * holding.lot.purchase_price
    sign = "+" if pnl >= 0 else "-"
    style = "green" if pnl > 0 else ("red" if pnl < 0 else "")
    return Text(f"{sign}{_amount(abs(pnl), 'USD')}", style=style)


def holdings_table(holdings) -> Table:
    """Holdings as a table, biggest current value first, unpriced last."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Qty", justify="right", style="dim")
    table.add_column("Card")
    table.add_column("Paid", justify="right")
    table.add_column("Now", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("As of", no_wrap=True)
    ordered = sorted(holdings, key=lambda h: (h.value is None, -(h.value or 0)))
    for holding in ordered:
        lot = holding.lot
        if holding.error is not None or holding.price is None:
            missing = Text("n/a", style="dim")
            table.add_row(
                str(lot.id),
                f"{lot.qty}x",
                _holding_card_text(holding),
                _amount(lot.purchase_price, "USD"),
                missing,
                missing,
                missing,
                Text("-", style="dim"),
            )
            continue
        price = holding.price
        table.add_row(
            str(lot.id),
            f"{lot.qty}x",
            _holding_card_text(holding),
            _amount(lot.purchase_price, "USD"),
            _holding_amount(price.price, price.currency),
            _holding_amount(holding.value, price.currency),
            _holding_pnl_text(holding),
            _relative_as_of(price.as_of),
        )
    return table


def portfolio_lots_table(lots) -> Table:
    """A bare lot listing, for disambiguating a remove by name."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Qty", justify="right", style="dim")
    table.add_column("Game")
    table.add_column("Card")
    table.add_column("Paid", justify="right")
    for lot in lots:
        table.add_row(
            str(lot.id),
            f"{lot.qty}x",
            lot.game,
            lot.name,
            _amount(lot.purchase_price, "USD"),
        )
    return table


def portfolio_summary_lines(summary) -> list[Text]:
    """Bold "Total value: $X" lines for the portfolio report.

    The totals cover holdings with a current USD price; the rest are
    counted separately, never folded silently into the numbers.
    """
    rendered = []

    def _line(label: str, amount: str, style: str = "bold") -> Text:
        text = Text(f"{label}: ", style="bold")
        text.append(amount, style=style)
        return text

    rendered.append(_line("Total value", _amount(summary.value, "USD")))
    rendered.append(_line("Total cost", _amount(summary.cost, "USD")))
    if summary.pnl_pct is None:
        pnl = f"{'+' if summary.pnl >= 0 else '-'}{_amount(abs(summary.pnl), 'USD')} (n/a)"
    else:
        sign = "+" if summary.pnl >= 0 else "-"
        pnl = f"{sign}{_amount(abs(summary.pnl), 'USD')} ({sign}{abs(summary.pnl_pct):.1f}%)"
    color = "green" if summary.pnl > 0 else ("red" if summary.pnl < 0 else "bold")
    rendered.append(_line("Unrealized P&L", pnl, color))
    return rendered


def portfolio_lot_json(action: str, lot) -> str:
    return json.dumps(
        {
            "command": "portfolio",
            "action": action,
            "lot": _lot_dict(lot),
        },
        indent=2,
    )


def portfolio_list_json(holdings, summary) -> str:
    return json.dumps(
        {
            "command": "portfolio",
            "action": "list",
            "holdings": [_holding_dict(h) for h in holdings],
            "summary": {
                "total_lots": summary.total_lots,
                "total_qty": summary.total_qty,
                "total_value_usd": summary.value,
                "total_cost_usd": summary.cost,
                "pnl_usd": summary.pnl,
                "pnl_pct": summary.pnl_pct,
                "excluded_holdings": summary.excluded,
            },
        },
        indent=2,
    )
