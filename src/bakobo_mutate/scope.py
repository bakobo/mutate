"""Which modules a run covers, which are stale, and the stamp that remembers (this.i 24xzjnz7).

A module is a ``.py`` file under ``[tool.mutmut].source_paths`` that ``do_not_mutate`` does not
exclude and that defines a function or method, which is all mutmut mutates. Everything here is
read from git objects at a ref (``origin/<default>``), never from a working tree, so ``stale``
answers without the mutation checkout and ``run`` and ``stale`` always agree.

The stamp, ``<target>/.ignored/mutation/stamp.json``, records per module the commit it was
last mutated at and whether every one of its mutants then had a verdict. A module is stale when
the ref changed it after that commit, when its last run left it incomplete, or, for a module
the stamp has never seen, when the ref changed it after the stamp's ``base``: the commit of the
last run whose scope was computed rather than named. With no base yet, an unstamped module is
stale if it changed inside the ``--since`` window. Anything git cannot answer counts as stale.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from bakobo.errors import ErrorCode  # type: ignore[import-untyped]

STAMP_SCHEMA = "bakobo.mutation-stamp/1"
# A stamp is one short record per module; this is far beyond any real repo's.
MAX_STAMP_BYTES = 4 * 1024 * 1024
REPORT_DIR = Path(".ignored") / "mutation"

CONFIG_UNREADABLE = ErrorCode(
    code="e.self.config.mutmut-table.f",
    title="The target's [tool.mutmut] table cannot be read, so no module could be chosen.",
    detail="pyproject.toml at {ref} is unusable: {problem}.",
    args=("ref", "problem"),
    hint="Fix pyproject.toml on the default branch; running this again before then will fail "
         "the same way.",
)
STAMP_MALFORMED = ErrorCode(
    code="e.self.corrupt.mutation-stamp.f",
    title="The record of earlier mutation runs is unreadable, so staleness is unknown.",
    detail="{path} {problem}.",
    args=("path", "problem"),
    hint=(
        "Delete that file. The next run then chooses modules from the --since window and "
        "writes a fresh record; mutmut's own cache of verdicts is not affected."
    ),
)


@dataclass
class Entry:
    commit: str
    complete: bool


@dataclass
class Stamp:
    base: str | None
    modules: dict[str, Entry] = field(default_factory=dict)


def _git(target: Path, *args: str, git: str = "git") -> subprocess.CompletedProcess:
    return subprocess.run([git, "-C", str(target), *args], capture_output=True, text=True)


def _show(target: Path, ref: str, path: str, git: str = "git") -> str | None:
    proc = _git(target, "show", f"{ref}:{path}", git=git)
    return proc.stdout if proc.returncode == 0 else None


def config_at(target: Path, ref: str, git: str = "git") -> dict:
    """``[tool.mutmut]`` from pyproject.toml at ``ref``, or {} when there is no pyproject."""
    text = _show(target, ref, "pyproject.toml", git)
    if text is None:
        return {}
    try:
        tool = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError as error:
        raise CONFIG_UNREADABLE(ref=ref, problem=f"it is not TOML ({error})") from None
    table = tool.get("mutmut", {}) if isinstance(tool, dict) else None
    if not isinstance(table, dict):
        raise CONFIG_UNREADABLE(ref=ref, problem="[tool] or [tool.mutmut] is not a table")
    return table


def sources(cfg: dict) -> list[str]:
    return cfg.get("source_paths", ["src"])


def excluded(path: str, cfg: dict) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in cfg.get("do_not_mutate", []))


def has_functions(source: str) -> bool:
    """Whether mutmut could mutate anything here: it mutates functions and methods only.

    A module that does not parse is kept, so that mutmut reports it rather than this skipping
    it silently.
    """
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return True
    for node in body:
        if isinstance(node, funcs):
            return True
        if isinstance(node, ast.ClassDef) and any(isinstance(n, funcs) for n in node.body):
            return True
    return False


def candidates(target: Path, ref: str, cfg: dict, paths, git: str = "git") -> list[str]:
    """The paths that are modules at ``ref``: existing, not excluded, defining functions."""
    found = []
    for path in sorted(set(paths)):
        if not path.endswith(".py") or excluded(path, cfg):
            continue
        source = _show(target, ref, path, git)
        if source is not None and has_functions(source):
            found.append(path)
    return found


def window(target: Path, ref: str, since: str, cfg: dict, git: str = "git") -> list[str]:
    """Modules a first-parent commit on ``ref`` touched since ``since``.

    ``--diff-merges=first-parent`` makes a merge commit list what the merge brought onto the
    branch, so a pull request whose own commits are older than the window still counts on the
    day it landed.
    """
    out = _git(target, "log", "--first-parent", "--diff-merges=first-parent",
               f"--since={since}", "--name-only", "--format=", ref, "--", *sources(cfg),
               git=git).stdout
    return candidates(target, ref, cfg, [line.strip() for line in out.splitlines()
                                         if line.strip()], git)


def changed_between(target: Path, base: str, ref: str, cfg: dict,
                    git: str = "git") -> set[str] | None:
    """Source paths that differ between ``base`` and ``ref``; None when git cannot say."""
    proc = _git(target, "diff", "--name-only", base, ref, "--", *sources(cfg), git=git)
    if proc.returncode != 0:
        return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def all_sources(target: Path, ref: str, cfg: dict, git: str = "git") -> set[str]:
    out = _git(target, "ls-tree", "-r", "--name-only", ref, "--", *sources(cfg),
               git=git).stdout
    return {line for line in out.splitlines() if line}


def stale(target: Path, ref: str, stamp: Stamp, since: str, cfg: dict,
          git: str = "git") -> list[str]:
    found: set[str] = set()
    by_commit: dict[str, list[str]] = {}
    for path, entry in stamp.modules.items():
        by_commit.setdefault(entry.commit, []).append(path)
        if not entry.complete:
            found.add(path)
    for commit, paths in by_commit.items():
        changed = changed_between(target, commit, ref, cfg, git)
        found.update(p for p in paths if changed is None or p in changed)
    if stamp.base is None:
        unstamped = set(window(target, ref, since, cfg, git))
    else:
        changed = changed_between(target, stamp.base, ref, cfg, git)
        unstamped = all_sources(target, ref, cfg, git) if changed is None else changed
    found.update(unstamped - stamp.modules.keys())
    return candidates(target, ref, cfg, found, git)


def run_scope(target: Path, ref: str, stamp: Stamp | None, named: list[str], since: str,
              cfg: dict, git: str = "git") -> tuple[list[str], str]:
    """The modules a run covers, and how they were chosen, in words for the report."""
    if named:
        return candidates(target, ref, cfg, named, git), "the modules named with --module"
    if stamp is None:
        return window(target, ref, since, cfg, git), f"modules changed since {since}"
    return stale(target, ref, stamp, since, cfg, git), "modules stale since the last run"


def stamp_path(target: Path) -> Path:
    return target / REPORT_DIR / "stamp.json"


def _malformed(path: Path, problem: str) -> Exception:
    return STAMP_MALFORMED(path=path, problem=problem)


def read_stamp(target: Path) -> Stamp | None:
    path = stamp_path(target)
    if not path.exists():
        return None
    with path.open("rb") as handle:
        raw = handle.read(MAX_STAMP_BYTES + 1)
    if len(raw) > MAX_STAMP_BYTES:
        raise _malformed(path, f"is larger than {MAX_STAMP_BYTES} bytes")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise _malformed(path, "is not JSON") from None
    if not isinstance(data, dict) or data.get("schema") != STAMP_SCHEMA:
        raise _malformed(path, f"is not a {STAMP_SCHEMA} record")
    base, modules = data.get("base"), data.get("modules")
    if not (base is None or isinstance(base, str)):
        raise _malformed(path, "has a base that is neither null nor a commit")
    if not isinstance(modules, dict):
        raise _malformed(path, "has modules that are not an object")
    entries = {}
    for module, entry in modules.items():
        if not (isinstance(entry, dict) and isinstance(entry.get("commit"), str)
                and isinstance(entry.get("complete"), bool)):
            raise _malformed(path, f"has an entry for {module!r} without a commit and "
                                   "a complete flag")
        entries[module] = Entry(entry["commit"], entry["complete"])
    return Stamp(base=base, modules=entries)


def write_stamp(target: Path, stamp: Stamp) -> None:
    path = stamp_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": STAMP_SCHEMA, "base": stamp.base,
            "modules": {m: {"commit": e.commit, "complete": e.complete}
                        for m, e in sorted(stamp.modules.items())}}
    path.write_text(json.dumps(data, indent=2) + "\n")


def updated(stamp: Stamp | None, complete: dict[str, bool], commit: str,
            advance_base: bool) -> Stamp:
    """The stamp after a run at ``commit`` that covered the modules in ``complete``."""
    modules = dict(stamp.modules) if stamp else {}
    for module, done in complete.items():
        modules[module] = Entry(commit, done)
    base = commit if advance_base else (stamp.base if stamp else None)
    return Stamp(base=base, modules=modules)
