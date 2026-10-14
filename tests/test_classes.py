"""The diagnostic class (this.i 2zhn29rr) and the dotted-callee message sink form.

A survivor whose mutation lies wholly inside the arguments of the exception a ``raise``
constructs, or in its ``from`` cause, changes only what the refusal says, not whether it
happens: it is counted and never ticked. Anything the AST cannot vouch for stays behavioural.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from bakobo.errors import BakoboError  # type: ignore[import-untyped]
from conftest import git

from bakobo_mutate import mutate, shared, ticks

SOURCE = '''\
def f(x, d, err):
    if x:
        raise ValueError(f"bad {x}", code="e.x.f")
    raise KeyError("missing") from err
    raise errors.Refused(detail="why",
                         field="name")
    y = 1
    raise TypeError(x)
    raise err
    raise make("kind")(x)
    return d


class C:
    def m(self):
        raise ValueError("a", self.x)
'''


@pytest.mark.parametrize("line, removed, added", [
    (3, ['        raise ValueError(f"bad {x}", code="e.x.f")'],
     ['        raise ValueError(None, code="e.x.f")']),
    (3, ['        raise ValueError(f"bad {x}", code="e.x.f")'],
     ['        raise ValueError(f"bad {x}", code="XXe.x.fXX")']),
    (3, ['        raise ValueError(f"bad {x}", code="e.x.f")'],
     ['        raise ValueError(f"bad {None}", code="e.x.f")']),
    (3, ['        raise ValueError(f"bad {x}", code="e.x.f")'],
     ['        raise ValueError(f"bad {x}", )']),
    (4, ['    raise KeyError("missing") from err'], ['    raise KeyError("missing") from None']),
    (6, ['                         field="name")'], ['                         field=None)']),
    # A method's diff is dedented by mutmut; the file's own indentation is restored.
    (16, ['    raise ValueError("a", self.x)'], ['    raise ValueError("a", None)']),
], ids=["positional", "keyword", "fstring-field", "dropped-keyword", "from-cause",
        "continuation-line", "method"])
def test_a_change_inside_a_raises_arguments_is_diagnostic(line, removed, added):
    assert shared.diagnostic(SOURCE, line, removed, added)


@pytest.mark.parametrize("line, removed, added", [
    (8, ['    raise TypeError(x)'], ['    raise ValueError(x)']),  # which exception
    (8, ['    raise TypeError(x)'], ['    pass']),  # the raise removed
    (7, ['    y = 1'], ['    y = 2']),  # the line after a raise
    (9, ['    raise err'], ['    raise None']),  # nothing constructed
    (10, ['    raise make("kind")(x)'], ['    raise make("XXkindXX")(x)']),  # the callee's own
    (4, ['    raise KeyError("missing") from err'], ['    raise KeyError("missing")']),
    (3, [], ['        pass']),  # removes nothing
    (3, ['        raise OtherError()'], ['        raise OtherError(1)']),  # not the source
    (99, ['x'], ['y']),  # beyond the file
    (0, ['x'], ['y']),
    (3, ['        raise ValueError(f"bad {x}", code="e.x.f")'], ['        raise ValueError(']),
    (3, ['        raise ValueError(f"bad {x}", code="e.x.f")'],
     ['        raise ValueError(f"bad {x}", code="e.x.f")']),  # no change at all
    (2, ['    if x:', '        raise ValueError(f"bad {x}", code="e.x.f")'],
     ['    if not x:', '        raise ValueError(None, code="e.x.f")']),  # partly outside
    (16, ['          raise ValueError("a", self.x)'], ['          raise ValueError("a", 1)']),
], ids=["callee", "removed", "after", "bare", "callee-args", "cause-dropped", "nothing-removed",
        "mismatch", "beyond", "line-zero", "unparsable", "unchanged", "partly-outside",
        "indent-mismatch"])
def test_anything_else_is_behavioural(line, removed, added):
    assert not shared.diagnostic(SOURCE, line, removed, added)


def test_unparsable_source_is_behavioural():
    assert not shared.diagnostic("def (:\n", 1, ["def (:"], ["def ):"])


# --- classification of a survivor -------------------------------------------------------------


@pytest.mark.parametrize("removed, added, sink, kind", [
    (['    raise ValueError("a", self.x)'], ['    raise ValueError("XXaXX", self.x)'],
     "raise ValueError", shared.STRING_ONLY),
    (['    raise ValueError("a", self.x)'], ['    raise ValueError("a", None)'], None,
     shared.DIAGNOSTIC),
    (['    raise ValueError("a", self.x)'], ['    raise TypeError("a", self.x)'], None,
     shared.BEHAVIOURAL),
], ids=["string-only-wins", "diagnostic", "behavioural"])
def test_survivor_class(removed, added, sink, kind):
    assert shared.survivor_class(SOURCE, 16, removed, added, sink) == kind


def test_without_source_nothing_is_diagnostic():
    assert shared.survivor_class(None, 16, ['    raise ValueError("a", self.x)'],
                                 ['    raise ValueError("a", None)'], None) == shared.BEHAVIOURAL


def test_the_report_schema_is_bumped_for_the_new_class():
    assert shared.SCHEMA == "bakobo.mutation-report/2"


def test_the_report_and_summary_count_diagnostic_survivors():
    def survivor(kind, id_):
        return mutate.Survivor(id=id_, mutmut_name="m", file="f.py", line=1, function="f",
                               removed=["a"], added=["b"], diff="-a\n+b", tests=1, kind=kind)

    outcome = mutate.Outcome(modules=["f.py"], survivors=[
        survivor(shared.DIAGNOSTIC, "a" * 12), survivor(shared.BEHAVIOURAL, "b" * 12)])
    data = mutate.report(outcome, {}, "c0ffee", "scope")
    assert data["classes"] == {"behavioural": 1, "string-only": 0, "diagnostic": 1}
    text = mutate.summary(data)
    assert "1 behavioural, 0 string-only, 1 diagnostic" in text and "| diagnostic |" in text


# --- ticks decides from the source at the report's commit -------------------------------------


@pytest.fixture
def ledger_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "f.py").write_text(SOURCE)
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    return root, git(root, "rev-parse", "HEAD").strip()


def record(id_, line, removed, added, file="src/f.py"):
    return {"id": id_, "mutmut_name": "m.x_f__mutmut_1", "file": file, "line": line,
            "function": "f", "mutation": {"removed": removed, "added": added, "diff": "-\n+"},
            "message_sink": None,
            "test_result": {"command": "pytest", "tests_run": 1, "exit_code": 0}}


TICK = """\
#!/bin/sh
echo "$@" >> "$(dirname "$0")/tick.log"
case "$1" in
  ls) echo "(no ticks)";;
  add) echo "4ghi  filed";;
