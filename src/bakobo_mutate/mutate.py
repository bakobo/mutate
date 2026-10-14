"""Mutation-test the source modules the default branch changed recently, and report survivors.

Branch coverage says a line ran under some test; a surviving mutant says no test would notice
that line being wrong.

    bakobo-mutate run [--since "7 days ago"] [--ref HEAD]
        [--module src/<pkg>/x.py ...] [--budget-minutes 300] [--out mutation-out]

Which modules: every ``.py`` file under ``[tool.mutmut].source_paths`` that a first-parent
commit on ``--ref`` touched since ``--since``, minus anything ``[tool.mutmut].do_not_mutate``
excludes. ``--module`` names the files explicitly instead, for a local run. ``mutmut`` itself
reads the rest of its configuration (the pytest arguments) from ``pyproject.toml``.

Writes ``<out>/mutation-report.json`` (one record per surviving mutant) and
``<out>/summary.md``. Exits 0 whether or not any mutant survived, including when nothing
changed or the time budget ran out; exits 2 only when the run itself could not be carried out —
mutmut crashed, or the unmutated suite failed — because a harness failure that reported success
would look exactly like a clean run.

Each record's ``class`` is ``string-only`` when the mutation changes nothing but the text inside
string literals that are message text, which is counted rather than ticked,
and ``behavioural`` otherwise. A literal is message text only when ``message_sink`` traces it,
in the source, into an exception constructor in a ``raise``, a ``print``, a logging call,
``warnings.warn``, or a sink the repo declares as ``[tool.mutation] message_sinks`` in
pyproject.toml (``Name`` or ``Name.kwarg``, see ``shared.message_sinks``); the record carries
that sink as ``message_sink``, null when there is none. A malformed declaration fails the run
with ``SINKS_MALFORMED`` before mutmut starts.
``classify`` decides from the diff and that sink, and leans behavioural whenever it cannot vouch
for the change.

A record's ``id`` is a digest of the file, the function and the mutated text, with no line
number or mutmut ordinal in it, so the same surviving mutation keeps its id across runs while
the code around it moves. Two identical mutations in one function are told apart by which
occurrence of the mutated line they sit on, counted in the source, so an id never depends on
which of them happened to survive. ``bakobo-mutate ticks`` dedupes on it.

A failed run carries one of the two codes below, ``failure_code``, whether retrying can help
(``failure_retryable``), and the code's ``failure_hint``, in the report and its summary.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import fnmatch
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from bakobo.errors import BakoboError, ErrorCode  # type: ignore[import-untyped]

from .shared import (
    BEHAVIOURAL,
    SCHEMA,
    STRING_ONLY,
    SinkConfigError,
    classify,
    message_sink,
    message_sinks,
    read_pyproject,
)

PASSED = "passed: every test covering this function passed with the mutant in place"
STRING_NOTE = "only the text of a message literal changed; counted, not ticked"

RUN_FAILED = ErrorCode(
    code="e.env.mutmut-run.f",
    title="mutmut could not finish the mutation run.",
    detail=(
        "mutmut exited {exit_code} before its mutants could be judged. The last lines of its "
        "output, all of which are in mutmut-run.log, were:\n{tail}"
    ),
    args=("exit_code", "tail"),
    hint=(
        "Run the suite on the same commit with the [tool.mutmut] pytest arguments: mutmut "
        "needs it to pass unmutated, and running the workflow again on this commit will fail "
        "the same way."
    ),
)
RESULTS_UNREADABLE = ErrorCode(
    code="e.env.mutmut-results.r",
    title="mutmut finished, but its results could not be read back.",
    detail="mutmut {command} exited {exit_code} while its results were being read: {stderr}",
    args=("command", "exit_code", "stderr"),
    hint=(
        "Run the workflow again. If it fails the same way, run that mutmut command in a local "
        "checkout of the commit, where its full error is visible."
    ),
)
SINKS_MALFORMED = ErrorCode(
    code="e.self.config.message-sinks.f",
    title="This repo's declared message sinks cannot be read, so nothing was classified.",
    detail="{problem}.",
    args=("problem",),
    hint=(
        "Fix [tool.mutation] message_sinks in pyproject.toml: each entry is a callable's name, "
        "Name, or that name and one keyword argument, Name.kwarg. Running again before then "
        "will fail the same way."
    ),
)
# mutmut mangles a method name with U+01C1 (LATIN LETTER LATERAL CLICK) as its separator:
# x<sep>Class<sep>method. Spelled as an escape so no reader mistakes it for two pipes.
METHOD_SEP = "\u01c1"
NOTHING_MATCHES = "Filtered for specific mutants, but nothing matches"
CLEAN_TEST_FAILED = "Failed to run clean test"
# Seconds mutmut gets to stop its workers after SIGTERM before the group is killed outright.
TERM_GRACE = 30



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
    failure: BakoboError | None = None
    note: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    survivors: list[Survivor] = field(default_factory=list)


def load_config(root: Path) -> dict:
    data = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    return data.get("tool", {}).get("mutmut", {})


def declared_sinks(root: Path) -> tuple[str, ...]:
    """The repo's [tool.mutation] message_sinks, or the SINKS_MALFORMED error saying why not."""
    try:
        return message_sinks(read_pyproject(root / "pyproject.toml"))
    except (OSError, ValueError, SinkConfigError) as error:  # ValueError: TOML or UTF-8
        raise SINKS_MALFORMED(problem=str(error)) from None


