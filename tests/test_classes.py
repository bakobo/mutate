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
    (4, ['    raise KeyError("missing") from err'], ['    raise KeyError("missing") from None']),
    # A method's diff is dedented by mutmut; the file's own indentation is restored.
    (16, ['    raise ValueError("a", self.x)'], ['    raise ValueError("a", None)']),
], ids=["positional", "keyword", "from-cause", "method"])
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
    # Not an inert change: the field's value is no longer formatted, so its __format__ no
    # longer runs and whatever it raised no longer escapes.
    (3, ['        raise ValueError(f"bad {x}", code="e.x.f")'],
     ['        raise ValueError(f"bad {None}", code="e.x.f")']),
    # A constructor's keywords are its own contract, which the AST cannot see: dropping one
    # or making it None can make the construction itself raise.
    (3, ['        raise ValueError(f"bad {x}", code="e.x.f")'],
     ['        raise ValueError(f"bad {x}", )']),
    (6, ['                         field="name")'], ['                         field=None)']),
], ids=["callee", "removed", "after", "bare", "callee-args", "cause-dropped", "nothing-removed",
        "mismatch", "beyond", "line-zero", "unparsable", "unchanged", "partly-outside",
        "indent-mismatch", "fstring-field", "dropped-keyword", "continuation-line"])
def test_anything_else_is_behavioural(line, removed, added):
    assert not shared.diagnostic(SOURCE, line, removed, added)


def test_unparsable_source_is_behavioural():
    assert not shared.diagnostic("def (:\n", 1, ["def (:"], ["def ):"])


# A difference inside a raise's arguments is diagnostic only when it is inert: it changes the
# text of a str constant, replaces a whole argument, keyword value or cause with None, or drops
# keywords. Anything else can change which exception escapes, not only what it says.
INERT = '''\
def g(x, d, err):
    raise ValueError(1 / 0)
    raise ValueError(x[0])
    raise ValueError(x)
    raise ValueError("{} bad".format(x))
    raise ValueError("bad", code="e.x.f")
    raise ValueError(f"bad {x} here")
    raise ValueError(x, code=d, field="f")
    raise KeyError("missing") from err
    raise ValueError("%s bad" % x)
    raise ValueError(f"{x:>3}")
    raise ValueError(x, x)
    raise ValueError(x, code=d)
    raise ValueError(d["k"])
    raise ValueError(int("3"))
    raise ValueError("bad " + x)
    raise ValueError("%c" % "A")
    raise ValueError("bad {}".format("a"))
    raise MyError(x)
    raise UnicodeDecodeError(x, x, x, x, x)
    raise KeyError("missing") from f(x)
    raise ValueError(x.y.z, f"at {x.y}" + "!")
    raise dict(x)
    raise errors.Refused(x)
'''


