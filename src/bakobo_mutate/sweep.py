"""Staleness, enrolment and the weekly sweep that signals it (this.i 24xzjnz7).

    bakobo-mutate stale [TARGET] [--fetch]   # the stale modules, one per line
    bakobo-mutate enroll [TARGET]            # add a repo to the sweep
    bakobo-mutate unenroll [TARGET]          # remove one
    bakobo-mutate sweep                      # file, update or close each repo's stale tick

Results exist only where someone ran ``bakobo-mutate run``, so freshness is signalled rather
than assumed. ``stale`` computes what ``run`` would choose: the modules origin's default branch
changed after the commit they were last mutated at, and those whose last run was interrupted.
A repo with no stamp prints ``never mutated``.

Enrolment is a newline list of absolute repo roots in
``$XDG_CONFIG_HOME/bakobo-mutate/repos``. ``sweep`` fetches each enrolled repo, computes what
is stale, and keeps at most one open ``mutation-stale`` tick in that repo's own ledger: it
files one when something is stale and none is open, notes the open one when the stale list
changed since its last note, and ticks it off when nothing is stale. A repo that cannot be
swept is reported and skipped, and the sweep exits 1 at the end if any was.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

from bakobo.errors import BakoboError  # type: ignore[import-untyped]

from . import checkout, scope

TAG = "mutation-stale"
NEVER = "never mutated"
TICK_ID = re.compile(r"^([2-7][a-z2-7]{3})\s")
STALE_SET = re.compile(r"stale-set: ([0-9a-f]{12})")


def repos_path() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "bakobo-mutate" / "repos"


def enrolled() -> list[str]:
    path = repos_path()
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def save(repos: list[str]) -> None:
    path = repos_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{repo}\n" for repo in repos))


def digest(modules: list[str]) -> str:
    return hashlib.sha256("\n".join(modules).encode()).hexdigest()[:12]


def stale_modules(target: Path, since: str, git: str = "git") -> list[str] | None:
    """What is stale at origin's default branch, or None when the repo was never mutated."""
    stamp = scope.read_stamp(target)
    if stamp is None:
        return None
    ref = f"origin/{checkout.default_branch(target, git)}"
    return scope.stale(target, ref, stamp, since, scope.config_at(target, ref, git), git)


def refuse(error: BakoboError, where: str = "") -> None:
    print(f"{where}{error.code}: {error.detail}\nHint: {error.hint}", file=sys.stderr)


