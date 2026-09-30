"""Tests for watchlists, check, and target alerts. All network is faked."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topdeck import adapters as game_adapters
from topdeck.adapters.base import CardHit, GameAdapter, Price
from topdeck.cli import main
from topdeck.net import SourceError
from topdeck.watch import (
    WatchStore,
    build_row,
    describe_move,
    pick_tracked_price,
)


class FakeAdapter(GameAdapter):
    game_key = "mtg"
    display_name = "Magic: The Gathering"
    trust_tier = "solid"
    source_name = "fake"

    def __init__(self):
        self.hits: list[CardHit] = []
        self.prices: dict[str, list[Price]] = {}
        self.fail_search: str | None = None
        self.fail_prices: str | None = None

    def search(self, query: str) -> list[CardHit]:
        if self.fail_search:
            raise SourceError(self.fail_search)
        return self.hits

    def get_prices(self, hit: CardHit) -> list[Price]:
        if self.fail_prices:
            raise SourceError(self.fail_prices)
        return self.prices.get(hit.card_id, [])


def _price(
    value: float, market: str = "tcgplayer", currency: str = "USD", printing: str = "normal"
) -> Price:
    return Price(
        market=market,
        currency=currency,
        condition="near-mint",
        printing=printing,
        price=value,
        as_of="2026-09-30T00:00:00Z",
        source="fake",
    )


def _hit(card_id: str = "card-1", name: str = "Lightning Bolt", set_name: str = "Alpha") -> CardHit:
    return CardHit(
        card_id=card_id,
        name=name,
        set_code="LEA",
        set_name=set_name,
        collector_number="161",
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPDECK_DATA_DIR", str(tmp_path))
    return WatchStore()


@pytest.fixture
def fake(monkeypatch):
    adapter = FakeAdapter()
    monkeypatch.setitem(game_adapters.REGISTRY, "mtg", adapter)
    return adapter


# ---------------------------------------------------------------------------
# Store


def test_add_and_list_round_trip(store):
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha", 2.5)
    assert watch.id > 0
    watches = store.list()
    assert len(watches) == 1
    assert watches[0].name == "Lightning Bolt"
    assert watches[0].target_price == 2.5


def test_add_without_target_stores_none(store):
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    assert watch.target_price is None


def test_add_duplicate_game_and_card_raises(store):
    store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    with pytest.raises(sqlite3.IntegrityError):
        store.add("mtg", "card-1", "Lightning Bolt", "Beta")


def test_same_card_id_in_another_game_is_fine(store):
    store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    other = store.add("pokemon", "card-1", "Pikachu", "Base")
    assert other.id != store.list()[0].id


def test_set_target_updates(store):
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    store.set_target(watch.id, 3.0)
    assert store.get(watch.id).target_price == 3.0
    store.set_target(watch.id, None)
    assert store.get(watch.id).target_price is None


def test_remove_deletes_watch_and_snapshots(store):
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    store.record_snapshot(watch.id, 1.0, "USD", "fake")
    assert store.remove(watch.id).name == "Lightning Bolt"
    assert store.list() == []
    assert store.previous_price(watch.id) is None


def test_remove_missing_returns_none(store):
    assert store.remove(999) is None


def test_previous_price_skips_nulls_and_takes_latest(store):
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    assert store.previous_price(watch.id) is None
    store.record_snapshot(watch.id, 1.0, "USD", "fake")
    store.record_snapshot(watch.id, None, "USD", "fake")
    store.record_snapshot(watch.id, 1.5, "USD", "fake")
    assert store.previous_price(watch.id) == 1.5


def test_find_by_name_is_case_insensitive(store):
    store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    assert len(store.find_by_name("lightning bolt")) == 1
    assert store.find_by_name("Black Lotus") == []


# ---------------------------------------------------------------------------
# Delta math


def test_spike_at_exactly_ten_percent():
    delta_abs, delta_pct, spike, drop = describe_move(1.0, 1.10)
    assert spike and not drop
    assert delta_abs == pytest.approx(0.10)
    assert delta_pct == pytest.approx(10.0)


def test_drop_at_exactly_ten_percent():
    delta_abs, delta_pct, spike, drop = describe_move(1.0, 0.90)
    assert drop and not spike
    assert delta_pct == pytest.approx(-10.0)


def test_small_move_is_neither():
    _, _, spike, drop = describe_move(10.0, 10.50)
    assert not spike and not drop


def test_no_previous_means_no_delta():
    assert describe_move(None, 1.0) == (None, None, False, False)


def test_no_current_means_no_delta():
    assert describe_move(1.0, None) == (None, None, False, False)


def test_zero_previous_does_not_crash():
    _, delta_pct, spike, drop = describe_move(0.0, 1.0)
    assert delta_pct is None and not spike and not drop


# ---------------------------------------------------------------------------
# Rows and target hits


def test_build_row_flags_target_hit(store):
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha", 2.0)
    row = build_row(watch, 3.0, 1.5, "USD", "fake")
    assert row.target_hit
    assert row.alert


def test_build_row_no_hit_when_above_target(store):
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha", 1.0)
    row = build_row(watch, 1.5, 1.5, "USD", "fake")
    assert not row.target_hit
    assert not row.alert


def test_build_row_without_target_never_hits(store):
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    row = build_row(watch, 3.0, 0.01, "USD", "fake")
    assert not row.target_hit


def test_error_row_is_an_alert(store):
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    row = build_row(watch, 1.0, None, "USD", "", error="boom")
    assert row.alert


def test_pick_tracked_price_prefers_usd_tcgplayer_normal():
    prices = [
        _price(9.0, market="cardmarket", currency="EUR"),
        _price(1.5, printing="foil"),
        _price(1.2),
    ]
    assert pick_tracked_price(prices).price == 1.2


def test_pick_tracked_price_none_when_no_prices():
    assert pick_tracked_price([]) is None


# ---------------------------------------------------------------------------
# CLI: watch add / list / remove


def test_watch_add_and_list(store, fake, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.25)]}
    assert main(["watch", "add", "mtg", "Lightning Bolt", "--first"]) == 0
    out = capsys.readouterr().out
    assert 'Watching "Lightning Bolt"' in out
    watches = store.list()
    assert len(watches) == 1
    assert watches[0].target_price is None


def test_watch_add_stores_target(store, fake, capsys):
    fake.hits = [_hit()]
    assert main(["watch", "add", "mtg", "Bolt", "--first", "--target", "2.50"]) == 0
    assert store.list()[0].target_price == 2.5
    assert "Target: $2.50" in capsys.readouterr().out


def test_watch_add_json(store, fake, capsys):
    fake.hits = [_hit()]
    assert main(["--json", "watch", "add", "mtg", "Bolt", "--first", "--target", "2"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "added"
    assert payload["watches"][0]["target_price"] == 2.0


@pytest.mark.parametrize("bad", ["-5", "0", "abc", "1e999"])
def test_watch_add_rejects_bad_target(store, fake, capsys, bad):
    fake.hits = [_hit()]
    assert main(["watch", "add", "mtg", "Bolt", "--first", "--target", bad]) == 2
    assert "not a usable price" in capsys.readouterr().out
    assert store.list() == []


def test_watch_add_is_idempotent(store, fake, capsys):
    fake.hits = [_hit()]
    assert main(["watch", "add", "mtg", "Bolt", "--first"]) == 0
    capsys.readouterr()
    assert main(["watch", "add", "mtg", "Bolt", "--first"]) == 0
    assert "already on your watchlist" in capsys.readouterr().out
    assert len(store.list()) == 1


def test_watch_readd_with_target_updates_it(store, fake, capsys):
    fake.hits = [_hit()]
    assert main(["watch", "add", "mtg", "Bolt", "--first"]) == 0
    capsys.readouterr()
    assert main(["watch", "add", "mtg", "Bolt", "--first", "--target", "3"]) == 0
    assert "Updated target" in capsys.readouterr().out
    assert len(store.list()) == 1
    assert store.list()[0].target_price == 3.0


def test_watch_readd_json_reports_actions(store, fake, capsys):
    fake.hits = [_hit()]
    main(["--json", "watch", "add", "mtg", "Bolt", "--first"])
    capsys.readouterr()
    main(["--json", "watch", "add", "mtg", "Bolt", "--first"])
    assert json.loads(capsys.readouterr().out)["action"] == "already_on_watchlist"
    main(["--json", "watch", "add", "mtg", "Bolt", "--first", "--target", "1"])
    assert json.loads(capsys.readouterr().out)["action"] == "target_updated"


def test_watch_bare_command_lists(store, fake, capsys):
    fake.hits = [_hit()]
    main(["watch", "add", "mtg", "Bolt", "--first"])
    capsys.readouterr()
    assert main(["watch"]) == 0
    assert "Lightning Bolt" in capsys.readouterr().out


def test_watch_list_shows_target(store, fake, capsys):
    fake.hits = [_hit()]
    main(["watch", "add", "mtg", "Bolt", "--first", "--target", "2.5"])
    capsys.readouterr()
    assert main(["watch", "list"]) == 0
    assert "$2.50" in capsys.readouterr().out


def test_watch_list_empty_is_friendly(store, capsys):
    assert main(["watch", "list"]) == 0
    assert "watchlist is empty" in capsys.readouterr().out


def test_watch_list_json_empty(store, capsys):
    assert main(["--json", "watch"]) == 0
    assert json.loads(capsys.readouterr().out)["watches"] == []


def test_watch_remove_by_id(store, fake, capsys):
    fake.hits = [_hit()]
    main(["watch", "add", "mtg", "Bolt", "--first"])
    capsys.readouterr()
    watch_id = store.list()[0].id
    assert main(["watch", "remove", str(watch_id)]) == 0
    assert "Stopped watching" in capsys.readouterr().out
    assert store.list() == []


def test_watch_remove_by_name(store, fake, capsys):
    fake.hits = [_hit()]
    main(["watch", "add", "mtg", "Bolt", "--first"])
    capsys.readouterr()
    assert main(["watch", "remove", "lightning", "bolt"]) == 0
    assert store.list() == []


def test_watch_remove_missing_exits_one(store, capsys):
    assert main(["watch", "remove", "Black Lotus"]) == 1
    assert "not on your watchlist" in capsys.readouterr().out


def test_watch_remove_ambiguous_name_exits_two(store, capsys):
    store.add("mtg", "card-1", "Bolt", "Alpha")
    store.add("mtg", "card-2", "Bolt", "Beta")
    assert main(["watch", "remove", "Bolt"]) == 2
    assert "by ID" in capsys.readouterr().out
    assert len(store.list()) == 2


def test_watch_add_unknown_game(store, capsys):
    assert main(["watch", "add", "yugioh", "Blue-Eyes", "--first"]) == 2


# ---------------------------------------------------------------------------
# CLI: check


def test_check_empty_watchlist(store, capsys):
    assert main(["check"]) == 0
    assert "watchlist is empty" in capsys.readouterr().out


def test_check_flags_spike_and_stores_snapshot(store, fake, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.20)]}
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    store.record_snapshot(watch.id, 1.00, "USD", "fake")
    assert main(["--json", "check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    row = payload["rows"][0]
    assert row["spike"] is True
    assert row["drop"] is False
    assert row["target_hit"] is False
    assert row["delta_pct"] == pytest.approx(20.0)
    assert row["previous"] == 1.00
    assert row["current"] == 1.20
    assert store.previous_price(watch.id) == 1.20


def test_check_flags_target_hit(store, fake, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.50)]}
    store.add("mtg", "card-1", "Lightning Bolt", "Alpha", 2.0)
    assert main(["--json", "check"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert row["target_hit"] is True
    assert row["alert"] is True


def test_check_alert_only_json_filters_quiet_rows(store, fake, capsys):
    fake.hits = [_hit("card-1", "Bolt One"), _hit("card-2", "Bolt Two")]
    fake.prices = {"card-1": [_price(1.20)], "card-2": [_price(1.01)]}
    w1 = store.add("mtg", "card-1", "Bolt One", "Alpha")
    w2 = store.add("mtg", "card-2", "Bolt Two", "Alpha")
    store.record_snapshot(w1.id, 1.00, "USD", "fake")
    store.record_snapshot(w2.id, 1.00, "USD", "fake")
    assert main(["--json", "check", "--alert-only"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["alert_only"] is True
    assert [r["watch"]["name"] for r in payload["rows"]] == ["Bolt One"]


def test_check_alert_only_human_shows_movers_and_summary(store, fake, capsys):
    fake.hits = [_hit("card-1", "Bolt One"), _hit("card-2", "Bolt Two")]
    fake.prices = {"card-1": [_price(1.20)], "card-2": [_price(1.01)]}
    w1 = store.add("mtg", "card-1", "Bolt One", "Alpha")
    w2 = store.add("mtg", "card-2", "Bolt Two", "Alpha")
    store.record_snapshot(w1.id, 1.00, "USD", "fake")
    store.record_snapshot(w2.id, 1.00, "USD", "fake")
    assert main(["check", "--alert-only"]) == 0
    out = capsys.readouterr().out
    assert "Bolt One" in out
    assert "Bolt Two" not in out
    assert "1 of 2 watched cards need attention" in out


def test_check_alert_only_all_quiet(store, fake, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.01)]}
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    store.record_snapshot(watch.id, 1.00, "USD", "fake")
    assert main(["check", "--alert-only"]) == 0
    out = capsys.readouterr().out
    assert "All quiet" in out
    assert "Lightning Bolt" not in out


def test_check_includes_error_rows_in_alert_only(store, fake, capsys):
    fake.hits = [_hit()]
    fake.fail_search = "source is down"
    store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    assert main(["--json", "check", "--alert-only"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert len(rows) == 1
    assert rows[0]["error"] == "source is down"


def test_check_prefers_matching_card_id_but_falls_back(store, fake, capsys):
    fake.hits = [_hit("card-9", "Lightning Bolt")]
    fake.prices = {"card-9": [_price(2.00)]}
    store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    assert main(["--json", "check"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert row["current"] == 2.00
    assert row["note"] == "exact printing no longer listed; showing closest match"


def test_check_source_error_does_not_record_snapshot(store, fake, capsys):
    fake.fail_search = "source is down"
    watch = store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    assert main(["check"]) == 0
    assert "error" in capsys.readouterr().out
    assert store.previous_price(watch.id) is None


def test_check_human_table_shows_target_hit(store, fake, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.50)]}
    store.add("mtg", "card-1", "Lightning Bolt", "Alpha", 2.0)
    assert main(["check"]) == 0
    assert "TARGET HIT" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Watch store location: XDG data dir with honest fallbacks


def test_default_path_honors_override(monkeypatch, tmp_path):
    import os

    from topdeck.watch import _default_path

    monkeypatch.setenv("TOPDECK_DATA_DIR", str(tmp_path))
    assert _default_path() == os.path.join(str(tmp_path), "watchlist.sqlite")


def test_default_path_honors_xdg_data_home(monkeypatch, tmp_path):
    import os

    from topdeck.watch import _default_path

    monkeypatch.delenv("TOPDECK_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _default_path() == os.path.join(str(tmp_path), "topdeck", "watchlist.sqlite")


def test_default_path_falls_back_to_dot_topdeck(monkeypatch, tmp_path):
    import os

    from topdeck.watch import _default_path

    monkeypatch.delenv("TOPDECK_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    real_makedirs = os.makedirs

    def boom(path, exist_ok=False):
        if "topdeck" in path and ".topdeck" not in path:
            raise OSError("read-only")
        return real_makedirs(path, exist_ok=exist_ok)

    monkeypatch.setattr(os, "makedirs", boom)
    assert _default_path() == os.path.join(str(tmp_path), ".topdeck", "watchlist.sqlite")


def test_default_path_returns_first_when_nothing_writable(monkeypatch, tmp_path):
    import os

    from topdeck.watch import _default_path

    monkeypatch.delenv("TOPDECK_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    def boom(path, exist_ok=False):
        raise OSError("read-only")

    monkeypatch.setattr(os, "makedirs", boom)
    assert _default_path().endswith(os.path.join("topdeck", "watchlist.sqlite"))


# ---------------------------------------------------------------------------
# CLI: watch add search and pick paths


def test_watch_add_search_source_error(store, fake, capsys):
    fake.fail_search = "source is down"
    assert main(["watch", "add", "mtg", "Bolt", "--first"]) == 1
    assert "Could not look that up" in capsys.readouterr().out


def test_watch_add_search_empty(store, fake, capsys):
    fake.hits = []
    assert main(["watch", "add", "mtg", "zzz", "--first"]) == 0
    assert "No matches" in capsys.readouterr().out


def test_watch_add_pick_selects_numbered_match(store, fake):
    fake.hits = [_hit("card-1", "Bolt", "Alpha"), _hit("card-2", "Bolt", "Beta")]
    assert main(["watch", "add", "mtg", "Bolt", "--pick", "2"]) == 0
    assert store.find("mtg", "card-2") is not None


def test_watch_add_pick_out_of_range(store, fake, capsys):
    fake.hits = [_hit("card-1", "Bolt", "Alpha"), _hit("card-2", "Bolt", "Beta")]
    assert main(["watch", "add", "mtg", "Bolt", "--pick", "9"]) == 2
    assert "out of range" in capsys.readouterr().out


def test_watch_add_first_takes_recommended(store, fake):
    fake.hits = [_hit("card-1", "Bolt", "Alpha"), _hit("card-2", "Bolt", "Beta")]
    assert main(["watch", "add", "mtg", "Bolt", "--first"]) == 0
    assert store.find("mtg", "card-1") is not None


def test_watch_add_interactive_walkaway(store, fake, monkeypatch):
    fake.hits = [_hit("card-1", "Bolt", "Alpha"), _hit("card-2", "Bolt", "Beta")]
    monkeypatch.setattr("topdeck.cli._is_interactive", lambda: True)

    def boom(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    assert main(["watch", "add", "mtg", "Bolt"]) == 1


def test_watch_add_interactive_pick_selects(store, fake, monkeypatch):
    fake.hits = [_hit("card-1", "Bolt", "Alpha"), _hit("card-2", "Bolt", "Beta")]
    monkeypatch.setattr("topdeck.cli._is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    assert main(["watch", "add", "mtg", "Bolt"]) == 0
    assert store.find("mtg", "card-2") is not None


def test_watch_add_integrity_error_treated_as_watched(store, fake, monkeypatch, capsys):
    # Simulates losing a DB race with another process: the row exists, but
    # our own insert raised IntegrityError. The CLI must recover gracefully.
    fake.hits = [_hit()]
    real_add = WatchStore.add
    state = {"raced": False}

    def raced_add(self, *args, **kwargs):
        if not state["raced"]:
            state["raced"] = True
            real_add(self, *args, **kwargs)
            raise sqlite3.IntegrityError("lost the race")
        return real_add(self, *args, **kwargs)

    monkeypatch.setattr(WatchStore, "add", raced_add)
    assert main(["watch", "add", "mtg", "Bolt", "--first"]) == 0
    assert "already on your watchlist" in capsys.readouterr().out
    assert store.find("mtg", "card-1") is not None


def test_watch_add_integrity_error_json(store, fake, monkeypatch, capsys):
    fake.hits = [_hit()]
    real_add = WatchStore.add
    state = {"raced": False}

    def raced_add(self, *args, **kwargs):
        if not state["raced"]:
            state["raced"] = True
            real_add(self, *args, **kwargs)
            raise sqlite3.IntegrityError("lost the race")
        return real_add(self, *args, **kwargs)

    monkeypatch.setattr(WatchStore, "add", raced_add)
    assert main(["--json", "watch", "add", "mtg", "Bolt", "--first"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "already_on_watchlist"
    assert payload["watches"][0]["card_id"] == "card-1"


def test_watch_remove_json(store, fake, capsys):
    fake.hits = [_hit()]
    assert main(["watch", "add", "mtg", "Bolt", "--first"]) == 0
    capsys.readouterr()
    assert main(["--json", "watch", "remove", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "removed"


# ---------------------------------------------------------------------------
# CLI: check error and quiet paths


def test_check_unknown_game_is_error_row(store, fake, capsys):
    store.add("bogus", "c1", "Mystery Card", "Set")
    assert main(["--json", "check"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert "unknown game" in row["error"]


def test_check_card_no_longer_listed(store, fake, capsys):
    fake.hits = []
    store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    assert main(["--json", "check"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert "no longer listed" in row["error"]


def test_check_prices_error_row(store, fake, capsys):
    fake.hits = [_hit()]
    fake.fail_prices = "prices exploded"
    store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    assert main(["--json", "check"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert "exploded" in row["error"]


def test_check_no_prices_note(store, fake, capsys):
    fake.hits = [_hit()]
    fake.prices = {}
    store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    assert main(["--json", "check"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert row["note"] == "no prices right now"


def test_check_json_empty_watchlist(store, capsys):
    assert main(["--json", "check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == []
    assert payload["alert_only"] is False


def test_check_quiet_day_prints_plain_summary(store, fake, capsys):
    fake.hits = [_hit()]
    fake.prices = {"card-1": [_price(1.00)]}
    store.add("mtg", "card-1", "Lightning Bolt", "Alpha")
    assert main(["check"]) == 0
    capsys.readouterr()
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "Checked 1 watched card." in out
    assert "need attention" not in out
