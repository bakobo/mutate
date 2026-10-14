"""The integration oracle: bakobo-mutate run against the real mutmut 3.8.0, on a toy project.

Each test builds a small uv project (two modules, a test file, ``[tool.mutmut]``, mutmut pinned
in its dev group), pushes it to a bare origin, clones that as the target, and drives the tool
in-process with the real git, uv and mutmut. It pins what the unit tests' fake mutmut assumes:
that generation keeps cached verdicts and resets only changed functions, that a named mutant is
re-run even when cached, that the per-function glob (method form included) selects a function's
mutants, and that verdicts saved before an interrupt survive it.

uv manages Python and installs mutmut into the toy's checkout; with no network, set
UV_OFFLINE=1 and it installs from the uv cache.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import git

from bakobo_mutate import checkout, lock, meta, mutate, scope, sweep

pytestmark = pytest.mark.integration

SEP = "ǁ"

PYPROJECT = """\
[project]
name = "toy"
version = "0"
requires-python = ">=3.14"

[dependency-groups]
dev = ["pytest", "mutmut==3.8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/toy"]

[tool.mutmut]
source_paths = ["src/toy"]
pytest_add_cli_args = ["-q", "-p", "no:cacheprovider"]
"""

ALPHA = '''\
def add(a, b):
    return a + b


def check(x):
    if x < 0:
        raise ValueError(f"negative: {x}")
    return x


class Box:
    def grow(self, n):
        return n * 2
'''

BETA = "def double(x):\n    return x * 2\n"

TESTS = '''\
import os
import time

from toy.alpha import Box, add, check
from toy.beta import double


def slow():
    # Only while a mutant is under test, and only when the test asks for it.
    if "__mutmut_" in os.environ.get("MUTANT_UNDER_TEST", ""):
        time.sleep(float(os.environ.get("TOY_SLEEP", "0")))


def test_add():
    slow()
    assert add(1, 2) == 3


def test_check():
    slow()
    assert check(1) == 1


def test_box():
    slow()
    Box().grow(2)


def test_double():
    slow()
    assert double(2) == 4
'''

KILL_GROW = "\n\ndef test_box_grows():\n    assert Box().grow(2) == 4\n"


def clean_env() -> dict[str, str]:
    return checkout.clean_env()


@pytest.fixture
def toy(tmp_path):
    seed = tmp_path / "seed"
    (seed / "src" / "toy").mkdir(parents=True)
    (seed / "tests").mkdir()
    (seed / "pyproject.toml").write_text(PYPROJECT)
    (seed / ".gitignore").write_text(".venv/\n")
    (seed / "src" / "toy" / "__init__.py").write_text("")
    (seed / "src" / "toy" / "alpha.py").write_text(ALPHA)
    (seed / "src" / "toy" / "beta.py").write_text(BETA)
    (seed / "tests" / "test_toy.py").write_text(TESTS)
    subprocess.run(["uv", "lock", "--quiet"], cwd=seed, env=clean_env(), check=True)
    git(seed, "init", "-q", "-b", "main")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "toy")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    target = tmp_path / "target"
    git(tmp_path, "clone", "-q", str(bare), str(target))
    return target, seed


def push(seed: Path, rel: str, text: str) -> None:
    (seed / rel).write_text(text)
    git(seed, "commit", "-q", "-am", f"change {rel}")
    git(seed, "push", "-q", str(seed.parent / "origin.git"), "HEAD:main")


def run(target: Path, *extra: str) -> int:
    return mutate.main([str(target), *extra])


def wt(target: Path) -> Path:
    return target / ".worktrees" / "mutation"


def verdicts(target: Path) -> meta.Verdicts:
    return meta.read_meta(wt(target))


def durations(target: Path) -> dict[str, float]:
    found = {}
    for path in (wt(target) / "mutants").rglob("*.meta"):
        found.update(json.loads(path.read_text())["durations_by_key"])
    return found


def out(target: Path, name: str) -> str:
    return (target / ".ignored" / "mutation" / name).read_text()


def report(target: Path) -> dict:
    return json.loads(out(target, "report.json"))


def stamp(target: Path) -> dict:
    return json.loads(out(target, "stamp.json"))


def survivors(target: Path) -> set[str]:
    return {s["mutmut_name"] for s in report(target)["survivors"]}


def rerun(before: dict[str, float], after: dict[str, float]) -> set[str]:
    return {k for k in after if before.get(k) != after[k]}


def test_a_first_run_checks_everything_and_a_cached_run_only_survivors(toy):
    target, _ = toy
    assert run(target) == 0  # (a)
    first = verdicts(target)
    keys = {k for codes in first.values() for k in codes}
    assert keys and None not in {c for codes in first.values() for c in codes.values()}
    data = report(target)
    assert data["complete"] is True and sum(data["counts"].values()) == len(keys)
    # The method form of a mutant name, which the per-function glob relies on.
    assert any(k.startswith(f"toy.alpha.x{SEP}Box{SEP}grow__mutmut_") for k in keys)
    assert survivors(target)
    assert {s["class"] for s in data["survivors"]} >= {"behavioural", "diagnostic"}
    assert all(e["complete"] for e in stamp(target)["modules"].values())

    # (b) Nothing changed and survivors not asked for: mutmut generates and runs nothing.
    before = durations(target)
    assert run(target, "--no-survivors") == 0
    assert durations(target) == before
    log = out(target, "mutmut-run.log")
    assert mutate.NOTHING_MATCHES in log and "mutations/second" not in log
    assert report(target)["note"] == mutate.NOTHING_NEEDED

    # By default the survivors, and only they, are re-run.
    assert run(target) == 0
    assert rerun(before, durations(target)) == survivors(target)
    assert "mutations/second" in out(target, "mutmut-run.log")


def test_a_new_killing_test_removes_its_survivor(toy):
    target, seed = toy
    run(target)
    grow = {s for s in survivors(target) if f"{SEP}grow__" in s}
    assert grow
    push(seed, "tests/test_toy.py", TESTS + KILL_GROW)  # (c): tests change, source does not
    assert run(target) == 0
    assert not survivors(target) & grow


def test_an_edited_function_alone_is_rerun_and_stale_tracks_it(toy, capsys):
    target, seed = toy
    run(target, "--no-survivors")
    before = durations(target)
    push(seed, "src/toy/alpha.py", ALPHA.replace("return n * 2", "return 2 * n"))
    capsys.readouterr()
    assert sweep.stale_main([str(target), "--fetch"]) == 0  # (e) before
    assert capsys.readouterr().out == "src/toy/alpha.py\n"
    assert run(target, "--no-survivors") == 0
    changed = rerun(before, durations(target))  # (d)
    assert changed and all(f"{SEP}Box{SEP}grow__mutmut_" in k for k in changed)
    assert report(target)["modules"] == ["src/toy/alpha.py"]
    capsys.readouterr()
    assert sweep.stale_main([str(target)]) == 0  # (e) after
    assert capsys.readouterr().out == ""


def test_a_module_outside_the_named_scope_keeps_its_unchecked_verdicts(toy):
    target, _ = toy
    assert run(target, "--module", "src/toy/beta.py", "--no-survivors") == 0  # (f)
    found = verdicts(target)
    assert set(found["src/toy/alpha.py"].values()) == {None}
    assert None not in set(found["src/toy/beta.py"].values())
    assert list(stamp(target)["modules"]) == ["src/toy/beta.py"]


def test_an_interrupted_run_is_finished_by_the_next(toy, monkeypatch):
    target, _ = toy
    monkeypatch.setenv("TOY_SLEEP", "3")

    def interrupt_after_first_verdict():
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                if any(c is not None for codes in verdicts(target).values()
                       for c in codes.values()):
                    break
            except Exception:  # a .meta being rewritten mid-read
                pass
            time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_after_first_verdict, daemon=True).start()
    assert run(target, "--chunk", "1") == mutate.INTERRUPTED  # (g)
    assert report(target)["complete"] is False
    assert False in [e["complete"] for e in stamp(target)["modules"].values()]
    kept = {k for codes in verdicts(target).values() for k, c in codes.items()
            if c is not None}
    assert kept

    monkeypatch.delenv("TOY_SLEEP")
    before = durations(target)
    assert run(target, "--no-survivors") == 0
    assert all(e["complete"] for e in stamp(target)["modules"].values())
    assert not rerun(before, durations(target)) & kept  # what finished was not run again


def test_a_held_lock_refuses_with_its_code(toy, capsys):
    target, _ = toy
    with lock.machine_lock(Path("/another/repo")):
        assert run(target) == 2  # (h)
    assert capsys.readouterr().err.startswith(f"{lock.LOCK_HELD.code}: ")
    assert not wt(target).exists()


def test_the_scope_module_reads_the_toy_as_run_does(toy):
    target, _ = toy
    git(target, "fetch", "-q")
    cfg = scope.config_at(target, "origin/main")
    assert scope.window(target, "origin/main", "1 day ago", cfg) == [
        "src/toy/alpha.py", "src/toy/beta.py"]
