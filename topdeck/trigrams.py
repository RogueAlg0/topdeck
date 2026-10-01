"""Typo-tolerant name search over the synced TCGCSV name corpus.

`topdeck sync` indexes every synced product's (productId, name) into
character-trigram postings in the same SQLite database as the price
sidecar. A query tokenizes into trigrams the same way; candidates are
ranked by rarity-weighted trigram overlap: rare trigrams count more
than common ones, so "Lighnting Bolt" still finds "Lightning Bolt".
The typo breaks only the trigrams touching it; the rest of the name
still votes.

Tables (in the backbone database):
  names    (game, join_key, name, set_name, set_code)
  postings (game, trigram, join_key)

Pure Python and offline. A lookup is one indexed SQLite query plus a
small Python ranking pass: milliseconds on a 160k-name corpus.
"""

from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass

# A query shorter than this never gets suggestions; ranking noise is
# worse than no suggestion.
_MIN_QUERY_CHARS = 2
# A candidate must share this many trigrams with the query, or the
# overlap is coincidence rather than a match.
_MIN_SHARED_TRIGRAMS = 2
# ... and the shared trigrams must carry this fraction of the query's
# total trigram weight, so a long name cannot win on two common
# trigrams alone.
_MIN_COVERAGE = 0.30


def _normalize(name: str) -> str:
    return " ".join(name.lower().split())


def trigrams_for(name: str) -> list[str]:
    """Character trigrams of a name, padded so the edges count.

    Duplicates are dropped: a trigram votes once per name no matter
    how often it repeats inside it.
    """
    text = f"  {_normalize(name)}  "
    seen: set[str] = set()
    out: list[str] = []
    for i in range(len(text) - 2):
        trigram = text[i : i + 3]
        if trigram not in seen:
            seen.add(trigram)
            out.append(trigram)
    return out


def build_index(conn: sqlite3.Connection, game: str, entries: list[tuple]) -> None:
    """Replace one game's name index.

    entries are (join_key, name, set_name, set_code). Nameless rows are
    skipped: a row with no name can never be suggested. The caller owns
    the transaction.
    """
    conn.execute("DELETE FROM postings WHERE game = ?", (game,))
    conn.execute("DELETE FROM names WHERE game = ?", (game,))
    name_rows = []
    posting_rows = []
    for join_key, name, set_name, set_code in entries:
        if not _normalize(name):
            continue
        name_rows.append((game, join_key, str(name), str(set_name), str(set_code)))
        for trigram in trigrams_for(name):
            posting_rows.append((game, trigram, join_key))
    conn.executemany(
        "INSERT INTO names (game, join_key, name, set_name, set_code) VALUES (?, ?, ?, ?, ?)",
        name_rows,
    )
    conn.executemany(
        "INSERT INTO postings (game, trigram, join_key) VALUES (?, ?, ?)",
        posting_rows,
    )


@dataclass
class Suggestion:
    join_key: int
    name: str
    set_name: str
    set_code: str
    score: float  # rarity-weighted trigram overlap with the query
    coverage: float  # score as a fraction of the query's total weight


def _weight(total_names: int, doc_freq: int) -> float:
    """IDF-ish weight: a trigram found in few names counts more."""
    return math.log(1 + total_names / doc_freq)


def suggest(db_path: str, game: str, query: str, limit: int = 5) -> list[Suggestion]:
    """Typo-tolerant name suggestions for one game, best first.

    Never raises: a missing, empty, or unreadable database simply has
    no suggestions.
    """
    if len(_normalize(query)) < _MIN_QUERY_CHARS:
        return []
    if not os.path.exists(db_path):
        return []
    wanted = trigrams_for(query)
    try:
        conn = sqlite3.connect(db_path)
        try:
            return _rank(conn, game, wanted, limit)
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _rank(conn: sqlite3.Connection, game: str, wanted: list[str], limit: int) -> list[Suggestion]:
    row = conn.execute("SELECT COUNT(*) FROM names WHERE game = ?", (game,)).fetchone()
    total = row[0] if row else 0
    if total == 0:
        return []
    marks = ",".join("?" for _ in wanted)
    doc_freq = {
        trigram: count
        for trigram, count in conn.execute(
            "SELECT trigram, COUNT(DISTINCT join_key) FROM postings "
            f"WHERE game = ? AND trigram IN ({marks}) GROUP BY trigram",
            (game, *wanted),
        )
    }
    weights = {
        trigram: _weight(total, doc_freq[trigram]) for trigram in wanted if trigram in doc_freq
    }
    if not weights:
        return []
    query_weight = sum(weights.values())
    shared: dict[int, set[str]] = {}
    for join_key, trigram in conn.execute(
        f"SELECT join_key, trigram FROM postings WHERE game = ? AND trigram IN ({marks})",
        (game, *wanted),
    ):
        shared.setdefault(join_key, set()).add(trigram)
    scored = []
    for join_key, grams in shared.items():
        if len(grams) < _MIN_SHARED_TRIGRAMS:
            continue
        score = sum(weights[trigram] for trigram in grams)
        coverage = score / query_weight
        if coverage < _MIN_COVERAGE:
            continue
        scored.append((join_key, score, coverage))
    # Highest weighted overlap first; join_key breaks ties deterministically.
    scored.sort(key=lambda item: (-item[1], item[0]))
    top = scored[:limit]
    if not top:
        return []
    ids = [join_key for join_key, _, _ in top]
    marks = ",".join("?" for _ in ids)
    info = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT join_key, name, set_name, set_code FROM names "
            f"WHERE game = ? AND join_key IN ({marks})",
            (game, *ids),
        )
    }
    return [
        Suggestion(join_key, name, set_name, set_code, score, coverage)
        for join_key, score, coverage in top
        for name, set_name, set_code in [info[join_key]]
    ]
