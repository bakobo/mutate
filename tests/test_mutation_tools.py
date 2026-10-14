"""The mutation tool: bakobo_mutate.mutate (``run``) and bakobo_mutate.ticks (``ticks``).

mutmut, gh and tick are replaced by small stub executables that answer from a JSON spec and log
what they were asked, so nothing here runs a mutation, reaches GitHub, or touches a real ledger.
git is the real one, over a scratch repository, because which modules count as changed is
exactly the kind of thing a stub would get wrong in the same way as the code under test.
"""

from __future__ import annotations

import json
import os
import re
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from bakobo_mutate import mutate, shared, ticks

SEP = "\u01c1"

STUB = """\
#!{python}
import json, os, signal, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
spec = json.loads((here / "spec.json").read_text())
with (here / "calls.log").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
answer = spec.get(sys.argv[1], {{}})
if isinstance(answer, dict) and "by_arg" in answer:
    answer = answer["by_arg"].get(sys.argv[-1], answer.get("default", {{}}))
if isinstance(answer, str):
    answer = {{"out": answer}}
if answer.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(answer.get("sleep", 0))
if "copy" in answer:
    dest = Path(sys.argv[sys.argv.index("--dir") + 1])
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "mutation-report.json").write_text(Path(answer["copy"]).read_text())
sys.stdout.write(answer.get("out", ""))
sys.exit(answer.get("exit", 0))
"""


