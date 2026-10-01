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
topdeck check --no-smart           # skip the price-history smart alerts
topdeck history mtg "Lightning Bolt"  # 30-day price trend: series, min/max, % change
topdeck doctor                    # are the price sources healthy?
topdeck portfolio add mtg "Lightning Bolt" 4 1.25  # record a purchase
topdeck portfolio                                  # value, cost, unrealized P&L
topdeck --json watch list         # machine-readable output for scripts
```

## Portfolio

`topdeck portfolio add <game> <card> <qty> <price>` records a purchase: how many copies and what you paid per copy (USD). `topdeck portfolio` re-prices every lot at current prices and reports total value, total cost, and unrealized profit and loss, with your biggest holdings first. Holdings whose game's synced price data is stale say so, and lots with no current USD price are listed separately instead of being folded silently into the totals. `topdeck portfolio remove <id or name>` drops a lot. Like the watchlist, your portfolio lives in a small SQLite file under `~/.local/share/topdeck`, on your machine, nowhere else.

## Watchlists

`topdeck watch add <game> <card>` puts a card on your watchlist. Add `--target <price>` to set the price you would actually pay; `topdeck check` re-prices everything you watch and calls out TARGET HIT when a card drops to your number. Moves of 10% or more since the last check are flagged as spikes or drops. Smart alerts go further: with 5 or more daily price snapshots on file, `check` compares the new price against the 14-day moving average (alerting past 15% deviation) and against 14-day Bollinger-style bands at 2 standard deviations. Tune them with `--window N`, `--deviation PCT`, and `--band-k K`, or turn them off with `--no-smart`. `topdeck check --alert-only` prints just the rows that need your attention, which makes it a good fit for a daily cron job. Your watchlist and its price history live in a small SQLite file under `~/.local/share/topdeck`, on your machine, nowhere else.

## Search

Card search tolerates typos: `topdeck sync` builds a trigram index over every synced card name, so "Lighnting Bolt" still finds Lightning Bolt. When a query matches nothing, the CLI suggests the closest names from the index.

## Price quality flags

The price table marks rows it does not trust. A `!` next to a price means that printing sits absurdly far from the card's other printings (a bad data row, flagged rather than removed). Cards from sets released in the last 14 days get a volatility note: release-week prices swing hard, so read them with skepticism. Both flags also appear in `--json` output.

## Why a terminal tool

Phone apps want your account and your attention. topdeck wants neither. It is a small binary that answers one question fast: what is my cardboard worth right now, and is today a good day to buy or sell. Pipe it into cron, pipe it into jq, put it in your prompt. Your call.

## Design principles

- Free forever. Price data comes from free public APIs. There is nothing to sell you.
- No account, no telemetry. Your watchlist lives on your machine.
- Scriptable first. Every command speaks JSON with `--json`.
- Multi-game from day one. One interface, one adapter per game.

## Status

0.1.0 is in the works: live price lookups, watchlists with target alerts, re-price checks, source health reports, and a portfolio that tracks what you own with profit and loss. The [API reference](api.md) below documents what exists so far.
