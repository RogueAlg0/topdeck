"""Tests for the trigram typo-tolerant search. No network; SQLite in tmp."""

from __future__ import annotations

import sqlite3

from topdeck import backbone, trigrams
from topdeck.trigrams import build_index, suggest, trigrams_for


def _db(tmp_path):
    """A fresh backbone database file, using the real schema."""
    path = str(tmp_path / "index.db")
    conn = sqlite3.connect(path)
    conn.executescript(backbone._SCHEMA)
    conn.commit()
    conn.close()
    return path


def _corpus_db(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    names = [
        "Lightning Bolt",
        "Lightning Strike",
        "Chain Lightning",
        "Shock",
        "Lightning Helix",
    ]
    build_index(conn, "mtg", [(i, name, "Alpha", "LEA") for i, name in enumerate(names)])
    conn.commit()
    conn.close()
    return path


def test_trigrams_pad_edges():
    grams = trigrams_for("Bolt")
    assert "  b" in grams
    assert "t  " in grams
    assert "bol" in grams


def test_trigrams_dedupe_repeats():
    assert trigrams_for("banana").count("ana") == 1


def test_trigrams_normalize_case_and_whitespace():
    assert trigrams_for("  Lightning   Bolt\n") == trigrams_for("lightning bolt")


def test_build_index_skips_nameless_rows(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        build_index(
            conn,
            "mtg",
            [
                (1, "Lightning Bolt", "Alpha", "LEA"),
                (2, "   ", "Alpha", "LEA"),
                (3, "", "Alpha", "LEA"),
            ],
        )
        conn.commit()
        names = conn.execute("SELECT join_key FROM names WHERE game = 'mtg'").fetchall()
        postings = conn.execute("SELECT COUNT(*) FROM postings WHERE game = 'mtg'").fetchone()
    finally:
        conn.close()
    assert [row[0] for row in names] == [1]
    assert postings[0] > 0


def test_suggest_typo_finds_lightning_bolt(tmp_path):
    path = _corpus_db(tmp_path)
    results = suggest(path, "mtg", "Lighnting Bolt")
    assert results
    assert results[0].name == "Lightning Bolt"
    assert results[0].join_key == 0


def test_suggest_exact_query_ranks_first(tmp_path):
    path = _corpus_db(tmp_path)
    results = suggest(path, "mtg", "Shock")
    assert results[0].name == "Shock"


def test_suggest_carries_set_info(tmp_path):
    path = _corpus_db(tmp_path)
    suggestion = suggest(path, "mtg", "Chain Lightnin")[0]
    assert suggestion.name == "Chain Lightning"
    assert suggestion.set_name == "Alpha"
    assert suggestion.set_code == "LEA"


def test_suggest_respects_limit(tmp_path):
    path = _corpus_db(tmp_path)
    results = suggest(path, "mtg", "lightning", limit=2)
    assert len(results) == 2
    # Deterministic: the same call twice gives the same order.
    again = suggest(path, "mtg", "lightning", limit=2)
    assert [s.name for s in again] == [s.name for s in results]


def test_suggest_gibberish_is_empty(tmp_path):
    path = _corpus_db(tmp_path)
    assert suggest(path, "mtg", "xqz wobble") == []


def test_suggest_short_queries_are_empty(tmp_path):
    path = _corpus_db(tmp_path)
    assert suggest(path, "mtg", "") == []
    assert suggest(path, "mtg", "a") == []
    assert suggest(path, "mtg", "  ") == []


def test_suggest_missing_database_is_empty(tmp_path):
    assert suggest(str(tmp_path / "nope.db"), "mtg", "Lightning Bolt") == []


def test_suggest_unreadable_database_is_empty(tmp_path):
    path = tmp_path / "junk.db"
    path.write_bytes(b"this is not a database")
    assert suggest(str(path), "mtg", "Lightning Bolt") == []


def test_suggest_unknown_game_is_empty(tmp_path):
    path = _corpus_db(tmp_path)
    assert suggest(path, "pokemon", "Lightning Bolt") == []


def test_suggest_game_with_no_names_is_empty(tmp_path):
    path = _db(tmp_path)
    assert suggest(path, "mtg", "Lightning Bolt") == []


def test_suggest_low_coverage_candidates_are_filtered(tmp_path):
    # Every name shares the common "the" trigrams with the query, but
    # none shares enough of the query's distinctive weight.
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    names = ["the a1", "the b2", "the c3", "the d4", "the e5", "the f6"]
    build_index(conn, "mtg", [(i, name, "S", "S") for i, name in enumerate(names)])
    conn.commit()
    conn.close()
    assert suggest(path, "mtg", "the a1 b2 c3 d4 e5 f6") == []
    # ... while a real near-match still comes through.
    assert suggest(path, "mtg", "the a1")[0].name == "the a1"


def test_build_index_replaces_previous_index(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        build_index(conn, "mtg", [(1, "Lightning Bolt", "Alpha", "LEA")])
        build_index(conn, "mtg", [(2, "Shock", "Beta", "BT")])
        conn.commit()
    finally:
        conn.close()
    results = suggest(path, "mtg", "Lightning Bolt")
    assert [s.name for s in results] == []
    results = suggest(path, "mtg", "Shock")
    assert results[0].name == "Shock"
    assert results[0].set_name == "Beta"


def test_trigrams_module_public_names():
    assert trigrams._MIN_QUERY_CHARS == 2