def stub(directory: Path, name: str, spec: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(STUB.format(python=sys.executable))
    path.chmod(0o755)
    (directory / "spec.json").write_text(json.dumps(spec))
    return path


def calls(directory: Path) -> list[list[str]]:
    log = directory / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def git(root: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True,
                          text=True).stdout


MODULE = '''\
"""A module to mutate."""


def saidify(sad, label="d"):
    if label not in sad:
        raise ValueError("no label")
    return dict(sad)


class Box:
    # a leading comment mutmut's diff counts as the method's first line
    @staticmethod
    def open(x):
        return x + 1
'''

DIFF_SAIDIFY = """\
--- src/pkg/mod.py
+++ src/pkg/mod.py
@@ -1,4 +1,4 @@
 def saidify(sad, label="d"):
     if label not in sad:
-        raise ValueError("no label")
+        raise ValueError(None)
     return dict(sad)"""

DIFF_OPEN = """\
--- src/pkg/mod.py
+++ src/pkg/mod.py
@@ -2,3 +2,3 @@
 @staticmethod
 def open(x):
-    return x + 1
+    return x - 1"""

NAME_SAIDIFY = "pkg.mod.x_saidify__mutmut_3"
NAME_OPEN = f"pkg.mod.x{SEP}Box{SEP}open__mutmut_1"

SHOWS = {NAME_SAIDIFY: f"# {NAME_SAIDIFY}: survived\n{DIFF_SAIDIFY}\n", NAME_OPEN: DIFF_OPEN + "\n"}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text(MODULE)
    return root


def show_spec(shows=None, tests=None):
    return {
        "show": {"by_arg": shows or SHOWS},
        "tests-for-mutant": {"by_arg": tests or {NAME_SAIDIFY: "tests/a.py::t1\ntests/a.py::t2\n"},
                             "default": "tests/b.py::t\n"},
    }


def survivors_of(repo, mutmut, names, sinks=()):
    return [mutate.survivor(str(mutmut), repo, name, "src/pkg/mod.py", sinks) for name in names]


# --- the pure parts ---------------------------------------------------------------------------


def test_module_names_follow_mutmut():
    assert mutate.module_name("src/pkg/a/b.py") == "pkg.a.b"
    assert mutate.module_name("src/pkg/a/__init__.py") == "pkg.a"


def test_function_of():
    assert mutate.function_of(NAME_SAIDIFY, "src/pkg/mod.py") == "saidify"
    assert mutate.function_of(NAME_OPEN, "src/pkg/mod.py") == "Box.open"
    assert mutate.function_of("pkg.x_f__mutmut_1", "src/pkg/__init__.py") == "f"


def test_parse_diff():
    assert mutate.parse_diff(DIFF_SAIDIFY) == (
        3, ['        raise ValueError("no label")'], ["        raise ValueError(None)"])
    added_only = "--- a\n+++ a\n@@ -5,2 +5,3 @@\n x = 1\n+y = 2\n z = 3"
    assert mutate.parse_diff(added_only) == (6, [], ["y = 2"])


def test_locate_finds_the_removed_line_in_the_file():
    assert mutate.locate(MODULE, "saidify", 3, ['        raise ValueError("no label")']) == 6
    # The diff counts the method's leading comment, so its offset is one off; the text wins.
    assert mutate.locate(MODULE, "Box.open", 3, ["    return x + 1"]) == 14


def test_locate_falls_back_to_the_offset():
    assert mutate.locate(MODULE, "saidify", 2, []) == 5
    assert mutate.locate(MODULE, "saidify", 0, ["no such line"]) == 4
    assert mutate.locate(MODULE, "saidify", 99, []) == 7


def test_locate_without_the_function():
    assert mutate.locate(MODULE, "missing", 1, []) == 0
    assert mutate.locate(MODULE, "Nope.open", 1, []) == 0


def test_mutant_ids_are_stable_and_distinguish_repeats():
    first = mutate.mutant_id("f.py", "g", ["a"], ["b"], 0)
    assert first == mutate.mutant_id("f.py", "g", ["  a"], ["b  "], 0)
    assert mutate.mutant_id("f.py", "g", ["a"], ["b"], 1) != first
    assert mutate.mutant_id("f.py", "h", ["a"], ["b"], 0) != first


TWIN = "def twin(x):\n    y = x + 1\n    y = x + 1\n    return y\n"


def test_occurrence_counts_identical_lines_in_source_order():
    assert mutate.occurrence(TWIN, "twin", 2, ["    y = x + 1"]) == 0
    assert mutate.occurrence(TWIN, "twin", 3, ["    y = x + 1"]) == 1
    # A mutation that removes nothing is placed by its offset in the function.
    assert mutate.occurrence(TWIN, "twin", 4, []) == 3
    assert mutate.occurrence(TWIN, "missing", 2, ["y"]) == 0


def test_cell_escapes_and_truncates():
    assert mutate.cell(" a|b `c` ") == "a\\|b 'c'"
    assert mutate.cell("x" * 100) == "x" * 77 + "..."


# --- a survivor read back from mutmut -------------------------------------------------------


def test_survivors_are_read_back_from_mutmut(repo, tmp_path):
    mutmut = stub(tmp_path / "bin", "mutmut", show_spec())
    first, second = survivors_of(repo, mutmut, [NAME_SAIDIFY, NAME_OPEN])
    assert calls(tmp_path / "bin")[:2] == [["show", NAME_SAIDIFY],
                                            ["tests-for-mutant", NAME_SAIDIFY]]
    assert first.line == 6 and first.removed == ['        raise ValueError("no label")']
    assert not first.diff.startswith("#") and first.tests == 2
    assert (second.file, second.line, second.function) == ("src/pkg/mod.py", 14, "Box.open")
    assert second.tests == 1 and first.id != second.id
    record = first.record(["-n0", "--no-cov"])
    assert record["test_result"] == {
        "command": "pytest -n0 --no-cov", "tests_run": 2, "exit_code": 0,
        "outcome": "passed: every test covering this function passed with the mutant in place"}


# --- filing ticks -----------------------------------------------------------------------------


def survivor(id_, line=6):
    return {
        "id": id_, "mutmut_name": NAME_SAIDIFY, "file": "src/pkg/mod.py", "line": line,
        "function": "saidify",
        "mutation": {"removed": ["a"], "added": ["b"], "diff": "-a\n+b"},
        "test_result": {"command": "pytest -n0 --no-cov", "tests_run": 2, "exit_code": 0,
                        "outcome": "passed"},
    }


@pytest.fixture
def sample(tmp_path):
    data = {"schema": mutate.SCHEMA, "commit": "c0ffee", "failure": None,
            "survivors": [survivor("aaaaaaaaaaaa"), survivor("bbbbbbbbbbbb", 9),
                          survivor("bbbbbbbbbbbb", 9), survivor("cccccccccccc", 12)]}
    path = tmp_path / "mutation-report.json"
    path.write_text(json.dumps(data))
    return path


def tick_stub(tmp_path):
    return stub(tmp_path / "tickbin", "tick", {
        "ls": "2abc  debt   mutation survivor [m:aaaaaaaaaaaa] src/pkg/mod.py:6 in saidify\n"
              "3def  debt   mutation survivor [m:dddddddddddd] x.py:1 in f  (closed)\n",
        "add": "4ghi  mutation survivor ...\npaste the mark  ~4ghi  wherever this lives\n",
        "note": "noted on 4ghi\n",
        # 2abc already carries its detail note; a tick without one is retried.
        "show": {"by_arg": {"2abc": "title: ...\n\nmutant-id: aaaaaaaaaaaa\n"},
                 "default": "title: ...\n"},
    })


def test_ticks_files_only_new_survivors(sample, tmp_path, capsys):
    tick = tick_stub(tmp_path)
    assert ticks.main(["--report", str(sample), "--tick", str(tick)]) == 0
    log = calls(tmp_path / "tickbin")
    assert log[0] == ["ls", "--all", "--tag", "mutation-survivor"]
    adds = [c for c in log if c[0] == "add"]
    assert [c[-1] for c in adds] == [
        "mutation survivor [m:bbbbbbbbbbbb] src/pkg/mod.py:9 in saidify",
        "mutation survivor [m:cccccccccccc] src/pkg/mod.py:12 in saidify"]
    assert adds[0][:5] == ["add", "--kind", "debt", "--tag", "mutation-survivor"]
    notes = [c for c in log if c[0] == "note"]
    assert len(notes) == 2 and notes[0][1] == "4ghi"
    assert "commit c0ffee" in notes[0][2] and "-a\n+b" in notes[0][2]
    assert "Test command: pytest -n0 --no-cov" in notes[0][2]
    assert ("filed 2; 2 already ticked; 0 re-noted; 0 string-only survivors not ticked; "
            "4 survivors" in capsys.readouterr().out)


def test_ticks_dry_run_files_nothing(sample, tmp_path, capsys):
    tick = tick_stub(tmp_path)
    assert ticks.main(["--report", str(sample), "--tick", str(tick), "--dry-run"]) == 0
    # Only the already-ticked survivor's tick is read, to see whether its note landed.
    assert [c[0] for c in calls(tmp_path / "tickbin")] == ["ls", "show"]
    out = capsys.readouterr().out
    assert "would file: mutation survivor [m:bbbbbbbbbbbb]" in out
    assert "would file 2; 2 already ticked" in out


def test_ticks_downloads_the_latest_run(sample, tmp_path):
    gh = stub(tmp_path / "ghbin", "gh", {"run": {"by_arg": {
        ".[0].databaseId": {"out": "123\n"}}, "default": {"copy": str(sample)}}})
    tick = tick_stub(tmp_path)
    assert ticks.main(["--gh", str(gh), "--tick", str(tick), "--dry-run"]) == 0
    first, second = calls(tmp_path / "ghbin")
    assert first[:4] == ["run", "list", "--workflow", "mutation.yml"]
    # Only scheduled runs on main: a manual run of a feature branch is never "the latest".
    assert first[first.index("--branch") + 1] == "main"
    assert first[first.index("--event") + 1] == "schedule"
    assert second[:5] == ["run", "download", "123", "--name", "mutation-report"]


def test_ticks_downloads_a_named_run(sample, tmp_path):
    gh = stub(tmp_path / "ghbin", "gh", {"run": {"copy": str(sample)}})
    tick = tick_stub(tmp_path)
    assert ticks.main(["--run-id", "77", "--gh", str(gh), "--tick", str(tick),
                       "--dry-run"]) == 0
    assert calls(tmp_path / "ghbin")[0][:3] == ["run", "download", "77"]


def test_ticks_without_a_successful_run(tmp_path, capsys):
    gh = stub(tmp_path / "ghbin", "gh", {"run": {"out": "\n"}})
    assert ticks.main(["--gh", str(gh)]) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"{ticks.NO_REPORT.code}: ") and ticks.NO_REPORT.code.endswith(".r")
    assert f"Hint: {ticks.NO_REPORT.hint}" in err


