"""Mutation-test what has landed on a repo's default branch, re-testing only what changed.

Branch coverage says a line ran under some test; a surviving mutant says no test would notice
that line being wrong.

    bakobo-mutate run [TARGET] [--module src/<pkg>/x.py ...] [--since "7 days ago"]
        [--all-survivors | --no-survivors] [--chunk 400]

TARGET is the repo, by default the one around the current directory. A run takes the
machine-wide lock (``lock``), refreshes the target's persistent checkout at
``.worktrees/mutation`` to ``origin/<default>`` and syncs its environment (``checkout``), and
chooses its modules (``scope``): those named with ``--module``, else those stale since the last
run, else, before any run, those a first-parent commit touched since ``--since``. It then runs
the checkout's own mutmut once to generate mutants, which keeps every cached verdict whose
function did not change, and once more per chunk of names for exactly the mutants that need
running (``meta``): the ones in scope with no verdict yet and, unless ``--no-survivors``, every
survivor and "no tests" verdict anywhere, since re-running them is the only way a newly added
test is noticed.

It writes ``report.json`` (one record per surviving mutant), ``summary.md``, ``mutmut-run.log``
and ``stamp.json`` to ``<TARGET>/.ignored/mutation/``; a run that fails leaves the previous
``stamp.json`` as it was, while an interrupted one still writes it. It exits 0 whether or not
any mutant survived; 130 when interrupted, after reporting what finished, since mutmut saves
each verdict as it completes and the next run starts where this one stopped; and 2 when the run
could not be carried out or was refused, because a harness failure that reported success would
look exactly like a clean run.

Each record's ``class`` is ``string-only`` when the mutation changes nothing but the text inside
string literals that are message text; ``diagnostic`` when it lies wholly inside the arguments
of the exception a ``raise`` constructs, or that raise's ``from`` cause, and is inert there
(``shared.diagnostic``), so the refusal stands and only what it says changes; and
``behavioural`` otherwise. The first two are counted rather than ticked. A literal is message
text only when ``message_sink`` traces it, in the source, into an exception constructor in a
``raise``, a ``print``, a logging call, ``warnings.warn``, or a sink the repo declares as
``[tool.mutation] message_sinks`` in pyproject.toml (see ``shared.message_sinks``); the record
carries that sink as ``message_sink``, null when there is none. A malformed declaration fails
the run with ``SINKS_MALFORMED`` before mutmut starts. ``classify`` decides from the diff and
that sink, and leans behavioural whenever it cannot vouch for the change.

A record's ``id`` is a digest of the file, the function and the mutated text, with no line
number or mutmut ordinal in it, so the same surviving mutation keeps its id across runs while
the code around it moves. Two identical mutations in one function are told apart by which
occurrence of the mutated line they sit on, counted in the source, so an id never depends on
which of them happened to survive. ``bakobo-mutate ticks`` dedupes on it.

A failed run carries its code as ``failure_code``, whether retrying can help
(``failure_retryable``), and the code's ``failure_hint``, in the report and its summary.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime
import hashlib
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bakobo.errors import BakoboError, ErrorCode  # type: ignore[import-untyped]

from . import checkout, lock, meta, scope
from .shared import (
    BEHAVIOURAL,
    DIAGNOSTIC,
    SCHEMA,
    STRING_ONLY,
    SinkConfigError,
    message_sink,
    message_sinks,
    read_pyproject,
    survivor_class,
)

PASSED = "passed: every test covering this function passed with the mutant in place"
STRING_NOTE = "only the text of a message literal changed; counted, not ticked"
DIAGNOSTIC_NOTE = ("only what a raised exception is given changed, not whether it is raised; "
                   "counted, not ticked")
NOTHING_NEEDED = ("Nothing needed running: every mutant in scope already has a verdict, and "
                  "no survivor is to be re-checked.")
INTERRUPTED_NOTE = ("The run was interrupted. Mutants still marked 'not checked' were never "
                    "run; the next run starts with them, and every verdict saved before the "
                    "interrupt is kept.")
INTERRUPTED = 130  # the shell's exit status for a process stopped by SIGINT

RUN_FAILED = ErrorCode(
    code="e.env.mutmut-run.f",
    title="mutmut could not finish the mutation run.",
    detail=(
        "mutmut exited {exit_code} before its mutants could be judged. The last lines of its "
        "output, all of which are in mutmut-run.log, were:\n{tail}"
    ),
    args=("exit_code", "tail"),
    hint=(
        "Run the target's suite in .worktrees/mutation with the [tool.mutmut] pytest "
        "arguments: mutmut needs it to pass unmutated, and running bakobo-mutate run again on "
        "this commit will fail the same way."
    ),
)
RESULTS_UNREADABLE = ErrorCode(
    code="e.env.mutmut-results.r",
    title="mutmut finished, but its results could not be read back.",
    detail="mutmut {command} exited {exit_code} while its results were being read: {stderr}",
    args=("command", "exit_code", "stderr"),
    hint=(
        "Run bakobo-mutate run again. If it fails the same way, run that mutmut command in "
        ".worktrees/mutation, where its full error is visible."
    ),
)
SINKS_MALFORMED = ErrorCode(
    code="e.self.config.message-sinks.f",
    title="This repo's declared message sinks cannot be read, so nothing was classified.",
    detail="{problem}.",
    args=("problem",),
    hint=(
        "Fix [tool.mutation] message_sinks in pyproject.toml: each entry is a callable's name, "
        "Name; that name and one keyword argument, Name.kwarg; or a dotted callee followed by "
        "(), such as console.out.write(). Running again before then will fail the same way."
    ),
)
# mutmut mangles a method name with U+01C1 (LATIN LETTER LATERAL CLICK) as its separator:
# x<sep>Class<sep>method. Spelled as an escape so no reader mistakes it for two pipes.
METHOD_SEP = "ǁ"
NOTHING_MATCHES = "Filtered for specific mutants, but nothing matches"
CLEAN_TEST_FAILED = "Failed to run clean test"
# A name no mutant can have: mutmut generates every mutant, then refuses to run it, which is
# how mutants are generated without calling mutmut's private API.
GENERATE_ONLY = "__bakobo_mutate_generate_only__"
# Seconds mutmut gets to stop its workers after SIGTERM before the group is killed outright.
TERM_GRACE = 30
TAIL = 15


@dataclass
class Survivor:
    id: str
    mutmut_name: str
    file: str
    line: int
    function: str
    removed: list[str]
    added: list[str]
    diff: str
    tests: int
    kind: str = BEHAVIOURAL
    sink: str | None = None

    def record(self, pytest_args: list[str]) -> dict:
        return {
            "id": self.id,
            "mutmut_name": self.mutmut_name,
            "file": self.file,
            "line": self.line,
            "function": self.function,
            "class": self.kind,
            "message_sink": self.sink,
            "mutation": {"removed": self.removed, "added": self.added, "diff": self.diff},
            "test_result": {
                "command": " ".join(["pytest", *pytest_args]),
                "tests_run": self.tests,
                "exit_code": 0,
                "outcome": PASSED,
            },
        }


@dataclass
class Outcome:
    modules: list[str]
    complete: bool = True
    interrupted: bool = False
    generated: bool = False
    failure: BakoboError | None = None
    note: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    survivors: list[Survivor] = field(default_factory=list)
    verdicts: meta.Verdicts | None = None


def declared_sinks(root: Path) -> tuple[str, ...]:
    """The repo's [tool.mutation] message_sinks, or the SINKS_MALFORMED error saying why not."""
    try:
        return message_sinks(read_pyproject(root / "pyproject.toml"))
    except (OSError, ValueError, SinkConfigError) as error:  # ValueError: TOML or UTF-8
        raise SINKS_MALFORMED(problem=str(error)) from None


