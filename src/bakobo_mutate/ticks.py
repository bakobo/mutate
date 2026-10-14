"""File one ``tick`` per surviving mutant in the latest scheduled mutation report.

The mutation tool never runs this on its own; a person runs it, so survivors are triaged before
they become anyone's work.

    bakobo-mutate ticks               # latest scheduled run on main
    bakobo-mutate ticks --run-id N    # a particular run
    bakobo-mutate ticks --report F    # a report already on disk
    ... --dry-run                     # say what would be filed, file nothing

Each survivor becomes ``tick add --kind debt --tag mutation-survivor "<title>"`` followed by
``tick note <id> <detail>``. The title carries the survivor's stable id as ``[m:<id>]``, and the
note opens with ``mutant-id: <id>``. A survivor whose id already appears in a
``mutation-survivor`` tick, open or closed, is not filed again, so a survivor someone dismissed
stays dismissed; but if that tick's body lacks the note's ``mutant-id`` line, the note never
landed (``tick note`` failed after ``tick add`` succeeded), and it is added now.

A survivor whose mutation only changes the text of a message literal is counted in the closing
summary and not ticked. That is decided by ``mutate.classify`` from the record's own diff and
its ``message_sink``, never from the stored ``class``, and it files whenever it cannot tell: a
record without a sink is behavioural. A sink the repo declares (``[tool.mutation]
message_sinks``, read from ``--root``'s pyproject.toml) counts only while the repo still
declares it: the record's ``declared <entry>`` must name an entry there, so the report and the
repo agree, and a malformed declaration is refused with ``SINKS_MALFORMED`` before the ledger
is touched.

A report is read at most 16 MiB (``shared.MAX_REPORT_BYTES``) and refused unread beyond that.
One that cannot be read, is not UTF-8 JSON, carries any schema but ``shared.SCHEMA``, or is not
the shape ``bakobo-mutate run`` writes is refused whole with ``MALFORMED``, naming the record it tripped
on, before the ledger is touched. Every refusal below is printed as ``<code>: <detail>`` with
the code's hint. No tick mark is placed in the code, since a survivor is a question about the
tests and the line it names moves.

Without ``--run-id`` the report comes from the latest successful SCHEDULED run on main, so a
manual run of a feature branch never becomes "the latest"; name such a run explicitly.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from bakobo.errors import BakoboError, ErrorCode  # type: ignore[import-untyped]

from .mutate import SINKS_MALFORMED, declared_sinks  # the same reading of the same table
from .shared import (
    DECLARED,
    SCHEMA,
    STRING_ONLY,
    UnreadableReportError,
    classify,
    known_sink,
    read_report,
)

TAG = "mutation-survivor"
WORKFLOW = "mutation.yml"
ARTIFACT = "mutation-report"
REPORT = "mutation-report.json"
ID_IN_TITLE = re.compile(r"\[m:([0-9a-f]{12})\]")
MUTANT_ID = re.compile(r"[0-9a-f]{12}")
MALFORMED = ErrorCode(
    code="e.input.format.mutation-report.f",
    title=(
        "The mutation report is not one tools/mutation/mutate.py writes, so nothing was "
        "filed."
    ),
    detail="{problem}. Nothing was filed and the ledger was not read.",
    args=("problem",),
    hint=(
        "Download the report again or regenerate it with tools/mutation/mutate.py; running "
        "this again on the same file will fail the same way."
    ),
)
NO_REPORT = ErrorCode(
    code="e.state.mutation-report-missing.r",
    title="There is no nightly mutation report to file ticks from yet.",
    detail=(
        "No scheduled mutation.yml run on main has succeeded, so there is no report to fetch."
    ),
    hint=(
        "Wait for the next scheduled run on main and run this again, or name a particular run "
        "with --run-id."
    ),
)
FAILED_RUN = ErrorCode(
    code="e.state.mutation-run-failed.f",
    title="The mutation run behind this report failed, so the report holds no results.",
    detail="The report records the failure as: {failure}",
    args=("failure",),
    hint=(
        "Fix what that failure names and run the workflow again, then file from the new run; "
        "this report will never hold results."
    ),
)
NEXT_STEP = ("Either a test is missing an assertion that would notice this, or the mutation is "
             "equivalent and the tick can be closed with a note saying why.")


class MalformedReportError(Exception):
    """A report, or one record in it, is not the shape mutate.py writes."""


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
            f"its message_sink {s.get('message_sink')!r} is not a form mutate.py records")


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


def run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


def download(gh: str, run_id: str | None, into: Path) -> Path:
    if run_id is None:
        run_id = run([gh, "run", "list", "--workflow", WORKFLOW, "--status", "success",
                      "--branch", "main", "--event", "schedule", "--limit", "1", "--json",
                      "databaseId", "--jq", ".[0].databaseId"])
        run_id = run_id.strip()
        if not run_id:
            raise NO_REPORT()
    run([gh, "run", "download", run_id, "--name", ARTIFACT, "--dir", str(into)])
    return into / REPORT


def ticked(tick: str) -> dict[str, str]:
    """Mutant id to tick id, for every mutation-survivor tick, closed ones included."""
    found = {}
    for line in run([tick, "ls", "--all", "--tag", TAG]).splitlines():
        match = ID_IN_TITLE.search(line)
        if match:
            found.setdefault(match.group(1), line.split()[0])
    return found


def title(s: dict) -> str:
    return f"mutation survivor [m:{s['id']}] {s['file']}:{s['line']} in {s['function']}"


def detail(s: dict, data: dict) -> str:
    result = s["test_result"]
    opening = (f"A mutant of {s['function']} in {s['file']} survived the nightly mutation run "
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

    Every sink is re-checked, not only declared ones: it must be a form mutate.py records, and
    a declared one must be a sink this repo still declares.
    """
    if sink is None or not known_sink(sink):
        return None
    if sink.startswith(DECLARED) and sink.removeprefix(DECLARED) not in sinks:
        return None
    return sink