def test_ticks_refuses_a_failed_report(tmp_path, capsys):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"schema": mutate.SCHEMA, "failure": "mutmut exited 1",
                                "survivors": []}))
    assert ticks.main(["--report", str(path), "--tick", "/nonexistent"]) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"{ticks.FAILED_RUN.code}: ") and "mutmut exited 1" in err
    assert f"Hint: {ticks.FAILED_RUN.hint}" in err


def test_ticks_entry_point(sample, tmp_path, monkeypatch):
    tick = tick_stub(tmp_path)
    monkeypatch.setattr(sys, "argv", ["ticks.py", "--report", str(sample), "--tick", str(tick),
                                      "--dry-run"])
    with pytest.raises(SystemExit) as exit_:
        # Unimported first, as `python -m` would find it; monkeypatch puts it back afterwards.
        monkeypatch.delitem(sys.modules, "bakobo_mutate.ticks")
        runpy.run_module("bakobo_mutate.ticks", run_name="__main__", alter_sys=True)
    assert exit_.value.code == 0


# --- string-only survivors --------------------------------------------------------------------
#
# A survivor whose mutation only changes the text inside a string literal is counted, not
# ticked. The class is decided from the diff alone, and conservatively: anything the token
# comparison cannot vouch for is behavioural.

BEHAVIOURAL, STRING_ONLY = "behavioural", "string-only"
REAL = json.loads((Path(__file__).resolve().parent
                   / "mutation_survivors_2026-10-05.json").read_text("utf-8"))["survivors"]


@pytest.mark.parametrize("removed, added", [
    (['    raise ValueError("no label")'], ['    raise ValueError("XXno labelXX")']),
    (['raise E(f"a {x} b")'], ['raise E(f"A {x} B")']),
    (['        "cannot carry one")'], ['        "CANNOT CARRY ONE")']),
    (['    raise E("one "'], ['    raise E("ONE "']),
    (['f("a",', '  "b")'], ['f("a",', '  "XXbXX")']),
])
def test_a_change_inside_a_message_literal_is_string_only(removed, added):
    assert mutate.classify(removed, added, "raise ValueError") == STRING_ONLY


@pytest.mark.parametrize("removed, added", [
    (['    raise ValueError("no label")'], ['    raise ValueError("XXno labelXX")']),
    (['    return "allow"'], ['    return "XXallowXX"']),
])
def test_a_string_change_outside_a_message_sink_is_behavioural(removed, added):
    assert mutate.classify(removed, added, None) == BEHAVIOURAL


@pytest.mark.parametrize("removed, added", [
    (['raise E("no label")'], ['raise E(None)']),
    (['raise E(f"a {type(x).__name__}")'], ['raise E(f"a {type(None).__name__}")']),
    (['y = x + 1'], ['y = x - 1']),
    (['y = 1'], []),
    ([], ['y = 1']),
    (['y = "a"'], ['y = "a"']),
    (['y = "a"'], ['y = "a", "b"']),
    (['if "v" in plain:'], ['if "XXvXX" in plain:']),
    (['if kind == "json":'], ['if kind == "JSON":']),
    (['x = d["key"]'], ['x = d["KEY"]']),
    (['x = {"key": 1}'], ['x = {"KEY": 1}']),
    (['y = "abc'], ['y = "ABC']),
    (['y = 1', 'z = "a"'], ['y = 1']),
])
def test_anything_else_is_behavioural(removed, added):
    assert mutate.classify(removed, added, "raise ValueError") == BEHAVIOURAL




def test_the_real_utina_survivors_split_under_its_declared_sinks():
    # Every one changes text handed to Refusal(...). utina declares Refusal's missing and
    # detail as message sinks, so 54 are string-only. Three change a literal or a slice inside
    # an f-string's {...} (', '.join, [:16] to [:17]), which is code, and stay behavioural.
    classes = {s["id"]: mutate.classify(s["removed"], s["added"], s["message_sink"])
               for s in REAL["utina"]}
    assert len(classes) == 57
    assert sorted(i for i, c in classes.items() if c == BEHAVIOURAL) == [
        "3844d16a640a", "e594c88ec0cc", "eb3b234984ca"]
    assert {s["message_sink"] for s in REAL["utina"]} == {
        "declared Refusal.missing", "declared Refusal.detail"}


def test_the_report_carries_each_survivors_class(repo, tmp_path):
    diff = DIFF_SAIDIFY.replace('raise ValueError(None)', 'raise ValueError("NO LABEL")')
    mutmut = stub(tmp_path / "bin", "mutmut", show_spec({**SHOWS, NAME_SAIDIFY: diff + "\n"}))
    outcome = mutate.Outcome(modules=["src/pkg/mod.py"],
                             survivors=survivors_of(repo, mutmut, [NAME_SAIDIFY, NAME_OPEN]))
    data = mutate.report(outcome, {}, "c0ffee", "scope")
    text = mutate.summary(data)
    assert [s["class"] for s in data["survivors"]] == [STRING_ONLY, BEHAVIOURAL]
    assert [s["message_sink"] for s in data["survivors"]] == ["raise ValueError", None]
    assert data["classes"][BEHAVIOURAL] == 1 and data["classes"][STRING_ONLY] == 1
    assert "1 behavioural, 1 string-only" in text
    assert "| string-only |" in text and "| behavioural |" in text


def test_a_report_without_survivors_has_empty_classes():
    data = mutate.report(mutate.Outcome(modules=[], counts={"killed": 1}), {}, "c0ffee", "s")
    assert set(data["classes"].values()) == {0}
    text = mutate.summary(data)
    assert "string-only" not in text and "No mutant survived." in text
    assert "<details>" not in text and "| killed | 1 |" in text


