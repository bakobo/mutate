"""File one ``tick`` per surviving mutant in a target's mutation report.

The mutation tool never runs this on its own; a person runs it, so survivors are triaged before
they become anyone's work.

    bakobo-mutate ticks [TARGET]      # <TARGET>/.ignored/mutation/report.json
    bakobo-mutate ticks --report F    # a report somewhere else
    ... --dry-run                     # say what would be filed, file nothing

TARGET is the repo the report is about, by default the one around the current directory; its
pyproject.toml declares its message sinks and its git holds the source the report names.
Every ``tick`` runs in TARGET, so the ticks land in its ledger wherever this is run from.

Each survivor becomes ``tick add --kind debt --tag mutation-survivor "<title>"`` followed by
``tick note <id> <detail>``. The title carries the survivor's stable id as ``[m:<id>]``, and the
note opens with ``mutant-id: <id>``. A survivor whose id already appears in a
``mutation-survivor`` tick, open or closed, is not filed again, so a survivor someone dismissed
stays dismissed; but if that tick's body lacks the note's ``mutant-id`` line, the note never
landed (``tick note`` failed after ``tick add`` succeeded), and it is added now.

A survivor is counted in the closing summary and not ticked when its mutation only changes the
text of a message literal (string-only) or lies wholly inside the arguments of the exception a
``raise`` constructs (diagnostic). That is decided by ``shared.survivor_class`` from the
record's own diff, its ``message_sink`` and the source file at the report's commit, read with
``git show`` in TARGET, never from the stored ``class``; and it files whenever it cannot tell:
a record without a sink is not string-only, and one whose source git cannot produce is not
diagnostic. A sink the repo declares (``[tool.mutation] message_sinks``) counts only while the
repo still declares it: the record's ``declared <entry>`` must name an entry there, so the
report and the repo agree, and a malformed declaration is refused with ``SINKS_MALFORMED``
before the ledger is touched.

A report is read at most 16 MiB (``shared.MAX_REPORT_BYTES``) and refused unread beyond that.
One that does not exist is ``NO_REPORT``. One that cannot be read, is not UTF-8 JSON, carries
any schema but ``shared.SCHEMA``, or is not the shape ``bakobo-mutate run`` writes is refused
whole with ``MALFORMED``, naming the record it tripped on, before the ledger is touched. Every
refusal below is printed as ``<code>: <detail>`` with the code's hint. No tick mark is placed
in the code, since a survivor is a question about the tests and the line it names moves.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from bakobo.errors import BakoboError, ErrorCode  # type: ignore[import-untyped]

from .checkout import target_root
from .mutate import SINKS_MALFORMED, declared_sinks  # the same reading of the same table
from .scope import REPORT_DIR
from .shared import (
    DECLARED,
    DIAGNOSTIC,
    SCHEMA,
    STRING_ONLY,
    UnreadableReportError,
    known_sink,
    read_report,
    survivor_class,
)

TAG = "mutation-survivor"
ID_IN_TITLE = re.compile(r"\[m:([0-9a-f]{12})\]")
MUTANT_ID = re.compile(r"[0-9a-f]{12}")
MALFORMED = ErrorCode(
    code="e.input.format.mutation-report.f",
    title="The mutation report is not one bakobo-mutate run writes, so nothing was filed.",
    detail="{problem}. Nothing was filed and the ledger was not read.",
    args=("problem",),
    hint=(
        "Regenerate the report with bakobo-mutate run; running this again on the same file "
        "will fail the same way."
    ),
)
NO_REPORT = ErrorCode(
    code="e.state.mutation-report-missing.r",
    title="There is no mutation report to file ticks from yet.",
    detail="There is no report at {path}.",
    args=("path",),
    hint=(
        "Run bakobo-mutate run in the target first, which writes .ignored/mutation/report.json, "
        "or name a report with --report."
    ),
)
FAILED_RUN = ErrorCode(
    code="e.state.mutation-run-failed.f",
    title="The mutation run behind this report failed, so the report holds no results.",
    detail="The report records the failure as: {failure}",
    args=("failure",),
    hint=(
        "Fix what that failure names and run bakobo-mutate run again, then file from the new "
        "report; this report will never hold results."
    ),
)
NEXT_STEP = ("Either a test is missing an assertion that would notice this, or the mutation is "
             "equivalent and the tick can be closed with a note saying why.")


class MalformedReportError(Exception):
    """A report, or one record in it, is not the shape bakobo-mutate run writes."""


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_record(s: object) -> None:
    """Raise MalformedReportError naming the first field missing or of the wrong type."""
    if not isinstance(s, dict):
        raise MalformedReportError(f"it is a {type(s).__name__}, not an object")
    if not (isinstance(s.get("id"), str) and MUTANT_ID.fullmatch(s["id"])):
        raise MalformedReportError("its id is not 12 lowercase hex digits")
    for key in ("mutmut_name", "file", "function"):
        if not isinstance(s.get(key), str):
            raise MalformedReportError(f"its {key} is not text")
    if not _integer(s.get("line")):
        raise MalformedReportError("its line is not an integer")
    mutation = s.get("mutation")
    if not (isinstance(mutation, dict) and _strings(mutation.get("removed"))
            and _strings(mutation.get("added")) and isinstance(mutation.get("diff"), str)):
        raise MalformedReportError(
            "its mutation lacks removed and added lists of lines, or a diff")
    result = s.get("test_result")
    if not (isinstance(result, dict) and isinstance(result.get("command"), str)
            and _integer(result.get("tests_run")) and _integer(result.get("exit_code"))):
        raise MalformedReportError("its test_result lacks a command, tests_run or exit_code")
    if not known_sink(s.get("message_sink")):
        raise MalformedReportError(
            f"its message_sink {s.get('message_sink')!r} is not a form bakobo-mutate run "
            "records")


def check_header(data: object) -> None:
    """The report is an object of this schema, and its failure fields are what mutate writes.

    ``failure`` is null for a run that worked and a non-empty sentence for one that did not;
    anything else would let a broken report pass for a clean one.
    """
    if not isinstance(data, dict):
        raise MalformedReportError(f"the report is a {type(data).__name__}, not an object")
    if data.get("schema") != SCHEMA:
        raise MalformedReportError(
            f"the report's schema is {data.get('schema')!r}, and only {SCHEMA!r} is read")
    failure = data.get("failure")
    if not (failure is None or (isinstance(failure, str) and failure.strip())):
        raise MalformedReportError("the report's failure is neither null nor a sentence")
    for key, kinds in (("failure_code", str), ("failure_retryable", bool),
                       ("failure_hint", str)):
        if not isinstance(data.get(key), (kinds, type(None))):
            raise MalformedReportError(f"the report's {key} is not a {kinds.__name__} or null")


def check_report(data: object) -> None:
    check_header(data)
    assert isinstance(data, dict)  # check_header refused anything else
    if not isinstance(data.get("commit"), str):
        raise MalformedReportError("the report names no commit")
    if not isinstance(data.get("survivors"), list):
        raise MalformedReportError("the report's survivors are not a list")
    for index, record in enumerate(data["survivors"]):
        try:
            check_record(record)
        except MalformedReportError as error:
            raise MalformedReportError(
                f"survivor {index} in the report is malformed: {error}") from None


def run(argv: list[str], cwd: Path | None = None) -> str:
    """``argv``'s output, run in ``cwd``: for tick, the target, whose ledger it then reads."""
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True).stdout


