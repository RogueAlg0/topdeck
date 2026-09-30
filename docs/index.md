# topdeck

**Topdeck the price.**

Terminal-native TCG price watcher. Track your cards, catch the spikes, never pay full price for cardboard.

Magic, Pokemon, Lorcana, One Piece, and Riftbound. One CLI, no account, no app, no telemetry. Free forever.

## Quickstart

```bash
pipx install topdeck

topdeck price mtg "Black Lotus"   # look up live market prices for a card
topdeck watch add mtg "Lightning Bolt" --target 2.50  # watch a card, alert at $2.50
topdeck watch list                # everything you are watching
topdeck check                     # re-price the watchlist, flag the movers
topdeck check --alert-only         # only movers, target hits, and errors (cron-friendly)
topdeck doctor                    # are the price sources healthy?
topdeck --json watch list         # machine-readable output for scripts
```

## Watchlists

`topdeck watch add <game> <card>` puts a card on your watchlist. Add `--target <price>` to set the price you would actually pay; `topdeck check` re-prices everything you watch and calls out TARGET HIT when a card drops to your number. Moves of 10% or more since the last check are flagged as spikes or drops. `topdeck check --alert-only` prints just the rows that need your attention, which makes it a good fit for a daily cron job. Your watchlist and its price history live in a small SQLite file under `~/.local/share/topdeck`, on your machine, nowhere else.

## Why a terminal tool

Phone apps want your account and your attention. topdeck wants neither. It is a small binary that answers one question fast: what is my cardboard worth right now, and is today a good day to buy or sell. Pipe it into cron, pipe it into jq, put it in your prompt. Your call.

## Design principles

- Free forever. Price data comes from free public APIs. There is nothing to sell you.
- No account, no telemetry. Your watchlist lives on your machine.
- Scriptable first. Every command speaks JSON with `--json`.
- Multi-game from day one. One interface, one adapter per game.

## Status

0.1.0 is in the works: live price lookups, watchlists with target alerts, re-price checks, and source health reports. The [API reference](api.md) below documents what exists so far.