def module_name(path: str) -> str:
    """The dotted name mutmut gives a source file (mutmut's format_utils.get_mutant_name)."""
    name = path.removesuffix(".py").replace("/", ".").removeprefix("src.")
    return name.removesuffix(".__init__")


def function_of(mutmut_name: str, path: str) -> str:
    """``saidify`` or ``Class.method`` from a mutant name."""
    mangled = mutmut_name.partition("__mutmut_")[0]
    local = mangled.removeprefix(module_name(path) + ".")
    if local.startswith("x" + METHOD_SEP):
        return ".".join(part for part in local.split(METHOD_SEP)[1:] if part)
    return local.removeprefix("x_")


def parse_diff(diff: str) -> tuple[int, list[str], list[str]]:
    """(function-relative line of the first change, removed lines, added lines)."""
    removed: list[str] = []
    added: list[str] = []
    first = 0
    line = 0
    for text in diff.splitlines():
        if text.startswith(("---", "+++")):
            continue
        if text.startswith("@@"):
            line = int(text.split()[1].lstrip("-").split(",")[0])
            continue
        if text.startswith("-"):
            removed.append(text[1:])
            first = first or line
            line += 1
        elif text.startswith("+"):
            added.append(text[1:])
            first = first or line
        else:
            line += 1
    return first, removed, added


