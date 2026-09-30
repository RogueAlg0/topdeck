"""Tests for price history snapshots. Network is faked; the clock is frozen."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import topdeck.net
from topdeck import backbone
from topdeck.adapters.base import Price
from topdeck.cli import main
from topdeck.watch import WatchStore


@pytest.fixture
def cache_home(tmp_path, monkeypatch):
    """Backbone reads and writes only under this fake XDG cache dir."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    """Watchlists live under this fake XDG data dir."""
    monkeypatch.setenv("TOPDECK_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_net(monkeypatch):
    """Every test decides exactly what the network returns."""
    routes = {}

    def fake(url, **kwargs):
        handler = routes.get(url)
        assert handler is not None, f"unexpected URL: {url}"
        return handler()

    fake.routes = routes
    monkeypatch.setattr(topdeck.net, "fetch_json", fake)
    return fake


def _raw_connect():
    """Direct sqlite connection; the parent dir must already exist."""
    os.makedirs(os.path.dirname(backbone.db_path()), exist_ok=True)
    return sqlite3.connect(backbone.db_path())


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _price(value=1.20, provenance="market", source="scryfall"):
    return Price(
        market="tcgplayer",
        currency="USD",
        condition="near-mint",
        printing="normal",
        price=value,
        as_of="2026-09-30T00:00:00Z",
        source=source,
        provenance=provenance,
    )


def _history_rows(game="mtg", join_key=12345):
    conn = _raw_connect()
    try:
        return conn.execute(
            "SELECT game, join_key, date, market_cents, mid_cents, source"
            " FROM price_history ORDER BY date",
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# record_history / get_history


def test_record_and_read_history_oldest_first(cache_home):
    backbone.record_history("mtg", 7, 150, 120, "tcgcsv", date="2026-09-28")
    backbone.record_history("mtg", 7, 160, None, "scryfall", date="2026-09-30")
    backbone.record_history("mtg", 7, 155, 130, "tcgcsv", date="2026-09-29")
    assert backbone.get_history("mtg", 7) == [
        ("2026-09-28", 150, 120, "tcgcsv"),
        ("2026-09-29", 155, 130, "tcgcsv"),
        ("2026-09-30", 160, None, "scryfall"),
    ]


def test_same_day_rewrite_is_an_upsert(cache_home):
    backbone.record_history("mtg", 7, 150, 120, "tcgcsv", date="2026-09-30")
    backbone.record_history("mtg", 7, 175, None, "scryfall", date="2026-09-30")
    rows = _history_rows()
    assert len(rows) == 1
    assert rows[0][2:] == ("2026-09-30", 175, None, "scryfall")


def test_history_is_keyed_per_game_and_card(cache_home):
    backbone.record_history("mtg", 7, 150, None, "tcgcsv", date="2026-09-30")
    backbone.record_history("pokemon", 7, 250, None, "tcgcsv", date="2026-09-30")
    backbone.record_history("mtg", 8, 350, None, "tcgcsv", date="2026-09-30")
    assert backbone.get_history("mtg", 7) == [("2026-09-30", 150, None, "tcgcsv")]
    assert backbone.get_history("pokemon", 7) == [("2026-09-30", 250, None, "tcgcsv")]
    assert backbone.get_history("mtg", 8) == [("2026-09-30", 350, None, "tcgcsv")]
    assert backbone.get_history("mtg", 9) == []


def test_get_history_days_filter(cache_home):
    today = datetime.now(timezone.utc)
    recent = (today - timedelta(days=5)).strftime("%Y-%m-%d")
    old = (today - timedelta(days=40)).strftime("%Y-%m-%d")
    backbone.record_history("mtg", 7, 100, None, "tcgcsv", date=old)
    backbone.record_history("mtg", 7, 110, None, "tcgcsv", date=recent)
    backbone.record_history("mtg", 7, 120, None, "tcgcsv", date=_utc_today())
    assert [r[0] for r in backbone.get_history("mtg", 7, days=30)] == [recent, _utc_today()]
    assert len(backbone.get_history("mtg", 7, days=365)) == 3
    # days=0 keeps only today's rows.
    assert backbone.get_history("mtg", 7, days=0) == [(_utc_today(), 120, None, "tcgcsv")]


def test_prune_deletes_rows_older_than_365_days(cache_home):
    today = datetime.now(timezone.utc)
    cutoff = (today - timedelta(days=365)).strftime("%Y-%m-%d")
    older = (today - timedelta(days=366)).strftime("%Y-%m-%d")
    backbone._connect().close()  # create the current schema first
    conn = _raw_connect()
    try:
        # Inserted raw so no prune runs before the assertion below.
        conn.execute(
            "INSERT INTO price_history (game, join_key, date, market_cents, mid_cents, source)"
            " VALUES ('mtg', 7, ?, 100, NULL, 'tcgcsv'),"
            "        ('mtg', 7, ?, 110, NULL, 'tcgcsv')",
            (older, cutoff),
        )
        conn.commit()
    finally:
        conn.close()
    backbone.record_history("mtg", 7, 120, None, "tcgcsv", date=_utc_today())
    dates = [r[0] for r in backbone.get_history("mtg", 7)]
    assert older not in dates
    assert cutoff in dates
    assert _utc_today() in dates


# ---------------------------------------------------------------------------
# record_lookup: headline leg to cents, provenance preserved


def test_record_lookup_stores_headline_market_leg(cache_home):
    backbone.record_lookup("mtg", 12345, [_price(1.20), _price(2.00, source="other")])
    assert backbone.get_history("mtg", 12345) == [(_utc_today(), 120, None, "scryfall")]


def test_record_lookup_mid_provenance_lands_in_mid_column(cache_home):
    backbone.record_lookup("mtg", 12345, [_price(0.75, provenance="mid")])
    assert backbone.get_history("mtg", 12345) == [(_utc_today(), None, 75, "scryfall")]


def test_record_lookup_rounds_float_cents(cache_home):
    # 4.35 cannot be represented exactly in binary; the snapshot is exact.
    backbone.record_lookup("mtg", 12345, [_price(4.35)])
    assert backbone.get_history("mtg", 12345) == [(_utc_today(), 435, None, "scryfall")]


def test_record_lookup_ignores_unusable_lookups(cache_home):
    backbone.record_lookup("mtg", None, [_price(1.20)])
    backbone.record_lookup("mtg", "not-an-int", [_price(1.20)])
    backbone.record_lookup("mtg", 12345, [])
    backbone.record_lookup("mtg", 12345, [_price(None)])
    assert backbone.get_history("mtg", 12345) == []


# ---------------------------------------------------------------------------
# Schema upgrades


def _make_v2_db():
    conn = _raw_connect()
    try:
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE prices ("
            " game TEXT NOT NULL,"
            " join_key INTEGER NOT NULL,"
            " market_cents INTEGER,"
            " mid_cents INTEGER,"
            " PRIMARY KEY (game, join_key)) WITHOUT ROWID;"
        )
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('synced_at:mtg', '2026-09-30T12:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO prices (game, join_key, market_cents, mid_cents)"
            " VALUES ('mtg', 42, 150, 120)"
        )
        conn.commit()
    finally:
        conn.close()


def test_migration_v2_to_v3_preserves_prices_and_meta(cache_home):
    _make_v2_db()
    conn = backbone._connect()
    try:
        assert conn.execute("SELECT join_key, market_cents, mid_cents FROM prices").fetchall() == [
            (42, 150, 120)
        ]
        assert (
            conn.execute("SELECT value FROM meta WHERE key = 'synced_at:mtg'").fetchone()[0]
            == "2026-09-30T12:00:00+00:00"
        )
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[
            0
        ] == str(backbone.SCHEMA_VERSION)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"meta", "prices", "price_history"} <= tables
    finally:
        conn.close()
    # The migrated database takes snapshots without a rebuild.
    backbone.record_history("mtg", 42, 160, None, "tcgcsv", date="2026-09-30")
    assert backbone.get_history("mtg", 42) == [("2026-09-30", 160, None, "tcgcsv")]
    fresh = _raw_connect()
    try:
        assert fresh.execute("SELECT join_key, market_cents FROM prices").fetchall() == [(42, 150)]
    finally:
        fresh.close()


def test_unknown_version_still_rebuilds(cache_home):
    conn = backbone._connect()
    try:
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
        conn.execute(
            "INSERT INTO prices (game, join_key, market_cents, mid_cents)"
            " VALUES ('mtg', 1, 100, 90)"
        )
        conn.commit()
    finally:
        conn.close()
    conn = backbone._connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[
            0
        ] == str(backbone.SCHEMA_VERSION)
    finally:
        conn.close()


def test_rebuild_when_version_row_is_missing(cache_home):
    conn = _raw_connect()
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta (key, value) VALUES ('synced_at:mtg', 'x')")
        conn.commit()
    finally:
        conn.close()
    conn = backbone._connect()
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[
            0
        ] == str(backbone.SCHEMA_VERSION)
    finally:
        conn.close()


def test_rebuild_when_meta_table_is_missing(cache_home):
    conn = _raw_connect()
    try:
        conn.execute(
            "CREATE TABLE prices (game TEXT NOT NULL, join_key INTEGER NOT NULL,"
            " market_cents INTEGER, mid_cents INTEGER,"
            " PRIMARY KEY (game, join_key)) WITHOUT ROWID"
        )
        conn.commit()
    finally:
        conn.close()
    conn = backbone._connect()
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[
            0
        ] == str(backbone.SCHEMA_VERSION)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Wiring: real adapters record a snapshot on every lookup


def _scryfall_card(name="Lightning Bolt", tcgplayer_id=12345, usd="1.20"):
    return {
        "id": "scry-1",
        "name": name,
        "set": "LEA",
        "set_name": "Alpha",
        "collector_number": "161",
        "finishes": ["normal"],
        "released_at": "1993-08-05",
        "scryfall_uri": "https://scryfall.com/card/lea/161",
        "tcgplayer_id": tcgplayer_id,
        "prices": {"usd": usd},
    }


def test_price_command_records_snapshot(cache_home, fake_net, capsys):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card()]
    }
    assert main(["--json", "price", "mtg", "Lightning Bolt"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["prices"][0]["price"] == 1.20
    assert backbone.get_history("mtg", 12345) == [(_utc_today(), 120, None, "scryfall")]


def test_price_command_records_sidecar_headline_when_fresh(cache_home, fake_net, capsys):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card()]
    }
    conn = backbone._connect()
    try:
        conn.execute(
            "INSERT INTO prices (game, join_key, market_cents, mid_cents)"
            " VALUES ('mtg', 12345, 150, 120)"
        )
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('synced_at:mtg', ?)",
            (stamp,),
        )
        conn.commit()
    finally:
        conn.close()
    assert main(["--json", "price", "mtg", "Lightning Bolt"]) == 0
    # The sidecar led, so the snapshot carries the sidecar's provenance.
    assert backbone.get_history("mtg", 12345) == [(_utc_today(), 150, None, "tcgcsv")]


def test_price_command_records_mid_fallback_provenance(cache_home, fake_net, capsys):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card()]
    }
    conn = backbone._connect()
    try:
        conn.execute(
            "INSERT INTO prices (game, join_key, market_cents, mid_cents)"
            " VALUES ('mtg', 12345, NULL, 75)"
        )
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('synced_at:mtg', ?)",
            (stamp,),
        )
        conn.commit()
    finally:
        conn.close()
    assert main(["--json", "price", "mtg", "Lightning Bolt"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["prices"][0]["provenance"] == "mid"
    assert backbone.get_history("mtg", 12345) == [(_utc_today(), None, 75, "tcgcsv")]


def test_check_run_records_snapshot(cache_home, data_home, fake_net, capsys):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card()]
    }
    store = WatchStore()
    store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    assert main(["--json", "check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0]["current"] == 1.20
    assert backbone.get_history("mtg", 12345) == [(_utc_today(), 120, None, "scryfall")]


def test_decklist_batch_records_snapshot_per_card(cache_home, fake_net, capsys, tmp_path):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card()]
    }
    deck = tmp_path / "deck.txt"
    deck.write_text("4 Lightning Bolt\n")
    assert main(["--json", "price", "mtg", "--file", str(deck)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lines"][0]["unit_price"]["price"] == 1.20
    assert backbone.get_history("mtg", 12345) == [(_utc_today(), 120, None, "scryfall")]
