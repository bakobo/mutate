"""Which modules a run covers, and which are stale (this.i 24xzjnz7).

Everything is read from git objects at a ref, never from a working tree, so ``stale`` can
answer without the mutation checkout and agrees with ``run``. git is the real one.
"""

from __future__ import annotations

import json
import textwrap

import pytest
from bakobo.errors import BakoboError  # type: ignore[import-untyped]
from conftest import git

from bakobo_mutate import scope

FUNC = "def f():\n    return 1\n"
OLD, NEW = "2020-01-02T00:00:00", "2030-01-01T00:00:00"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "pyproject.toml").write_text(textwrap.dedent("""\
        [tool.mutmut]
        source_paths = ["src/pkg"]
        do_not_mutate = ["*/pkg/vendored.py"]
        """))
    (root / "src" / "pkg" / "mod.py").write_text(FUNC)
    (root / "src" / "pkg" / "other.py").write_text(FUNC)
    (root / "src" / "pkg" / "consts.py").write_text("X = 1\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base", date="2020-01-01T00:00:00")
    return root


def commit(root, files: dict[str, str | None], date: str = NEW) -> str:
    for rel, text in files.items():
        path = root / rel
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "c", date=date)
    return git(root, "rev-parse", "HEAD").strip()


def head(root) -> str:
    return git(root, "rev-parse", "HEAD").strip()


# --- configuration at a ref -------------------------------------------------------------------


def test_the_mutmut_table_is_read_at_the_ref(repo):
    assert scope.config_at(repo, "HEAD")["source_paths"] == ["src/pkg"]


def test_a_ref_without_a_pyproject_has_no_table(repo):
    commit(repo, {"pyproject.toml": None})
    assert scope.config_at(repo, "HEAD") == {}


def test_a_ref_git_cannot_resolve_is_refused_not_read_as_no_pyproject(repo):
    with pytest.raises(BakoboError) as refused:
        scope.config_at(repo, "no-such-ref")
    assert refused.value.code == scope.GIT_FAILED.code == "e.env.git.mutation-scope.f"
    assert "no-such-ref" in refused.value.detail


def test_a_failed_listing_is_refused_not_read_as_no_sources(repo):
    with pytest.raises(BakoboError) as refused:
        scope.all_sources(repo, "no-such-ref", {"source_paths": ["src/pkg"]})
    assert refused.value.code == scope.GIT_FAILED.code
    assert "ls-tree" in refused.value.detail


def test_the_sources_at_a_ref_are_listed(repo):
    assert scope.all_sources(repo, "HEAD", {"source_paths": ["src/pkg"]}) == {
        "src/pkg/mod.py", "src/pkg/other.py", "src/pkg/consts.py"}


@pytest.mark.parametrize("body", ["not = [toml", 'tool = "x"', "[tool]\nmutmut = 3\n"],
                         ids=["not-toml", "tool-not-table", "mutmut-not-table"])
def test_an_unreadable_mutmut_table_is_refused(repo, body):
    commit(repo, {"pyproject.toml": body})
    with pytest.raises(BakoboError) as refused:
        scope.config_at(repo, "HEAD")
    assert refused.value.code == scope.CONFIG_UNREADABLE.code == "e.self.config.mutmut-table.f"


# --- what counts as a module ------------------------------------------------------------------


def test_candidates_keep_existing_mutable_python_modules(repo):
    commit(repo, {"src/pkg/vendored.py": FUNC, "src/pkg/notes.txt": "x\n",
                  "src/pkg/broken.py": "def (:\n"})
    cfg = scope.config_at(repo, "HEAD")
    paths = ["src/pkg/mod.py", "src/pkg/consts.py", "src/pkg/vendored.py", "src/pkg/notes.txt",
             "src/pkg/gone.py", "src/pkg/broken.py"]
    # A module mutmut could not parse is kept, so the run says so rather than skipping it.
    assert scope.candidates(repo, "HEAD", cfg, paths) == ["src/pkg/broken.py", "src/pkg/mod.py"]


def test_has_functions():
    assert scope.has_functions("def f():\n    pass\n")
    assert scope.has_functions("async def f():\n    pass\n")
    assert scope.has_functions("class C:\n    def m(self):\n        pass\n")
    assert not scope.has_functions("class C:\n    x = 1\n")
    assert not scope.has_functions("X = 1\n")
    assert scope.has_functions("def (:\n")


# --- the --since window (no stamp yet) --------------------------------------------------------


