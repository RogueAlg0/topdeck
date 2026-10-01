"""Tests for `topdeck completions`: scripts generated from the live parser."""

import shutil
import subprocess

import pytest

from topdeck import completions
from topdeck.cli import build_parser, main


def test_generate_bash_has_hook_and_content():
    script = completions.generate("bash")
    assert "complete -F _topdeck topdeck" in script
    # Commands, global flags, game slugs, and sub-actions all come
    # from the live parser and registry.
    for word in ("price", "watch", "portfolio", "--json", "--version", "mtg", "add"):
        assert word in script


def test_generate_bash_script_parses(tmp_path):
    path = tmp_path / "topdeck.bash"
    path.write_text(completions.generate("bash"))
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True)
    assert result.returncode == 0


def test_generate_zsh_has_compdef_and_parses(tmp_path):
    script = completions.generate("zsh")
    assert script.startswith("#compdef topdeck")
    assert "compadd" in script
    assert "mtg" in script
    assert "bash zsh fish" in script  # the shell positional's own choices
    path = tmp_path / "_topdeck"
    path.write_text(script)
    if shutil.which("zsh") is None:
        pytest.skip("zsh is not installed")
    result = subprocess.run(["zsh", "-n", str(path)], capture_output=True)
    assert result.returncode == 0


def test_generate_fish_has_hook_and_content():
    script = completions.generate("fish")
    assert "complete -c topdeck" in script
    assert "__fish_use_subcommand" in script
    assert "__fish_seen_subcommand_from price" in script
    assert "mtg" in script
    assert "bash zsh fish" in script


def test_generate_unknown_shell_raises():
    with pytest.raises(ValueError, match="unknown shell"):
        completions.generate("powershell")


def test_completions_command_hidden_from_help():
    help_text = build_parser().format_help()
    assert "completions" not in help_text
    # ...while the real commands still are.
    for command in ("price", "watch", "check", "history", "sync", "portfolio"):
        assert command in help_text


def test_completions_end_to_end(capsys):
    assert main(["completions", "bash"]) == 0
    assert "complete -F _topdeck topdeck" in capsys.readouterr().out
    assert main(["completions", "zsh"]) == 0
    assert capsys.readouterr().out.startswith("#compdef topdeck")
    assert main(["completions", "fish"]) == 0
    assert "complete -c topdeck" in capsys.readouterr().out


def test_completions_needs_shell_choice(capsys):
    with pytest.raises(SystemExit):
        main(["completions"])
