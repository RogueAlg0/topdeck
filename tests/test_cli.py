"""Real tests for the topdeck CLI skeleton.

These test the contract that matters right now: the binary runs, every
documented subcommand exits 0, and --json produces parseable output.
"""

import json

import pytest

from topdeck import __version__
from topdeck.cli import COMMAND_DESCRIPTIONS, main


def test_version_string():
    assert __version__ == "0.1.0"


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "topdeck" in out
    for command in COMMAND_DESCRIPTIONS:
        assert command in out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize("command", list(COMMAND_DESCRIPTIONS))
def test_stub_subcommand_exits_zero(command, capsys):
    assert main([command]) == 0
    out = capsys.readouterr().out
    assert "0.1.0" in out


@pytest.mark.parametrize("command", list(COMMAND_DESCRIPTIONS))
def test_stub_json_output_parses(command, capsys):
    assert main(["--json", command]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == command
    assert payload["status"] == "coming_soon"
    assert payload["version"] == __version__


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out