def test_the_window_follows_first_parent_merges(repo):
    pkg = "src/pkg/"
    git(repo, "checkout", "-q", "-b", "topic")
    commit(repo, {pkg + "merged.py": FUNC}, date=OLD)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", "topic", date=NEW)
    commit(repo, {pkg + "mod.py": FUNC + "\n", pkg + "vendored.py": FUNC, "outside.py": FUNC,
                  pkg + "consts.py": None})
    cfg = scope.config_at(repo, "HEAD")
    assert scope.window(repo, "HEAD", "2029-12-01", cfg) == ["src/pkg/merged.py",
                                                             "src/pkg/mod.py"]
    assert scope.window(repo, "HEAD", "2031-01-01", cfg) == []


def test_the_window_defaults_to_src(repo):
    commit(repo, {"pyproject.toml": "[project]\nname = 'x'\n"}, date="2020-01-01T00:00:01")
    assert scope.window(repo, "HEAD", "2019-01-01", {}) == ["src/pkg/mod.py",
                                                            "src/pkg/other.py"]


@pytest.mark.parametrize("since", ["nonsense", "2099-01-01", "now"],
                         ids=["nonsense", "future", "now"])
def test_a_since_git_does_not_read_as_a_past_date_is_refused(repo, since):
    # git's approxidate reads nonsense as "now", which would silently select nothing.
    with pytest.raises(BakoboError) as refused:
        scope.check_since(repo, since)
    assert refused.value.code == scope.SINCE_MALFORMED.code == "e.input.format.since.f"
    assert repr(since) in refused.value.detail


@pytest.mark.parametrize("since", ["7 days ago", "2020-01-01", "yesterday"])
def test_a_since_in_the_past_is_accepted(repo, since):
    scope.check_since(repo, since)


def test_a_since_git_prints_nothing_for_is_refused(repo, tmp_path):
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    with pytest.raises(BakoboError) as refused:
        scope.check_since(repo, "7 days ago", git=str(fake))
    assert refused.value.code == scope.SINCE_MALFORMED.code


# --- the stamp --------------------------------------------------------------------------------


def test_there_is_no_stamp_before_the_first_run(tmp_path):
    assert scope.read_stamp(tmp_path) is None


def test_a_stamp_round_trips(tmp_path):
    stamp = scope.Stamp(base="abc", modules={"src/a.py": scope.Entry("abc", True)})
    scope.write_stamp(tmp_path, stamp)
    path = tmp_path / ".ignored" / "mutation" / "stamp.json"
    assert json.loads(path.read_text())["schema"] == scope.STAMP_SCHEMA
    assert scope.read_stamp(tmp_path) == stamp


@pytest.mark.parametrize("text", [
    "not json", "[]", '{"schema": "other", "base": null, "modules": {}}',
    '{"schema": "bakobo.mutation-stamp/1", "base": 3, "modules": {}}',
    '{"schema": "bakobo.mutation-stamp/1", "base": null, "modules": []}',
    '{"schema": "bakobo.mutation-stamp/1", "base": null, "modules": {"a": {"commit": 1}}}',
    '{"schema": "bakobo.mutation-stamp/1", "base": null, '
    '"modules": {"a": {"commit": "c", "complete": "yes"}}}',
    '{"schema": "bakobo.mutation-stamp/1", "base": null, "modules": {"a": 3}}',
], ids=["not-json", "list", "schema", "base", "modules", "commit", "complete", "entry"])
def test_a_malformed_stamp_is_refused(tmp_path, text):
    path = tmp_path / ".ignored" / "mutation" / "stamp.json"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(BakoboError) as refused:
        scope.read_stamp(tmp_path)
    assert refused.value.code == scope.STAMP_MALFORMED.code == "e.self.corrupt.mutation-stamp.f"
    assert str(path) in refused.value.detail


def test_an_oversized_stamp_is_refused_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, "MAX_STAMP_BYTES", 16)
    path = tmp_path / ".ignored" / "mutation" / "stamp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": scope.STAMP_SCHEMA, "pad": "x" * 50}))
    with pytest.raises(BakoboError) as refused:
        scope.read_stamp(tmp_path)
    assert "larger than 16 bytes" in refused.value.detail


# --- staleness --------------------------------------------------------------------------------


def fresh(repo, *modules, complete=True, base=True):
    at = head(repo)
    return scope.Stamp(base=at if base else None,
                       modules={m: scope.Entry(at, complete) for m in modules})


def stale(repo, stamp, since="7 days ago"):
    return scope.stale(repo, "HEAD", stamp, since, scope.config_at(repo, "HEAD"))


def test_nothing_is_stale_right_after_a_complete_run(repo):
    assert stale(repo, fresh(repo, "src/pkg/mod.py", "src/pkg/other.py")) == []


def test_a_module_changed_since_its_commit_is_stale(repo):
    stamp = fresh(repo, "src/pkg/mod.py", "src/pkg/other.py")
    commit(repo, {"src/pkg/mod.py": FUNC + "\n"})
    assert stale(repo, stamp) == ["src/pkg/mod.py"]


