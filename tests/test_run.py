"""``bakobo-mutate run`` end to end against a fake mutmut.

git is real (a bare origin and a clone as the target); uv is a recorder; mutmut is a fake that
keeps .meta files the way mutmut 3.8 does: generation keeps cached verdicts and resets the ones
the spec says changed, and a named run re-checks every mutant it names, saving each verdict as
it finishes. The integration module pins those behaviours against the real mutmut.
"""

from __future__ import annotations

import json
import os
import runpy
import signal
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from conftest import git, push_change, recorded, recorder

from bakobo_mutate import checkout, lock, meta, mutate

SEP = "ǁ"
SENTINEL = "__bakobo_mutate_generate_only__"

MODULE = '''\
"""A module to mutate."""


def saidify(sad, label="d"):
    if label not in sad:
        raise ValueError("no label")
    return dict(sad)


class Box:
    @staticmethod
    def open(x):
        return x + 1
'''
OTHER = "def other(x):\n    return x * 2\n"

DIFF_SAIDIFY = """\
--- src/pkg/mod.py
+++ src/pkg/mod.py
@@ -1,4 +1,4 @@
 def saidify(sad, label="d"):
-    if label not in sad:
+    if label in sad:
         raise ValueError("no label")
     return dict(sad)"""

DIFF_OPEN = """\
--- src/pkg/mod.py
+++ src/pkg/mod.py
@@ -1,3 +1,3 @@
 @staticmethod
 def open(x):
-    return x + 1
+    return x - 1"""

S1, S2, S3 = (f"pkg.mod.x_saidify__mutmut_{n}" for n in (1, 2, 3))
O1, O2 = f"pkg.mod.x{SEP}Box{SEP}open__mutmut_1", f"pkg.mod.x{SEP}Box{SEP}open__mutmut_2"
T1 = "pkg.other.x_other__mutmut_1"
MUTANTS = {"src/pkg/mod.py": [S1, S2, S3, O1, O2], "src/pkg/other.py": [T1]}
VERDICTS = {S1: 0, S2: 1, S3: 1, O1: 0, O2: 1, T1: 1}

FAKE = """\
#!{python}
import fnmatch, json, signal, sys, time
from pathlib import Path
spec_path = Path({spec!r})
spec = json.loads(spec_path.read_text())
with spec_path.with_name("calls.log").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
command, args = sys.argv[1], sys.argv[2:]
if command == "show":
    sys.stdout.write(spec["show"].get(args[0], ""))
    sys.exit(spec.get("show_exit", 0))
if command == "tests-for-mutant":
    sys.stdout.write("tests/t.py::t1\\ntests/t.py::t2\\n")
    sys.exit(spec.get("tests_exit", 0))
behaviour = spec.get("run", {{}})
if behaviour.get("generating"):
    spec_path.with_name("generating").touch()
    time.sleep(behaviour["generating"])
metas = {{}}
for path, keys in spec["mutants"].items():
    meta = Path("mutants") / (path + ".meta")
    old = json.loads(meta.read_text())["exit_code_by_key"] if meta.exists() else {{}}
    codes = {{k: None if k in spec.get("reset", []) else old.get(k) for k in keys}}
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({{"exit_code_by_key": codes}}))
    metas[path] = (meta, codes)
if behaviour.get("corrupt"):
    next(iter(metas.values()))[0].write_text("not json")
matched = [(p, k) for p, (_, codes) in metas.items() for k in codes
           if k in args or any(fnmatch.fnmatch(k, a) for a in args)]
if not matched:
    print("AssertionError: Filtered for specific mutants, but nothing matches")
    sys.exit(1)
if behaviour.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
spec_path.with_name("started").touch()
if "exit" in behaviour:
    print(behaviour.get("out", ""))
    sys.exit(behaviour["exit"])
for path, key in matched:
    time.sleep(behaviour.get("per_mutant", 0))
    meta, codes = metas[path]
    codes[key] = spec["verdicts"].get(key, 1)
    meta.write_text(json.dumps({{"exit_code_by_key": codes}}))
    spec_path.with_name("progress").touch()
print("done")
"""


