"""Tests for topdeck doctor. The network is faked at the probe layer."""

from __future__ import annotations

import json
import urllib.error

import pytest

from topdeck import doctor
from topdeck.cli import main
from topdeck.watch import WatchStore


def _healthy(url, user_agent):
    return True, 120, "responding"


def _slow(url, user_agent):
    return True, 2500, "responding"


def _down(url, user_agent):
    return False, 5000, "timed out"


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPDECK_DATA_DIR", str(tmp_path))


def test_doctor_all_healthy(monkeypatch, isolated_data, capsys):
    monkeypatch.setattr(doctor, "_probe", _healthy)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "All price sources healthy." in out
    assert "ok" in out


def test_doctor_reports_down_but_still_exits_zero(monkeypatch, isolated_data, capsys):
    monkeypatch.setattr(doctor, "_probe", _down)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "down" in out
    assert "price source(s) down" in out


def test_doctor_flags_slow_sources(monkeypatch, isolated_data, capsys):
    monkeypatch.setattr(doctor, "_probe", _slow)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "slow" in out
    assert "some slowly" in out


def test_doctor_mixed_health(monkeypatch, isolated_data, capsys):
    def mixed(url, user_agent):
        if "scryfall" in url:
            return False, 12, "HTTP 500"
        return True, 100, "responding"

    monkeypatch.setattr(doctor, "_probe", mixed)
    assert main(["--json", "doctor"]) == 0
    sources = json.loads(capsys.readouterr().out)["sources"]
    by_game = {s["game"]: s for s in sources}
    assert by_game["mtg"]["status"] == "down"
    assert by_game["mtg"]["detail"] == "HTTP 500"
    assert by_game["pokemon"]["status"] == "ok"
    assert len(sources) == 5


def test_doctor_json_includes_local_state(monkeypatch, isolated_data, capsys):
    monkeypatch.setattr(doctor, "_probe", _healthy)
    WatchStore().add("mtg", "card-1", "Lightning Bolt", "Alpha")
    assert main(["--json", "doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    local = {row["label"]: row["detail"] for row in payload["local"]}
    assert "1 card watched" in local["Watchlist"]
    assert local["HTTP cache"]


def test_probe_sends_user_agent_and_accept(monkeypatch):
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    healthy, _, _ = doctor._probe("https://example.invalid/")
    assert healthy is True
    assert "user-agent" in seen
    assert "accept" in seen


def test_probe_never_raises_on_network_failure(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    healthy, latency_ms, detail = doctor._probe("https://example.invalid/")
    assert healthy is False
    assert latency_ms is not None
    assert detail


def test_probe_reports_http_errors(monkeypatch):
    def http_500(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", http_500)
    healthy, _, detail = doctor._probe("https://example.invalid/")
    assert healthy is False
    assert detail == "HTTP 500"


def test_check_sources_marks_slow_above_threshold(monkeypatch):
    monkeypatch.setattr(doctor, "_probe", _slow)
    results = doctor.check_sources()
    assert all(r.status == "slow" for r in results)
    assert len(results) == 5


def test_check_sources_marks_down_below_threshold(monkeypatch):
    monkeypatch.setattr(doctor, "_probe", _down)
    results = doctor.check_sources()
    assert all(r.status == "down" for r in results)


# ---------------------------------------------------------------------------
# Local state summaries: honest about what they cannot read


def test_cache_summary_no_cache_yet(monkeypatch):
    monkeypatch.setattr(doctor.os.path, "exists", lambda p: False)
    assert doctor._cache_summary() == "no cache yet"


def test_cache_summary_without_db_reports_size(tmp_path, monkeypatch):
    from topdeck import net as net_mod

    cache_file = tmp_path / "http_cache.sqlite"
    cache_file.write_bytes(b"x" * 2048)
    monkeypatch.setattr(net_mod, "_cache_path", lambda: str(cache_file))
    monkeypatch.setattr(net_mod, "_db", lambda: None)
    assert doctor._cache_summary() == "2 KB on disk"


def test_cache_summary_counts_entries(tmp_path, monkeypatch):
    from topdeck import net as net_mod

    cache_file = tmp_path / "http_cache.sqlite"
    monkeypatch.setattr(net_mod, "_cache_path", lambda: str(cache_file))
    conn = net_mod._db()
    assert conn is not None
    conn.execute(
        "INSERT INTO http_cache (url, fetched_at, body) VALUES (?, ?, ?)",
        ("https://example.invalid/x", 0.0, "{}"),
    )
    conn.commit()
    conn.close()
    summary = doctor._cache_summary()
    assert "1 entries" in summary
    assert "KB on disk" in summary


def test_cache_summary_unreadable_file(monkeypatch, tmp_path):
    from topdeck import net as net_mod

    cache_file = tmp_path / "http_cache.sqlite"
    cache_file.write_bytes(b"x")

    def boom(path):
        raise OSError("permission denied")

    monkeypatch.setattr(net_mod, "_cache_path", lambda: str(cache_file))
    monkeypatch.setattr(doctor.os.path, "getsize", boom)
    assert doctor._cache_summary() == "unreadable"


def test_watch_summary_store_unreadable(monkeypatch):
    def boom():
        raise OSError("locked")

    monkeypatch.setattr(doctor, "WatchStore", boom)
    assert doctor._watch_summary() == "unreadable"


def test_watch_summary_count_failure(monkeypatch):
    class BadStore:
        def count(self):
            raise RuntimeError("corrupt")

    monkeypatch.setattr(doctor, "WatchStore", lambda: BadStore())
    assert doctor._watch_summary() == "unreadable"
