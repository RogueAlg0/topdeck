"""Command line interface for topdeck.

`price`, `watch`, `check`, and `doctor` are live. The remaining
subcommands land as their milestones do; until then each one explains
itself and exits cleanly. No telemetry.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel

from topdeck import __version__
from topdeck import adapters as game_adapters
from topdeck.doctor import check_sources, local_checks
from topdeck.net import SourceError
from topdeck.output import (
    candidate_table,
    check_json,
    check_table,
    doctor_json,
    doctor_table,
    json_payload,
    print_result,
    watch_json,
    watch_table,
)
from topdeck.pick import interactive_pick
from topdeck.watch import WatchStore, build_row, pick_tracked_price

COMMAND_DESCRIPTIONS = {
    "portfolio": "See the total value of the cards you own.",
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
        help="Look up the market price of a card.",
        description="Look up the market price of a card, across all five games.",
    )
    price.add_argument(
        "game",
        help="which game: " + ", ".join(sorted(game_adapters.REGISTRY)),
    )
    price.add_argument(
        "query",
        nargs="+",
        help='card name, e.g. topdeck price mtg "Black Lotus"',
    )
    price.add_argument(
        "--pick",
        type=int,
        default=None,
        metavar="N",
        help="choose match number N instead of being asked",
    )
    price.add_argument(
        "--first",
        action="store_true",
        help="take the recommended match without asking",
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

    sub.add_parser(
        "doctor",
        help="Check every price source.",
        description="Check every price source and report honestly which are healthy.",
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


def cmd_price(args: argparse.Namespace, console: Console) -> int:
    adapter = game_adapters.resolve_game(args.game)
    if adapter is None:
        console.print(f'[red]Unknown game "{args.game}".[/red]')
        console.print(f"Valid games: {_game_list()}")
        return 2
    query = " ".join(args.query).strip()
    try:
        hits = adapter.search(query)
    except SourceError as exc:
        console.print(f"[red]Could not look that up: {exc}[/red]")
        return 1
    if not hits:
        console.print(
            f'No matches for "{query}" in {adapter.display_name}. '
            "Try a shorter query or check the spelling."
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
            f'No matches for "{query}" in {adapter.display_name}. '
            "Try a shorter query or check the spelling."
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


def _check_one(store: WatchStore, watch):
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
    if tracked is None:
        row = build_row(watch, previous, None, "USD", "", note=note or "no prices right now")
    else:
        row = build_row(watch, previous, tracked.price, tracked.currency, tracked.source, note=note)
    store.record_snapshot(watch.id, row.current, row.currency, row.source)
    return row


def cmd_check(args: argparse.Namespace, console: Console, as_json: bool) -> int:
    store = WatchStore()
    watches = store.list()
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not watches:
        if as_json:
            print(check_json([], args.alert_only, checked_at))
        else:
            console.print(
                'Your watchlist is empty. Add a card with: topdeck watch add <game> "<card>"'
            )
        return 0
    rows = [_check_one(store, watch) for watch in watches]
    shown = [row for row in rows if row.alert] if args.alert_only else rows
    if as_json:
        print(check_json(shown, args.alert_only, checked_at))
        return 0
    if shown:
        console.print(check_table(shown))
    alerts = sum(1 for row in rows if row.alert)
    total = len(rows)
    noun = "card" if total == 1 else "cards"
    if args.alert_only:
        if shown:
            console.print(f"{alerts} of {total} watched {noun} need attention.")
        else:
            console.print(f"All quiet: {total} watched {noun}, no movers or target hits.")
    else:
        console.print(f"Checked {total} watched {noun}.", end="")
        if alerts:
            console.print(f" {alerts} need attention.")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "price":
        return cmd_price(args, console)
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
    return _coming_soon(console, args.command, args.json)


if __name__ == "__main__":
    sys.exit(main())