class Fake:
    def __init__(self, directory: Path):
        self.dir = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "mutmut"
        self.path.write_text(FAKE.format(python=sys.executable,
                                         spec=str(directory / "spec.json")))
        self.path.chmod(0o755)
        self.spec = {"mutants": MUTANTS, "verdicts": dict(VERDICTS),
                     "show": {S1: f"# {S1}: survived\n{DIFF_SAIDIFY}\n", O1: DIFF_OPEN + "\n"}}
        self.save()

    def save(self):
        (self.dir / "spec.json").write_text(json.dumps(self.spec))

    def set(self, **changes):
        self.spec.update(changes)
        self.save()

    def calls(self) -> list[list[str]]:
        log = self.dir / "calls.log"
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    def runs(self) -> list[list[str]]:
        return [c[1:] for c in self.calls() if c[0] == "run"]

    def clear(self):
        (self.dir / "calls.log").unlink(missing_ok=True)


@pytest.fixture
def target(origin):
    _, target, seed = origin
    (seed / "pyproject.toml").write_text(textwrap.dedent("""\
        [tool.mutmut]
        source_paths = ["src/pkg"]
        pytest_add_cli_args = ["-n0", "--no-cov"]
        """))
    (seed / "src" / "pkg").mkdir(parents=True)
    (seed / "src" / "pkg" / "other.py").write_text(OTHER)
    push_change(seed, "src/pkg/mod.py", MODULE, "the package")
    return target, seed


@pytest.fixture
def fake(tmp_path):
    return Fake(tmp_path / "fake")


@pytest.fixture
def uv(tmp_path):
    return recorder(tmp_path / "uvbin", "uv", {"sync": {}})


def run(target, fake, uv, *extra) -> int:
    return mutate.main([str(target), "--mutmut", str(fake.path), "--uv", str(uv), *extra])


def outputs(target) -> tuple[dict, str, dict]:
    out = target / ".ignored" / "mutation"
    return (json.loads((out / "report.json").read_text()), (out / "summary.md").read_text(),
            json.loads((out / "stamp.json").read_text()))


def origin_head(target) -> str:
    return git(target, "rev-parse", "origin/main").strip()


# --- a first run, and the runs after it -------------------------------------------------------


def test_a_first_run_checks_every_mutant_in_the_window(target, fake, uv, capsys):
    target, _ = target
    assert run(target, fake, uv) == 0
    gen, named = fake.runs()
    assert gen == [SENTINEL]
    assert named == ["pkg.mod.x_saidify__mutmut_*", f"pkg.mod.x{SEP}Box{SEP}open__mutmut_*",
                     "pkg.other.x_other__mutmut_*"]
    report, summary, stamp = outputs(target)
    assert report["schema"] == mutate.SCHEMA and report["commit"] == origin_head(target)
    assert report["since"] == "modules changed since 7 days ago"
    assert report["modules"] == ["src/pkg/mod.py", "src/pkg/other.py"]
    assert report["complete"] is True and report["failure"] is None
    assert report["counts"] == {"killed": 4, "survived": 2}
    first, second = report["survivors"]
    assert (first["mutmut_name"], first["line"], first["function"]) == (S1, 5, "saidify")
    assert (second["mutmut_name"], second["line"]) == (O1, 13)
    assert first["test_result"]["command"] == "pytest -n0 --no-cov"
    assert first["test_result"]["tests_run"] == 2
    assert summary.startswith("# Mutation testing\n") and "(pilot)" not in summary
    assert "## 2 surviving mutants" in summary and "modules changed since 7 days ago: 2" in summary
    assert stamp["base"] == origin_head(target)
    assert stamp["modules"] == {m: {"commit": origin_head(target), "complete": True}
                                for m in ("src/pkg/mod.py", "src/pkg/other.py")}
    log = (target / ".ignored" / "mutation" / "mutmut-run.log").read_text()
    assert "nothing matches" in log and "done" in log
    assert "## 2 surviving mutants" in capsys.readouterr().out


def test_the_checkout_is_prepared_and_synced(target, fake, uv):
    target, _ = target
    run(target, fake, uv)
    wt = target / ".worktrees" / "mutation"
    assert git(wt, "rev-parse", "HEAD").strip() == origin_head(target)
    assert recorded(uv.parent, "uv") == [{"argv": ["sync", "--frozen", "--all-extras"],
                                          "cwd": str(wt)}]
    assert (wt / "mutants" / "src" / "pkg" / "mod.py.meta").exists()
    assert git(target, "status", "--porcelain") == ""  # nothing to commit in the target