def ticked(tick: str, cwd: Path | None = None) -> dict[str, str]:
    """Mutant id to tick id, for every mutation-survivor tick, closed ones included."""
    found = {}
    for line in run([tick, "ls", "--all", "--tag", TAG], cwd).splitlines():
        match = ID_IN_TITLE.search(line)
        if match:
            found.setdefault(match.group(1), line.split()[0])
    return found


def title(s: dict) -> str:
    return f"mutation survivor [m:{s['id']}] {s['file']}:{s['line']} in {s['function']}"


def detail(s: dict, data: dict) -> str:
    result = s["test_result"]
    opening = (f"A mutant of {s['function']} in {s['file']} survived a bakobo-mutate run "
               f"at commit {data['commit']}: {result['tests_run']} tests cover the function, "
               f"and all of them passed with this change in place "
               f"(exit {result['exit_code']}).")
    name = f"mutmut name: {s['mutmut_name']} (the ordinal is only meaningful for that run)."
    return "\n".join([
        f"mutant-id: {s['id']}",
        opening,
        "",
        s["mutation"]["diff"],
        "",
        f"Test command: {result['command']}",
        name,
        NEXT_STEP,
    ])


def vouched(sink: str | None, sinks: tuple[str, ...]) -> str | None:
    """A record's sink if it can still vouch for message text, else None.

    Every sink is re-checked, not only declared ones: it must be a form run records, and
    a declared one must be a sink this repo still declares.
    """
    if sink is None or not known_sink(sink):
        return None
    if sink.startswith(DECLARED) and sink.removeprefix(DECLARED) not in sinks:
        return None
    return sink