def test_an_incomplete_module_is_stale_though_unchanged(repo):
    stamp = fresh(repo, "src/pkg/mod.py")
    stamp.modules["src/pkg/other.py"] = scope.Entry(head(repo), False)
    assert stale(repo, stamp) == ["src/pkg/other.py"]


def test_a_module_whose_commit_git_does_not_know_is_stale(repo):
    stamp = fresh(repo, "src/pkg/mod.py", "src/pkg/other.py")
    stamp.modules["src/pkg/mod.py"] = scope.Entry("0" * 40, True)
    assert stale(repo, stamp) == ["src/pkg/mod.py"]


def test_a_module_absent_from_the_stamp_is_stale_if_changed_since_the_base(repo):
    stamp = fresh(repo, "src/pkg/mod.py")
    assert stale(repo, stamp) == []  # other.py has not changed since the base
    commit(repo, {"src/pkg/other.py": FUNC + "\n", "src/pkg/new.py": FUNC})
    assert stale(repo, stamp) == ["src/pkg/new.py", "src/pkg/other.py"]


def test_an_unknown_base_makes_every_unstamped_module_stale(repo):
    stamp = fresh(repo, "src/pkg/mod.py")
    stamp.base = "0" * 40
    assert stale(repo, stamp) == ["src/pkg/other.py"]


def test_without_a_base_unstamped_modules_use_the_window(repo):
    stamp = fresh(repo, "src/pkg/mod.py", base=False)
    commit(repo, {"src/pkg/other.py": FUNC + "\n"})
    assert stale(repo, stamp, since="2029-12-01") == ["src/pkg/other.py"]
    assert stale(repo, stamp, since="2031-01-01") == []


def test_a_deleted_or_excluded_module_is_never_stale(repo):
    stamp = fresh(repo, "src/pkg/mod.py", "src/pkg/other.py", complete=False)
    commit(repo, {"src/pkg/mod.py": None, "pyproject.toml": textwrap.dedent("""\
        [tool.mutmut]
        source_paths = ["src/pkg"]
        do_not_mutate = ["*/pkg/other.py"]
        """)})
    assert stale(repo, stamp) == []


@pytest.mark.parametrize("base", [True, False], ids=["base", "no-base"])
def test_staleness_at_a_ref_git_cannot_resolve_is_refused(repo, base):
    stamp = fresh(repo, "src/pkg/mod.py", base=base)
    with pytest.raises(BakoboError) as refused:
        scope.stale(repo, "no-such-ref", stamp, "7 days ago", {"source_paths": ["src/pkg"]})
    assert refused.value.code == scope.GIT_FAILED.code


# --- what a run covers ------------------------------------------------------------------------


def test_named_modules_are_the_scope_after_the_usual_filters(repo):
    cfg = scope.config_at(repo, "HEAD")
    got = scope.run_scope(repo, "HEAD", None, ["src/pkg/consts.py", "src/pkg/mod.py"],
                          "7 days ago", cfg)
    assert got == (["src/pkg/mod.py"], "the modules named with --module")


def test_without_a_stamp_the_scope_is_the_window(repo):
    commit(repo, {"src/pkg/mod.py": FUNC + "\n"})
    cfg = scope.config_at(repo, "HEAD")
    assert scope.run_scope(repo, "HEAD", None, [], "2029-12-01", cfg) == (
        ["src/pkg/mod.py"], "modules changed since 2029-12-01")


def test_with_a_stamp_the_scope_is_what_is_stale(repo):
    stamp = fresh(repo, "src/pkg/mod.py", "src/pkg/other.py")
    commit(repo, {"src/pkg/other.py": FUNC + "\n"})
    cfg = scope.config_at(repo, "HEAD")
    assert scope.run_scope(repo, "HEAD", stamp, [], "7 days ago", cfg) == (
        ["src/pkg/other.py"], "modules stale since the last run")


# --- the stamp after a run --------------------------------------------------------------------


def test_a_run_records_each_in_scope_module_and_keeps_the_rest():
    before = scope.Stamp(base="old", modules={"a.py": scope.Entry("old", True),
                                              "b.py": scope.Entry("old", False)})
    after = scope.updated(before, {"b.py": True, "c.py": False}, "new", advance_base=True)
    assert after == scope.Stamp(base="new", modules={
        "a.py": scope.Entry("old", True), "b.py": scope.Entry("new", True),
        "c.py": scope.Entry("new", False)})


def test_a_run_of_named_modules_leaves_the_base_alone():
    before = scope.Stamp(base="old", modules={})
    assert scope.updated(before, {"a.py": True}, "new", advance_base=False).base == "old"
    assert scope.updated(None, {"a.py": True}, "new", advance_base=False).base is None
    assert scope.updated(None, {"a.py": True}, "new", advance_base=True).base == "new"
