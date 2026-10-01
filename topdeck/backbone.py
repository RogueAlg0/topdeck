"""Local TCGCSV price backbone: a daily bulk sync into SQLite.

`topdeck sync` downloads TCGCSV's per-game product and price dumps into
a local SQLite database under the XDG cache dir. The database is a price
sidecar on purpose: it stores only (game, join_key, market_cents,
mid_cents). Card identity and search stay with the per-game catalog
adapters (Scryfall, TCGdex, Lorcast, TCGCSV groups); the join key is the
TCGplayer product ID each of those adapters already carries. Alongside
the prices, each sync rebuilds a character-trigram index over the
synced product names, so typo'd queries still find their card.

A second table, price_history, keeps one row per game/join_key/day so
later work can chart prices and alert on moves. Every price lookup
(single, decklist batch, watch check) records its headline USD leg.

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
from typing import TYPE_CHECKING

from topdeck import net, trigrams
from topdeck.progress import Progress

if TYPE_CHECKING:
    from topdeck.adapters.base import Price

SCHEMA_VERSION = 4

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

# The five core games `topdeck sync` refreshes by default. A full
# sweep takes 25-40 minutes, so the default covers the games the CLI
# itself supports; `topdeck sync --all` does the rest.
CORE_GAMES = ("mtg", "pokemon", "lorcana", "onepiece", "riftbound")

_SYNC_WORKERS = 8
# Same politeness as the live adapter; threads overlap latency, the
# per-host token bucket still spaces every request.
_MIN_INTERVAL = 0.3

# Price sidecar on purpose: no names, no sets, no currency column
# (always USD), money as integer cents. Card identity lives with the
# catalog adapters; the prices table answers one question per row.
# price_history keeps one snapshot per game/join_key/day so prices can
# be charted and alerts can fire on moves. The names and postings
# tables are only the trigram search corpus, rebuilt from each sync.
_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE prices (
  game TEXT NOT NULL,
  join_key INTEGER NOT NULL,
  market_cents INTEGER,
  mid_cents INTEGER,
  PRIMARY KEY (game, join_key)
) WITHOUT ROWID;
CREATE TABLE price_history (
  game TEXT NOT NULL,
  join_key INTEGER NOT NULL,
  date TEXT NOT NULL,
  market_cents INTEGER,
  mid_cents INTEGER,
  source TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (game, join_key, date)
) WITHOUT ROWID;
CREATE TABLE names (
  game TEXT NOT NULL,
  join_key INTEGER NOT NULL,
  name TEXT NOT NULL,
  set_name TEXT NOT NULL,
  set_code TEXT NOT NULL,
  PRIMARY KEY (game, join_key)
) WITHOUT ROWID;
CREATE TABLE postings (
  game TEXT NOT NULL,
  trigram TEXT NOT NULL,
  join_key INTEGER NOT NULL,
  PRIMARY KEY (game, trigram, join_key)
) WITHOUT ROWID;
CREATE INDEX postings_game_trigram ON postings (game, trigram);
"""

# History rows older than this are pruned on every snapshot write.
HISTORY_DAYS = 365


def backbone_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "topdeck", "tcgcsv")


def db_path() -> str:
    return os.path.join(backbone_dir(), "prices.db")


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _schema_version(conn: sqlite3.Connection) -> str | None:
    """The stored schema version, or None when it cannot be read."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Additive upgrade: keep every prices/meta row, add price_history."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS price_history ("
        "game TEXT NOT NULL, "
        "join_key INTEGER NOT NULL, "
        "date TEXT NOT NULL, "
        "market_cents INTEGER, "
        "mid_cents INTEGER, "
        "source TEXT NOT NULL DEFAULT '', "
        "PRIMARY KEY (game, join_key, date)) WITHOUT ROWID"
    )
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '3')")
    conn.commit()


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Additive upgrade: keep every row, add the trigram search corpus."""
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS names ("
        "game TEXT NOT NULL, "
        "join_key INTEGER NOT NULL, "
        "name TEXT NOT NULL, "
        "set_name TEXT NOT NULL, "
        "set_code TEXT NOT NULL, "
        "PRIMARY KEY (game, join_key)) WITHOUT ROWID;"
        " CREATE TABLE IF NOT EXISTS postings ("
        "game TEXT NOT NULL, "
        "trigram TEXT NOT NULL, "
        "join_key INTEGER NOT NULL, "
        "PRIMARY KEY (game, trigram, join_key)) WITHOUT ROWID;"
        " CREATE INDEX IF NOT EXISTS postings_game_trigram"
        " ON postings (game, trigram);"
    )
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '4')")
    conn.commit()


