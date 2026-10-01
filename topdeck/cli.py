"""Command line interface for topdeck.

`price`, `watch`, `check`, `doctor`, `sync`, and `portfolio` are live.
The remaining subcommand (`ev`) lands with its milestone; until then
it explains itself and exits cleanly. No telemetry.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel

from topdeck import __version__, alerts, backbone, output, progress, trigrams
from topdeck import adapters as game_adapters
from topdeck.adapters.base import CardHit, Price
from topdeck.doctor import check_sources, local_checks
from topdeck.net import SourceError
from topdeck.output import (
    batch_json,
    batch_table,
    candidate_table,
    check_json,
    check_table,
    doctor_json,
    doctor_table,
    grand_total_lines,
    holdings_table,
    json_payload,
    portfolio_list_json,
    portfolio_lot_json,
    portfolio_lots_table,
    portfolio_summary_lines,
    print_result,
    sync_json,
    sync_table,
    watch_json,
    watch_table,
)
from topdeck.pick import interactive_pick
from topdeck.portfolio import PortfolioStore, join_key_for, price_holding, summarize
from topdeck.progress import Progress
from topdeck.watch import WatchStore, build_row, pick_tracked_price

COMMAND_DESCRIPTIONS = {
    "ev": "Compute the expected value of opening a pack or box.",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topdeck",
        description="Topdeck the price. Terminal-native TCG price watcher.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of rich terminal output",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    price = sub.add_parser(
        "price",
        help="Look up the market price of a card, or a whole decklist.",
        description="Look up the market price of a card.",
    )
    price.add_argument(
        "game",
        help="which game: " + ", ".join(sorted(game_adapters.REGISTRY)),
    )
    price.add_argument(
        "query",
        nargs="*",
        help='card name, e.g. topdeck price mtg "Black Lotus" (omit to read a decklist from stdin)',
    )
    price.add_argument(
        "--file",
        metavar="PATH",
        default=None,
        help=(
            "price every card in a decklist file instead of one query. "
            'Each line is "<qty> <card name>", e.g. "4 Lightning Bolt" or '
            '"4x Lightning Bolt"; a bare name means one copy. Blank lines '
            "and # comments are skipped. Without --file and without a query, "
            "names are read from stdin, one per line."
        ),
    )
    price.add_argument(
        "--pick",
        type=int,
        default=None,
        metavar="N",
        help="choose match number N instead of being asked (applies to every card in batch mode)",
    )
    price.add_argument(
        "--first",
        action="store_true",
        help="take the recommended match without asking (applies to every card in batch mode)",
    )

    watch = sub.add_parser(
        "watch",
        help="Track cards on a watchlist and get alerted on price moves.",
        description="Track cards on a watchlist and get alerted on price moves.",
    )
    watch_sub = watch.add_subparsers(dest="watch_command", metavar="<action>")
    watch_add = watch_sub.add_parser(
        "add",
        help="Watch a card.",
        description="Add a card to the watchlist.",
    )
    watch_add.add_argument(
        "game",
        help="which game: " + ", ".join(sorted(game_adapters.REGISTRY)),
    )
    watch_add.add_argument(
        "query",
        nargs="+",
        help="card name",
    )
    watch_add.add_argument(
        "--target",
        default=None,
        metavar="PRICE",
        help="alert when the tracked price drops to this (USD), e.g. --target 12.50",
    )
    watch_add.add_argument(
        "--pick",
        type=int,
        default=None,
        metavar="N",
        help="choose match number N instead of being asked",
    )
    watch_add.add_argument(
        "--first",
        action="store_true",
        help="take the recommended match without asking",
    )
    watch_sub.add_parser(
        "list",
        help="Show the watchlist.",
        description="Show every watched card.",
    )
    watch_remove = watch_sub.add_parser(
        "remove",
        help="Stop watching a card.",
        description="Remove a card from the watchlist.",
    )
    watch_remove.add_argument(
        "target",
        nargs="+",
        help="watch ID or card name",
    )

    check = sub.add_parser(
        "check",
        help="Re-price the watchlist and flag the movers.",
        description="Re-price every watched card and flag spikes, drops, and target hits.",
    )
    check.add_argument(
        "--alert-only",
        action="store_true",
        help="only show movers, target hits, and errors",
    )
    check.add_argument(
        "--smart",
        dest="smart",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "evaluate smart alerts from price history (default: on; --no-smart "
            "disables them). Smart alerts need at least 5 daily snapshots "
            "before the current price; thinner history stays silent."
        ),
    )
    check.add_argument(
        "--window",
        type=int,
        default=alerts.DEFAULT_WINDOW,
        metavar="N",
        help=(
            "daily snapshots the moving average and the Bollinger-style "
            f"bands are computed over (default: {alerts.DEFAULT_WINDOW})"
        ),
    )
    check.add_argument(
        "--deviation",
        type=float,
        default=alerts.DEFAULT_DEVIATION_PCT,
        metavar="PCT",
        help=(
            "percent move vs the window average that fires a smart alert "
            f"(default: {alerts.DEFAULT_DEVIATION_PCT:g})"
        ),
    )
    check.add_argument(
        "--band-k",
        type=float,
        default=alerts.DEFAULT_BAND_K,
        metavar="K",
        help=(
            "band width in standard deviations for the Bollinger-style "
            f"bands (default: {alerts.DEFAULT_BAND_K:g})"
        ),
    )

    sub.add_parser(
        "doctor",
        help="Check every price source.",
        description="Check every price source and report honestly which are healthy.",
    )

    history = sub.add_parser(
        "history",
        help="Show a card's recorded price history.",
        description=(
            "Show a card's price history over the last "
            f"{output.HISTORY_WINDOW_DAYS} days: the series oldest to "
            "newest, plus low, high, current, and % change."
        ),
    )
    history.add_argument(
        "game",
        help="which game: " + ", ".join(sorted(game_adapters.REGISTRY)),
    )
    history.add_argument(
        "query",
        nargs="+",
        help="card name",
    )
    history.add_argument(
        "--pick",
        type=int,
        default=None,
        metavar="N",
        help="choose match number N instead of being asked",
    )
    history.add_argument(
        "--first",
        action="store_true",
        help="take the recommended match without asking",
    )

    sync = sub.add_parser(
        "sync",
        help="Download bulk TCGCSV prices into the local database.",
        description=(
            "Download TCGCSV's bulk product and price data into a local "
            "SQLite database, so price lookups serve fresh USD market prices "
            "without a live fetch per card. `topdeck sync` refreshes the "
            f"{len(backbone.CORE_GAMES)} core games ({', '.join(backbone.CORE_GAMES)}); "
            f"`topdeck sync --all` refreshes all {len(backbone.GAMES)} games; "
            "`topdeck sync pokemon` refreshes one. "
            "Synced data stays fresh for 36 hours, then lookups fall back to "
            "live fetching until the next sync.\n\n"
            "There is no in-tool scheduler. Run it from cron instead, e.g.:\n"
            "  0 6 * * * topdeck sync >/dev/null 2>&1"
        ),
    )
    sync.add_argument(
        "game",
        nargs="?",
        default=None,
        help="sync one game only (default: the core games, or all with --all)",
    )
    sync.add_argument(
        "--all",
        action="store_true",
        help=f"sync all {len(backbone.GAMES)} games (default is the core games only)",
    )

    portfolio = sub.add_parser(
        "portfolio",
        help="Track what you own and see what it is worth.",
        description="Track what you own and see what it is worth.",
    )
    portfolio_sub = portfolio.add_subparsers(dest="portfolio_command", metavar="<action>")
    portfolio_add = portfolio_sub.add_parser(
        "add",
        help="Record a purchase.",
        description=(
            "Record a purchase: which card, how many copies, and what you paid per copy (USD)."
        ),
    )
    portfolio_add.add_argument(
        "game",
        help="which game: " + ", ".join(sorted(game_adapters.REGISTRY)),
    )
    portfolio_add.add_argument(
        "card",
        help="card name",
    )
    portfolio_add.add_argument(
        "qty",
        help="how many copies, e.g. 4",
    )
    portfolio_add.add_argument(
        "price",
        metavar="PRICE",
        help="what you paid per copy (USD), e.g. 12.50",
    )
    portfolio_add.add_argument(
        "--pick",
        type=int,
        default=None,
        metavar="N",
        help="choose match number N instead of being asked",
    )
    portfolio_add.add_argument(
        "--first",
        action="store_true",
        help="take the recommended match without asking",
    )
    portfolio_sub.add_parser(
        "list",
        help="Show the portfolio.",
        description="Show the portfolio: current value, cost, and unrealized P&L.",
    )
    portfolio_remove = portfolio_sub.add_parser(
        "remove",
        help="Remove a lot.",
        description="Remove a lot from the portfolio.",
    )
    portfolio_remove.add_argument(
        "target",
        nargs="+",
        help="lot ID or card name",
    )

    for name, desc in COMMAND_DESCRIPTIONS.items():
        sub.add_parser(name, help=desc, description=desc)
    return parser


def _coming_soon(console: Console, command: str, as_json: bool) -> int:
    """Stub output for subcommands whose milestone has not landed yet."""
    if as_json:
        console.print(
            json.dumps(
                {
                    "command": command,
                    "status": "coming_soon",
                    "version": __version__,
                    "detail": COMMAND_DESCRIPTIONS[command],
                }
            )
        )
        return 0
    console.print(
        Panel(
            f"[bold]{command}[/bold] is still on the workbench.\n\n"
            f"{COMMAND_DESCRIPTIONS[command]}\n\n"
            "It is on the roadmap and it will get here. "
            "For now, `topdeck price` is the one that works.",
            title="topdeck",
            border_style="gold1",
        )
    )
    return 0


def _game_list() -> str:
    registry = game_adapters.REGISTRY
    return ", ".join(f"{k} ({registry[k].display_name})" for k in sorted(registry))


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _maybe_auto_sync(adapter, as_json: bool) -> None:
    """Sync a never-synced game before its first price lookup.

    Only "never" triggers: a stale sync still serves the live price
    path, so it does not surprise anyone with a bulk download. The
    sync's own progress runs on stderr, and it is completely silent
    under --json. A failed sync is a stderr note; the lookup then
    falls back to the existing live price path.
    """
    game = adapter.game_key
    if backbone.sync_status(game) != "never":
        return
    if not as_json:
        print(
            f'Price data for "{game}" was never synced. Syncing it now.',
            file=sys.stderr,
        )
    result = backbone.sync_game(game)
    if as_json:
        return
    if result.ok:
        print(
            f"Synced {game}: {result.groups} groups, {result.products} products with prices.",
            file=sys.stderr,
        )
    else:
        print(
            f"Could not sync {game} prices ({result.error or 'unknown error'}). "
            "Using live prices instead.",
            file=sys.stderr,
        )


def _price_sparkline(adapter, hit: CardHit) -> str | None:
    """Trend sparkline for the headline price row.

    Drawn from the last HISTORY_WINDOW_DAYS of recorded history, which
    get_prices has just extended with today's snapshot. Capped at
    TREND_COLUMN_CHARS blocks (the recent end), so the 80-column table
    always holds. None when the card has no join key or fewer than
    three priced days, in which case the table keeps its classic
    columns.
    """
    join_key = adapter.history_key(hit)
    if join_key is None:
        return None
    rows = backbone.get_history(adapter.game_key, join_key, days=output.HISTORY_WINDOW_DAYS)
    sparkline = output.render_sparkline(output.history_values(rows))
    if sparkline is not None and len(sparkline) > output.TREND_COLUMN_CHARS:
        # The table shows the recent end of the trend; the full
        # sparkline is in `topdeck history`.
        sparkline = sparkline[-output.TREND_COLUMN_CHARS :]
    return sparkline


def _did_you_mean(adapter, query: str) -> str:
    """A "Did you mean ...?" line from the synced name index, or "".

    Only used when the adapter itself found nothing; the TCGCSV
    adapters already resolve typo'd queries into real matches, so for
    them this stays silent.
    """
    suggestions = trigrams.suggest(backbone.db_path(), adapter.game_key, query, limit=3)
    if not suggestions:
        return ""
    names = ", ".join(f'"{suggestion.name}"' for suggestion in suggestions)
    return f" Did you mean: {names}?"


def cmd_price(args: argparse.Namespace, console: Console) -> int:
    adapter = game_adapters.resolve_game(args.game)
    if adapter is None:
        console.print(f'[red]Unknown game "{args.game}".[/red]')
        console.print(f"Valid games: {_game_list()}")
        return 2
    _maybe_auto_sync(adapter, args.json)
    if args.file is not None or not args.query:
        return cmd_price_from_list(args, adapter, console)
    query = " ".join(args.query).strip()
    try:
        hits = adapter.search(query)
    except SourceError as exc:
        console.print(f"[red]Could not look that up: {exc}[/red]")
        return 1
    if not hits:
        console.print(
            f'No matches for "{query}" in {adapter.display_name}.'
            f"{_did_you_mean(adapter, query)}"
            " Try a shorter query or check the spelling."
        )
        return 0

    ranked = game_adapters.rank_candidates(hits, query)
    alternatives: list = []
    recommended = True

    if len(ranked) == 1:
        chosen = ranked[0]
    elif args.pick is not None:
        if not 1 <= args.pick <= len(ranked):
            console.print(
                f"[red]--pick {args.pick} is out of range. "
                f"There are {len(ranked)} matches; pick 1 to {len(ranked)}.[/red]"
            )
            return 2
        chosen = ranked[args.pick - 1]
        recommended = args.pick == 1
        alternatives = [h for h in ranked if h is not chosen]
    elif args.first or args.json or not _is_interactive():
        chosen = ranked[0]
        alternatives = ranked[1:]
    else:
        picked = interactive_pick(console, ranked, query)
        if picked is None:
            return 1
        chosen = picked
        recommended = chosen is ranked[0]

    try:
        prices = adapter.get_prices(chosen)
    except SourceError as exc:
        console.print(f"[red]Could not fetch prices: {exc}[/red]")
        return 1

    if args.json:
        # Plain print, not rich: machine output must not be wrapped.
        print(
            json_payload(
                game=adapter.game_key,
                query=query,
                chosen=chosen,
                prices=prices,
                alternatives=alternatives,
                recommended=recommended,
            )
        )
        return 0

    print_result(
        console,
        adapter_name=adapter.display_name,
        trust_tier=adapter.trust_tier,
        hit=chosen,
        prices=prices,
        sparkline=_price_sparkline(adapter, chosen) if prices else None,
    )
    if alternatives:
        ranked_numbers = [i + 1 for i, h in enumerate(ranked) if h is not chosen]
        shown = alternatives[:9]
        noun = "match" if len(alternatives) == 1 else "matches"
        console.print()
        console.print(
            f"[dim]{len(alternatives)} other {noun}. Re-run with --pick N to choose one:[/dim]"
        )
        console.print(candidate_table(shown, numbers=ranked_numbers[:9], recommended=None))
    return 0


# ---------------------------------------------------------------------------
# Price history


def cmd_history(args: argparse.Namespace, console: Console, as_json: bool) -> int:
    """Show one card's recorded price history.

    Resolves the card exactly like the price command (same search,
    ranking, and disambiguation), then reads the backbone history.
    Read-only: looking at history never files a snapshot.
    """
    adapter = _resolve_game_or_error(args, console)
    if adapter is None:
        return 2
    query = " ".join(args.query).strip()
    ranked, error_kind = _search_ranked(adapter, query, console)
    if error_kind == "source":
        return 1
    if error_kind == "empty":
        return 0
    chosen, code = _pick_ranked(ranked, query, args, console)
    if code:
        return code
    join_key = adapter.history_key(chosen)
    rows = (
        backbone.get_history(adapter.game_key, join_key, days=output.HISTORY_WINDOW_DAYS)
        if join_key is not None
        else []
    )
    points = output.history_points(rows)
    stats = output.history_stats(points)
    if as_json:
        # Plain print, not rich: machine output must not be wrapped.
        print(
            output.history_json(
                game=adapter.game_key,
                query=query,
                card=chosen,
                window_days=output.HISTORY_WINDOW_DAYS,
                points=points,
                stats=stats,
            )
        )
        return 0
    console.print()
    console.print(output.card_line(chosen))
    if not points:
        console.print(
            f'No price history for "{chosen.name}" yet. '
            "Add it to your watchlist and run `topdeck check` to start building one."
        )
        return 0
    console.print(f"[dim]Last {output.HISTORY_WINDOW_DAYS} days:[/dim]")
    console.print(output.history_table(points))
    console.print(output.history_summary(stats, output.HISTORY_WINDOW_DAYS))
    return 0


# ---------------------------------------------------------------------------
# Decklist batch pricing


_DECKLIST_QTY = re.compile(r"^(\d+)\s*[xX]?\s+(.+)$")
_DECKLIST_QTY_ONLY = re.compile(r"^(\d+)\s*[xX]\s*$")


class _UnparseableLine(Exception):
    """A decklist line that looks like an entry but names no card."""


def _parse_decklist_line(stripped: str) -> tuple[int, str]:
    """Parse one non-blank, non-comment decklist line into (quantity, name)."""
    match = _DECKLIST_QTY.match(stripped)
    if match:
        quantity = int(match.group(1))
        name = match.group(2).strip()
        if quantity < 1 or not name:
            raise _UnparseableLine(f'"{stripped}" is not a usable entry')
        return quantity, name
    if _DECKLIST_QTY_ONLY.match(stripped):
        raise _UnparseableLine(f'"{stripped}" names no card')
    return 1, stripped


def parse_decklist(lines: list[str]) -> tuple[list[tuple[int, str]], list[str]]:
    """Parse decklist lines into (quantity, name) entries.

    Blank lines and # comments are skipped. Broken lines are skipped with
    a warning naming the line number; parsing never crashes.
    """
    entries: list[tuple[int, str]] = []
    warnings: list[str] = []
    for lineno, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            entries.append(_parse_decklist_line(stripped))
        except _UnparseableLine as exc:
            warnings.append(f"line {lineno}: {exc} (skipped)")
    return entries, warnings


@dataclass
class BatchLine:
    """One decklist entry after resolution."""

    query: str
    quantity: int
    hit: CardHit | None
    prices: list[Price]
    unit: Price | None  # the one price the line total is built on
    error: str | None
    note: str | None

    @property
    def line_total(self) -> float | None:
        if self.unit is None or self.unit.price is None:
            return None
        return self.quantity * self.unit.price


def _resolve_batch_card(
    adapter, name: str, args: argparse.Namespace, console: Console
) -> tuple[CardHit | None, list[Price], str | None, str | None]:
    """Resolve one decklist name. Returns (hit, prices, error, note).

    Never prints: batch JSON must stay pure on stdout, and terminal
    batch output renders problems inline in the table.
    """
    try:
        hits = adapter.search(name)
    except SourceError as exc:
        return None, [], f"Could not look that up: {exc}", None
    if not hits:
        return (
            None,
            [],
            f"No matches.{_did_you_mean(adapter, name)} Try a shorter query or check the spelling.",
            None,
        )
    ranked = game_adapters.rank_candidates(hits, name)
    note = None
    if len(ranked) == 1:
        chosen = ranked[0]
    elif args.pick is not None:
        if not 1 <= args.pick <= len(ranked):
            return (
                None,
                [],
                f"--pick {args.pick} is out of range ({len(ranked)} matches).",
                None,
            )
        chosen = ranked[args.pick - 1]
    elif args.first or args.json or not _is_interactive():
        chosen = ranked[0]
        if not args.first:
            note = f'{len(ranked)} matches; using top hit "{chosen.name}".'
    else:
        picked = interactive_pick(console, ranked, name)
        if picked is None:
            return None, [], "Skipped.", None
        chosen = picked
    try:
        prices = adapter.get_prices(chosen)
    except SourceError as exc:
        return chosen, [], f"Could not fetch prices: {exc}", note
    return chosen, prices, None, note


def cmd_price_batch(
    args: argparse.Namespace,
    adapter,
    entries: list[tuple[int, str]],
    warnings: list[str],
    console: Console,
) -> int:
    """Price every entry. One lookup per distinct card name."""
    distinct: list[str] = []
    seen: set[str] = set()
    for _, name in entries:
        key = name.strip().lower()
        if key not in seen:
            seen.add(key)
            distinct.append(name)
    notes: list[str] = []
    ticker = Progress("Pricing decklist", len(distinct))

    def _one(name: str) -> BatchLine:
        hit, prices, error, note = _resolve_batch_card(adapter, name, args, console)
        unit = pick_tracked_price(prices) if error is None else None
        if note:
            notes.append(f"{name}: {note}")
        ticker.tick()
        return BatchLine(
            query=name,
            quantity=0,
            hit=hit,
            prices=prices,
            unit=unit,
            error=error,
            note=note,
        )

    if _is_interactive() and not args.json and args.pick is None and not args.first:
        # The disambiguation picker needs the terminal to itself.
        resolved = [_one(name) for name in distinct]
    else:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(_one, distinct))
    ticker.finish()

    by_name = {name.strip().lower(): line for name, line in zip(distinct, resolved)}
    lines = [
        BatchLine(
            query=name,
            quantity=quantity,
            hit=base.hit,
            prices=base.prices,
            unit=base.unit,
            error=base.error,
            note=base.note,
        )
        for quantity, name in entries
        for base in [by_name[name.strip().lower()]]
    ]

    totals: dict[str, float] = {}
    for line in lines:
        if line.line_total is not None:
            totals[line.unit.currency] = totals.get(line.unit.currency, 0.0) + line.line_total

    if args.json or not _is_interactive():
        # Automatic top-hit choices get a stderr note, never silent.
        for note_text in notes:
            print(note_text, file=sys.stderr)
    if args.json:
        print(batch_json(game=adapter.game_key, lines=lines, grand_total=totals, warnings=warnings))
        return 0
    console.print(batch_table(lines))
    for text in grand_total_lines(totals):
        console.print(text)
    return 0


def cmd_price_from_list(args: argparse.Namespace, adapter, console: Console) -> int:
    """Batch mode: read the decklist from --file or stdin, then price it."""
    if args.file is not None and args.query:
        console.print("[red]Give either a card name or --file, not both.[/red]")
        return 2
    if args.file is not None:
        try:
            with open(args.file, encoding="utf-8") as handle:
                raw_lines = handle.read().splitlines()
        except OSError as exc:
            console.print(f'[red]Could not read "{args.file}": {exc}[/red]')
            return 1
    elif sys.stdin.isatty():
        console.print("[red]Give a card name, or a decklist via --file or stdin.[/red]")
        console.print('Example: topdeck price mtg "Black Lotus"')
        return 2
    else:
        raw_lines = sys.stdin.read().splitlines()
    entries, warnings = parse_decklist(raw_lines)
    for warning in warnings:
        print(warning, file=sys.stderr)
    if not entries:
        if args.json:
            print(batch_json(game=adapter.game_key, lines=[], grand_total={}, warnings=warnings))
        else:
            console.print("No card names found. The decklist is empty or only comments.")
        return 0
    return cmd_price_batch(args, adapter, entries, warnings, console)


# ---------------------------------------------------------------------------
# Watchlists


def _search_ranked(adapter, query: str, console: Console):
    """Search and rank. Returns (ranked, error_kind); error_kind is
    None, "source", or "empty". The message is already printed."""
    try:
        hits = adapter.search(query)
    except SourceError as exc:
        console.print(f"[red]Could not look that up: {exc}[/red]")
        return None, "source"
    if not hits:
        console.print(
            f'No matches for "{query}" in {adapter.display_name}.'
            f"{_did_you_mean(adapter, query)}"
            " Try a shorter query or check the spelling."
        )
        return None, "empty"
    return game_adapters.rank_candidates(hits, query), None


def _pick_ranked(ranked, query: str, args: argparse.Namespace, console: Console):
    """Disambiguate like the price command. Returns (hit, exit_code)."""
    if len(ranked) == 1:
        return ranked[0], 0
    if args.pick is not None:
        if not 1 <= args.pick <= len(ranked):
            console.print(
                f"[red]--pick {args.pick} is out of range. "
                f"There are {len(ranked)} matches; pick 1 to {len(ranked)}.[/red]"
            )
            return None, 2
        return ranked[args.pick - 1], 0
    if args.first or args.json or not _is_interactive():
        return ranked[0], 0
    picked = interactive_pick(console, ranked, query)
    if picked is None:
        return None, 1
    return picked, 0


def _parse_target(raw: str) -> tuple[float | None, str | None]:
    """Parse --target. Returns (value, error_message)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value) or value <= 0:
        return None, (
            f'--target "{raw}" is not a usable price. Give a positive number like --target 12.50.'
        )
    return value, None


