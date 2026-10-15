"""mutmut's verdicts, read from its ``.meta`` files, and the names that select what to run.

mutmut 3.8 keeps one ``mutants/<source path>.meta`` per source file, a JSON object whose
``exit_code_by_key`` maps each mutant's name to the exit code of its last check, or null for
one never checked. It saves each verdict as soon as the mutant finishes, and it re-runs every
mutant it is given by name, cached or not, so the names passed to it are the whole of what a
run costs (this.i 7wj9uurv). Reading the files directly, rather than parsing ``mutmut
results``, needs no subprocess and no guess at its output format; a file that cannot be read
is refused with META_UNREADABLE rather than skipped.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from bakobo.errors import ErrorCode  # type: ignore[import-untyped]

# mutmut 3.8's status_by_exit_code; any other code is "suspicious", as there.
STATUS = {
    None: "not checked", 0: "survived", 1: "killed", 3: "killed",
    2: "check was interrupted by user", 5: "no tests", 33: "no tests", 34: "skipped",
    35: "suspicious", 36: "timeout", 37: "caught by type check", -24: "timeout",
    24: "timeout", 152: "timeout", 255: "timeout", -11: "segfault", -9: "segfault",
}
NOT_CHECKED, SURVIVED = None, 0
# No verdict on the mutant: never checked, or its check was interrupted by the user. A module
# holding one is incomplete, and one in scope is run again.
UNSETTLED = (NOT_CHECKED, 2)
# Verdicts only a new test can change: survived, and both of mutmut's "no tests". They do not
# make a module incomplete, but mutmut's cache is keyed on source, not tests, so like survivors
# they are run again on every run, repo-wide, unless survivors are switched off.
RECHECKED = (SURVIVED, 5, 33)
# A .meta is a few hundred bytes per mutant; this is beyond any real module's.
MAX_META_BYTES = 64 * 1024 * 1024
MUTANT = "__mutmut_"

META_UNREADABLE = ErrorCode(
    code="e.env.mutmut-meta.f",
    title="mutmut's record of its verdicts cannot be read, so nothing was reported.",
    detail="{path} {problem}.",
    args=("path", "problem"),
    hint=(
        "Delete the mutation checkout's mutants/ directory, which makes the next run check "
        "every mutant again; running this again first will fail the same way."
    ),
)

Verdicts = dict[str, dict[str, int | None]]


def status(code: int | None) -> str:
    return STATUS.get(code, "suspicious")


def _code(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _read_one(path: Path, shown: str) -> dict[str, int | None]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_META_BYTES + 1)
    if len(raw) > MAX_META_BYTES:
        raise META_UNREADABLE(path=shown, problem=f"is larger than {MAX_META_BYTES} bytes")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise META_UNREADABLE(path=shown, problem="is not JSON") from None
    codes = data.get("exit_code_by_key") if isinstance(data, dict) else None
    if not isinstance(codes, dict):
        raise META_UNREADABLE(path=shown, problem="has no exit_code_by_key object")
    for key, value in codes.items():
        if not _code(value):
            raise META_UNREADABLE(path=shown,
                                  problem=f"records {value!r} for {key!r}, not an exit code")
    return codes


def read_meta(wt: Path) -> Verdicts:
    """Verdicts by source path, for every .meta whose source still exists in ``wt``."""
    root = wt / "mutants"
    found: Verdicts = {}
    for path in sorted(root.rglob("*.meta")):
        source = path.relative_to(root).as_posix().removesuffix(".meta")
        if (wt / source).is_file():
            found[source] = _read_one(path, f"mutants/{source}.meta")
    return found


def needed(verdicts: Verdicts, scope: set[str], survivors: bool) -> set[str]:
    """In-scope mutants with no verdict yet (UNSETTLED), and if ``survivors`` every RECHECKED one.

    Survivors and "no tests" verdicts are re-run because mutmut's cache is keyed on source, not
    tests: re-running them is the only way a newly added test is noticed.
    """
    return {key for path, codes in verdicts.items() for key, code in codes.items()
            if (path in scope and code in UNSETTLED)
            or (survivors and code in RECHECKED)}


def names(verdicts: Verdicts, need: set[str]) -> list[str]:
    """Names selecting exactly ``need``: a function's glob when all of it is needed, else keys."""
    found = []
    for _, codes in sorted(verdicts.items()):
        functions: dict[str, list[str]] = {}
        for key in codes:
            functions.setdefault(key.partition(MUTANT)[0], []).append(key)
        for function, keys in sorted(functions.items()):
            chosen = sorted(k for k in keys if k in need)
            if chosen and len(chosen) == len(keys):
                found.append(f"{function}{MUTANT}*")
            else:
                found.extend(chosen)
    return found


def chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def complete(verdicts: Verdicts, modules: list[str]) -> dict[str, bool]:
    """Per module, whether every one of its mutants has a verdict: none is UNSETTLED.

    Defined from UNSETTLED, as ``needed`` is, so a complete module never has a mutant that a
    run in scope would still need.
    """
    return {m: all(code not in UNSETTLED for code in verdicts.get(m, {}).values())
            for m in modules}


def counts(verdicts: Verdicts, modules: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for module in modules:
        for code in verdicts.get(module, {}).values():
            found[status(code)] = found.get(status(code), 0) + 1
    return found


def survivors(verdicts: Verdicts) -> list[tuple[str, str]]:
    """(mutant name, source path) for every survivor on record, sorted by name."""
    return sorted((key, path) for path, codes in verdicts.items()
                  for key, code in codes.items() if code == SURVIVED)
