"""Riftbound adapter.

There is no official price API, so TCGCSV category 89 (daily TCGplayer
dump) is the whole price stack. Coverage is real but thin, hence the
experimental tier: every price still carries its source and as-of.
"""

from __future__ import annotations

from topdeck.adapters.tcgcsv import TcgcsvBulkAdapter


class RiftboundAdapter(TcgcsvBulkAdapter):
    game_key = "riftbound"
    display_name = "Riftbound"
    trust_tier = "experimental"
    source_name = "tcgcsv"
    category_id = 89