def _resolve_game_or_error(args: argparse.Namespace, console: Console):
    adapter = game_adapters.resolve_game(args.game)
    if adapter is None:
        console.print(f'[red]Unknown game "{args.game}".[/red]')
        console.print(f"Valid games: {_game_list()}")
    return adapter


def cmd_watch_add(args: argparse.Namespace, console: Console, as_json: bool) -> int:
    adapter = _resolve_game_or_error(args, console)
    if adapter is None:
        return 2
    target = None
    if args.target is not None:
        target, error = _parse_target(args.target)
        if error:
            console.print(f"[red]{error}[/red]")
            return 2
    query = " ".join(args.query).strip()
    ranked, error_kind = _search_ranked(adapter, query, console)
    if error_kind == "source":
        return 1
    if error_kind == "empty":
        return 0
    chosen, code = _pick_ranked(ranked, query, args, console)
    if code:
        return code

    store = WatchStore()
    existing = store.find(adapter.game_key, chosen.card_id)
    if existing is not None:
        if target is not None:
            store.set_target(existing.id, target)
            refreshed = store.get(existing.id)
            if as_json:
                print(watch_json("target_updated", [refreshed]))
            else:
                console.print(f'Updated target for "{existing.name}" to ${target:,.2f}.')
            return 0
        if as_json:
            print(watch_json("already_on_watchlist", [existing]))
        else:
            console.print(f'"{existing.name}" is already on your watchlist.')
        return 0

    try:
        watch = store.add(
            adapter.game_key,
            chosen.card_id,
            chosen.name,
            chosen.set_name or "",
            target,
        )
    except sqlite3.IntegrityError:
        # Lost a race with another add; treat as already watched.
        existing = store.find(adapter.game_key, chosen.card_id)
        if as_json:
            print(watch_json("already_on_watchlist", [existing] if existing else []))
        else:
            console.print(f'"{chosen.name}" is already on your watchlist.')
        return 0

    if as_json:
        print(watch_json("added", [watch]))
    else:
        extra = f" Target: ${target:,.2f}." if target else ""
        console.print(f'Watching "{watch.name}" ({adapter.display_name}).{extra}')
    return 0


