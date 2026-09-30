"""Local TCGCSV price backbone: a daily bulk sync into SQLite.

`topdeck sync` downloads TCGCSV's per-game product and price dumps into
a local SQLite database under the XDG cache dir. The database is a price
sidecar on purpose: it stores only (game, join_key, market_cents,
mid_cents). Card identity and search stay with the per-game catalog
adapters (Scryfall, TCGdex, Lorcast, TCGCSV groups); the join key is the
TCGplayer product ID each of those adapters already carries.

Freshness rule: the database is only trusted while the per-game sync
timestamp is younger than FRESHNESS_HOURS. Older data is never served
as current; lookups fall back to the live path instead.
"""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from topdeck import net
from topdeck.progress import Progress

SCHEMA_VERSION = 2

# How long a sync stays trustworthy. Older data is never served as
# current; lookups fall back to the live path until the next sync.
FRESHNESS_HOURS = 36

# TCGCSV category per game, verified against /tcgplayer/categories.
# Every card-game category is listed; supplies, comics, miniatures,
# and other non-card-game categories are left out.
CATEGORY_IDS = {
    "mtg": 1,
    "yugioh": 2,
    "pokemon": 3,
    "epic": 7,
    "redakai": 10,
    "wow": 13,
    "vanguard": 16,
    "fow": 17,
    "buddyfight": 19,
    "weiss": 20,
    "mlp": 21,
    "dbz": 23,
    "finalfantasy": 24,
    "universus": 25,
    "swdestiny": 26,
    "dbsm": 27,
    "dragoborne": 28,
    "metax": 30,
    "zombie": 36,
    "caster": 37,
    "mlpccg": 38,
    "exodus": 47,
    "lightseekers": 48,
    "munchkin": 53,
    "aoschampions": 54,
    "architect": 55,
    "transformers": 57,
    "bakugan": 58,
    "keyforge": 59,
    "clash": 60,
    "argent": 61,
    "fab": 62,
    "digimon": 63,
    "altsouls": 64,
    "gateruler": 65,
    "metazoo": 66,
    "wixoss": 67,
    "onepiece": 68,
    "lorcana": 71,
    "bss": 72,
    "svevolve": 73,
    "grandarchive": 74,
    "akora": 75,
    "kryptik": 76,
    "sorcery": 77,
    "alphaclash": 78,
    "swu": 79,
    "dbsfw": 80,
    "unionarena": 81,
    "elestrals": 83,
    "neopets": 84,
    "pokemonjp": 85,
    "gundam": 86,
    "hololive": 87,
    "godzilla": 88,
    "riftbound": 89,
    "cookierun": 90,
    "palworld": 91,
    "cyberpunk": 92,
    "naruto": 93,
    "ikorr": 94,
}

