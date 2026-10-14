"""The persistent detached checkout at <target>/.worktrees/mutation (this.i 87y4twc4).

git is the real one, over a bare origin and a clone of it; uv is a stub that records argv.
"""

from __future__ import annotations

import pytest
from bakobo.errors import BakoboError  # type: ignore[import-untyped]
from conftest import git, push_change, recorded, recorder

from bakobo_mutate import checkout


def code_of(excinfo) -> str:
    return excinfo.value.code


# --- which repo, which branch -----------------------------------------------------------------


def test_the_target_is_the_repo_root_from_a_subdirectory(origin):
    _, target, _ = origin
    (target / "sub").mkdir()
    assert checkout.target_root(target / "sub") == target.resolve()


def test_the_target_is_the_main_checkout_from_inside_a_worktree(origin):
    _, target, _ = origin
    git(target, "worktree", "add", "-q", "--detach", str(target / ".worktrees" / "x"))
    assert checkout.target_root(target / ".worktrees" / "x") == target.resolve()


def test_a_directory_outside_any_repo_is_refused(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(BakoboError) as refused:
        checkout.target_root(tmp_path / "plain")
    assert code_of(refused) == checkout.NOT_A_REPO.code == "e.state.missing.git-repo.f"
    assert str(tmp_path / "plain") in refused.value.detail


def test_a_bare_repo_is_refused(origin):
    bare, _, _ = origin
    with pytest.raises(BakoboError) as refused:
        checkout.target_root(bare)
    assert code_of(refused) == checkout.NOT_A_REPO.code


def test_the_default_branch_comes_from_origin_head(origin):
    _, target, _ = origin
    assert checkout.default_branch(target) == "main"


def test_an_unset_origin_head_is_refused(origin):
    _, target, _ = origin
    git(target, "remote", "set-head", "origin", "-d")
    with pytest.raises(BakoboError) as refused:
        checkout.default_branch(target)
    assert code_of(refused) == checkout.NO_DEFAULT_BRANCH.code == "e.state.missing.origin-head.f"
    assert "set-head" in refused.value.hint


# --- the checkout -----------------------------------------------------------------------------


def head(path) -> str:
    return git(path, "rev-parse", "HEAD").strip()


def test_an_absent_checkout_is_added_detached_at_origin(origin):
    _, target, seed = origin
    landed = push_change(seed, "a.py", "x = 1\n")  # on origin, not yet fetched by the target
    wt, branch = checkout.ensure_checkout(target)
    assert wt == target / ".worktrees" / "mutation" and branch == "main"
    assert head(wt) == landed
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"  # detached
    assert head(target) != landed  # the developer's own checkout is untouched


def test_a_present_checkout_is_refreshed(origin):
    _, target, seed = origin
    wt, _ = checkout.ensure_checkout(target)
    landed = push_change(seed, "b.py", "y = 2\n")
    assert checkout.ensure_checkout(target)[0] == wt
    assert head(wt) == landed


def test_mutants_and_ignored_are_excluded_once(origin):
    _, target, _ = origin
    wt, _ = checkout.ensure_checkout(target)
    checkout.ensure_checkout(target)
    exclude = (target / ".git" / "info" / "exclude").read_text()
    assert exclude.count("mutants/") == 1 and exclude.count(".ignored/") == 1
    git(wt, "check-ignore", "-q", "mutants/src/x.py.meta")
    git(target, "check-ignore", "-q", ".ignored/mutation/report.json")


def test_an_exclusion_the_repo_already_has_is_not_repeated(origin):
    _, target, seed = origin
    push_change(seed, ".gitignore", ".venv/\nmutants/\n.ignored/\n")
    (target / ".git" / "info" / "exclude").unlink()
    git(target, "pull", "-q")
    checkout.ensure_checkout(target)
    assert not (target / ".git" / "info" / "exclude").exists()


def test_an_exclude_file_without_a_trailing_newline_is_extended_cleanly(origin):
    _, target, _ = origin
    (target / ".git" / "info" / "exclude").write_text("*.tmp")
    checkout.ensure_checkout(target)
    lines = (target / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines[0] == "*.tmp" and sorted(lines[1:]) == [".ignored/", "mutants/"]


def test_a_missing_info_directory_is_created(origin):
    _, target, _ = origin
    (target / ".git" / "info" / "exclude").unlink()
    (target / ".git" / "info").rmdir()
    checkout.ensure_checkout(target)
    assert "mutants/" in (target / ".git" / "info" / "exclude").read_text()


@pytest.mark.parametrize("dirt", ["modified", "untracked"])
def test_a_dirty_checkout_is_refused(origin, dirt):
    _, target, _ = origin
    wt, _ = checkout.ensure_checkout(target)
    (wt / ("README" if dirt == "modified" else "stray.py")).write_text("changed\n")
    with pytest.raises(BakoboError) as refused:
        checkout.ensure_checkout(target)
    assert code_of(refused) == checkout.CHECKOUT_DIRTY.code
    assert checkout.CHECKOUT_DIRTY.code == "e.state.conflict.mutation-checkout.f"
    assert str(wt) in refused.value.detail


def test_ignored_files_do_not_make_it_dirty(origin):
    _, target, _ = origin
    wt, _ = checkout.ensure_checkout(target)
    (wt / "mutants" / "src").mkdir(parents=True)
    (wt / "mutants" / "src" / "a.py.meta").write_text("{}")
    (wt / ".venv").mkdir()
    checkout.ensure_checkout(target)
    assert (wt / "mutants" / "src" / "a.py.meta").exists()  # the cache survives a refresh


def test_a_path_that_is_not_a_worktree_is_refused(origin):
    _, target, _ = origin
    (target / ".worktrees" / "mutation").mkdir(parents=True)
    before = head(target)
    with pytest.raises(BakoboError) as refused:
        checkout.ensure_checkout(target)
    assert code_of(refused) == checkout.NOT_A_WORKTREE.code
    assert head(target) == before


def test_a_failed_fetch_is_a_coded_failure(origin):
    _, target, _ = origin
    git(target, "remote", "set-url", "origin", str(target.parent / "nowhere.git"))
    with pytest.raises(BakoboError) as refused:
        checkout.ensure_checkout(target)
    assert code_of(refused) == checkout.GIT_FAILED.code == "e.env.git.mutation-checkout.r"
    assert "fetch" in refused.value.detail


def test_a_failed_worktree_add_is_a_coded_failure(origin):
    _, target, _ = origin
    (target / ".worktrees").write_text("a file where a directory must go\n")
    with pytest.raises(BakoboError) as refused:
        checkout.ensure_checkout(target)
    assert code_of(refused) == checkout.GIT_FAILED.code
    assert "worktree" in refused.value.detail


# --- its environment and its mutmut -----------------------------------------------------------


def test_the_checkout_is_synced_with_uv(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/somewhere/else")
    uv = recorder(tmp_path / "bin", "uv", {"sync": {"out": "ok\n"}})
    wt = tmp_path / "wt"
    wt.mkdir()
    checkout.sync(wt, str(uv))
    (call,) = recorded(tmp_path / "bin", "uv")
    assert call == {"argv": ["sync", "--frozen", "--all-extras"], "cwd": str(wt)}


def test_the_sync_runs_without_the_callers_virtualenv(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/somewhere/else")
    env = checkout.clean_env()
    assert "VIRTUAL_ENV" not in env and "UV_PROJECT_ENVIRONMENT" not in env


def test_a_failed_sync_is_a_coded_failure(tmp_path):
    uv = recorder(tmp_path / "bin", "uv", {"sync": {"exit": 2, "err": "lockfile out of date\n"}})
    with pytest.raises(BakoboError) as refused:
        checkout.sync(tmp_path, str(uv))
    assert code_of(refused) == checkout.SYNC_FAILED.code == "e.env.uv-sync.f"
    assert "lockfile out of date" in refused.value.detail


def test_the_checkouts_own_mutmut_is_used(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "mutmut").touch()
    assert checkout.find_mutmut(tmp_path, None) == str(tmp_path / ".venv" / "bin" / "mutmut")


def test_an_explicit_mutmut_wins(tmp_path):
    assert checkout.find_mutmut(tmp_path, "/opt/mutmut") == "/opt/mutmut"


def test_a_checkout_without_mutmut_is_refused(tmp_path):
    with pytest.raises(BakoboError) as refused:
        checkout.find_mutmut(tmp_path, None)
    assert code_of(refused) == checkout.NO_MUTMUT.code == "e.state.missing.mutmut.f"
    assert "dependency group" in refused.value.hint
