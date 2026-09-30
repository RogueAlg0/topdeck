"""Tests for the stderr progress counter. No network, no wall clock."""

import threading

import pytest

from topdeck import progress
from topdeck.progress import Progress


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(progress, "_enabled", True)


def test_set_enabled_toggles_global_flag():
    progress.set_enabled(False)
    assert not progress.is_enabled()
    progress.set_enabled(True)
    assert progress.is_enabled()


def test_progress_counts_up(capsys):
    shown = Progress("Fetching cards", 3)
    assert shown.active
    shown.tick()
    shown.tick()
    shown.tick()
    shown.finish()
    err = capsys.readouterr().err
    assert "Fetching cards: 3/3" in err


def test_progress_single_item_stays_silent(capsys):
    shown = Progress("Fetching cards", 1)
    assert not shown.active
    shown.tick()
    shown.finish()
    assert capsys.readouterr().err == ""


def test_progress_disabled_flag_stays_silent(capsys, monkeypatch):
    monkeypatch.setattr(progress, "_enabled", False)
    shown = Progress("Fetching cards", 5)
    assert not shown.active
    shown.tick()
    shown.finish()
    assert capsys.readouterr().err == ""


def test_progress_tick_is_thread_safe(capsys):
    shown = Progress("Fetching cards", 200)

    def work():
        for _ in range(25):
            shown.tick()

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    shown.finish()
    assert "Fetching cards: 200/200" in capsys.readouterr().err


def test_json_mode_disables_progress(tmp_path, monkeypatch, capsys):
    """--json suppresses stderr progress; human mode turns it back on."""
    from topdeck.cli import main

    monkeypatch.setenv("TOPDECK_DATA_DIR", str(tmp_path))
    try:
        assert main(["--json", "watch", "list"]) == 0
        assert not progress.is_enabled()
        assert main(["watch", "list"]) == 0
        assert progress.is_enabled()
    finally:
        progress.set_enabled(True)
