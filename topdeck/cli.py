"""Command line interface for topdeck.

`price` is live. The remaining subcommands land as their milestones do;
until then each one explains itself and exits cleanly. No telemetry.
"""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.panel import Panel

from topdeck import __version__
from topdeck import adapters as game_adapters
from topdeck.net import SourceError
from topdeck.output import candidate_table, json_payload, print_result
from topdeck.pick import interactive_pick

COMMAND_DESCRIPTIONS = {
    "watch": "Track cards on a watchlist and get alerted when prices move.",
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "price":
        return cmd_price(args, console)
    return _coming_soon(console, args.command, args.json)


if __name__ == "__main__":
    sys.exit(main())