def real_report(tmp_path, repo_name):
    survivors = []
    for s in REAL[repo_name]:
        record = survivor(s["id"], s["line"])
        record.update(file=s["file"], function=s["function"])
        # No "class" field: ticks.py decides from the diff itself, never from a stored label.
        record["mutation"] = {"removed": s["removed"], "added": s["added"], "diff": "-\n+"}
        record["message_sink"] = s["message_sink"]
        survivors.append(record)
    path = tmp_path / f"{repo_name}.json"
    path.write_text(json.dumps({"schema": mutate.SCHEMA, "commit": "c0ffee", "failure": None,
                                "survivors": survivors}))
    return path


def test_ticks_files_utinas_behavioural_survivors(tmp_path, capsys):
    root = sinks_root(tmp_path, f"[tool.mutation]\nmessage_sinks = {list(UTINA_SINKS)!r}\n")
    tick = tick_stub(tmp_path)
    assert ticks.main(["--report", str(real_report(tmp_path, "utina")), "--tick",
                       str(tick), "--root", str(root)]) == 0
    adds = [c[-1] for c in calls(tmp_path / "tickbin") if c[0] == "add"]
    assert sorted(a.split()[2] for a in adds) == [
        "[m:3844d16a640a]", "[m:e594c88ec0cc]", "[m:eb3b234984ca]"]
    assert "filed 3; 0 already ticked; 0 re-noted; 54 string-only survivors not ticked" in (
        capsys.readouterr().out)




# --- where a string literal's text goes -------------------------------------------------------

SINKS = """\
import logging
import warnings
log = logging.getLogger(__name__)


def f(x, d):
    raise ValueError("plain")
    raise TypeError(f"x is {type(x).__name__}")
    raise KeyError(code="e.x.f", text="keyword")
    raise ValueError("a long message "
                     "over two lines")
    raise ValueError("{} formatted".format(x))
    raise ValueError("joined " + str(x))
    raise ValueError("percent %s" % x)
    print("printed")
    logging.warning("logged")
    log.info("logged too")
    warnings.warn("warned")
    return "returned"
    re.compile("pattern")
    raise ValueError({"key": 1})
    raise ValueError("a" if x == "b" else "c")
    x = d["subscript"]
    other.warn("not warnings")
    raise ValueError("ok", d["key"])
    raise ValueError(f"{x:>{'10'}}")
    "{}".format("argument")
    raise ValueError("bound".format)
"""


@pytest.mark.parametrize("line, sink", [
    (7, "raise ValueError"), (8, "raise TypeError"), (9, "raise KeyError"),
    # 12 and 14 are format strings with placeholders: a mutation there can break formatting.
    (10, "raise ValueError"), (11, "raise ValueError"), (12, None),
    (13, "raise ValueError"), (14, None), (15, "print"),
    (16, "logging.warning"), (17, "log.info"), (18, "warnings.warn"),
    (19, None), (20, None), (21, None), (22, None), (23, None), (24, None), (25, None),
    # The f-string goes to the raise; a change to the literal inside its {...} is caught by
    # classify, which reads the replacement field as code.
    (26, "raise ValueError"), (27, None), (28, None), (1, None),
])
def test_message_sink(line, sink):
    assert mutate.message_sink(SINKS, line, line) == sink


def test_message_sink_needs_every_literal_in_the_lines_to_be_a_message():
    # Lines 15 and 16 are both messages, but to two sinks, which no one record can name.
    assert mutate.message_sink(SINKS, 15, 16) is None
    assert mutate.message_sink(SINKS, 10, 11) == "raise ValueError"
    assert mutate.message_sink(SINKS, 18, 19) is None


# --- a note that never landed, and records that are not records ------------------------------


def test_a_tick_whose_note_never_landed_gets_its_note(tmp_path, capsys):
    tick = stub(tmp_path / "tickbin", "tick", {
        "ls": "2abc  debt   mutation survivor [m:aaaaaaaaaaaa] src/pkg/mod.py:6 in saidify\n"
              "5jkl  debt   a survivor someone filed by hand, with no id\n",
        "show": "title: mutation survivor [m:aaaaaaaaaaaa]\n",
        "note": "noted on 2abc\n",
    })
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"schema": mutate.SCHEMA, "commit": "c0ffee", "failure": None,
                                "survivors": [survivor("aaaaaaaaaaaa")]}))
    assert ticks.main(["--report", str(path), "--tick", str(tick)]) == 0
    log = calls(tmp_path / "tickbin")
    assert [c[0] for c in log] == ["ls", "show", "note"]
    assert log[1] == ["show", "2abc"]
    assert log[2][1] == "2abc" and "mutant-id: aaaaaaaaaaaa" in log[2][2]
    assert "filed 0; 0 already ticked; 1 re-noted;" in capsys.readouterr().out


def test_a_retried_note_on_a_dry_run_is_only_reported(tmp_path, capsys):
    tick = stub(tmp_path / "tickbin", "tick", {
        "ls": "2abc  debt   mutation survivor [m:aaaaaaaaaaaa] src/pkg/mod.py:6 in saidify\n",
        "show": "title: ...\n",
    })
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"schema": mutate.SCHEMA, "commit": "c0ffee", "failure": None,
                                "survivors": [survivor("aaaaaaaaaaaa")]}))
    assert ticks.main(["--report", str(path), "--tick", str(tick), "--dry-run"]) == 0
    assert [c[0] for c in calls(tmp_path / "tickbin")] == ["ls", "show"]
    assert "would re-note 2abc: mutation survivor [m:aaaaaaaaaaaa]" in capsys.readouterr().out


def test_every_filed_note_carries_its_mutant_id(sample, tmp_path):
    tick = tick_stub(tmp_path)
    ticks.main(["--report", str(sample), "--tick", str(tick)])
    notes = [c for c in calls(tmp_path / "tickbin") if c[0] == "note"]
    assert "mutant-id: bbbbbbbbbbbb" in notes[0][2]
    assert "mutant-id: cccccccccccc" in notes[1][2]


