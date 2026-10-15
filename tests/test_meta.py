"""Reading mutmut's verdicts from its .meta files, and naming only what needs running (7wj9uurv)."""

from __future__ import annotations

import fnmatch
import json

import pytest
from bakobo.errors import BakoboError  # type: ignore[import-untyped]

from bakobo_mutate import meta

SEP = "ǁ"


def write_meta(wt, path, codes, **extra):
    target = wt / "mutants" / (path + ".meta")
    target.parent.mkdir(parents=True, exist_ok=True)
    body = {"exit_code_by_key": codes, "hash_by_function_name": {}, "durations_by_key": {},
            "estimated_durations_by_key": {}, "type_check_error_by_key": {}, **extra}
    target.write_text(json.dumps(body))
    source = wt / path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.touch()


@pytest.mark.parametrize("code, status", [
    (None, "not checked"), (0, "survived"), (1, "killed"), (3, "killed"), (33, "no tests"),
    (5, "no tests"), (37, "caught by type check"), (36, "timeout"), (-9, "segfault"),
    (2, "check was interrupted by user"), (34, "skipped"), (99, "suspicious"),
])
def test_statuses_follow_mutmut(code, status):
    assert meta.status(code) == status


def test_every_meta_for_an_existing_source_is_read(tmp_path):
    write_meta(tmp_path, "src/pkg/a.py", {"pkg.a.x_f__mutmut_1": None, "pkg.a.x_f__mutmut_2": 0})
    write_meta(tmp_path, "src/pkg/b.py", {})
    write_meta(tmp_path, "src/pkg/gone.py", {"pkg.gone.x_f__mutmut_1": 0})
    (tmp_path / "src" / "pkg" / "gone.py").unlink()  # its .meta outlived it
    assert meta.read_meta(tmp_path) == {
        "src/pkg/a.py": {"pkg.a.x_f__mutmut_1": None, "pkg.a.x_f__mutmut_2": 0},
        "src/pkg/b.py": {}}


def test_a_checkout_never_mutated_has_no_verdicts(tmp_path):
    assert meta.read_meta(tmp_path) == {}


@pytest.mark.parametrize("text, problem", [
    ("not json", "not JSON"),
    ("[]", "exit_code_by_key"),
    ('{"exit_code_by_key": []}', "exit_code_by_key"),
    ('{"exit_code_by_key": {"k": "survived"}}', "'k'"),
    ('{"exit_code_by_key": {"k": true}}', "'k'"),
], ids=["not-json", "list", "codes-list", "code-text", "code-bool"])
def test_an_unreadable_meta_is_refused(tmp_path, text, problem):
    write_meta(tmp_path, "src/a.py", {})
    (tmp_path / "mutants" / "src" / "a.py.meta").write_text(text)
    with pytest.raises(BakoboError) as refused:
        meta.read_meta(tmp_path)
    assert refused.value.code == meta.META_UNREADABLE.code == "e.env.mutmut-meta.f"
    assert "src/a.py.meta" in refused.value.detail and problem in refused.value.detail


def test_an_oversized_meta_is_refused_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(meta, "MAX_META_BYTES", 8)
    write_meta(tmp_path, "src/a.py", {"k": None})
    with pytest.raises(BakoboError) as refused:
        meta.read_meta(tmp_path)
    assert "larger than 8 bytes" in refused.value.detail


VERDICTS = {
    "src/pkg/a.py": {"pkg.a.x_f__mutmut_1": None, "pkg.a.x_f__mutmut_2": 33,
                     "pkg.a.x_g__mutmut_1": 1, "pkg.a.x_g__mutmut_2": None,
                     f"pkg.a.x{SEP}C{SEP}m__mutmut_1": 0},
    "src/pkg/b.py": {"pkg.b.x_h__mutmut_1": None, "pkg.b.x_h__mutmut_2": 0},
}


def test_needed_is_unchecked_in_scope_plus_every_survivor():
    assert meta.needed(VERDICTS, {"src/pkg/a.py"}, survivors=True) == {
        "pkg.a.x_f__mutmut_1", "pkg.a.x_f__mutmut_2", "pkg.a.x_g__mutmut_2",
        f"pkg.a.x{SEP}C{SEP}m__mutmut_1", "pkg.b.x_h__mutmut_2"}


def test_without_survivors_only_unchecked_in_scope_is_needed():
    assert meta.needed(VERDICTS, {"src/pkg/b.py"}, survivors=False) == {"pkg.b.x_h__mutmut_1"}