def test_an_unchanged_repo_reruns_only_its_survivors(target, fake, uv):
    target, _ = target
    run(target, fake, uv)
    fake.clear()
    assert run(target, fake, uv) == 0
    assert fake.runs() == [[SENTINEL], [S1, O1]]
    report, _, _ = outputs(target)
    assert report["since"] == "modules stale since the last run" and report["modules"] == []
    assert len(report["survivors"]) == 2  # survivors anywhere are reported


def test_without_survivors_an_unchanged_repo_runs_nothing(target, fake, uv, capsys):
    target, _ = target
    run(target, fake, uv)
    fake.clear()
    assert run(target, fake, uv, "--no-survivors") == 0
    assert fake.runs() == [[SENTINEL]]
    report, _, _ = outputs(target)
    assert report["note"] == mutate.NOTHING_NEEDED and report["complete"] is True


def test_a_killed_survivor_leaves_the_report(target, fake, uv):
    target, _ = target
    run(target, fake, uv)
    fake.spec["verdicts"][S1] = 1  # someone added the test that kills it
    fake.save()
    run(target, fake, uv)
    report, _, _ = outputs(target)
    assert [s["mutmut_name"] for s in report["survivors"]] == [O1]


def test_an_edited_function_is_rerun_and_nothing_else(target, fake, uv):
    target, seed = target
    run(target, fake, uv, "--no-survivors")
    fake.clear()
    push_change(seed, "src/pkg/other.py", OTHER + "\n")
    fake.set(reset=[T1])
    run(target, fake, uv, "--no-survivors")
    assert fake.runs() == [[SENTINEL], ["pkg.other.x_other__mutmut_*"]]
    report, _, stamp = outputs(target)
    assert report["modules"] == ["src/pkg/other.py"]
    assert stamp["modules"]["src/pkg/other.py"]["commit"] == origin_head(target)
    assert stamp["base"] == origin_head(target)


def test_part_of_a_function_is_named_by_key(target, fake, uv):
    target, seed = target
    run(target, fake, uv, "--no-survivors")
    fake.clear()
    push_change(seed, "src/pkg/mod.py", MODULE + "\n")
    fake.set(reset=[S2])
    run(target, fake, uv, "--no-survivors")
    assert fake.runs()[1] == [S2]


def test_named_modules_leave_the_rest_unchecked(target, fake, uv):
    target, _ = target
    assert run(target, fake, uv, "--module", "src/pkg/other.py") == 0
    assert fake.runs()[1] == ["pkg.other.x_other__mutmut_*"]
    verdicts = meta.read_meta(target / ".worktrees" / "mutation")
    assert set(verdicts["src/pkg/mod.py"].values()) == {None}
    report, _, stamp = outputs(target)
    assert report["since"] == "the modules named with --module"
    assert report["counts"] == {"killed": 1}
    assert stamp["base"] is None and list(stamp["modules"]) == ["src/pkg/other.py"]


def test_names_are_passed_in_chunks(target, fake, uv):
    target, _ = target
    run(target, fake, uv, "--chunk", "2")
    assert [len(r) for r in fake.runs()] == [1, 2, 1]


@pytest.mark.parametrize("bad", ["0", "-3", "x"])
def test_a_chunk_must_be_a_positive_integer(target, fake, uv, bad, capsys):
    target, _ = target
    with pytest.raises(SystemExit) as exit_:
        run(target, fake, uv, "--chunk", bad)
    assert exit_.value.code == 2


def test_the_budget_is_gone(target, fake, uv):
    target, _ = target
    with pytest.raises(SystemExit):
        run(target, fake, uv, "--budget-minutes", "1")


def test_the_target_defaults_to_the_repo_around_the_cwd(target, fake, uv, monkeypatch):
    target, _ = target
    (target / "docs").mkdir()
    monkeypatch.chdir(target / "docs")
    assert mutate.main(["--mutmut", str(fake.path), "--uv", str(uv)]) == 0
    assert (target / ".ignored" / "mutation" / "report.json").exists()


# --- failures ---------------------------------------------------------------------------------