def cmd_watch_list(args: argparse.Namespace, console: Console, as_json: bool) -> int:
    watches = WatchStore().list()
    if as_json:
        print(watch_json("list", watches))
        return 0
    if not watches:
        console.print('Your watchlist is empty. Add a card with: topdeck watch add <game> "<card>"')
        return 0
    console.print(watch_table(watches))
    return 0


def cmd_watch_remove(args: argparse.Namespace, console: Console, as_json: bool) -> int:
    key = " ".join(args.target).strip()
    store = WatchStore()
    candidates = []
    if key.isdigit():
        watch = store.get(int(key))
        if watch is not None:
            candidates = [watch]
    else:
        candidates = store.find_by_name(key)
    if not candidates:
        console.print(f'"{key}" is not on your watchlist.')
        return 1
    if len(candidates) > 1:
        console.print(f'Several watched cards are named "{key}". Remove one by ID:')
        console.print(watch_table(candidates))
        return 2
    removed = store.remove(candidates[0].id)
    if as_json:
        print(watch_json("removed", [removed] if removed else []))
    else:
        console.print(f'Stopped watching "{removed.name}".')
    return 0


def _check_one(
    store: WatchStore,
    watch,
    *,
    smart: bool = True,
    window: int = alerts.DEFAULT_WINDOW,
    deviation: float = alerts.DEFAULT_DEVIATION_PCT,
    band_k: float = alerts.DEFAULT_BAND_K,
):
    """Re-price one watch. Records a snapshot unless the source errored."""
    previous = store.previous_price(watch.id)
    adapter = game_adapters.resolve_game(watch.game)
    if adapter is None:
        return build_row(watch, previous, None, "USD", "", error=f'unknown game "{watch.game}"')
    try:
        hits = adapter.search(watch.name)
    except SourceError as exc:
        return build_row(watch, previous, None, "USD", "", error=str(exc))
    if not hits:
        return build_row(watch, previous, None, "USD", "", error="no longer listed by the source")
    ranked = game_adapters.rank_candidates(hits, watch.name)
    hit = next((h for h in ranked if h.card_id == watch.card_id), ranked[0])
    note = None
    if hit.card_id != watch.card_id:
        note = "exact printing no longer listed; showing closest match"
    try:
        prices = adapter.get_prices(hit)
    except SourceError as exc:
        return build_row(watch, previous, None, "USD", "", error=str(exc), note=note)
    tracked = pick_tracked_price(prices)
    smart_alerts = []
    if tracked is None:
        row = build_row(watch, previous, None, "USD", "", note=note or "no prices right now")
    else:
        # Smart alerts run on the USD history the lookup just recorded.
        # Non-USD tracked prices are left alone: the arithmetic is USD only.
        if smart and tracked.currency == "USD":
            join_key = adapter.history_key(hit)
            if join_key is not None:
                smart_alerts = alerts.check_card(
                    watch.game,
                    join_key,
                    window=window,
                    deviation_pct=deviation,
                    band_k=band_k,
                )
        row = build_row(
            watch,
            previous,
            tracked.price,
            tracked.currency,
            tracked.source,
            note=note,
            smart_alerts=smart_alerts,
        )
    store.record_snapshot(watch.id, row.current, row.currency, row.source)
    return row