def _parser(prog: str | None, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("target", nargs="?", type=Path, default=None,
                        help="the repo (default: the one around the current directory)")
    return parser


def stale_main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = _parser(prog, "Print the modules whose mutation results are stale.")
    parser.add_argument("--fetch", action="store_true", help="fetch origin first")
    parser.add_argument("--since", default="7 days ago",
                        help="before any base exists, count changes since this git date")
    parser.add_argument("--git", default="git")
    args = parser.parse_args(argv)
    try:
        target = checkout.target_root(args.target or Path.cwd(), args.git)
        scope.check_since(target, args.since, args.git)
        if args.fetch:
            checkout.fetch(target, args.git)
        modules = stale_modules(target, args.since, args.git)
    except BakoboError as error:
        refuse(error)
        return 2
    for line in [NEVER] if modules is None else modules:
        print(line)
    return 0


def enroll_main(argv: list[str] | None = None, prog: str | None = None) -> int:
    args = _parser(prog, "Add a repo to the weekly staleness sweep.").parse_args(argv)
    try:
        target = str(checkout.target_root(args.target or Path.cwd()))
    except BakoboError as error:
        refuse(error)
        return 2
    repos = enrolled()
    if target in repos:
        print(f"{target} is already enrolled")
        return 0
    save([*repos, target])
    print(f"enrolled {target}")
    return 0


def unenroll_main(argv: list[str] | None = None, prog: str | None = None) -> int:
    args = _parser(prog, "Remove a repo from the weekly staleness sweep.").parse_args(argv)
    try:
        # A named repo may be gone already, so it is taken as written rather than resolved.
        target = (str(args.target.resolve()) if args.target
                  else str(checkout.target_root(Path.cwd())))
    except BakoboError as error:
        refuse(error)
        return 2
    repos = enrolled()
    if target not in repos:
        print(f"{target} was not enrolled")
        return 0
    save([repo for repo in repos if repo != target])
    print(f"unenrolled {target}")
    return 0


def title(modules: list[str] | None) -> str:
    if modules is None:
        return "mutation testing has never run in this repo; run bakobo-mutate run"
    count = len(modules)
    noun = "module" if count == 1 else "modules"
    return f"mutation results are stale in {count} {noun}; run bakobo-mutate run"


def note(modules: list[str] | None) -> str:
    if modules is None:
        body = "This repo has never been mutation-tested, so nothing is known about its tests."
        key = [NEVER]
    else:
        body = ("These modules changed on the default branch after they were last mutated, or "
                "their last run was interrupted:\n" + "\n".join(f"- {m}" for m in modules))
        key = modules
    return (f"stale-set: {digest(key)}\n{body}\nRun `bakobo-mutate run` in this repo to bring "
            "them up to date; the weekly sweep closes this tick once nothing is stale.")


class Ledger:
    """The tick CLI, run in one repo without its update check."""

    def __init__(self, tick: str, repo: Path):
        self.tick, self.repo = tick, repo
        self.env = dict(os.environ, TICK_NO_UPDATE_CHECK="1")

    def __call__(self, *args: str) -> str:
        return subprocess.run([self.tick, *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, check=True).stdout


def reconcile(ledger: Ledger, modules: list[str] | None) -> str:
    """Bring the repo's mutation-stale tick in line with ``modules``; say what was done."""
    open_ids = [m.group(1) for line in ledger("ls", "--tag", TAG).splitlines()
                if (m := TICK_ID.match(line))]
    stale = modules is None or bool(modules)
    keep = open_ids[0] if open_ids and stale else None
    closed = [i for i in open_ids if i != keep]
    for tick_id in closed:
        ledger("off", tick_id)
    if not stale:
        return f"fresh; ticked off {', '.join(closed)}" if closed else "fresh"
    text = note(modules)
    if keep is None:
        tick_id = ledger("add", "--kind", "debt", "--tag", TAG, title(modules)).split()[0]
        ledger("note", tick_id, text)
        return f"stale; filed {tick_id}"
    noted = STALE_SET.findall(ledger("show", keep))
    if noted and noted[-1] == digest([NEVER] if modules is None else modules):
        return f"stale; unchanged {keep}"
    ledger("note", keep, text)
    return f"stale; noted {keep}"


def sweep_one(repo: str, tick: str, since: str, git: str) -> str:
    target = checkout.target_root(Path(repo), git)
    scope.check_since(target, since, git)
    checkout.fetch(target, git)
    return reconcile(Ledger(tick, target), stale_modules(target, since, git))


def sweep_main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=prog, description="File, update or close each enrolled repo's mutation-stale tick.")
    parser.add_argument("--tick", default="tick")
    parser.add_argument("--since", default="7 days ago",
                        help="before any base exists, count changes since this git date")
    parser.add_argument("--git", default="git")
    args = parser.parse_args(argv)
    repos = enrolled()
    if not repos:
        print(f"No repo is enrolled; add one with 'bakobo-mutate enroll' ({repos_path()}).")
        return 0
    failed = 0
    for repo in repos:
        try:
            print(f"{repo}: {sweep_one(repo, args.tick, args.since, args.git)}")
        except BakoboError as error:
            failed += 1
            refuse(error, f"{repo}: ")
        except subprocess.CalledProcessError as error:
            failed += 1
            output = (error.stderr or error.stdout or "").strip() or "(it printed nothing)"
            print(f"{repo}: tick {error.cmd[1]} exited {error.returncode}: {output}",
                  file=sys.stderr)
        except OSError as error:
            failed += 1
            print(f"{repo}: tick could not be run: {error}", file=sys.stderr)
    if failed:
        print(f"{failed} of {len(repos)} repos failed; the others were swept.", file=sys.stderr)
        return 1
    return 0