def refused(capsys, code) -> str:
    err = capsys.readouterr().err
    assert err.startswith(f"{code}: ")
    return err


def test_a_generation_pass_that_fails_another_way_fails_the_run(target, fake, uv):
    target, _ = target
    fake.path.write_text(f"#!{sys.executable}\nimport sys\nprint('boom')\nsys.exit(3)\n")
    assert run(target, fake, uv) == 2
    out = target / ".ignored" / "mutation"
    report = json.loads((out / "report.json").read_text())
    assert report["failure_code"] == mutate.RUN_FAILED.code and "boom" in report["failure"]
    assert "The run failed" in (out / "summary.md").read_text()
    assert not (out / "stamp.json").exists()


def test_a_clean_test_failure_beside_nothing_matches_fails_the_run(target, fake, uv):
    target, _ = target
    fake.path.write_text(f"#!{sys.executable}\nimport sys\n"
                         f"print('{mutate.CLEAN_TEST_FAILED}')\n"
                         f"print('{mutate.NOTHING_MATCHES}')\nsys.exit(1)\n")
    assert run(target, fake, uv) == 2
    assert not (target / ".ignored" / "mutation" / "stamp.json").exists()


def test_a_failing_chunk_fails_the_run_and_keeps_what_finished(target, fake, uv):
    target, _ = target
    fake.set(run={"exit": 1, "out": mutate.CLEAN_TEST_FAILED})
    assert run(target, fake, uv) == 2
    report, _, stamp = outputs(target)
    assert report["failure_code"] == "e.env.mutmut-run.f" and report["failure_retryable"] is False
    assert "bakobo-mutate run" in report["failure_hint"]
    assert "workflow" not in report["failure_hint"]
    assert stamp["modules"]["src/pkg/mod.py"]["complete"] is False


def test_an_unreadable_meta_fails_the_run(target, fake, uv):
    target, _ = target
    fake.set(run={"corrupt": True})
    assert run(target, fake, uv) == 2
    out = target / ".ignored" / "mutation"
    report = json.loads((out / "report.json").read_text())
    assert report["failure_code"] == meta.META_UNREADABLE.code
    assert not (out / "stamp.json").exists()


@pytest.mark.parametrize("which", ["show_exit", "tests_exit"])
def test_a_failing_mutmut_query_is_a_recorded_failure(target, fake, uv, which):
    target, _ = target
    fake.set(**{which: 1})
    assert run(target, fake, uv) == 2
    report, summary, _ = outputs(target)
    assert report["failure_code"] == mutate.RESULTS_UNREADABLE.code
    assert report["survivors"] == [] and "Running it again may help" in summary
    assert "workflow" not in report["failure_hint"]


def test_malformed_sinks_fail_before_mutmut(target, fake, uv):
    target, seed = target
    with (seed / "pyproject.toml").open("a") as handle:
        handle.write('\n[tool.mutation]\nmessage_sinks = "Refusal"\n')
    push_change(seed, "README", "sinks\n")
    assert run(target, fake, uv) == 2
    assert fake.calls() == []
    report = json.loads((target / ".ignored" / "mutation" / "report.json").read_text())
    assert report["failure_code"] == mutate.SINKS_MALFORMED.code
    assert not (target / ".ignored" / "mutation" / "stamp.json").exists()


def test_a_target_without_mutmut_is_refused(target, uv, capsys):
    target, _ = target
    assert mutate.main([str(target), "--uv", str(uv)]) == 2
    refused(capsys, checkout.NO_MUTMUT.code)


def test_the_checkouts_mutmut_is_the_default(target, fake, uv, monkeypatch):
    target, _ = target
    real_sync = checkout.sync

    def sync_then_install(wt, uv_):
        real_sync(wt, uv_)
        installed = wt / ".venv" / "bin" / "mutmut"
        installed.parent.mkdir(parents=True)
        installed.write_text(fake.path.read_text())
        installed.chmod(0o755)

    monkeypatch.setattr(checkout, "sync", sync_then_install)
    assert mutate.main([str(target), "--uv", str(uv)]) == 0
    assert fake.runs()[0] == [SENTINEL]