GAME_NAMES = {
    "mtg": "Magic: The Gathering",
    "yugioh": "YuGiOh",
    "pokemon": "Pokemon",
    "epic": "Epic Card Game",
    "redakai": "Redakai",
    "wow": "World of Warcraft TCG",
    "vanguard": "Cardfight!! Vanguard",
    "fow": "Force of Will",
    "buddyfight": "Future Card Buddyfight",
    "weiss": "Weiss Schwarz",
    "mlp": "My Little Pony",
    "dbz": "Dragon Ball Z TCG",
    "finalfantasy": "Final Fantasy TCG",
    "universus": "UniVersus",
    "swdestiny": "Star Wars: Destiny",
    "dbsm": "Dragon Ball Super: Masters",
    "dragoborne": "Dragoborne",
    "metax": "MetaX TCG",
    "zombie": "Zombie World Order TCG",
    "caster": "The Caster Chronicles",
    "mlpccg": "My Little Pony CCG",
    "exodus": "Exodus TCG",
    "lightseekers": "Lightseekers TCG",
    "munchkin": "Munchkin CCG",
    "aoschampions": "Warhammer Age of Sigmar: Champions",
    "architect": "Architect TCG",
    "transformers": "Transformers TCG",
    "bakugan": "Bakugan TCG",
    "keyforge": "KeyForge",
    "clash": "Chrono Clash System",
    "argent": "Argent Saga TCG",
    "fab": "Flesh and Blood TCG",
    "digimon": "Digimon Card Game",
    "altsouls": "Alternate Souls",
    "gateruler": "Gate Ruler",
    "metazoo": "MetaZoo",
    "wixoss": "WIXOSS",
    "onepiece": "One Piece Card Game",
    "lorcana": "Disney Lorcana",
    "bss": "Battle Spirits Saga",
    "svevolve": "Shadowverse: Evolve",
    "grandarchive": "Grand Archive TCG",
    "akora": "Akora TCG",
    "kryptik": "Kryptik TCG",
    "sorcery": "Sorcery: Contested Realm",
    "alphaclash": "Alpha Clash",
    "swu": "Star Wars: Unlimited",
    "dbsfw": "Dragon Ball Super: Fusion World",
    "unionarena": "Union Arena",
    "elestrals": "Elestrals",
    "neopets": "Neopets Battledome",
    "pokemonjp": "Pokemon Japan",
    "gundam": "Gundam Card Game",
    "hololive": "hololive Official Card Game",
    "godzilla": "Godzilla Card Game",
    "riftbound": "Riftbound",
    "cookierun": "CookieRun: Braverse TCG",
    "palworld": "Palworld Official Card Game",
    "cyberpunk": "Cyberpunk TCG",
    "naruto": "Naruto Card Game",
    "ikorr": "Rush of Ikorr",
}

GAMES = tuple(GAME_NAMES)

_SYNC_WORKERS = 8
# Same politeness as the live adapter; threads overlap latency, the
# per-host token bucket still spaces every request.
_MIN_INTERVAL = 0.3

# Price sidecar on purpose: no names, no sets, no currency column
# (always USD), money as integer cents. Card identity lives with the
# catalog adapters; this table answers one question per row.
_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE prices (
  game TEXT NOT NULL,
  join_key INTEGER NOT NULL,
  market_cents INTEGER,
  mid_cents INTEGER,
  PRIMARY KEY (game, join_key)
) WITHOUT ROWID;
"""


def backbone_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "topdeck", "tcgcsv")


def db_path() -> str:
    return os.path.join(backbone_dir(), "prices.db")


def _connect() -> sqlite3.Connection:
    """Open the backbone database, rebuilding it on schema mismatch.

    There are no migrations: a version bump wipes the tables and the
    next `topdeck sync` repopulates them.
    """
    os.makedirs(backbone_dir(), exist_ok=True)
    path = db_path()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = row[0] if row else None
    except sqlite3.Error:
        version = None
    if version != str(SCHEMA_VERSION):
        try:
            conn.executescript("DROP TABLE IF EXISTS prices; DROP TABLE IF EXISTS meta;")
        except sqlite3.DatabaseError:
            # Not a database file at all: remove it and start over.
            conn.close()
            os.remove(path)
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    return conn


def _meta(key: str) -> str | None:
    if not os.path.exists(db_path()):
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def synced_at(game: str) -> str | None:
    """ISO timestamp of the last successful sync for a game, if any."""
    return _meta(f"synced_at:{game}")


def sync_status(game: str, *, now: datetime | None = None) -> str:
    """Report one game's backbone state: "never", "fresh", or "stale".

    `now` is injectable so tests never depend on the wall clock.
    """
    stamp = synced_at(game)
    if stamp is None:
        return "never"
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        # A sync happened but its timestamp is unreadable: do not trust it.
        return "stale"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current - moment <= timedelta(hours=FRESHNESS_HOURS):
        return "fresh"
    return "stale"


def lookup_price(game: str, join_key: int) -> dict | None:
    """Fetch one synced price row, or None.

    Returns {"market_cents", "mid_cents", "as_of"} only when the game's
    data is fresh; stale, missing, or unreadable data is never served
    as current, so callers fall back to their live price path. This is
    the single decision point for the freshness rule.
    """
    if sync_status(game) != "fresh":
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT market_cents, mid_cents FROM prices WHERE game = ? AND join_key = ?",
            (game, join_key),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"market_cents": row[0], "mid_cents": row[1], "as_of": synced_at(game) or ""}


def _pick_price_row(rows: list[dict]) -> dict | None:
    """The Normal-subtype row is the headline market price; else the first."""
    if not rows:
        return None
    for row in rows:
        if str(row.get("subTypeName", "")) == "Normal":
            return row
    return rows[0]


def _to_cents(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(round(float(raw) * 100))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _group_rows(category_id: int, group: dict, progress: Progress) -> list[tuple]:
    """Download one group's products and prices; return backbone rows."""
    group_id = group["groupId"]
    products = net.fetch_json(
        f"https://tcgcsv.com/tcgplayer/{category_id}/{group_id}/products",
        ttl=0,
        min_interval=_MIN_INTERVAL,
        user_agent=net.BROWSER_UA,
    )
    prices = net.fetch_json(
        f"https://tcgcsv.com/tcgplayer/{category_id}/{group_id}/prices",
        ttl=0,
        min_interval=_MIN_INTERVAL,
        user_agent=net.BROWSER_UA,
    )
    by_product: dict[int, list[dict]] = {}
    price_rows = prices.get("results", []) if isinstance(prices, dict) else []
    for row in price_rows:
        if isinstance(row, dict) and row.get("productId") is not None:
            by_product.setdefault(row["productId"], []).append(row)
    out: list[tuple] = []
    prod_rows = products.get("results", []) if isinstance(products, dict) else []
    for prod in prod_rows:
        if not isinstance(prod, dict) or prod.get("productId") is None:
            continue
        chosen = _pick_price_row(by_product.get(prod["productId"], []))
        if chosen is None:
            continue
        market = _to_cents(chosen.get("marketPrice"))
        mid = _to_cents(chosen.get("midPrice"))
        if market is None and mid is None:
            continue
        out.append((prod["productId"], market, mid))
    progress.tick()
    return out


