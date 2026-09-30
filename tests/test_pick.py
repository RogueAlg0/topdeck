"""Tests for the disambiguation picker. The terminal and prompts are faked."""

from __future__ import annotations

import sys

import pytest
from rich.console import Console

from topdeck import pick
from topdeck.adapters.base import CardHit
from topdeck.pick import _numbered_pick, _questionary_pick, interactive_pick


def _hit(name, set_name="Set", number="1"):
    return CardHit(
        card_id=name,
        name=name,
        set_code="S",
        set_name=set_name,
        collector_number=number,
        url="https://example.com/card",
    )


def _hits(n):
    return [_hit(f"Card {i}") for i in range(n)]


def _console():
    return Console(width=120)


class _FakeSelect:
    def __init__(self, answer=None, exc=None):
        self._answer = answer
        self._exc = exc

    def ask(self):
        if self._exc is not None:
            raise self._exc
        return self._answer


class _FakeQuestionary:
    """Stands in for the questionary module. Records the choices it was
    given and answers with a fixed index (or raises, or walks away)."""

    Choice = staticmethod(lambda title, value: (title, value))

    def __init__(self, answer=None, exc=None):
        self._answer = answer
        self._exc = exc
        self.prompts = []

    def select(self, prompt, choices, use_arrow_keys):
        self.prompts.append((prompt, list(choices), use_arrow_keys))
        return _FakeSelect(self._answer, self._exc)


@pytest.fixture
def fake_q(monkeypatch):
    """Install a controllable fake questionary module. Returns a factory
    for the fake so each test can pick its answer."""

    def install(answer=None, exc=None):
        fq = _FakeQuestionary(answer=answer, exc=exc)
        monkeypatch.setitem(sys.modules, "questionary", fq)
        return fq

    return install


def _tty(monkeypatch, value):
    monkeypatch.setattr(pick, "_tty", lambda: value)


# ---------------------------------------------------------------------------
# _tty


def test_tty_needs_both_streams(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert pick._tty() is False
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert pick._tty() is True


# ---------------------------------------------------------------------------
# _questionary_pick


def test_questionary_returns_chosen_index(fake_q):
    fq = fake_q(answer=2)
    index = _questionary_pick(_console(), _hits(3), "card")
    assert index == 2
    prompt, choices, use_arrows = fq.prompts[0]
    assert "3 cards" in prompt
    assert use_arrows is True
    assert "[recommended]" in choices[0][0]
    assert "[recommended]" not in choices[1][0]


def test_questionary_caps_choices_at_ten(fake_q):
    fq = fake_q(answer=0)
    _questionary_pick(_console(), _hits(12), "card")
    assert len(fq.prompts[0][1]) == 10


def test_questionary_labels_carry_set_and_number(fake_q):
    fq = fake_q(answer=0)
    hits = [_hit("Bolt", set_name="Alpha", number="161")]
    _questionary_pick(_console(), hits, "bolt")
    assert "Alpha" in fq.prompts[0][1][0][0]
    assert "161" in fq.prompts[0][1][0][0]


def test_questionary_walkaway_returns_none(fake_q):
    fake_q(answer=None)
    assert _questionary_pick(_console(), _hits(2), "card") is None


def test_questionary_eof_returns_none(fake_q):
    fake_q(exc=EOFError())
    assert _questionary_pick(_console(), _hits(2), "card") is None


def test_questionary_interrupt_returns_none(fake_q):
    fake_q(exc=KeyboardInterrupt())
    assert _questionary_pick(_console(), _hits(2), "card") is None


def test_questionary_unexpected_error_returns_none(fake_q):
    fake_q(exc=RuntimeError("terminal exploded"))
    assert _questionary_pick(_console(), _hits(2), "card") is None


def test_questionary_import_error_returns_none(monkeypatch):
    monkeypatch.setitem(sys.modules, "questionary", None)
    assert _questionary_pick(_console(), _hits(2), "card") is None


# ---------------------------------------------------------------------------
# _numbered_pick


def test_numbered_pick_empty_means_first(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert _numbered_pick(_console(), _hits(3), "card") == 0


def test_numbered_pick_parses_number(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "3")
    assert _numbered_pick(_console(), _hits(3), "card") == 2


def test_numbered_pick_rejects_garbage(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "abc")
    assert _numbered_pick(_console(), _hits(3), "card") is None


def test_numbered_pick_rejects_out_of_range(monkeypatch):
    for raw in ("0", "99"):
        monkeypatch.setattr("builtins.input", lambda prompt="", r=raw: r)
        assert _numbered_pick(_console(), _hits(3), "card") is None


def test_numbered_pick_caps_at_ten(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "11")
    assert _numbered_pick(_console(), _hits(12), "card") is None


def test_numbered_pick_eof_is_walkaway(monkeypatch):
    def boom(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    assert _numbered_pick(_console(), _hits(3), "card") is None


def test_numbered_pick_interrupt_is_walkaway(monkeypatch):
    def boom(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", boom)
    assert _numbered_pick(_console(), _hits(3), "card") is None


# ---------------------------------------------------------------------------
# interactive_pick routing


def test_interactive_pick_uses_questionary_on_tty(monkeypatch, fake_q):
    _tty(monkeypatch, True)
    fake_q(answer=1)
    hits = _hits(3)
    assert interactive_pick(_console(), hits, "card") is hits[1]


def test_interactive_pick_falls_back_to_numbers(monkeypatch, fake_q):
    _tty(monkeypatch, True)
    monkeypatch.setitem(sys.modules, "questionary", None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    hits = _hits(3)
    assert interactive_pick(_console(), hits, "card") is hits[1]


def test_interactive_pick_skips_questionary_off_tty(monkeypatch):
    _tty(monkeypatch, False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    hits = _hits(3)
    assert interactive_pick(_console(), hits, "card") is hits[0]


def test_interactive_pick_walkaway_returns_none(monkeypatch, capsys):
    _tty(monkeypatch, False)

    def boom(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    console = _console()
    assert interactive_pick(console, _hits(3), "card") is None
    assert "No problem" in capsys.readouterr().out