def function_span(source: str, function: str) -> tuple[int, int] | None:
    """First and last line of ``function`` (``name`` or ``Class.name``), decorators included."""
    tree = ast.parse(source)
    *owner, name = function.split(".")
    body = tree.body
    if owner:
        classes = [n for n in body if isinstance(n, ast.ClassDef) and n.name == owner[0]]
        body = classes[0].body if classes else []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            return start, node.end_lineno or node.lineno
    return None


def locate(source: str, function: str, relative: int, removed: list[str]) -> int:
    """The file line a mutation sits on.

    mutmut's diff numbers lines from the function's own first line, which may be a leading
    comment rather than the ``def``, so the removed text is looked for inside the function and
    the occurrence nearest the diff's own offset wins. A mutation that removes nothing falls
    back to that offset.
    """
    span = function_span(source, function)
    if span is None:
        return 0
    start, end = span
    guess = start + max(relative, 1) - 1
    if removed:
        lines = source.splitlines()
        target = removed[0].strip()
        hits = [n for n in range(start, end + 1) if lines[n - 1].strip() == target]
        if hits:
            return min(hits, key=lambda n: abs(n - guess))
    return min(guess, end)


def occurrence(source: str, function: str, line: int, removed: list[str]) -> int:
    """Which occurrence, within its function, of the line it removes a mutation sits on.

    Counted in the source, so it is the same whether or not any identical twin survived. A
    mutation that removes nothing is placed by its offset from the function's first line.
    """
    span = function_span(source, function)
    if span is None:
        return 0
    start, _ = span
    if not removed:
        return line - start
    lines = source.splitlines()
    target = removed[0].strip()
    return sum(1 for n in range(start, line) if lines[n - 1].strip() == target)


