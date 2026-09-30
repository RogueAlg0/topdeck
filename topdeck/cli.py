"""Command line interface for topdeck.

Subcommand implementations land in 0.1.0. Until then each subcommand
explains itself and exits cleanly. No network calls, no telemetry.
"""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.panel import Panel

from topdeck import __version__

COMMAND_DESCRIPTIONS = {
    "watch": "Track cards on a watchlist and get alerted when prices move.",
    "prices": "Look up live market prices for a card.",
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
    for name, desc in COMMAND_DESCRIPTIONS.items():
        sub.add_parser(name, help=desc, description=desc)
    return parser


def _coming_soon(console: Console, command: str, as_json: bool) -> int:
    """Stub output for subcommands whose implementation lands in 0.1.0."""
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
            f"[bold]{command}[/bold] is landing in 0.1.0.\n\n"
            f"{COMMAND_DESCRIPTIONS[command]}\n\n"
            "The price research is still being gathered, so the real "
            "thing is not wired up yet. Check back soon.",
            title="topdeck",
            border_style="gold1",
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()
    if args.command is None:
        parser.print_help()
        return 0
    return _coming_soon(console, args.command, args.json)


if __name__ == "__main__":
    sys.exit(main())