def _check_parallel(
    store: WatchStore,
    watches,
    *,
    smart: bool = True,
    window: int = alerts.DEFAULT_WINDOW,
    deviation: float = alerts.DEFAULT_DEVIATION_PCT,
    band_k: float = alerts.DEFAULT_BAND_K,
) -> list:
    """Re-price every watch on threads.

    Network latency overlaps; the per-host throttle in net.fetch_json
    still paces every request, and pool.map keeps watchlist order.
    """
    ticker = Progress("Checking watchlist", len(watches))

    def _one(watch):
        row = _check_one(
            store, watch, smart=smart, window=window, deviation=deviation, band_k=band_k
        )
        ticker.tick()
        return row

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(_one, watches))
    ticker.finish()
    return rows


def cmd_check(args: argparse.Namespace, console: Console, as_json: bool) -> int:
    store = WatchStore()
    watches = store.list()
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.window < alerts.MIN_BASELINE_POINTS + 1:
        console.print(
            f"[red]--window must be at least {alerts.MIN_BASELINE_POINTS + 1}. "
            "Smart alerts need 5 baseline snapshots to say anything honest.[/red]"
        )
        return 2
    if not math.isfinite(args.deviation) or args.deviation <= 0:
        console.print("[red]--deviation must be a positive number.[/red]")
        return 2
    if not math.isfinite(args.band_k) or args.band_k < 0:
        console.print("[red]--band-k must be zero or a positive number.[/red]")
        return 2
    if not watches:
        if as_json:
            print(check_json([], args.alert_only, checked_at))
        else:
            console.print(
                'Your watchlist is empty. Add a card with: topdeck watch add <game> "<card>"'
            )
        return 0
    rows = _check_parallel(
        store,
        watches,
        smart=args.smart,
        window=args.window,
        deviation=args.deviation,
        band_k=args.band_k,
    )
    shown = [row for row in rows if row.alert] if args.alert_only else rows
    if as_json:
        print(check_json(shown, args.alert_only, checked_at))
        return 0
    if shown:
        console.print(check_table(shown))
    alert_count = sum(1 for row in rows if row.alert)
    total = len(rows)
    noun = "card" if total == 1 else "cards"
    if args.alert_only:
        if shown:
            console.print(f"{alert_count} of {total} watched {noun} need attention.")
        else:
            console.print(f"All quiet: {total} watched {noun}, no movers or target hits.")
    else:
        console.print(f"Checked {total} watched {noun}.", end="")
        if alert_count:
            console.print(f" {alert_count} need attention.")
        else:
            console.print()
    return 0