def changed_modules(root: Path, ref: str, since: str, cfg: dict, git: str = "git") -> list[str]:
    """Source files a first-parent commit on ``ref`` touched since ``since`` that still exist.

    ``--diff-merges=first-parent`` makes a merge commit list what the merge brought onto the
    branch, so a pull request whose own commits are older than the window still counts on the
    day it landed.
    """
    sources = cfg.get("source_paths", ["src"])
    out = subprocess.run(
        [git, "log", "--first-parent", "--diff-merges=first-parent", f"--since={since}",
         "--name-only", "--format=", ref, "--", *sources],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    paths = {line.strip() for line in out.splitlines() if line.strip()}
    return sorted(p for p in paths if p.endswith(".py") and (root / p).is_file()
                  and not excluded(p, cfg))


def excluded(path: str, cfg: dict) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in cfg.get("do_not_mutate", []))


def module_name(path: str) -> str:
    """The dotted name mutmut gives a source file (mutmut's format_utils.get_mutant_name)."""
    name = path.removesuffix(".py").replace("/", ".").removeprefix("src.")
    return name.removesuffix(".__init__")


def globs_for(path: str) -> list[str]:
    """Patterns selecting this module's mutants and no submodule's.

    Every mutant name is ``<module>.x_<function>`` or ``<module>.x<sep><Class><sep><method>``,
    so a pattern ending in ``.x_*`` cannot match ``<module>.<submodule>.x_*`` (fnmatch's ``*``
    crosses dots, which is why the bare ``<module>.*`` is not used).
    """
    mod = module_name(path)
    return [f"{mod}.x_*", f"{mod}.x{METHOD_SEP}*"]


def has_functions(source: str) -> bool:
    """Whether mutmut could mutate anything here: it mutates functions and methods only."""
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.parse(source).body:
        if isinstance(node, funcs):
            return True
        if isinstance(node, ast.ClassDef) and any(isinstance(n, funcs) for n in node.body):
            return True
    return False


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


def run_mutmut(mutmut: str, globs: list[str], budget: float, log: Path,
               root: Path) -> tuple[int | None, str]:
    """Run mutmut in its own process group; on budget exhaustion stop the whole group.

    Returns (exit code, or None if the budget ran out; combined output).
    """
    with log.open("w") as sink:
        proc = subprocess.Popen([mutmut, "run", *globs], cwd=root, stdout=sink,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code: int | None = proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=TERM_GRACE)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            code = None
    return code, log.read_text(errors="replace")


def statuses(mutmut: str, root: Path) -> dict[str, str]:
    out = subprocess.run([mutmut, "results", "--all", "true"], cwd=root, capture_output=True,
                         text=True, check=True).stdout
    result = {}
    for line in out.splitlines():
        name, sep, status = line.strip().rpartition(": ")
        if sep and "__mutmut_" in name:
            result[name] = status
    return result


def nothing_to_run(mutmut: str, root: Path, code: int, output: str, globs: list[str]) -> bool:
    """Whether a non-zero exit means only that the changed modules have no mutants.

    mutmut says so by failing an assertion (exit 1) whose text is NOTHING_MATCHES, but that
    text proves nothing on its own: it can sit in the same output as a failed clean-test run.
    So the claim is accepted only with positive evidence from mutmut's own record, that it
    holds no mutant at all matching the changed modules. Anything else is a failed run.
    """
    if code != 1 or NOTHING_MATCHES not in output or CLEAN_TEST_FAILED in output:
        return False
    try:
        names = statuses(mutmut, root)
    except subprocess.CalledProcessError:
        return False
    return not any(fnmatch.fnmatch(name, g) for name in names for g in globs)


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
        kind=classify(removed, added, sink), sink=sink,
    )


