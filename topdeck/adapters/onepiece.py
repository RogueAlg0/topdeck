"""One Piece Card Game adapter.

TCGCSV category 68 carries the daily TCGplayer dump, which covers both
search and prices with no key required. optcgapi.com remains the nicer
community catalog and is a future upgrade path for card identity; it is
not needed for the price lookup itself.
"""

from __future__ import annotations

from topdeck.adapters.tcgcsv import TcgcsvBulkAdapter


class OnePieceAdapter(TcgcsvBulkAdapter):
    game_key = "onepiece"
    display_name = "One Piece Card Game"
    trust_tier = "solid"
    source_name = "tcgcsv"
    category_id = 68