def bad(record):
    return {"schema": mutate.SCHEMA, "commit": "c0ffee", "failure": None,
            "survivors": [survivor("aaaaaaaaaaaa"), record]}


@pytest.mark.parametrize("report_data, named", [
    (bad({}), "survivor 1"),
    (bad({**survivor("aaaaaaaaaaaa"), "id": "not-hex"}), "survivor 1"),
    (bad({**survivor("aaaaaaaaaaaa"), "line": "6"}), "survivor 1"),
    (bad({**survivor("aaaaaaaaaaaa"), "file": None}), "survivor 1"),
    (bad({**survivor("aaaaaaaaaaaa"), "mutation": {"removed": "a", "added": []}}),
     "survivor 1"),
    (bad({**survivor("aaaaaaaaaaaa"), "test_result": None}), "survivor 1"),
    (bad({**survivor("aaaaaaaaaaaa"), "message_sink": 3}), "survivor 1"),
    (bad("a string"), "survivor 1"),
    ({"commit": "c0ffee", "failure": None, "survivors": {}}, "the report"),
    ({"failure": None, "survivors": []}, "the report"),
    ({"schema": "bakobo.mutation-report/2", "commit": "c0ffee", "failure": None,
      "survivors": []}, "the report's schema"),
    ({"schema": mutate.SCHEMA, "failure": None, "survivors": []}, "names no commit"),
    ({"schema": mutate.SCHEMA, "commit": "c0ffee", "failure": None, "survivors": {}},
     "the report"),
    ([], "the report"),
])
def test_a_malformed_report_is_refused_with_a_code(tmp_path, capsys, report_data, named):
    tick = tick_stub(tmp_path)
    path = tmp_path / "r.json"
    path.write_text(json.dumps(report_data))
    assert ticks.main(["--report", str(path), "--tick", str(tick)]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{ticks.MALFORMED.code}: ") and named in err
    assert f"Hint: {ticks.MALFORMED.hint}" in err
    assert calls(tmp_path / "tickbin") == []


# --- an assignment is a value, not a message ------------------------------


def test_a_string_assigned_to_a_name_is_behavioural():
    source = 'def f(observed):\n    outcome = "kept"\n    return outcome\n'
    assert mutate.message_sink(source, 2, 2) is None
    assert mutate.classify(['    outcome = "kept"'], ['    outcome = "XXkeptXX"'],
                           mutate.message_sink(source, 2, 2)) == BEHAVIOURAL


# --- reports are bounded and read defensively ------------


@pytest.mark.parametrize("raw, reason", [
    (b"\xff\xfe not utf-8", "not UTF-8"),
    (b'{"schema": "bakobo.mutation-re', "not JSON"),
])
def test_an_unreadable_report_is_refused_before_the_ledger(tmp_path, capsys, raw, reason):
    tick = tick_stub(tmp_path)
    path = tmp_path / "r.json"
    path.write_bytes(raw)
    assert ticks.main(["--report", str(path), "--tick", str(tick)]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{ticks.MALFORMED.code}: ") and reason in err
    assert calls(tmp_path / "tickbin") == []


def test_an_oversized_report_is_refused_unread(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(shared, "MAX_REPORT_BYTES", 64)
    tick = tick_stub(tmp_path)
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"schema": mutate.SCHEMA, "pad": "x" * 100}))
    assert ticks.main(["--report", str(path), "--tick", str(tick)]) == 2
    assert "larger than 64 bytes" in capsys.readouterr().err
    assert calls(tmp_path / "tickbin") == []


def test_a_missing_report_is_refused(tmp_path, capsys):
    absent = str(tmp_path / "absent.json")
    assert ticks.main(["--report", absent, "--tick", "/nonexistent"]) == 2
    assert "could not be read" in capsys.readouterr().err


def test_the_report_limit_is_documented_and_generous():
    assert shared.MAX_REPORT_BYTES == 16 * 1024 * 1024
    assert "16 MiB" in ticks.__doc__


# --- ids come from the source, not from what survived -----


DIFF_TWIN_1 = """\
--- src/pkg/mod.py
+++ src/pkg/mod.py
@@ -1,4 +1,4 @@
 def twin(x):
-    y = x + 1
+    y = x - 1
     y = x + 1
     return y"""

DIFF_TWIN_2 = """\
--- src/pkg/mod.py
+++ src/pkg/mod.py
@@ -1,4 +1,4 @@
 def twin(x):
     y = x + 1
-    y = x + 1
+    y = x - 1
     return y"""


def twin_ids(repo, tmp_path, survived):
    (repo / "src" / "pkg" / "mod.py").write_text(TWIN)
    names = {1: "pkg.mod.x_twin__mutmut_1", 2: "pkg.mod.x_twin__mutmut_2"}
    spec = show_spec({names[1]: DIFF_TWIN_1 + "\n", names[2]: DIFF_TWIN_2 + "\n"})
    mutmut = stub(tmp_path / f"bin{len(survived)}", "mutmut", spec)
    return {s.mutmut_name[-1]: s.id for s in survivors_of(repo, mutmut,
                                                         [names[n] for n in sorted(survived)])}


def test_a_survivors_id_does_not_depend_on_its_twin_surviving(repo, tmp_path):
    both = twin_ids(repo, tmp_path, {1, 2})
    alone = twin_ids(repo, tmp_path, {2})
    assert both["1"] != both["2"] and alone == {"2": both["2"]}


# --- fix-diff hostile pass: failure fields and nesting ---------


