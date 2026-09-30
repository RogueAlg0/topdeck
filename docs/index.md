# topdeck

**Topdeck the price.**

Terminal-native TCG price watcher. Track your cards, catch the spikes, never pay full price for cardboard.

Magic, Pokemon, Lorcana, One Piece, and Riftbound. One CLI, no account, no app, no telemetry. Free forever.

## Quickstart

```bash
pipx install topdeck

topdeck watch           # track cards, get alerted on price moves
topdeck prices          # look up live market prices for a card
topdeck portfolio       # see what your collection is worth
topdeck ev              # expected value of opening a pack or box
topdeck --json watch    # machine-readable output for scripts
```

## Why a terminal tool

Phone apps want your account and your attention. topdeck wants neither. It is a small binary that answers one question fast: what is my cardboard worth right now, and is today a good day to buy or sell. Pipe it into cron, pipe it into jq, put it in your prompt. Your call.

## Design principles

- Free forever. Price data comes from free public APIs. There is nothing to sell you.
- No account, no telemetry. Your watchlist lives on your machine.
- Scriptable first. Every command speaks JSON with `--json`.
- Multi-game from day one. One interface, one adapter per game.

## Status

Early days. The 0.1.0 milestone wires up live price data across all five games. The CLI skeleton is in place; the [API reference](api.md) below documents what exists so far.
