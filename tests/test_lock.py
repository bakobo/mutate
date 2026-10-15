"""One mutation run at a time per machine (this.i 2x5x696m)."""

from __future__ import annotations

import fcntl
import json
import os

import pytest

from bakobo.errors import BakoboError  # type: ignore[import-untyped]

from bakobo_mutate import lock


def test_the_lock_lives_under_the_state_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert lock.lock_path() == tmp_path / "state" / "bakobo-mutate" / "lock"


def test_without_xdg_state_home_it_is_under_local_state(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert lock.lock_path() == tmp_path / ".local" / "state" / "bakobo-mutate" / "lock"


def test_an_empty_xdg_state_home_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert lock.lock_path().parent.parent == tmp_path / ".local" / "state"


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state" / "bakobo-mutate" / "lock"


def test_the_holder_is_written_into_the_lock(state, tmp_path):
    with lock.machine_lock(tmp_path / "repo"):
        holder = json.loads(state.read_text())
        assert holder == {"pid": os.getpid(), "target": str(tmp_path / "repo")}


def test_a_held_lock_refuses_naming_the_holder(state, tmp_path):
    with lock.machine_lock(tmp_path / "first"):
        with pytest.raises(BakoboError) as refused:
            with lock.machine_lock(tmp_path / "second"):
                pytest.fail("a second run started while the first held the lock")
    error = refused.value
    assert error.code == "e.state.conflict.mutation-lock.r" and error.retryable
    assert str(os.getpid()) in error.detail and str(tmp_path / "first") in error.detail


def test_the_lock_is_released_afterwards(state, tmp_path):
    with lock.machine_lock(tmp_path / "a"):
        pass
    with lock.machine_lock(tmp_path / "b"):
        assert json.loads(state.read_text())["target"] == str(tmp_path / "b")


def _fail() -> None:
    raise RuntimeError("boom")


def test_the_lock_is_released_when_the_run_raises(state, tmp_path):
    with pytest.raises(RuntimeError):
        with lock.machine_lock(tmp_path / "a"):
            _fail()
    with lock.machine_lock(tmp_path / "b"):
        pass


@pytest.mark.parametrize("content", ["", "not json", "[1]", '{"pid": "x"}', "x" * 10000],
                         ids=["empty", "not-json", "list", "bad-pid", "oversized"])
def test_an_unreadable_holder_is_still_a_refusal(state, tmp_path, content):
    state.parent.mkdir(parents=True)
    state.write_text(content)
    with state.open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BakoboError) as refused:
            with lock.machine_lock(tmp_path / "second"):
                pass
    assert "an unknown process" in refused.value.detail