esac
"""


def test_ticks_counts_diagnostic_survivors_and_files_the_rest(ledger_repo, tmp_path, capsys):
    root, commit = ledger_repo
    tick = tmp_path / "bin" / "tick"
    tick.parent.mkdir()
    tick.write_text(TICK)
    tick.chmod(0o755)
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"schema": shared.SCHEMA, "commit": commit, "failure": None,
                                  "survivors": [
        record("a" * 12, 16, ['    raise ValueError("a", self.x)'],
               ['    raise ValueError("a", None)']),
        record("b" * 12, 8, ['    raise TypeError(x)'], ['    raise ValueError(x)']),
        # The file is gone at that commit, so nothing can vouch for it.
        record("c" * 12, 16, ['    raise ValueError("a", self.x)'],
               ['    raise ValueError("a", None)'], file="src/gone.py"),
    ]}))
    assert ticks.main(["--root", str(root), "--report", str(report), "--tick", str(tick)]) == 0
    adds = [line for line in (tick.parent / "tick.log").read_text().splitlines()
            if line.startswith("add")]
    assert len(adds) == 2 and "[m:bbbbbbbbbbbb]" in adds[0] and "[m:cccccccccccc]" in adds[1]
    assert ("0 string-only and 1 diagnostic survivors not ticked"
            in capsys.readouterr().out)


def test_the_source_is_read_once_per_file(ledger_repo, monkeypatch):
    root, commit = ledger_repo
    seen = []
    real = subprocess.run

    def counting(argv, **kwargs):
        seen.append(argv)
        return real(argv, **kwargs)

    monkeypatch.setattr(ticks.subprocess, "run", counting)
    sources = ticks.Sources(root, commit)
    assert sources.get("src/f.py") == SOURCE and sources.get("src/f.py") == SOURCE
    assert sources.get("src/gone.py") is None
    assert len(seen) == 2


# --- a dotted callee declared with () ---------------------------------------------------------


@pytest.mark.parametrize("entry", ["console.out.write()", "session.console.out.write()",
                                   "lines.append()", "note()"])
def test_a_dotted_callee_entry_is_accepted(entry):
    assert shared.message_sinks({"tool": {"mutation": {"message_sinks": [entry]}}}) == (entry,)
    assert shared.known_sink(f"declared {entry}")


@pytest.mark.parametrize("entry", ["a..b()", "()", "1a.b()", "if.x()", "a.b.c", "a.b()()",
                                   "a.b( )", "a.()"])
def test_a_malformed_dotted_callee_is_refused(entry, tmp_path):
    with pytest.raises(shared.SinkConfigError):
        shared.message_sinks({"tool": {"mutation": {"message_sinks": [entry]}}})
    assert not shared.known_sink(f"declared {entry}")
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.mutation]\nmessage_sinks = ["{entry}"]\n')
    with pytest.raises(BakoboError) as refused:
        mutate.declared_sinks(tmp_path)
    assert refused.value.code == mutate.SINKS_MALFORMED.code


UI = '''\
def show(console, session, lines, x):
    console.out.write("shown")
    session.console.out.write("shown too")
    lines.append("a line")
    x.write("not the console")
    console.out.write(f"{x} left")
    console.out.flush("nope")
'''
DOTTED = ("console.out.write()", "session.console.out.write()", "lines.append()")


@pytest.mark.parametrize("line, sink", [
    (2, "declared console.out.write()"), (3, "declared session.console.out.write()"),
    (4, "declared lines.append()"), (5, None), (6, "declared console.out.write()"), (7, None),
])
def test_a_dotted_callee_matches_only_its_exact_spelling(line, sink):
    assert shared.message_sink(UI, line, line, DOTTED) == sink


def test_a_dotted_callee_vouches_in_ticks_only_while_declared():
    sink = "declared console.out.write()"
    assert ticks.vouched(sink, DOTTED) == sink
    assert ticks.vouched(sink, ("lines.append()",)) is None


def test_a_dotted_callee_makes_its_survivor_string_only():
    removed, added = ['    console.out.write("shown")'], ['    console.out.write("XXshownXX")']
    sink = shared.message_sink(UI, 2, 2, DOTTED)
    assert shared.classify(removed, added, sink) == shared.STRING_ONLY


def test_the_sinks_hint_names_the_dotted_form():
    assert "console.out.write()" in mutate.SINKS_MALFORMED.hint