class Sources:
    """Source files at the report's commit, read from the target's git, each once.

    A file git cannot produce is None, and a survivor in it can never be called diagnostic.
    """

    def __init__(self, root: Path, commit: str, git: str = "git"):
        self.root, self.commit, self.git = root, commit, git
        self.cache: dict[str, str | None] = {}

    def get(self, path: str) -> str | None:
        if path not in self.cache:
            proc = subprocess.run([self.git, "-C", str(self.root), "show",
                                   f"{self.commit}:{path}"], capture_output=True, text=True)
            self.cache[path] = proc.stdout if proc.returncode == 0 else None
        return self.cache[path]


def file_ticks(data: dict, tick: str, dry_run: bool, sinks: tuple[str, ...] = (),
               sources: Sources | None = None,
               cwd: Path | None = None) -> tuple[list[str], int, int, dict[str, int]]:
    """Returns (ids filed, count already ticked, count re-noted, counts by uncounted class).

    Every tick runs in ``cwd``, the target, so the ticks land in its ledger and not in the
    ledger of whatever repo the caller stands in.
    """
    have = ticked(tick, cwd)
    done: set[str] = set()
    filed: list[str] = []
    skipped = renoted = 0
    quiet = {STRING_ONLY: 0, DIAGNOSTIC: 0}
    for s in data["survivors"]:
        mutation = s["mutation"]
        source = sources.get(s["file"]) if sources else None
        kind = survivor_class(source, s["line"], mutation["removed"], mutation["added"],
                              vouched(s.get("message_sink"), sinks))
        if kind in quiet:
            quiet[kind] += 1
            continue
        if s["id"] in done:
            skipped += 1
            continue
        done.add(s["id"])
        if s["id"] in have:
            tick_id = have[s["id"]]
            if f"mutant-id: {s['id']}" in run([tick, "show", tick_id], cwd):
                skipped += 1
            elif dry_run:
                print(f"would re-note {tick_id}: {title(s)}")
                renoted += 1
            else:
                run([tick, "note", tick_id, detail(s, data)], cwd)
                print(f"re-noted {tick_id}: {title(s)}")
                renoted += 1
            continue
        if dry_run:
            print(f"would file: {title(s)}")
        else:
            out = run([tick, "add", "--kind", "debt", "--tag", TAG, title(s)], cwd)
            tick_id = out.split()[0]
            run([tick, "note", tick_id, detail(s, data)], cwd)
            print(f"filed {tick_id}: {title(s)}")
        filed.append(s["id"])
    return filed, skipped, renoted, quiet


def load(path: Path) -> dict:
    """The report, checked; or the BakoboError that says why there is nothing to file."""
    if not path.exists():
        raise NO_REPORT(path=path)
    try:
        data = read_report(path)
    except UnreadableReportError as error:
        raise MALFORMED(problem=str(error)) from None
    try:
        check_header(data)
        failure = data.get("failure")  # type: ignore[union-attr]
        if failure:
            raise FAILED_RUN(failure=failure.strip().splitlines()[0])
        check_report(data)
    except MalformedReportError as error:
        raise MALFORMED(problem=str(error)) from None
    return data  # type: ignore[return-value]


def parse(argv: list[str] | None, prog: str | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n\n")[0])
    parser.add_argument("target", nargs="?", type=Path, default=None,
                        help="the repo the report is about (default: the one around the "
                             "current directory)")
    parser.add_argument("--report", type=Path,
                        help="a report.json somewhere other than <target>/.ignored/mutation")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tick", default="tick")
    return parser.parse_args(argv)


def executable(name: str) -> str:
    """``name`` as tick is run in the target: a path made absolute against the caller's cwd.

    tick runs with the target as its cwd, so a relative path such as ``bin/tick`` would
    otherwise be looked for there. A bare name, with no separator, stays a PATH lookup.
    """
    seps = os.sep + (os.altsep or "")
    return os.path.abspath(name) if any(sep in name for sep in seps) else name


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    args = parse(argv, prog)
    args.tick = executable(args.tick)
    try:
        root = args.target or target_root(Path.cwd())
        sinks = declared_sinks(root)
        data = load(args.report or root / REPORT_DIR / "report.json")
    except BakoboError as error:
        print(f"{error.code}: {error.detail}\nHint: {error.hint}", file=sys.stderr)
        return 1 if error.code in (NO_REPORT.code, FAILED_RUN.code) else 2
    sources = Sources(root, data["commit"])
    filed, skipped, renoted, quiet = file_ticks(data, args.tick, args.dry_run, sinks, sources,
                                                 cwd=root)
    verb = "would file" if args.dry_run else "filed"
    print(f"{verb} {len(filed)}; {skipped} already ticked; {renoted} re-noted; "
          f"{quiet[STRING_ONLY]} string-only and {quiet[DIAGNOSTIC]} diagnostic survivors not "
          f"ticked; {len(data['survivors'])} survivors in the report")
    return 0


if __name__ == "__main__":
    sys.exit(main())