@pytest.mark.parametrize("fields, named", [
    ({"failure": ""}, "failure"),
    ({"failure": 3}, "failure"),
    ({"failure": None, "failure_code": 5}, "failure_code"),
    ({"failure": None, "failure_retryable": "no"}, "failure_retryable"),
    ({"failure": None, "failure_hint": []}, "failure_hint"),
])
def test_an_invalid_failure_field_is_refused(tmp_path, capsys, fields, named):
    tick = tick_stub(tmp_path)
    data = {**bad(survivor("bbbbbbbbbbbb")), **fields}
    path = tmp_path / "r.json"
    path.write_text(json.dumps(data))
    assert ticks.main(["--report", str(path), "--tick", str(tick)]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{ticks.MALFORMED.code}: ") and named in err
    assert calls(tmp_path / "tickbin") == []


def test_a_report_without_the_optional_failure_fields_is_read(sample, tmp_path):
    # Reports written before failure_code existed carry only "failure": null.
    assert ticks.main(["--report", str(sample), "--tick", str(tick_stub(tmp_path)),
                       "--dry-run"]) == 0


def test_a_parser_recursion_error_is_a_coded_refusal(tmp_path, capsys, monkeypatch):
    # Whether real nesting overflows the parser depends on the Python build, so the overflow
    # itself is simulated here; the test below feeds real nesting and asks only for a refusal.
    def overflow(text):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(shared.json, "loads", overflow)
    path = tmp_path / "r.json"
    path.write_text("[]")
    assert ticks.main(["--report", str(path), "--tick", "/nonexistent"]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{ticks.MALFORMED.code}: ") and "nested too deeply" in err


def test_deeply_nested_json_is_refused_not_a_crash(tmp_path, capsys):
    path = tmp_path / "r.json"
    path.write_text("[" * 100_000 + "]" * 100_000)
    assert ticks.main(["--report", str(path), "--tick", "/nonexistent"]) == 2
    # Refused either as too deep for this parser or as a list rather than an object.
    assert capsys.readouterr().err.startswith(f"{ticks.MALFORMED.code}: ")


# --- message sinks a repo declares ([tool.mutation] message_sinks) ----------------------------

UTINA_SINKS = ("Refusal.missing", "Refusal.detail")


def sinks_root(tmp_path, body):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text(body)
    return root


@pytest.mark.parametrize("data, sinks", [
    ({}, ()),
    ({"tool": {"mutation": {}}}, ()),
    ({"tool": {"mutation": {"message_sinks": ["Refusal.missing", "Note"]}}},
     ("Refusal.missing", "Note")),
])
def test_declared_sinks_are_read(data, sinks):
    assert shared.message_sinks(data) == sinks


@pytest.mark.parametrize("data, problem", [
    ({"tool": {"mutation": "sinks"}}, "[tool.mutation] is not a table"),
    ({"tool": {"mutation": {"message_sinks": "Refusal"}}}, "is not a list"),
    ({"tool": {"mutation": {"message_sinks": [3]}}}, "3"),
    ({"tool": {"mutation": {"message_sinks": [""]}}}, "''"),
    ({"tool": {"mutation": {"message_sinks": ["a.b.c"]}}}, "'a.b.c'"),
    ({"tool": {"mutation": {"message_sinks": ["1x"]}}}, "'1x'"),
    ({"tool": {"mutation": {"message_sinks": ["Refusal.missing "]}}}, "'Refusal.missing '"),
])
def test_a_malformed_sink_declaration_is_refused(data, problem):
    with pytest.raises(shared.SinkConfigError, match=re.escape(problem)):
        shared.message_sinks(data)


def test_this_repos_own_declaration_is_well_formed():
    import tomllib

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    shared.message_sinks(tomllib.loads(pyproject.read_text("utf-8")))


DECLARED = """\
def f(kind, x):
    return Refusal(seal_kind="digest", missing="what is missing", detail="why")
    return Refusal(kind, "positional text", "more")
    return Refusal(missing="joined " + str(x))
    return other.Refusal(missing="attribute call")
    return Note("noted", flag="also noted")
    return Note(tag="label".upper())
"""


@pytest.mark.parametrize("line, sinks, sink", [
    (2, UTINA_SINKS, None),  # seal_kind's literal is on the line too, and it is behaviour
    (3, UTINA_SINKS, None),  # Refusal.missing names the keyword only
    (4, UTINA_SINKS, "declared Refusal.missing"),
    (5, UTINA_SINKS, None),  # the call must be spelled exactly as declared
    (6, ("Note",), "declared Note"),  # a bare name takes every string argument
    (6, ("Note.flag",), None),
    (7, ("Note",), None),  # a method call's result is not the literal itself
    (4, (), None),
])
def test_message_sink_with_declared_sinks(line, sinks, sink):
    assert mutate.message_sink(DECLARED, line, line, sinks) == sink


def test_each_keyword_of_a_declared_sink_counts_on_its_own_line():
    source = ('def f(kind):\n    return Refusal(\n        seal_kind=kind,\n'
              '        missing="what is missing",\n        detail="why",\n    )\n')
    assert mutate.message_sink(source, 4, 4, UTINA_SINKS) == "declared Refusal.missing"
    assert mutate.message_sink(source, 5, 5, UTINA_SINKS) == "declared Refusal.detail"
    assert mutate.message_sink(source, 4, 4, ()) is None


REFUSING = '''\
"""A module that refuses."""


def refuse(kind):
    return Refusal(
        seal_kind=kind,
        missing="a rule for this act",
        detail="nothing in the law governs it",
    )
'''

DIFF_REFUSE = """\
--- src/pkg/mod.py
+++ src/pkg/mod.py
@@ -1,6 +1,6 @@
 def refuse(kind):
     return Refusal(
         seal_kind=kind,
-        missing="a rule for this act",
+        missing="XXa rule for this actXX",
         detail="nothing in the law governs it",
     )"""


def refusing_survivor(repo, tmp_path, sinks=()):
    (repo / "src" / "pkg" / "mod.py").write_text(REFUSING)
    name = "pkg.mod.x_refuse__mutmut_1"
    mutmut = stub(tmp_path / "bin", "mutmut", show_spec({name: DIFF_REFUSE + "\n"}))
    (survivor_,) = survivors_of(repo, mutmut, [name], sinks)
    return survivor_


def test_mutate_reads_the_declared_sinks(repo, tmp_path):
    (repo / "pyproject.toml").write_text('[tool.mutation]\nmessage_sinks = ["Refusal.missing"]\n')
    record = refusing_survivor(repo, tmp_path, mutate.declared_sinks(repo))
    assert record.line == 7 and record.sink == "declared Refusal.missing"
    assert record.kind == STRING_ONLY


def test_without_a_declaration_the_same_survivor_is_behavioural(repo, tmp_path):
    record = refusing_survivor(repo, tmp_path)
    assert record.sink is None and record.kind == BEHAVIOURAL


@pytest.mark.parametrize("body", ['\n[tool.mutation]\nmessage_sinks = "Refusal"\n',
                                  'tool = "bad"\n'])
def test_a_malformed_declaration_is_refused_with_its_code(repo, body):
    (repo / "pyproject.toml").write_text(body)
    with pytest.raises(mutate.BakoboError) as refused:
        mutate.declared_sinks(repo)
    assert refused.value.code == mutate.SINKS_MALFORMED.code == "e.self.config.message-sinks.f"


def declared_report(tmp_path, sink):
    record = survivor("aaaaaaaaaaaa")
    record["mutation"] = {"removed": ['    missing="a rule"'],
                          "added": ['    missing="XXa ruleXX"'], "diff": "-\n+"}
    record["message_sink"] = sink
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"schema": mutate.SCHEMA, "commit": "c0ffee", "failure": None,
                                "survivors": [record]}))
    return path


