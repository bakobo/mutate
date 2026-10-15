"""The bakobo-mutate command: subcommands dispatch to the tools and pass their argv through."""

from __future__ import annotations

import pytest

from bakobo_mutate import cli


def test_help_lists_both_commands(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--help"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out
    for command in ("run", "ticks", "stale", "enroll", "unenroll", "sweep"):
        assert command in out
    assert "mutation-test what landed" in out


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main([])
    assert exit_.value.code == 2
    assert "<command>" in capsys.readouterr().err


def test_an_unknown_command_is_refused(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["bogus"])
    assert exit_.value.code == 2
    assert "bogus" in capsys.readouterr().err


@pytest.mark.parametrize("command, option", [
    ("run", "--chunk"), ("ticks", "--dry-run"), ("stale", "--fetch"), ("enroll", "target"),
    ("unenroll", "target"), ("sweep", "--tick"),
])
def test_a_commands_help_is_its_own(command, option, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main([command, "--help"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: bakobo-mutate {command} ")
    assert option in out


@pytest.mark.parametrize("command", ["run", "ticks", "stale", "enroll", "unenroll", "sweep"])
def test_the_rest_of_argv_is_passed_through(command, monkeypatch):
    seen = {}

    def handler(argv, prog):
        seen.update(argv=argv, prog=prog)
        return 7

    monkeypatch.setitem(cli.COMMANDS, command, (handler, "stub"))
    assert cli.main([command, "--root", "/x", "positional"]) == 7
    assert seen == {"argv": ["--root", "/x", "positional"], "prog": f"bakobo-mutate {command}"}


def test_argv_defaults_to_the_command_line(monkeypatch):
    seen = {}
    monkeypatch.setitem(cli.COMMANDS, "ticks", (lambda argv, prog: seen.setdefault("a", argv)
                                                 and 0, "stub"))
    monkeypatch.setattr("sys.argv", ["bakobo-mutate", "ticks", "--dry-run"])
    assert cli.main() == 0
    assert seen["a"] == ["--dry-run"]