def cmd_doctor(args: argparse.Namespace, console: Console, as_json: bool) -> int:
    sources = check_sources()
    local = local_checks()
    if as_json:
        print(doctor_json(sources, local))
        return 0
    console.print(doctor_table(sources, local))
    down = [s for s in sources if s.status == "down"]
    if down:
        console.print(
            f"[red]{len(down)} price source(s) down. "
            "Prices from those games may be stale or missing.[/red]"
        )
    else:
        slow = [s for s in sources if s.status == "slow"]
        if slow:
            console.print("[yellow]All sources responding, some slowly.[/yellow]")
        else:
            console.print("[green]All price sources healthy.[/green]")
    return 0


def cmd_sync(args: argparse.Namespace, console: Console, as_json: bool) -> int:
    if args.game is not None and args.all:
        console.print("[red]Give either --all or one game, not both.[/red]")
        return 2
    if args.game is None:
        games = list(backbone.GAMES) if args.all else list(backbone.CORE_GAMES)
    else:
        key = args.game.strip().lower()
        key = game_adapters.GAME_ALIASES.get(key, key)
        if key not in backbone.GAMES:
            console.print(f'[red]Unknown game "{args.game}".[/red]')
            console.print(f"Valid games: {', '.join(backbone.GAMES)}")
            return 2
        games = [key]
    # One game's failure never stops the rest; the summary says which.
    results = [backbone.sync_game(game) for game in games]
    if as_json:
        print(sync_json(results))
    else:
        console.print(sync_table(results))
    return 1 if any(not res.ok for res in results) else 0