def test_a_whole_function_is_named_by_a_glob_and_a_part_by_its_keys():
    need = meta.needed(VERDICTS, {"src/pkg/a.py"}, survivors=True)
    assert meta.names(VERDICTS, need) == [
        "pkg.a.x_f__mutmut_*", "pkg.a.x_g__mutmut_2", f"pkg.a.x{SEP}C{SEP}m__mutmut_*",
        "pkg.b.x_h__mutmut_2"]


def test_names_select_exactly_the_needed_mutants():
    need = meta.needed(VERDICTS, {"src/pkg/a.py"}, survivors=True)
    every = [k for codes in VERDICTS.values() for k in codes]
    chosen = {k for k in every for n in meta.names(VERDICTS, need)
              if k == n or fnmatch.fnmatch(k, n)}
    assert chosen == need


def test_a_function_glob_does_not_reach_a_function_sharing_its_prefix():
    verdicts = {"src/a.py": {"a.x_f__mutmut_1": None, "a.x_fg__mutmut_1": 1}}
    (name,) = meta.names(verdicts, {"a.x_f__mutmut_1"})
    assert not fnmatch.fnmatch("a.x_fg__mutmut_1", name)


def test_names_come_in_bounded_chunks():
    assert list(meta.chunks(["a", "b", "c", "d", "e"], 2)) == [["a", "b"], ["c", "d"], ["e"]]
    assert list(meta.chunks([], 400)) == []


def test_modules_are_complete_when_none_of_their_mutants_is_unchecked():
    assert meta.complete(VERDICTS, ["src/pkg/a.py", "src/pkg/b.py", "src/pkg/none.py"]) == {
        "src/pkg/a.py": False, "src/pkg/b.py": False, "src/pkg/none.py": True}
    done = {"src/a.py": {"k1": 0, "k2": 33, "k3": 1}}
    assert meta.complete(done, ["src/a.py"]) == {"src/a.py": True}


def test_counts_cover_only_the_modules_in_scope():
    assert meta.counts(VERDICTS, ["src/pkg/a.py"]) == {
        "not checked": 2, "no tests": 1, "killed": 1, "survived": 1}


def test_survivors_are_found_anywhere():
    assert meta.survivors(VERDICTS) == [
        ("pkg.a.x" + SEP + "C" + SEP + "m__mutmut_1", "src/pkg/a.py"),
        ("pkg.b.x_h__mutmut_2", "src/pkg/b.py")]


def test_only_an_unchecked_or_interrupted_mutant_has_no_verdict():
    assert meta.UNSETTLED == (None, 2)
    assert meta.RECHECKED == (0, 5, 33)


def test_an_interrupted_verdict_in_scope_is_needed_again():
    # mutmut 3.8 records 2 for a check the user interrupted; it is no verdict on the mutant, so
    # a cache holding one must not keep it from being run, and only in scope.
    verdicts = {"src/pkg/a.py": {"pkg.a.x_f__mutmut_1": 2, "pkg.a.x_f__mutmut_2": 1}}
    assert meta.needed(verdicts, {"src/pkg/a.py"}, survivors=False) == {"pkg.a.x_f__mutmut_1"}
    assert meta.needed(verdicts, set(), survivors=True) == set()


@pytest.mark.parametrize("code", [0, 5, 33])
def test_a_survivor_or_no_tests_verdict_is_rechecked_anywhere_unless_survivors_are_off(code):
    # Only a new test can change these, and mutmut's cache is keyed on source, so they are run
    # again on every run, repo-wide, like survivors; but they are verdicts, not gaps in one.
    verdicts = {"src/pkg/a.py": {"pkg.a.x_f__mutmut_1": code, "pkg.a.x_f__mutmut_2": 1}}
    assert meta.needed(verdicts, set(), survivors=True) == {"pkg.a.x_f__mutmut_1"}
    assert meta.needed(verdicts, {"src/pkg/a.py"}, survivors=False) == set()
    assert meta.complete(verdicts, ["src/pkg/a.py"]) == {"src/pkg/a.py": True}


@pytest.mark.parametrize("code", [*meta.STATUS, 99])  # STATUS holds None
def test_a_module_is_complete_exactly_when_nothing_in_it_is_needed(code):
    verdicts = {"src/a.py": {"a.x_f__mutmut_1": code}}
    need = meta.needed(verdicts, {"src/a.py"}, survivors=False)
    assert meta.complete(verdicts, ["src/a.py"])["src/a.py"] == (not need)