def test_ticks_honours_a_sink_this_repo_declares(tmp_path, capsys):
    root = sinks_root(tmp_path, '[tool.mutation]\nmessage_sinks = ["Refusal.missing"]\n')
    tick = stub(tmp_path / "tickbin", "tick", {"ls": "(no ticks)\n"})
    path = declared_report(tmp_path, "declared Refusal.missing")
    assert ticks.main(["--report", str(path), "--tick", str(tick), "--root", str(root)]) == 0
    assert [c[0] for c in calls(tmp_path / "tickbin")] == ["ls"]
    assert "1 string-only survivors not ticked" in capsys.readouterr().out


def test_ticks_files_a_survivor_whose_sink_is_no_longer_declared(tmp_path):
    # The report and the repo must agree: a sink the repo does not declare vouches for nothing.
    root = sinks_root(tmp_path, '[project]\nname = "x"\n')
    tick = stub(tmp_path / "tickbin", "tick", {"ls": "(no ticks)\n", "add": "4ghi  t\n"})
    path = declared_report(tmp_path, "declared Refusal.missing")
    assert ticks.main(["--report", str(path), "--tick", str(tick), "--root", str(root)]) == 0
    assert [c[0] for c in calls(tmp_path / "tickbin")] == ["ls", "add", "note"]