def file_ticks(data: dict, tick: str, dry_run: bool,
               sinks: tuple[str, ...] = ()) -> tuple[list[str], int, int, int]:
    """Returns (ids filed, count already ticked, count re-noted, count string-only)."""
    have = ticked(tick)
    done: set[str] = set()
    filed: list[str] = []
    skipped = renoted = strings = 0
    for s in data["survivors"]:
        mutation = s["mutation"]
        kind = classify(mutation["removed"], mutation["added"],
                        vouched(s.get("message_sink"), sinks))
        if kind == STRING_ONLY:
            strings += 1
            continue
        if s["id"] in done:
            skipped += 1
            continue
        done.add(s["id"])
        if s["id"] in have:
            tick_id = have[s["id"]]
            if f"mutant-id: {s['id']}" in run([tick, "show", tick_id]):
                skipped += 1
            elif dry_run:
                print(f"would re-note {tick_id}: {title(s)}")
                renoted += 1
            else:
                run([tick, "note", tick_id, detail(s, data)])
                print(f"re-noted {tick_id}: {title(s)}")
                renoted += 1
            continue
        if dry_run:
            print(f"would file: {title(s)}")
        else:
            out = run([tick, "add", "--kind", "debt", "--tag", TAG, title(s)])
            tick_id = out.split()[0]
            run([tick, "note", tick_id, detail(s, data)])
            print(f"filed {tick_id}: {title(s)}")
        filed.append(s["id"])
    return filed, skipped, renoted, strings


def load(args: argparse.Namespace) -> dict:
    """The report, checked; or the BakoboError that says why there is nothing to file."""
    with tempfile.TemporaryDirectory() as scratch:
        path = args.report or download(args.gh, args.run_id, Path(scratch))
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
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--report", type=Path, help="a mutation-report.json already on disk")
    source.add_argument("--run-id", help="the mutation.yml run to download the report from")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--tick", default="tick")
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="the repo whose pyproject.toml declares its message sinks")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    args = parse(argv, prog)
    try:
        sinks = declared_sinks(args.root)
        data = load(args)
    except BakoboError as error:
        print(f"{error.code}: {error.detail}\nHint: {error.hint}", file=sys.stderr)
        return 2 if error.code in (MALFORMED.code, SINKS_MALFORMED.code) else 1
    filed, skipped, renoted, strings = file_ticks(data, args.tick, args.dry_run, sinks)
    verb = "would file" if args.dry_run else "filed"
    print(f"{verb} {len(filed)}; {skipped} already ticked; {renoted} re-noted; {strings} "
          f"string-only survivors not ticked; {len(data['survivors'])} survivors in the report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
