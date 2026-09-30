# topdeck

**Topdeck the price.**

[![CI](https://img.shields.io/github/actions/workflow/status/RogueAlg0/topdeck/ci.yml?label=CI)](https://github.com/RogueAlg0/topdeck/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/RogueAlg0/topdeck/badge)](https://scorecard.dev/viewer/?uri=github.com/RogueAlg0/topdeck)

Terminal-native TCG price watcher. Track your cards, catch the spikes, never pay full price for cardboard.

Magic, Pokemon, Lorcana, One Piece, and Riftbound. One CLI, no account, no app, no telemetry. Free forever.

## Status

0.1.0 is in the works: live price lookups, watchlists with target alerts, re-price checks with spike and drop flags, and a `doctor` command that reports on every price source. `portfolio` and `ev` are still on the workbench.

## Install

```bash
pipx install topdeck
```

Coming with the 0.1.0 release. Not on PyPI yet.

## Quickstart

```bash
topdeck price mtg "Black Lotus"   # look up live market prices for a card
topdeck watch add mtg "Lightning Bolt" --target 2.50  # watch a card, alert at $2.50
topdeck watch list                # everything you are watching
topdeck check                     # re-price the watchlist, flag the movers
topdeck check --alert-only         # only movers, target hits, and errors (cron-friendly)
topdeck doctor                    # are the price sources healthy?
topdeck --json watch list         # machine-readable output for scripts
```

## MCP server

`topdeck-mcp` serves the same price data to MCP clients over stdio. Two tools: `search_cards` returns the ranked candidate list for a name, and `price_lookup` returns prices with market, currency, condition, printing, as-of, and source on every number. It never prompts; ambiguous queries return the candidates so the caller can decide.

## Why a terminal tool

Phone apps want your account and your attention. topdeck wants neither. It is a small binary that answers one question fast: what is my cardboard worth right now, and is today a good day to buy or sell. Pipe it into cron, pipe it into jq, put it in your prompt. Your call.

## Design principles

- Free forever. Price data comes from free public APIs. There is nothing to sell you.
- No account, no telemetry. Your watchlist lives on your machine.
- Scriptable first. Every command speaks JSON with `--json`.
- Multi-game from day one. One interface, one adapter per game.

## Roadmap

0.1.0: live prices, watchlists with target alerts, re-price checks, and source health reports for all five games. The full plan lives in [ROADMAP.md](ROADMAP.md).

## Contributing

Small PRs welcome. See CONTRIBUTING.md. Keep the CLI fast and the output honest.
