"""Game registry: every supported game behind one lookup."""

from __future__ import annotations

from topdeck import backbone
from topdeck.adapters.base import (
    CardHit,
    GameAdapter,
    Price,
    prices_from_cents,
    rank_candidates,
)
from topdeck.adapters.lorcana import LorcastAdapter
from topdeck.adapters.mtg import ScryfallAdapter
from topdeck.adapters.onepiece import OnePieceAdapter
from topdeck.adapters.pokemon import TcgdexAdapter
from topdeck.adapters.riftbound import RiftboundAdapter
from topdeck.adapters.tcgcsv import TcgcsvBulkAdapter


def _tcgcsv_game(game_key: str, display_name: str, category_id: int) -> TcgcsvBulkAdapter:
    """One parameterized adapter per TCGCSV-only game.

    Coverage for these games is unverified, hence the beta tier. No
    per-game code: the category id is the only thing that differs.
    """
    adapter = TcgcsvBulkAdapter()
    adapter.game_key = game_key
    adapter.display_name = display_name
    adapter.category_id = category_id
    adapter.source_name = "tcgcsv"
    adapter.trust_tier = "beta"
    return adapter


REGISTRY: dict[str, GameAdapter] = {
    "mtg": ScryfallAdapter(),
    "pokemon": TcgdexAdapter(),
    "lorcana": LorcastAdapter(),
    "onepiece": OnePieceAdapter(),
    "riftbound": RiftboundAdapter(),
}
for _key in backbone.GAMES:
    if _key not in REGISTRY:
        REGISTRY[_key] = _tcgcsv_game(_key, backbone.GAME_NAMES[_key], backbone.CATEGORY_IDS[_key])
del _key

GAME_ALIASES = {
    "magic": "mtg",
    "ygo": "yugioh",
    "yu-gi-oh": "yugioh",
}


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
    "prices_from_cents",
    "rank_candidates",
    "resolve_game",
]