def mutant_id(path: str, function: str, removed: list[str], added: list[str],
              ordinal: int) -> str:
    """Stable across runs: no line number and no mutmut ordinal goes into it.

    Two identical mutations in one function (the same line written twice) are told apart by
    ``ordinal``, the occurrence of the mutated line in the source (see ``occurrence``).
    """
    key = "\0".join([path, function, "\n".join(s.strip() for s in removed),
                     "\n".join(s.strip() for s in added)])
    if ordinal:
        key += f"\0{ordinal}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _stop(proc: subprocess.Popen) -> None:
    """Stop mutmut's whole process group: SIGTERM, then SIGKILL after TERM_GRACE seconds."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=TERM_GRACE)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def run_mutmut(mutmut: str, names: list[str], log: Path, cwd: Path) -> tuple[int | None, str]:
    """``mutmut run <names>`` in its own process group, its output appended to ``log``.

    Returns (exit code, or None if it was interrupted; this call's output). An interrupt,
    Ctrl-C or the SIGTERM ``main`` turns into one, stops the whole group; every verdict mutmut
    saved before then stays saved.
    """
    with log.open("a") as sink:
        start = sink.tell()
        proc = subprocess.Popen([mutmut, "run", *names], cwd=cwd, stdout=sink,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                env=checkout.clean_env())
        try:
            code: int | None = proc.wait()
        except KeyboardInterrupt:
            _stop(proc)
            code = None
    with log.open(errors="replace") as text:
        text.seek(start)
        return code, text.read()


def _failed(code: int, output: str) -> BakoboError:
    return RUN_FAILED(exit_code=code, tail="\n".join(output.splitlines()[-TAIL:]))


def survivor(mutmut: str, root: Path, name: str, path: str,
             sinks: tuple[str, ...] = ()) -> Survivor:
    diff = subprocess.run([mutmut, "show", name], cwd=root, capture_output=True, text=True,
                          check=True).stdout
    diff = "\n".join(line for line in diff.splitlines() if not line.startswith("# "))
    tests = subprocess.run([mutmut, "tests-for-mutant", name], cwd=root, capture_output=True,
                           text=True, check=True).stdout
    relative, removed, added = parse_diff(diff)
    function = function_of(name, path)
    source = (root / path).read_text("utf-8")
    line = locate(source, function, relative, removed)
    sink = message_sink(source, line, line + max(len(removed), 1) - 1, sinks)
    return Survivor(
        id=mutant_id(path, function, removed, added,
                     occurrence(source, function, line, removed)),
        mutmut_name=name, file=path, line=line,
        function=function, removed=removed, added=added, diff=diff,
        tests=len([t for t in tests.splitlines() if t.strip()]),
        kind=survivor_class(source, line, removed, added, sink), sink=sink,
    )


@dataclass
class Plan:
    modules: list[str]
    survivors: bool = True
    chunk: int = 400
    sinks: tuple[str, ...] = ()


def _interrupted(outcome: Outcome) -> None:
    outcome.complete = False
    outcome.interrupted, outcome.note = True, INTERRUPTED_NOTE


def _run_needed(wt: Path, plan: Plan, mutmut: str, log: Path, verdicts: meta.Verdicts,
                outcome: Outcome) -> None:
    """Run the needed mutants chunk by chunk; an interrupt anywhere here stops the run."""
    try:
        names = meta.names(verdicts, meta.needed(verdicts, set(plan.modules), plan.survivors))
        if not names:
            outcome.note = NOTHING_NEEDED
        for chunk in meta.chunks(names, plan.chunk):
            code, output = run_mutmut(mutmut, chunk, log, wt)
            if code is None:
                _interrupted(outcome)
                return
            if code != 0:
                outcome.failure = _failed(code, output)
                return
    except KeyboardInterrupt:
        _interrupted(outcome)


def mutate(wt: Path, plan: Plan, mutmut: str, log: Path) -> Outcome:
    """Generate, run what is needed, and read back every verdict; never raises a BakoboError."""
    outcome = Outcome(modules=plan.modules)
    code, output = run_mutmut(mutmut, [GENERATE_ONLY], log, wt)
    if code is None:
        _interrupted(outcome)
    elif code != 1 or NOTHING_MATCHES not in output or CLEAN_TEST_FAILED in output:
        outcome.failure = _failed(code, output)
        return outcome
    else:
        outcome.generated = True
    try:
        verdicts = meta.read_meta(wt)
        if not outcome.interrupted:
            _run_needed(wt, plan, mutmut, log, verdicts, outcome)
        verdicts = meta.read_meta(wt)
    except BakoboError as error:
        outcome.failure = error
        return outcome
    outcome.verdicts = verdicts
    outcome.counts = meta.counts(verdicts, plan.modules)
    if not all(meta.complete(verdicts, plan.modules).values()):
        outcome.complete = False
    try:
        for name, path in meta.survivors(verdicts):
            outcome.survivors.append(survivor(mutmut, wt, name, path, plan.sinks))
    except subprocess.CalledProcessError as error:
        outcome.counts, outcome.survivors = {}, []
        outcome.failure = RESULTS_UNREADABLE(
            command=error.cmd[1], exit_code=error.returncode,
            stderr=(error.stderr or "").strip() or "(it printed nothing)")
    return outcome


def report(outcome: Outcome, cfg: dict, commit: str, since: str) -> dict:
    failure = outcome.failure
    return {
        "schema": SCHEMA,
        "commit": commit,
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "since": since,
        "modules": outcome.modules,
        "complete": outcome.complete,
        "failure": failure.detail if failure else None,
        "failure_code": failure.code if failure else None,
        "failure_retryable": failure.retryable if failure else None,
        "failure_hint": failure.hint if failure else None,
        "note": outcome.note,
        "counts": outcome.counts,
        "classes": {kind: sum(s.kind == kind for s in outcome.survivors)
                    for kind in (BEHAVIOURAL, STRING_ONLY, DIAGNOSTIC)},
        "survivors": [s.record(cfg.get("pytest_add_cli_args", [])) for s in outcome.survivors],
    }


def cell(text: str) -> str:
    text = text.strip().replace("|", "\\|").replace("`", "'")
    return text if len(text) <= 80 else text[:77] + "..."


def summary(data: dict) -> str:
    scope_line = f"Commit `{data['commit'][:12]}`; {data['since']}: {len(data['modules'])}."
    lines = ["# Mutation testing", "", scope_line, ""]
    if data["failure"]:
        again = "may help" if data["failure_retryable"] else "will not help"
        lines += [f"**The run failed (`{data['failure_code']}`), so nothing below is a result."
                  f"** Running it again {again}.", "", "```", data["failure"], "```", "",
                  f"Hint: {data['failure_hint']}", ""]
    if data["note"]:
        lines += [data["note"], ""]
    if data["counts"]:
        lines += ["| status | mutants |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in sorted(data["counts"].items())]
        lines.append("")
    survivors = data["survivors"]
    if survivors:
        classes = data["classes"]
        split = (f"{classes[BEHAVIOURAL]} behavioural, {classes[STRING_ONLY]} string-only, "
                 f"{classes[DIAGNOSTIC]} diagnostic. String-only: {STRING_NOTE}. Diagnostic: "
                 f"{DIAGNOSTIC_NOTE}.")
        lines += [f"## {len(survivors)} surviving mutants", "", split, "",
                  "| id | class | where | function | mutation |", "|---|---|---|---|---|"]
        for s in survivors:
            change = " / ".join(cell(x) for x in s["mutation"]["removed"]) or "(nothing)"
            change += " &rarr; " + (" / ".join(cell(x) for x in s["mutation"]["added"])
                                    or "(removed)")
            lines.append(f"| `{s['id']}` | {s['class']} | `{s['file']}:{s['line']}` | "
                         f"`{s['function']}` | {change} |")
        lines.append("")
    elif not data["failure"]:
        lines += ["No mutant survived.", ""]
    if data["modules"]:
        lines += ["<details><summary>Modules</summary>", ""]
        lines += [f"- `{m}`" for m in data["modules"]]
        lines += ["", "</details>", ""]
    return "\n".join(lines)


def positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text} is not a positive integer")
    return value


def parse(argv: list[str] | None, prog: str | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n\n")[0])
    parser.add_argument("target", nargs="?", type=Path, default=None,
                        help="the repo to mutate (default: the one around the current "
                             "directory)")
    parser.add_argument("--module", action="append", default=[],
                        help="mutate this file instead of the stale ones (repeatable)")
    parser.add_argument("--since", default="7 days ago",
                        help="before any run, mutate modules changed since this git date")
    survivors = parser.add_mutually_exclusive_group()
    survivors.add_argument("--all-survivors", dest="survivors", action="store_true",
                           default=True,
                           help="re-check every survivor and every 'no tests' verdict "
                                "anywhere (default)")
    survivors.add_argument("--no-survivors", dest="survivors", action="store_false",
                           help="re-check neither; run only what in scope has no verdict yet")
    parser.add_argument("--chunk", type=positive, default=400,
                        help="the most mutant names passed to one mutmut run")
    parser.add_argument("--mutmut", default=None,
                        help="the mutmut executable (default: the checkout's .venv/bin/mutmut)")
    parser.add_argument("--git", default="git")
    parser.add_argument("--uv", default="uv")
    return parser.parse_args(argv)


def write(out: Path, outcome: Outcome, cfg: dict, commit: str, since: str) -> None:
    data = report(outcome, cfg, commit, since)
    (out / "report.json").write_text(json.dumps(data, indent=2) + "\n")
    text = summary(data)
    (out / "summary.md").write_text(text)
    print(text)


def execute(target: Path, args: argparse.Namespace) -> int:
    """One run against ``target``, under the lock; returns the exit status."""
    wt, branch = checkout.ensure_checkout(target, args.git)
    checkout.sync(wt, args.uv)
    checkout.prune(wt)
    mutmut = checkout.find_mutmut(wt, args.mutmut)
    ref = f"origin/{branch}"
    commit = subprocess.run([args.git, "-C", str(target), "rev-parse", ref],
                            capture_output=True, text=True, check=True).stdout.strip()
    stamp = scope.read_stamp(target)
    out = target / scope.REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    log = out / "mutmut-run.log"
    log.write_text("")
    try:  # first, since a pyproject.toml that cannot declare sinks cannot be read for mutmut
        sinks = declared_sinks(wt)
    except BakoboError as error:
        write(out, Outcome(modules=[], failure=error), {}, commit,
              "modules chosen before the run was refused")
        return 2
    cfg = scope.config_at(target, ref, args.git)
    modules, since = scope.run_scope(target, ref, stamp, args.module, args.since, cfg,
                                     args.git)
    plan = Plan(modules=modules, survivors=args.survivors, chunk=args.chunk, sinks=sinks)
    outcome = mutate(wt, plan, mutmut, log)
    if outcome.verdicts is not None and outcome.failure is None:
        # A failed run is no measurement, so the last stamp stands; an interrupted one is, as
        # far as it got. A module with no mutants on record is complete only if generation
        # finished.
        done = {m: ok and outcome.generated
                for m, ok in meta.complete(outcome.verdicts, modules).items()}
        scope.write_stamp(target, scope.updated(stamp, done, commit,
                                                advance_base=not args.module))
    write(out, outcome, cfg, commit, since)
    if outcome.failure:
        return 2
    return INTERRUPTED if outcome.interrupted else 0


def _terminate(signum, frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    args = parse(argv, prog)
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        target = checkout.target_root(args.target or Path.cwd(), args.git)
        scope.check_since(target, args.since, args.git)
        with lock.machine_lock(target):
            return execute(target, args)
    except BakoboError as error:
        print(f"{error.code}: {error.detail}\nHint: {error.hint}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("bakobo-mutate run was interrupted outside mutmut, so nothing was reported.",
              file=sys.stderr)
        return INTERRUPTED
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
