"""Disambiguation picker: arrow keys when possible, numbers otherwise.

Interactive terminals get a questionary arrow-key list with the
recommended match marked. Anything else gets a plain numbered list and
a typed choice. --json and piped output never prompt at all.
"""

from __future__ import annotations

import sys

from rich.console import Console

from topdeck.adapters.base import CardHit
from topdeck.output import candidate_table


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _questionary_pick(console: Console, hits: list[CardHit], query: str) -> int | None:
    """Arrow-key picker. Returns the chosen index, or None on failure."""
    try:
        import questionary
    except ImportError:
        return None
    choices = []
    for i, hit in enumerate(hits[:10]):
        label = f"{hit.name}  |  {hit.set_name or hit.set_code} #{hit.collector_number}"
        if i == 0:
            label += "  [recommended]"
        choices.append(questionary.Choice(title=label, value=i))
    try:
        answer = questionary.select(
            f'"{query}" matches {len(hits)} cards. Which one did you mean?',
            choices=choices,
            use_arrow_keys=True,
        ).ask()
    except (EOFError, KeyboardInterrupt):
        return None
    except Exception:
        return None
    return answer


def _numbered_pick(console: Console, hits: list[CardHit], query: str) -> int | None:
    """Plain numbered list with a typed choice. The recommended one is 1."""
    console.print()
    console.print(f'"{query}" matches {len(hits)} cards:')
    console.print(candidate_table(hits[:10]))
    try:
        raw = input("Pick a number [1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return 0
    try:
        index = int(raw) - 1
    except ValueError:
        return None
    if 0 <= index < min(len(hits), 10):
        return index
    return None


def interactive_pick(console: Console, hits: list[CardHit], query: str) -> CardHit | None:
    """Let the user choose among matches. None means they walked away."""
    if _tty():
        index = _questionary_pick(console, hits, query)
        if index is not None:
            return hits[index]
        # questionary missing or bailed; fall through to numbers
    index = _numbered_pick(console, hits, query)
    if index is None:
        console.print("No problem. Run it again when you know which one.")
        return None
    return hits[index]