def test_ticks_refuses_a_malformed_declaration_before_the_ledger(tmp_path, capsys):
    root = sinks_root(tmp_path, '[tool.mutation]\nmessage_sinks = [1]\n')
    tick = tick_stub(tmp_path)
    path = declared_report(tmp_path, "declared Refusal.missing")
    assert ticks.main(["--report", str(path), "--tick", str(tick), "--root", str(root)]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{mutate.SINKS_MALFORMED.code}: ")
    assert f"Hint: {mutate.SINKS_MALFORMED.hint}" in err
    assert calls(tmp_path / "tickbin") == []


def test_ticks_refuses_an_unreadable_pyproject(tmp_path, capsys):
    root = sinks_root(tmp_path, "this is = not [toml")
    path = declared_report(tmp_path, None)
    assert ticks.main(["--report", str(path), "--tick", "/nonexistent", "--root",
                       str(root)]) == 2
    assert capsys.readouterr().err.startswith(f"{mutate.SINKS_MALFORMED.code}: ")


def test_ticks_finds_the_repo_root_by_default():
    # Installed as a tool, the module sits in no repo, so the repo is the one it is run from.
    assert ticks.parse([]).root == Path.cwd()


# --- second hostile pass on message sinks ----------------------------------------------------


@pytest.mark.parametrize("removed, added", [
    (['    return Refusal(missing="a rule")'], ['    return Refusal(missing="")']),
    (['    return Refusal(missing="")'], ['    return Refusal(missing="XXXX")']),
    (["    return Refusal(missing='a rule')"], ["    return Refusal(missing=b'a rule')"]),
])
def test_emptying_or_filling_a_literal_is_behavioural(removed, added):
    # Validators refuse empty text, so whether a literal is empty is behaviour (utina#15 1).
    assert mutate.classify(removed, added, "declared Refusal.missing") == BEHAVIOURAL


def test_a_raw_literal_changing_case_is_still_string_only():
    assert mutate.classify(['    raise E(r"a rule")'], ['    raise E(r"A RULE")'],
                           "raise E") == STRING_ONLY


FORMATTED = """\
def f(x, fmt):
    raise ValueError("missing %s" % x)
    raise ValueError("missing {}".format(x))
    raise ValueError("100 percent" % ())
    raise ValueError("no braces".format())
    raise ValueError(fmt % "an argument")
    raise ValueError(("a %s" + "b") % x)
    return Refusal(missing="ground %(name)s" % {"name": x})
"""


@pytest.mark.parametrize("line, sink", [
    (2, None), (3, None),  # a format string with placeholders is behaviour (utina#15 2)
    (4, "raise ValueError"), (5, "raise ValueError"),  # no placeholder to break
    (6, "raise ValueError"),  # an argument to % is the text that gets substituted
    (7, None),
    (8, None),
])
def test_a_format_string_with_placeholders_is_not_message_text(line, sink):
    assert mutate.message_sink(FORMATTED, line, line, UTINA_SINKS) == sink


@pytest.mark.parametrize("removed, added", [
    (["""    return Refusal(missing=f"{grant('admin')}")"""],
     ["""    return Refusal(missing=f"{grant('XXadminXX')}")"""]),
    (['    raise E(f"{x:>10}")'], ['    raise E(f"{x:>20}")']),
    (['    raise E(f"a {f(\'b\')} c")'], ['    raise E(f"a {f(\'B\')} c")']),
])
def test_a_literal_inside_an_fstring_expression_is_behavioural(removed, added):
    # The replacement field is code; a literal there is an argument, not text.
    assert mutate.classify(removed, added, "raise E") == BEHAVIOURAL


def test_fstring_text_around_an_expression_is_still_string_only():
    assert mutate.classify(['    raise E(f"a {f(\'b\')} c")'],
                           ['    raise E(f"A {f(\'b\')} C")'],
                           "raise E") == STRING_ONLY


def test_a_raise_through_a_computed_callable_is_no_sink():
    assert mutate.message_sink('def f(m):\n    raise m.make()("text")\n', 2, 2) is None


@pytest.mark.parametrize("sink, known", [
    (None, True), ("print", True), ("warnings.warn", True), ("logging.warning", True),
    ("log.info", True), ("raise ValueError", True), ("raise errors.Refused", True),
    ("declared Refusal.missing", True), ("declared Note", True),
    ("Refusal.missing", False), ("declared a.b.c", False), ("declared ", False),
    ("raise make()", False), ("raise ", False), ("warnings.info", False),
    ("logger.send", False), ("printf", False), (3, False),
])
def test_known_sink_forms(sink, known):
    assert shared.known_sink(sink) is known


def test_the_report_validator_refuses_an_unknown_sink(tmp_path, capsys):
    tick = tick_stub(tmp_path)
    path = declared_report(tmp_path, "Refusal.missing")
    assert ticks.main(["--report", str(path), "--tick", str(tick)]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{ticks.MALFORMED.code}: ") and "message_sink" in err
    assert calls(tmp_path / "tickbin") == []


@pytest.mark.parametrize("sink, sinks, vouched", [
    ("raise ValueError", (), "raise ValueError"),
    ("declared Refusal.missing", ("Refusal.missing",), "declared Refusal.missing"),
    ("declared Refusal.missing", (), None),
    ("Refusal.missing", ("Refusal.missing",), None),
    (None, (), None),
])
def test_ticks_rechecks_every_sink(sink, sinks, vouched):
    assert ticks.vouched(sink, sinks) == vouched


@pytest.mark.parametrize("body, problem", [
    ('tool = "bad"\n', "[tool] is not a table"),
    (b"[tool.mutation]\nmessage_sinks = ['\xff']\n", "utf-8"),
])
def test_every_malformed_config_is_a_coded_refusal(tmp_path, capsys, body, problem):
    root = tmp_path / "root"
    root.mkdir()
    pyproject = root / "pyproject.toml"
    if isinstance(body, bytes):
        pyproject.write_bytes(body)
    else:
        pyproject.write_text(body)
    path = declared_report(tmp_path, None)
    assert ticks.main(["--report", str(path), "--tick", "/nonexistent", "--root",
                       str(root)]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{mutate.SINKS_MALFORMED.code}: ") and problem in err


def test_a_literal_python_cannot_evaluate_is_behavioural():
    assert mutate.classify(['    raise E("\\N{NO SUCH NAME} a")'],
                           ['    raise E("\\N{NO SUCH NAME} b")'], "raise E") == BEHAVIOURAL


# --- Copilot on message sinks ----------------------------------------------------------------


@pytest.mark.parametrize("entry", ["for", "None", "Refusal.for", "class.missing", "True"])
def test_a_reserved_word_is_no_sink_name(entry):
    # Each component must be an identifier and not a keyword (utina 4191350233).
    with pytest.raises(shared.SinkConfigError, match=re.escape(repr(entry))):
        shared.message_sinks({"tool": {"mutation": {"message_sinks": [entry]}}})
    assert not shared.known_sink(f"declared {entry}")


def test_soft_keywords_and_unicode_identifiers_are_names():
    assert shared.message_sinks({"tool": {"mutation": {"message_sinks": [
        "match.case", "Réfus"]}}}) == ("match.case", "Réfus")


def test_an_oversized_pyproject_is_refused_before_it_is_decoded(tmp_path, capsys,
                                                                 monkeypatch):
    # Read at most the limit plus one byte.
    monkeypatch.setattr(shared, "MAX_PYPROJECT_BYTES", 64)
    root = sinks_root(tmp_path, "# " + "x" * 100 + "\n")
    path = declared_report(tmp_path, None)
    assert ticks.main(["--report", str(path), "--tick", "/nonexistent", "--root",
                       str(root)]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{mutate.SINKS_MALFORMED.code}: ") and "larger than 64 bytes" in err


def test_the_pyproject_limit_is_generous():
    assert shared.MAX_PYPROJECT_BYTES == 1024 * 1024


def test_a_deeply_nested_pyproject_is_a_coded_refusal(tmp_path, capsys, monkeypatch):
    # hostile pass on the message-sinks PRs: tomllib's RecursionError escaped the coded refusal.
    def too_deep(text):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(shared.tomllib, "loads", too_deep)
    root = sinks_root(tmp_path, "[tool.mutation]\nmessage_sinks = []\n")
    path = declared_report(tmp_path, None)
    assert ticks.main(["--report", str(path), "--tick", "/nonexistent", "--root",
                       str(root)]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{mutate.SINKS_MALFORMED.code}: ") and "nested too deeply" in err


def test_a_missing_pyproject_is_a_coded_refusal(tmp_path, capsys):
    root = tmp_path / "empty"
    root.mkdir()
    path = declared_report(tmp_path, None)
    assert ticks.main(["--report", str(path), "--tick", "/nonexistent", "--root",
                       str(root)]) == 2
    assert capsys.readouterr().err.startswith(f"{mutate.SINKS_MALFORMED.code}: ")


MIXED = """\
def f(x):
    print("a"); log.info("b")
    print("a", "b")
    raise ValueError("a" + str(print("b")))
"""


@pytest.mark.parametrize("line, sink", [
    (2, None),  # two sinks on one line: no single one vouches for both
    (3, "print"),
    (4, None),
])
def test_literals_reaching_different_sinks_are_not_message_text(line, sink):
    assert mutate.message_sink(MIXED, line, line) == sink