@pytest.mark.parametrize("line, removed, added", [
    (2, ['    raise ValueError(1 / 0)'], ['    raise ValueError(1 * 0)']),
    (3, ['    raise ValueError(x[0])'], ['    raise ValueError(x[1])']),
    (4, ['    raise ValueError(x)'], ['    raise ValueError(d)']),
    (5, ['    raise ValueError("{} bad".format(x))'],
     ['    raise ValueError("XX{} badXX".format(x))']),
    (10, ['    raise ValueError("%s bad" % x)'], ['    raise ValueError("XX%s badXX" % x)']),
    (11, ['    raise ValueError(f"{x:>3}")'], ['    raise ValueError(f"{x:XX>3XX}")']),
    (7, ['    raise ValueError(f"bad {x} here")'], ['    raise ValueError(f"bad {d} here")']),
    (12, ['    raise ValueError(x, x)'], ['    raise ValueError(x)']),
    (8, ['    raise ValueError(x, code=d, field="f")'],
     ['    raise ValueError(x, code=d, field=d)']),
    (13, ['    raise ValueError(x, code=d)'], ['    raise ValueError(x, other=d)']),
    # A str that reaches the argument other than through +, %, .format or an f-string's text is
    # a key, an argument or a pattern, and changing it can change which exception escapes.
    (14, ['    raise ValueError(d["k"])'], ['    raise ValueError(d["XXkXX"])']),
    (15, ['    raise ValueError(int("3"))'], ['    raise ValueError(int("XX3XX"))']),
    (16, ['    raise ValueError("bad " + x)'], ['    raise ValueError("bad " + y)']),
    (12, ['    raise ValueError(x, x)'], ['    raise ValueError(x, 1)']),
    # The right of % and the arguments of .format are values being formatted: "%c" % "AA"
    # raises TypeError where "%c" % "A" did not.
    (17, ['    raise ValueError("%c" % "A")'], ['    raise ValueError("%c" % "AA")']),
    (18, ['    raise ValueError("bad {}".format("a"))'],
     ['    raise ValueError("bad {}".format("XXaXX"))']),
    # None in place of an argument removes its evaluation, which is inert only when that
    # evaluation could not raise and the constructor is a builtin exception that takes None.
    (2, ['    raise ValueError(1 / 0)'], ['    raise ValueError(None)']),
    (19, ['    raise MyError(x)'], ['    raise MyError(None)']),
    (20, ['    raise UnicodeDecodeError(x, x, x, x, x)'],
     ['    raise UnicodeDecodeError(x, None, x, x, x)']),
    (21, ['    raise KeyError("missing") from f(x)'], ['    raise KeyError("missing") from None']),
    (23, ['    raise dict(x)'], ['    raise dict(None)']),
    (24, ['    raise errors.Refused(x)'], ['    raise errors.Refused(None)']),
    (5, ['    raise ValueError("{} bad".format(x))'], ['    raise ValueError(None)']),
    (10, ['    raise ValueError("%s bad" % x)'], ['    raise ValueError(None)']),
    (8, ['    raise ValueError(x, code=d, field="f")'], ['    raise ValueError(x, field="f")']),
    (8, ['    raise ValueError(x, code=d, field="f")'],
     ['    raise ValueError(x, code=None, field="f")']),
], ids=["operator", "number", "name", "format-receiver-braces", "percent-left", "format-spec",
        "fstring-name", "positional-dropped", "keyword-to-name",
        "keyword-renamed", "subscript-key", "call-argument", "concatenated-name",
        "argument-to-number", "percent-right", "format-argument", "unsafe-arg-none",
        "custom-exception-none", "unicode-decode-none", "unsafe-cause-none", "not-an-exception",
        "dotted-callee-none", "formatted-arg-none", "percent-arg-none", "keyword-dropped",
        "keyword-none"])
def test_a_behavioural_change_inside_a_raises_arguments_is_not_diagnostic(line, removed, added):
    assert not shared.diagnostic(INERT, line, removed, added)


@pytest.mark.parametrize("line, removed, added", [
    (6, ['    raise ValueError("bad", code="e.x.f")'],
     ['    raise ValueError("XXbadXX", code="e.x.f")']),
    (7, ['    raise ValueError(f"bad {x} here")'], ['    raise ValueError(f"XXbad {x} hereXX")']),
    (4, ['    raise ValueError(x)'], ['    raise ValueError(None)']),
    (9, ['    raise KeyError("missing") from err'], ['    raise KeyError("missing") from None']),
    (22, ['    raise ValueError(x.y.z, f"at {x.y}" + "!")'],
     ['    raise ValueError(None, f"at {x.y}" + "!")']),
    (22, ['    raise ValueError(x.y.z, f"at {x.y}" + "!")'], ['    raise ValueError(x.y.z, None)']),
    (16, ['    raise ValueError("bad " + x)'], ['    raise ValueError("XXbad XX" + x)']),
], ids=["message", "fstring-literal", "arg-none", "cause-none", "concatenated",
        "dotted-arg-none", "built-arg-none"])
def test_an_inert_change_inside_a_raises_arguments_is_diagnostic(line, removed, added):
    assert shared.diagnostic(INERT, line, removed, added)


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
    assert ticks.main([str(root), "--report", str(report), "--tick", str(tick)]) == 0
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