# ---------------------------------------------------------------------------
# Portfolios


def _parse_qty(raw: str) -> tuple[int | None, str | None]:
    """Parse the lot quantity. Returns (value, error_message)."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or value < 1:
        return None, (f'"{raw}" is not a usable quantity. Give a positive whole number like 4.')
    return value, None


def _parse_purchase_price(raw: str) -> tuple[float | None, str | None]:
    """Parse the per-copy purchase price in USD. Returns (value, error_message)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value) or value < 0:
        return None, (f'"{raw}" is not a usable price. Give a non-negative number like 12.50.')
    return value, None


def cmd_portfolio_add(args: argparse.Namespace, console: Console, as_json: bool) -> int:
    adapter = _resolve_game_or_error(args, console)
    if adapter is None:
        return 2
    qty, error = _parse_qty(args.qty)
    if error:
        console.print(f"[red]{error}[/red]")
        return 2
    purchase_price, error = _parse_purchase_price(args.price)
    if error:
        console.print(f"[red]{error}[/red]")
        return 2
    query = args.card.strip()
    ranked, error_kind = _search_ranked(adapter, query, console)
    if error_kind == "source":
        return 1
    if error_kind == "empty":
        return 0
    chosen, code = _pick_ranked(ranked, query, args, console)
    if code:
        return code

    store = PortfolioStore()
    lot = store.add(
        adapter.game_key,
        chosen.card_id,
        join_key_for(chosen),
        chosen.name,
        chosen.set_name or "",
        qty,
        purchase_price,
    )
    if as_json:
        print(portfolio_lot_json("added", lot))
    else:
        console.print(
            f'Added {qty}x "{lot.name}" to your portfolio at '
            f"${purchase_price:,.2f} per copy ({adapter.display_name})."
        )
    return 0


