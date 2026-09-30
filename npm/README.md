# topdeck-prices

The npm installer for [topdeck](https://github.com/RogueAlg0/topdeck), the terminal-native TCG price watcher. Topdeck the price.

## What this installs

This package gives npm and Node users the same `topdeck` command that pip and uv users get. During install it fetches the real `topdeck` Python CLI from PyPI; the `topdeck` and `topdeck-mcp` shims then forward everything you type to it. If you already have `topdeck` installed, the installer leaves it alone.

Requirements: Node 18 or newer, plus `python3` with `pip` for the one-time install step.

## Commands

- `topdeck price <game> <query>` looks up live card prices across Magic, Pokémon, Lorcana, One Piece, and Riftbound
- `topdeck-mcp` runs the MCP server over stdio, with the same tools as the CLI

## JSON scripting

`topdeck` speaks JSON, so it slots right into shell pipelines:

```sh
topdeck price mtg "Black Lotus" --json | jq .
```

## Full docs

Everything else lives in the main repo: https://github.com/RogueAlg0/topdeck
