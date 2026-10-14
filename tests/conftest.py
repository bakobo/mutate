"""Helpers the test modules share: real git over scratch repos, and stub executables."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(root: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ, **GIT_ENV)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch, tmp_path):
    """No test reads the developer's git config, state directory or enrolment list."""
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


RECORDER = """\
#!{python}
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
spec = json.loads((here / "{name}.json").read_text())
with (here / "{name}.log").open("a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
answer = spec.get(sys.argv[1] if len(sys.argv) > 1 else "", spec.get("*", {{}}))
for rel in answer.get("touch", []):
    path = Path(os.getcwd()) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    path.chmod(0o755)
sys.stdout.write(answer.get("out", ""))
sys.stderr.write(answer.get("err", ""))
sys.exit(answer.get("exit", 0))
"""


def recorder(directory: Path, name: str, spec: dict) -> Path:
    """An executable that logs argv and cwd and answers by its first argument from ``spec``."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(RECORDER.format(python=sys.executable, name=name))
    path.chmod(0o755)
    (directory / f"{name}.json").write_text(json.dumps(spec))
    return path


def recorded(directory: Path, name: str) -> list[dict]:
    log = directory / f"{name}.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def origin(tmp_path):
    """A bare origin with one commit on main, and a clone of it as the target repo."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "README").write_text("seed\n")
    (seed / ".gitignore").write_text(".venv/\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "seed")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    target = tmp_path / "target"
    git(tmp_path, "clone", "-q", str(bare), str(target))
    return bare, target, seed


def push_change(seed: Path, rel: str, text: str, message: str = "change",
                date: str | None = None) -> str:
    path = seed / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", message, date=date)
    bare = seed.parent / "origin.git"
    git(seed, "push", "-q", str(bare), "HEAD:main")
    return git(seed, "rev-parse", "HEAD").strip()
