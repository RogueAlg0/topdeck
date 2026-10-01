"""Tests for smart alerts. Network is faked; the clock is frozen."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import topdeck.net
from topdeck import alerts, backbone
from topdeck.adapters.base import CardHit, GameAdapter
from topdeck.adapters.lorcana import LorcastAdapter
from topdeck.adapters.mtg import ScryfallAdapter
from topdeck.adapters.pokemon import TcgdexAdapter
from topdeck.adapters.tcgcsv import TcgcsvBulkAdapter
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


def _rows(cents, start=1, month="2026-09"):
    """History rows: one snapshot per day, market cents, oldest first."""
    return [
        (f"{month}-{day:02d}", value, None, "tcgcsv")
        for day, value in zip(range(start, start + len(cents)), cents)
    ]


def _hit(**extra) -> CardHit:
    return CardHit(
        card_id="card-1",
        name="Lightning Bolt",
        set_code="LEA",
        set_name="Alpha",
        collector_number="161",
        extra=extra,
    )


# ---------------------------------------------------------------------------
# evaluate: the two rules


def test_avg_deviation_fires_and_states_the_rule():
    # The $5.10 level is confirmed by a second consecutive snapshot;
    # a single wild print would stay silent as a suspected glitch.
    history = _rows([420] * 13) + [
        ("2026-09-14", 510, None, "scryfall"),
        ("2026-09-15", 510, None, "scryfall"),
    ]
    fired = alerts.evaluate(history)
    assert [a.rule for a in fired] == ["avg_deviation", "band_high"]
    first = fired[0]
    assert first.line == "14d avg $4.20, now $5.10 (+21.4%)"
    assert first.window == 14
    assert first.baseline_points == 13
    assert first.current_cents == 510
    assert first.deviation_pct == pytest.approx(21.4286, abs=1e-3)


def test_band_high_line_names_the_band():
    history = _rows([420] * 13) + [
        ("2026-09-14", 510, None, "scryfall"),
        ("2026-09-15", 510, None, "scryfall"),
    ]
    fired = alerts.evaluate(history)
    band = next(a for a in fired if a.rule == "band_high")
    assert band.line == "above 14d band $4.20, mean $4.20 +/- 2 std"


def test_band_low_fires_without_deviation():
    # Alternating 400/440: mean 420, std 20, bands 380/460.
    baseline = [400, 440] * 7
    history = _rows(baseline) + [("2026-09-15", 360, None, "scryfall")]
    fired = alerts.evaluate(history)
    # -14.3% is inside the 15% deviation threshold, but below the band.
    assert [a.rule for a in fired] == ["band_low"]
    assert fired[0].line == "below 14d band $3.80, mean $4.20 +/- 2 std"
    assert fired[0].deviation_pct == pytest.approx(-14.2857, abs=1e-3)


def test_quiet_inside_band_and_deviation():
    baseline = [400, 440] * 7
    history = _rows(baseline) + [("2026-09-15", 450, None, "scryfall")]
    assert alerts.evaluate(history) == []


def test_deviation_and_band_low_can_fire_together():
    baseline = [400, 440] * 6 + [400, 300]
    history = _rows(baseline) + [("2026-09-15", 300, None, "scryfall")]
    assert [a.rule for a in alerts.evaluate(history)] == ["avg_deviation", "band_low"]


def test_threshold_is_strict():
    # Exactly 15%: the rule fires on *more than* the threshold, not at
    # it. The move is confirmed twice so the threshold is what is
    # actually under test, not the glitch guard.
    history = _rows([400] * 13) + [
        ("2026-09-14", 460, None, "scryfall"),
        ("2026-09-15", 460, None, "scryfall"),
    ]
    fired = alerts.evaluate(history, deviation_pct=15.0)
    assert all(a.rule != "avg_deviation" for a in fired)


def test_window_caps_the_baseline():
    baseline = [100] * 5 + [420] * 5
    history = (
        _rows(baseline, start=1)
        + [("2026-09-11", 510, None, "scryfall")]
        + [("2026-09-12", 510, None, "scryfall")]
    )
    narrow = alerts.evaluate(history, window=6)
    assert narrow[0].rule == "avg_deviation"
    assert narrow[0].baseline_points == 5
    assert "6d avg $4.20" in narrow[0].line
    wide = alerts.evaluate(history, window=10)
    assert wide[0].baseline_points == 10
    assert "10d avg $3.01" in wide[0].line


def test_custom_deviation_and_band_k():
    baseline = [400, 440] * 7
    history = _rows(baseline) + [("2026-09-15", 450, None, "scryfall")]
    fired = alerts.evaluate(history, deviation_pct=5.0, band_k=5.0)
    # 7.1% clears the 5% deviation bar; 5-sigma bands stay quiet.
    assert [a.rule for a in fired] == ["avg_deviation"]
    assert fired[0].band_k == 5.0


# ---------------------------------------------------------------------------
# evaluate: thin history and provenance


def test_thin_history_stays_silent():
    assert alerts.evaluate([]) == []
    assert alerts.evaluate(_rows([420] * 4) + [("2026-09-05", 999, None, "x")]) == []
    # Four baseline points plus the current price: still too thin.
    assert alerts.evaluate(_rows([420] * 5)) == []


def test_exactly_five_baseline_points_is_enough():
    history = _rows([420] * 5) + [
        ("2026-09-06", 510, None, "scryfall"),
        ("2026-09-07", 510, None, "scryfall"),
    ]
    fired = alerts.evaluate(history)
    assert [a.rule for a in fired] == ["avg_deviation", "band_high"]
    assert fired[0].baseline_points == 5


def test_tiny_window_still_needs_five_baseline_points():
    # The CLI rejects window < 6, but evaluate() itself stays honest:
    # fewer than MIN_BASELINE_POINTS baseline snapshots means no alerts,
    # however the window is set.
    assert alerts.evaluate(_rows([420] * 10), window=3) == []


def test_market_cents_win_over_mid():
    history = [(f"2026-09-{day:02d}", 420, 999, "x") for day in range(1, 6)]
    history += [
        ("2026-09-06", 510, None, "scryfall"),
        ("2026-09-07", 510, None, "scryfall"),
    ]
    fired = alerts.evaluate(history)
    assert [a.rule for a in fired] == ["avg_deviation", "band_high"]
    assert "14d avg $4.20" in fired[0].line


def test_mid_cents_are_the_fallback():
    history = [(f"2026-09-{day:02d}", None, 420, "x") for day in range(1, 6)]
    history += [
        ("2026-09-06", None, 510, "scryfall"),
        ("2026-09-07", None, 510, "scryfall"),
    ]
    fired = alerts.evaluate(history)
    assert [a.rule for a in fired] == ["avg_deviation", "band_high"]
    assert "14d avg $4.20" in fired[0].line


def test_days_without_any_price_are_skipped():
    history = _rows([420] * 5)
    history.insert(2, ("2026-09-03", None, None, "x"))
    history.append(("2026-09-06", 510, None, "scryfall"))
    history.append(("2026-09-07", 510, None, "scryfall"))
    fired = alerts.evaluate(history)
    assert fired[0].baseline_points == 5


def test_zero_mean_baseline_has_no_deviation():
    history = _rows([0] * 5) + [
        ("2026-09-06", 100, None, "scryfall"),
        ("2026-09-07", 100, None, "scryfall"),
    ]
    fired = alerts.evaluate(history)
    assert [a.rule for a in fired] == ["band_high"]
    assert fired[0].deviation_pct is None
    assert fired[0].line == "above 14d band $0.00, mean $0.00 +/- 2 std"


def test_smart_alert_to_dict_is_machine_readable():
    history = _rows([420] * 13) + [
        ("2026-09-14", 510, None, "scryfall"),
        ("2026-09-15", 510, None, "scryfall"),
    ]
    payload = alerts.evaluate(history)[0].to_dict()
    assert payload == {
        "rule": "avg_deviation",
        "line": "14d avg $4.20, now $5.10 (+21.4%)",
        "window": 14,
        "baseline_points": 13,
        "mean_cents": 420.0,
        "current_cents": 510,
        "deviation_pct": pytest.approx(21.43),
        "band_k": 2.0,
        "upper_band_cents": 420.0,
        "lower_band_cents": 420.0,
    }


# ---------------------------------------------------------------------------
# evaluate: the glitch guard


def test_single_snapshot_spike_stays_silent():
    # One wild print is a suspected glitch until the next snapshot
    # confirms it: the move has no second consecutive snapshot.
    history = _rows([420] * 14) + [("2026-09-15", 510, None, "scryfall")]
    assert alerts.evaluate(history) == []


def test_two_snapshot_spike_fires():
    history = _rows([420] * 13) + [
        ("2026-09-14", 510, None, "scryfall"),
        ("2026-09-15", 510, None, "scryfall"),
    ]
    assert [a.rule for a in alerts.evaluate(history)] == ["avg_deviation", "band_high"]


def test_glitch_in_baseline_does_not_move_the_stats():
    # A $999.99 bad print in history is cut before the mean is
    # computed: the confirmed $5.10 move is judged against $4.20, not
    # against a glitch-inflated average.
    history = _rows([420] * 13 + [99999]) + [
        ("2026-09-15", 510, None, "scryfall"),
        ("2026-09-16", 510, None, "scryfall"),
    ]
    fired = alerts.evaluate(history)
    assert [a.rule for a in fired] == ["avg_deviation", "band_high"]
    assert fired[0].line == "14d avg $4.20, now $5.10 (+21.4%)"
    assert fired[0].baseline_points == 12


def test_glitch_then_revert_stays_silent():
    # Yesterday's $999.99 was the glitch; today's $4.30 is back near the
    # baseline. The move did not persist at the new level, so the smart
    # rules stay silent.
    history = _rows([420] * 13 + [99999]) + [("2026-09-15", 430, None, "scryfall")]
    assert alerts.evaluate(history) == []


def test_whipsaw_stays_silent():
    # Down hard yesterday, up hard today: violent, but not a confirmed
    # level on either side, so nothing fires.
    history = _rows([420] * 13) + [
        ("2026-09-14", 300, None, "scryfall"),
        ("2026-09-15", 500, None, "scryfall"),
    ]
    assert alerts.evaluate(history) == []


def test_thin_inlier_baseline_stays_silent():
    # Four calm snapshots and one glitch: the glitch is cut, leaving
    # fewer than five trustworthy baseline points, so the rules say
    # nothing rather than guessing.
    history = _rows([420] * 4 + [99999]) + [("2026-09-06", 510, None, "scryfall")]
    assert alerts.evaluate(history) == []


# ---------------------------------------------------------------------------
# check_card: history lookup plus evaluation


def test_check_card_reads_backbone_history(cache_home):
    for day in range(1, 6):
        backbone.record_history("mtg", 7, 420, None, "tcgcsv", date=f"2026-09-{day:02d}")
    # Yesterday already printed $5.10: the live price today is the
    # second consecutive snapshot, so the glitch guard lets it through.
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    backbone.record_history("mtg", 7, 510, None, "scryfall", date=yesterday)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    backbone.record_history("mtg", 7, 510, None, "scryfall", date=today)
    fired = alerts.check_card("mtg", 7, window=6)
    assert [a.rule for a in fired] == ["avg_deviation", "band_high"]
    assert "6d avg $4.20" in fired[0].line


def test_check_card_thin_history_says_nothing(cache_home):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    backbone.record_history("mtg", 7, 510, None, "scryfall", date=today)
    assert alerts.check_card("mtg", 7) == []


# ---------------------------------------------------------------------------
# history_key: each adapter exposes its join key


def test_base_adapter_files_no_history_key():
    assert GameAdapter().history_key(_hit()) is None


def test_scryfall_history_key():
    adapter = ScryfallAdapter()
    assert adapter.history_key(_hit(tcgplayer_id=12345)) == 12345
    assert adapter.history_key(_hit()) is None
    assert adapter.history_key(_hit(tcgplayer_id="12345")) is None


def test_lorcana_history_key():
    adapter = LorcastAdapter()
    assert adapter.history_key(_hit(tcgplayer_id=77)) == 77
    assert adapter.history_key(_hit()) is None


def test_pokemon_history_key_uses_product_id():
    adapter = TcgdexAdapter()
    assert adapter.history_key(_hit(product_id=999)) == 999
    assert adapter.history_key(_hit()) is None


def test_tcgcsv_history_key_parses_card_id():
    adapter = TcgcsvBulkAdapter()
    assert adapter.history_key(_hit()) is None
    numeric = _hit()
    object.__setattr__(numeric, "card_id", "42")
    assert adapter.history_key(numeric) == 42


# ---------------------------------------------------------------------------
# CLI: check wires smart alerts into the table and the JSON


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


def _seed_history(game="mtg", join_key=12345, cents=420, days=14):
    today = datetime.now(timezone.utc)
    for back in range(days, 0, -1):
        date = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        backbone.record_history(game, join_key, cents, None, "tcgcsv", date=date)


def _seed_confirmed_spike(game="mtg", join_key=12345, cents=510):
    """Overwrite yesterday's seed with the spike price.

    The live price the fake network returns today then becomes the
    second consecutive snapshot, which is what the glitch guard needs
    before a smart alert fires.
    """
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    backbone.record_history(game, join_key, cents, None, "scryfall", date=yesterday)


@pytest.fixture
def wide_terminal(monkeypatch):
    """Rich wraps long signal lines at 80 columns; give the table room."""
    monkeypatch.setenv("COLUMNS", "200")


def test_check_human_output_shows_smart_alert(
    cache_home, data_home, fake_net, capsys, wide_terminal
):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card(usd="5.10")]
    }
    _seed_history()
    _seed_confirmed_spike()
    store = WatchStore()
    store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "14d avg $4.20, now $5.10 (+21.4%)" in out


def test_check_json_carries_smart_alerts(cache_home, data_home, fake_net, capsys):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card(usd="5.10")]
    }
    _seed_history()
    _seed_confirmed_spike()
    store = WatchStore()
    store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    assert main(["--json", "check"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    rules = [a["rule"] for a in row["smart_alerts"]]
    assert rules == ["avg_deviation", "band_high"]
    assert row["alert"] is True
    assert row["smart_alerts"][0]["line"] == "14d avg $4.20, now $5.10 (+21.4%)"


def test_smart_alert_counts_as_alert_for_alert_only(
    cache_home, data_home, fake_net, capsys, wide_terminal
):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card(usd="5.10")]
    }
    _seed_history()
    _seed_confirmed_spike()
    store = WatchStore()
    watch = store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    # Same as the new price: no spike, no drop, no target. The smart
    # alerts are the only reason this row needs attention.
    store.record_snapshot(watch.id, 5.10, "USD", "scryfall")
    assert main(["check", "--alert-only"]) == 0
    out = capsys.readouterr().out
    assert "Lightning Bolt" in out
    assert "14d avg $4.20, now $5.10 (+21.4%)" in out


def test_no_smart_flag_silences_history_alerts(cache_home, data_home, fake_net, capsys):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card(usd="5.10")]
    }
    _seed_history()
    store = WatchStore()
    watch = store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    store.record_snapshot(watch.id, 4.00, "USD", "scryfall")
    assert main(["--json", "check", "--no-smart"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert row["smart_alerts"] == []
    assert row["alert"] is True  # the plain 10% spike still fires


def test_thin_history_check_says_nothing(cache_home, data_home, fake_net, capsys):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card(usd="5.10")]
    }
    store = WatchStore()
    store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "14d avg" not in out
    assert "band" not in out


def test_window_flag_changes_the_line(cache_home, data_home, fake_net, capsys, wide_terminal):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card(usd="5.10")]
    }
    # Six seeded days: the confirming snapshot occupies one baseline
    # slot and is trimmed as an outlier, leaving five inliers.
    _seed_history(days=6)
    _seed_confirmed_spike()
    store = WatchStore()
    store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    assert main(["check", "--window", "6"]) == 0
    out = capsys.readouterr().out
    assert "6d avg $4.20, now $5.10 (+21.4%)" in out


def test_deviation_flag_tunes_the_threshold(cache_home, data_home, fake_net, capsys):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card(usd="4.50")]
    }
    _seed_history()
    _seed_confirmed_spike(cents=450)
    store = WatchStore()
    store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    # +7.1%: silent at the default 15%, fires at 5%.
    assert main(["--json", "check"]) == 0
    quiet = json.loads(capsys.readouterr().out)["rows"][0]["smart_alerts"]
    assert [a["rule"] for a in quiet] == ["band_high"]
    assert main(["--json", "check", "--deviation", "5"]) == 0
    loud = json.loads(capsys.readouterr().out)["rows"][0]["smart_alerts"]
    assert [a["rule"] for a in loud] == ["avg_deviation", "band_high"]


def test_band_k_flag_tunes_the_bands(cache_home, data_home, fake_net, capsys):
    fake_net.routes["https://api.scryfall.com/cards/search?q=Lightning%20Bolt"] = lambda: {
        "data": [_scryfall_card(usd="4.50")]
    }
    _seed_history()
    _seed_confirmed_spike(cents=450)
    store = WatchStore()
    store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    # Zero-width bands: any confirmed move is outside the band.
    assert main(["--json", "check", "--band-k", "0"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert [a["rule"] for a in row["smart_alerts"]] == ["band_high"]
    assert row["smart_alerts"][0]["line"].startswith("above 14d band $4.20")


def test_invalid_window_rejected(cache_home, data_home, capsys):
    store = WatchStore()
    store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    assert main(["check", "--window", "3"]) == 2
    assert "--window must be at least 6" in capsys.readouterr().out
    assert main(["check", "--window", "5"]) == 2


def test_invalid_deviation_rejected(cache_home, data_home, capsys):
    store = WatchStore()
    store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    for bad in ("0", "-2", "nan"):
        assert main(["check", "--deviation", bad]) == 2
        assert "--deviation must be a positive number" in capsys.readouterr().out


def test_invalid_band_k_rejected(cache_home, data_home, capsys):
    store = WatchStore()
    store.add("mtg", "scry-1", "Lightning Bolt", "Alpha")
    for bad in ("-1", "nan"):
        assert main(["check", "--band-k", bad]) == 2
        assert "--band-k must be zero or a positive number" in capsys.readouterr().out
