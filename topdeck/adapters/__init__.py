"""Game registry: every supported game behind one lookup."""

from __future__ import annotations

from topdeck.adapters.base import CardHit, GameAdapter, Price, rank_candidates
from topdeck.adapters.lorcana import LorcastAdapter
from topdeck.adapters.mtg import ScryfallAdapter
from topdeck.adapters.onepiece import OnePieceAdapter
from topdeck.adapters.pokemon import TcgdexAdapter
from topdeck.adapters.riftbound import RiftboundAdapter

REGISTRY: dict[str, GameAdapter] = {
    "mtg": ScryfallAdapter(),
    "pokemon": TcgdexAdapter(),
    "lorcana": LorcastAdapter(),
    "onepiece": OnePieceAdapter(),
    "riftbound": RiftboundAdapter(),
}

GAME_ALIASES = {"magic": "mtg"}


def resolve_game(raw: str) -> GameAdapter | None:
    """Map a user's game word to an adapter, or None if unknown."""
    key = raw.strip().lower()
    key = GAME_ALIASES.get(key, key)
    return REGISTRY.get(key)


__all__ = [
    "CardHit",
    "GameAdapter",
    "Price",
    "GAME_ALIASES",
    "REGISTRY",
    "rank_candidates",
    "resolve_game",
]
