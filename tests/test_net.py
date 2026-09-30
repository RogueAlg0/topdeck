"""Tests for the polite HTTP layer: caching and rate limiting."""

import json

import pytest

import topdeck.net as net


class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Cache in a temp dir, fresh rate-limit state."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(net, "_last_call", {})
    return tmp_path


def _serve(monkeypatch, payloads):
    """Stub urlopen; payloads maps URL to the JSON-serializable body."""
    calls = []

    def fake_open(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(json.dumps(payloads[req.full_url]).encode())

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_open)
    return calls


def test_cache_serves_repeat_lookup_without_refetch(isolated, monkeypatch):
    calls = _serve(monkeypatch, {"https://x.test/a": {"n": 1}})
    assert net.fetch_json("https://x.test/a", ttl=3600) == {"n": 1}
    assert net.fetch_json("https://x.test/a", ttl=3600) == {"n": 1}
    assert calls == ["https://x.test/a"]


def test_expired_cache_refetches(isolated, monkeypatch):
    calls = _serve(monkeypatch, {"https://x.test/a": {"n": 1}})
    net.fetch_json("https://x.test/a", ttl=3600)
    net.fetch_json("https://x.test/a", ttl=0)  # ttl 0 forces a refetch
    assert calls == ["https://x.test/a", "https://x.test/a"]


def test_cache_survives_process_restart(isolated, monkeypatch, tmp_path):
    _serve(monkeypatch, {"https://x.test/a": {"n": 1}})
    net.fetch_json("https://x.test/a", ttl=3600)
    assert (tmp_path / "topdeck" / "http_cache.sqlite").exists()
    # new "process": wipe in-memory state, stub the network to explode
    monkeypatch.setattr(net, "_last_call", {})

    def boom(req, timeout=None):
        raise AssertionError("network should not be touched")

    monkeypatch.setattr(net.urllib.request, "urlopen", boom)
    assert net.fetch_json("https://x.test/a", ttl=3600) == {"n": 1}


def test_rate_limit_paces_same_host(isolated, monkeypatch):
    _serve(
        monkeypatch,
        {"https://x.test/a": {"n": 1}, "https://x.test/b": {"n": 2}},
    )
    sleeps = []
    monkeypatch.setattr(net.time, "sleep", lambda s: sleeps.append(s))
    # ttl=0 so both go to the network; different hosts would not pace
    net.fetch_json("https://x.test/a", ttl=0, min_interval=0.5)
    net.fetch_json("https://x.test/b", ttl=0, min_interval=0.5)
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 0.5


def test_different_hosts_are_not_paced(isolated, monkeypatch):
    _serve(
        monkeypatch,
        {"https://x.test/a": {"n": 1}, "https://y.test/a": {"n": 2}},
    )
    sleeps = []
    monkeypatch.setattr(net.time, "sleep", lambda s: sleeps.append(s))
    net.fetch_json("https://x.test/a", ttl=0, min_interval=0.5)
    net.fetch_json("https://y.test/a", ttl=0, min_interval=0.5)
    assert sleeps == []


def test_http_429_becomes_source_error(isolated, monkeypatch):
    import urllib.error

    def fake_open(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "slow down", {}, None)

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_open)
    with pytest.raises(net.SourceError, match="slow down"):
        net.fetch_json("https://x.test/a", ttl=0)


def test_unreachable_host_becomes_source_error(isolated, monkeypatch):
    import urllib.error

    def fake_open(req, timeout=None):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_open)
    with pytest.raises(net.SourceError, match="could not reach"):
        net.fetch_json("https://x.test/a", ttl=0)


def test_user_agent_is_identifying(isolated, monkeypatch):
    seen = []

    def fake_open(req, timeout=None):
        items = {k.lower(): v for k, v in req.header_items()}
        seen.append(items.get("user-agent", ""))
        return _Resp(b"{}")

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_open)
    net.fetch_json("https://x.test/a", ttl=0)
    assert seen[0] and "topdeck" in seen[0] and "python" not in seen[0].lower()
