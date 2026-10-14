"""The ``bakobo-mutate`` command: one subcommand per tool, the rest of argv passed through."""

from __future__ import annotations

import argparse
from collections.abc import Callable

from . import mutate, ticks

PROG = "bakobo-mutate"
COMMANDS: dict[str, tuple[Callable[..., int], str]] = {
    "run": (mutate.main, "mutation-test the changed modules and write a report"),
    "ticks": (ticks.main, "file one tick per surviving mutant in a report"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=("Mutation-test the modules a repo changed, and file a tick per surviving "
                     "mutant."),
        epilog="Run '%(prog)s <command> --help' for a command's own options.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    for name, (_, summary) in COMMANDS.items():
        commands.add_parser(name, help=summary, add_help=False)
    # Each subparser declares nothing, so everything after the command is left over for the
    # command's own parser, --help included.
    args, rest = parser.parse_known_args(argv)
    return COMMANDS[args.command][0](rest, prog=f"{PROG} {args.command}")