def test_a_malformed_stamp_is_refused_before_mutmut(target, fake, uv, capsys):
    target, _ = target
    stamp = target / ".ignored" / "mutation" / "stamp.json"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("not json")
    assert run(target, fake, uv) == 2
    refused(capsys, "e.self.corrupt.mutation-stamp.f")
    assert fake.calls() == []


def test_a_held_lock_refuses_and_writes_nothing(target, fake, uv, capsys):
    target, _ = target
    with lock.machine_lock(Path("/elsewhere")):
        assert run(target, fake, uv) == 2
    err = refused(capsys, lock.LOCK_HELD.code)
    assert str(os.getpid()) in err and "/elsewhere" in err
    assert fake.calls() == [] and not (target / ".ignored" / "mutation").exists()


def test_a_directory_outside_a_repo_is_refused(tmp_path, fake, uv, capsys):
    (tmp_path / "plain").mkdir()
    assert run(tmp_path / "plain", fake, uv) == 2
    refused(capsys, checkout.NOT_A_REPO.code)


# --- interruption -----------------------------------------------------------------------------


def interrupt_when(marker: Path, signum: int):
    """Send ``signum`` to this process once the fake mutmut creates ``marker``."""
    def wait_then_signal():
        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signum)

    thread = threading.Thread(target=wait_then_signal, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_an_interrupted_run_is_incomplete_and_the_next_finishes_it(target, fake, uv, signum,
                                                                   capsys):
    target, _ = target
    fake.set(run={"per_mutant": 0.3})
    previous = signal.getsignal(signal.SIGTERM)
    interrupt_when(fake.dir / "progress", signum)
    assert run(target, fake, uv, "--chunk", "1") == mutate.INTERRUPTED
    assert signal.getsignal(signal.SIGTERM) is previous
    report, summary, stamp = outputs(target)
    assert report["complete"] is False and "interrupted" in report["note"]
    assert "interrupted" in summary
    assert False in [m["complete"] for m in stamp["modules"].values()]
    verdicts = meta.read_meta(target / ".worktrees" / "mutation")
    checked = [k for codes in verdicts.values() for k, c in codes.items() if c is not None]
    assert checked  # what finished before the interrupt was kept
    fake.set(run={})
    fake.clear()
    (fake.dir / "progress").unlink()
    assert run(target, fake, uv) == 0
    report, _, stamp = outputs(target)
    assert report["complete"] is True
    assert all(m["complete"] for m in stamp["modules"].values())
    assert all(k not in checked for r in fake.runs()[1:] for k in r if k in (S2, S3, O2))


def test_an_interrupt_during_generation_runs_nothing_more(target, fake, uv):
    target, _ = target
    fake.set(run={"generating": 30})
    interrupt_when(fake.dir / "generating", signal.SIGINT)
    assert run(target, fake, uv) == mutate.INTERRUPTED
    assert fake.runs() == [[SENTINEL]]
    report, _, stamp = outputs(target)
    assert report["complete"] is False and "interrupted" in report["note"]
    assert not any(m["complete"] for m in stamp["modules"].values())


def test_a_mutmut_ignoring_sigterm_is_killed(target, fake, uv, monkeypatch):
    target, _ = target
    monkeypatch.setattr(mutate, "TERM_GRACE", 0.5)
    fake.set(run={"per_mutant": 5, "ignore_term": True})
    interrupt_when(fake.dir / "started", signal.SIGINT)
    started = time.monotonic()
    assert run(target, fake, uv) == mutate.INTERRUPTED
    assert time.monotonic() - started < 10


def test_an_interrupt_outside_mutmut_stops_cleanly(target, fake, uv, monkeypatch, capsys):
    target, _ = target

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(checkout, "sync", interrupted)
    assert run(target, fake, uv) == mutate.INTERRUPTED
    assert "interrupted" in capsys.readouterr().err
    assert fake.calls() == []


def test_entry_point(target, fake, uv, monkeypatch):
    target, _ = target
    monkeypatch.setattr(sys, "argv", ["mutate.py", str(target), "--mutmut", str(fake.path),
                                      "--uv", str(uv)])
    with pytest.raises(SystemExit) as exit_:
        monkeypatch.delitem(sys.modules, "bakobo_mutate.mutate")
        runpy.run_module("bakobo_mutate.mutate", run_name="__main__", alter_sys=True)
    assert exit_.value.code == 0