def cmd_portfolio_list(args: argparse.Namespace, console: Console, as_json: bool) -> int:
    del args  # no flags yet; list always shows everything
    store = PortfolioStore()
    lots = store.list()
    if not lots:
        if as_json:
            print(portfolio_list_json([], summarize([])))
        else:
            console.print(
                "Your portfolio is empty. Add a lot with: "
                'topdeck portfolio add <game> "<card>" <qty> <price>'
            )
        return 0
    holdings = [price_holding(lot) for lot in lots]
    summary = summarize(holdings)
    if as_json:
        print(portfolio_list_json(holdings, summary))
        return 0
    for line in portfolio_summary_lines(summary):
        console.print(line)
    console.print()
    console.print(holdings_table(holdings))
    if summary.excluded:
        noun = "holding" if summary.excluded == 1 else "holdings"
        verb = "has" if summary.excluded == 1 else "have"
        console.print(
            f"[dim]{summary.excluded} {noun} {verb} no current USD price "
            "and are excluded from the totals.[/dim]"
        )
    return 0


def cmd_portfolio_remove(args: argparse.Namespace, console: Console, as_json: bool) -> int:
    key = " ".join(args.target).strip()
    store = PortfolioStore()
    candidates = []
    if key.isdigit():
        lot = store.get(int(key))
        if lot is not None:
            candidates = [lot]
    else:
        candidates = store.find_by_name(key)
    if not candidates:
        console.print(f'"{key}" is not in your portfolio.')
        return 1
    if len(candidates) > 1:
        console.print(f'Several lots are named "{key}". Remove one by ID:')
        console.print(portfolio_lots_table(candidates))
        return 2
    removed = store.remove(candidates[0].id)
    if as_json:
        print(portfolio_lot_json("removed", removed))
    else:
        console.print(f'Removed {removed.qty}x "{removed.name}" from your portfolio.')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Progress goes to stderr; machine output must stay pure JSON.
    progress.set_enabled(not args.json)
    console = Console()
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "price":
        return cmd_price(args, console)
    if args.command == "history":
        return cmd_history(args, console, args.json)
    if args.command == "watch":
        action = args.watch_command or "list"
        if action == "add":
            return cmd_watch_add(args, console, args.json)
        if action == "remove":
            return cmd_watch_remove(args, console, args.json)
        return cmd_watch_list(args, console, args.json)
    if args.command == "check":
        return cmd_check(args, console, args.json)
    if args.command == "doctor":
        return cmd_doctor(args, console, args.json)
    if args.command == "sync":
        return cmd_sync(args, console, args.json)
    if args.command == "portfolio":
        action = args.portfolio_command or "list"
        if action == "add":
            return cmd_portfolio_add(args, console, args.json)
        if action == "remove":
            return cmd_portfolio_remove(args, console, args.json)
        return cmd_portfolio_list(args, console, args.json)
    return _coming_soon(console, args.command, args.json)


if __name__ == "__main__":
    sys.exit(main())
