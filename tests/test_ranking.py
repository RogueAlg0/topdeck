"""Tests for candidate ranking: exact name first, then most recent set."""

from topdeck.adapters.base import CardHit, rank_candidates


def _hit(name, released_at=""):
    return CardHit(
        card_id="x",
        name=name,
        set_code="s",
        set_name="Set",
        collector_number="1",
        released_at=released_at,
    )


def test_exact_match_wins_over_newer_partial():
    hits = [
        _hit("Lightning Boltish", "2024-01-01"),
        _hit("Lightning Bolt", "1993-01-01"),
    ]
    ranked = rank_candidates(hits, "Lightning Bolt")
    assert ranked[0].name == "Lightning Bolt"


def test_exact_match_is_case_insensitive():
    hits = [_hit("lightning bolt", "2020-01-01")]
    assert rank_candidates(hits, "Lightning Bolt")[0].name == "lightning bolt"


def test_recency_breaks_ties():
    hits = [
        _hit("Bolt", "2010-05-01"),
        _hit("Bolt", "2024-11-15"),
        _hit("Bolt", "2019-02-01"),
    ]
    ranked = rank_candidates(hits, "zzz")
    assert [h.released_at for h in ranked] == [
        "2024-11-15",
        "2019-02-01",
        "2010-05-01",
    ]


def test_unknown_dates_sort_last():
    hits = [_hit("Bolt", ""), _hit("Bolt", "2020-01-01")]
    ranked = rank_candidates(hits, "zzz")
    assert ranked[0].released_at == "2020-01-01"
    assert ranked[1].released_at == ""


def test_malformed_date_does_not_crash():
    hits = [_hit("Bolt", "not-a-date"), _hit("Bolt", "2020-01-01")]
    ranked = rank_candidates(hits, "zzz")
    assert ranked[0].released_at == "2020-01-01"