def mutate(root: Path, modules: list[str], mutmut: str, budget: float, out: Path,
           sinks: tuple[str, ...] = ()) -> Outcome:
    outcome = Outcome(modules=modules)
    if not modules:
        outcome.note = "No source module changed in the window, so there was nothing to mutate."
        return outcome
    mutable = [m for m in modules if has_functions((root / m).read_text("utf-8"))]
    if not mutable:
        outcome.note = "The changed modules define no functions or methods, which is all " \
                       "mutmut mutates."
        return outcome
    globs = [g for m in mutable for g in globs_for(m)]
    code, output = run_mutmut(mutmut, globs, budget, out / "mutmut-run.log", root)
    if code is None:
        outcome.complete = False
        outcome.note = f"The {budget / 60:.0f}-minute budget ran out; mutants still marked " \
                       "'not checked' were never run."
    elif code != 0:
        if nothing_to_run(mutmut, root, code, output, globs):
            outcome.note = "mutmut generated no mutants for the changed modules."
            return outcome
        outcome.failure = RUN_FAILED(exit_code=code, tail="\n".join(output.splitlines()[-15:]))
        return outcome
    owner = {g: m for m in mutable for g in globs_for(m)}
    try:
        for name, status in sorted(statuses(mutmut, root).items()):
            path = next((owner[g] for g in owner if fnmatch.fnmatch(name, g)), None)
            if path is None:
                continue
            outcome.counts[status] = outcome.counts.get(status, 0) + 1
            if status == "survived":
                outcome.survivors.append(survivor(mutmut, root, name, path, sinks))
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
                    for kind in (BEHAVIOURAL, STRING_ONLY)},
        "survivors": [s.record(cfg.get("pytest_add_cli_args", [])) for s in outcome.survivors],
    }


def cell(text: str) -> str:
    text = text.strip().replace("|", "\\|").replace("`", "'")
    return text if len(text) <= 80 else text[:77] + "..."


def summary(data: dict) -> str:
    scope = (f"Commit `{data['commit'][:12]}`; modules changed since {data['since']}: "
             f"{len(data['modules'])}.")
    lines = ["# Mutation testing (pilot)", "", scope, ""]
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
        split = (f"{classes[BEHAVIOURAL]} behavioural, {classes[STRING_ONLY]} string-only "
                 f"({STRING_NOTE}).")
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


def find_mutmut() -> str:
    beside = Path(sys.executable).parent / "mutmut"
    return str(beside) if beside.exists() else (shutil.which("mutmut") or "mutmut")


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--since", default="7 days ago")
    parser.add_argument("--module", action="append", default=[],
                        help="mutate this file instead of the changed ones (repeatable)")
    parser.add_argument("--budget-minutes", type=float, default=300)
    parser.add_argument("--out", type=Path, default=Path("mutation-out"))
    parser.add_argument("--mutmut", default=None, help="the mutmut executable")
    parser.add_argument("--git", default="git")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    out = args.out if args.out.is_absolute() else root / args.out
    out.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run([args.git, "rev-parse", args.ref], cwd=root, capture_output=True,
                            text=True, check=True).stdout.strip()
    try:  # first, since a pyproject.toml that cannot declare sinks cannot be read for mutmut
        sinks = declared_sinks(root)
    except BakoboError as error:
        cfg: dict = {}
        outcome = Outcome(modules=sorted(args.module), failure=error)
    else:
        cfg = load_config(root)
        modules = sorted(args.module) or changed_modules(root, args.ref, args.since, cfg,
                                                         args.git)
        outcome = mutate(root, modules, args.mutmut or find_mutmut(), args.budget_minutes * 60,
                         out, sinks)
    since = "an explicit module list" if args.module else args.since
    data = report(outcome, cfg, commit, since)
    (out / "mutation-report.json").write_text(json.dumps(data, indent=2) + "\n")
    text = summary(data)
    (out / "summary.md").write_text(text)
    print(text)
    return 2 if outcome.failure else 0


if __name__ == "__main__":
    sys.exit(main())