def _rebuild() -> sqlite3.Connection:
    """Build a fresh database, deleting whatever was there before."""
    path = db_path()
    conn = _open()
    try:
        conn.executescript(
            "DROP TABLE IF EXISTS prices;"
            " DROP TABLE IF EXISTS meta;"
            " DROP TABLE IF EXISTS price_history;"
            " DROP TABLE IF EXISTS names;"
            " DROP TABLE IF EXISTS postings;"
        )
    except sqlite3.DatabaseError:
        # Not a database file at all: remove it and start over.
        conn.close()
        os.remove(path)
        conn = _open()
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _connect() -> sqlite3.Connection:
    """Open the backbone database, upgrading or rebuilding as needed.

    Known older schemas upgrade in place: version 2 gains price_history
    (v3), then the trigram search corpus (v4), keeping every row. A fresh
    file, an unknown version, or a file that is not a database at all
    rebuilds from scratch; the next `topdeck sync` repopulates it.
    """
    os.makedirs(backbone_dir(), exist_ok=True)
    conn = _open()
    version = _schema_version(conn)
    if version == str(SCHEMA_VERSION):
        return conn
    if version == "2":
        _migrate_v2_to_v3(conn)
        version = "3"
    if version == "3":
        _migrate_v3_to_v4(conn)
        return conn
    conn.close()
    return _rebuild()


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


def record_history(
    game: str,
    join_key: int,
    market_cents: int | None,
    mid_cents: int | None,
    source: str,
    *,
    date: str | None = None,
) -> None:
    """Write one price-history snapshot for a game/join_key/day.

    A second snapshot for the same day is an UPSERT: it replaces the
    first, so there is always exactly one row per day. Rows older than
    HISTORY_DAYS are pruned on the same write, which keeps the delete
    cheap and the table bounded. `date` is YYYY-MM-DD, defaulting to
    today (UTC); tests pass it to freeze the clock.
    """
    day = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO price_history (game, join_key, date, market_cents, mid_cents, source)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (game, join_key, date) DO UPDATE SET"
            " market_cents = excluded.market_cents,"
            " mid_cents = excluded.mid_cents,"
            " source = excluded.source",
            (game, join_key, day, market_cents, mid_cents, source),
        )
        conn.execute("DELETE FROM price_history WHERE date < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def record_lookup(game: str, join_key: int | None, prices: list[Price]) -> None:
    """Record a snapshot for one lookup's headline USD leg.

    The headline leg is prices[0]: the sidecar's USD row when a sync is
    fresh, otherwise the first live leg. Its provenance decides which
    cents column the number lands in, and its source travels with it, so
    market-vs-mid provenance survives the float round-trip. Nothing is
    recorded when there is no headline leg, no price on it, or no
    integer join key to file it under.
    """
    if not isinstance(join_key, int) or not prices:
        return
    head = prices[0]
    if head.price is None:
        return
    cents = int(round(head.price * 100))
    if head.provenance == "mid":
        market_cents, mid_cents = None, cents
    else:
        market_cents, mid_cents = cents, None
    record_history(game, join_key, market_cents, mid_cents, head.source)


def get_history(
    game: str, join_key: int, days: int | None = None
) -> list[tuple[str, int | None, int | None, str]]:
    """Price history for one card, oldest first.

    Each row is (date, market_cents, mid_cents, source) with date as
    YYYY-MM-DD. `days` keeps only the most recent N days of history;
    None returns everything on file.
    """
    conn = _connect()
    try:
        if days is None:
            rows = conn.execute(
                "SELECT date, market_cents, mid_cents, source FROM price_history"
                " WHERE game = ? AND join_key = ? ORDER BY date ASC",
                (game, join_key),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT date, market_cents, mid_cents, source FROM price_history"
                " WHERE game = ? AND join_key = ? AND date >= date('now', '-' || ? || ' days')"
                " ORDER BY date ASC",
                (game, join_key, days),
            ).fetchall()
    finally:
        conn.close()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


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
    """Download one group's products and prices; return backbone rows.

    Rows are (productId, name, set_name, set_code, market_cents,
    mid_cents): the price sidecar plus the name corpus the trigram
    search index is built from.
    """
    group_id = group["groupId"]
    set_name = str(group.get("name", "") or "")
    set_code = str(group.get("abbreviation", "") or "")
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
        name = str(prod.get("cleanName") or prod.get("name") or "")
        out.append((prod["productId"], name, set_name, set_code, market, mid))
    progress.tick()
    return out


def _store(game: str, rows: list[tuple]) -> None:
    """Replace one game's rows and stamp the sync time, atomically.

    Prices and the trigram name index are rebuilt together, so a game
    never has prices without matching suggestions.
    """
    conn = _connect()
    try:
        conn.execute("DELETE FROM prices WHERE game = ?", (game,))
        conn.executemany(
            "INSERT INTO prices (game, join_key, market_cents, mid_cents) VALUES (?, ?, ?, ?)",
            [(game, pid, market, mid) for pid, _name, _set, _code, market, mid in rows],
        )
        trigrams.build_index(
            conn,
            game,
            [(pid, name, set_name, set_code) for pid, name, set_name, set_code, _m, _d in rows],
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
    try:
        category_id = CATEGORY_IDS[game]
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