def _store(game: str, rows: list[tuple]) -> None:
    """Replace one game's rows and stamp the sync time, atomically."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM prices WHERE game = ?", (game,))
        conn.executemany(
            "INSERT INTO prices (game, join_key, market_cents, mid_cents) VALUES (?, ?, ?, ?)",
            [(game, *row) for row in rows],
        )
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"synced_at:{game}", stamp),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_groups(category_id: int) -> list[dict]:
    data = net.fetch_json(
        f"https://tcgcsv.com/tcgplayer/{category_id}/groups",
        ttl=0,
        min_interval=_MIN_INTERVAL,
        user_agent=net.BROWSER_UA,
    )
    results = data.get("results", []) if isinstance(data, dict) else []
    return [g for g in results if isinstance(g, dict) and g.get("groupId") is not None]


@dataclass
class SyncResult:
    game: str
    ok: bool
    groups: int = 0
    products: int = 0
    error: str = ""


def sync_game(game: str) -> SyncResult:
    """Download one game's bulk data into the backbone database.

    Never raises: a failed game is a failed SyncResult, so one game's
    outage cannot kill the rest of a `topdeck sync` run.
    """
    category_id = CATEGORY_IDS[game]
    try:
        groups = _fetch_groups(category_id)
        progress = Progress(f"Syncing {GAME_NAMES[game]}", len(groups))

        def _one(group: dict) -> list[tuple]:
            return _group_rows(category_id, group, progress)

        rows: list[tuple] = []
        with ThreadPoolExecutor(max_workers=_SYNC_WORKERS) as pool:
            for chunk in pool.map(_one, groups):
                rows.extend(chunk)
        progress.finish()
        _store(game, rows)
    except Exception as exc:  # one game's failure stays one game's failure
        return SyncResult(game=game, ok=False, error=str(exc))
    return SyncResult(game=game, ok=True, groups=len(groups), products=len(rows))
