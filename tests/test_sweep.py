"""``stale``, ``enroll``, ``unenroll`` and the weekly ``sweep`` (this.i 24xzjnz7).

git is real; tick is a recorder that logs argv and cwd, so no real ledger is touched.
"""

from __future__ import annotations

import os
import re
import textwrap
from pathlib import Path

import pytest
from conftest import git, push_change, recorded, recorder

from bakobo_mutate import checkout, scope, sweep

FUNC = "def f():\n    return 1\n"


@pytest.fixture
def target(origin):
    """A target whose origin has two modules, fetched, with nothing mutated yet."""
    _, target, seed = origin
    (seed / "pyproject.toml").write_text(textwrap.dedent("""\
        [tool.mutmut]
        source_paths = ["src/pkg"]
        """))
    (seed / "src" / "pkg").mkdir(parents=True)
    (seed / "src" / "pkg" / "a.py").write_text(FUNC)
    push_change(seed, "src/pkg/b.py", FUNC, "modules")
    git(target, "fetch", "-q", "origin")
    return target, seed


def head(target) -> str:
    return git(target, "rev-parse", "origin/main").strip()


def mutated(target, *incomplete):
    """Stamp both modules as mutated at origin/main, some of them incomplete."""
    at = head(target)
    scope.write_stamp(target, scope.Stamp(base=at, modules={
        m: scope.Entry(at, m not in incomplete) for m in ("src/pkg/a.py", "src/pkg/b.py")}))


# --- stale ------------------------------------------------------------------------------------


def test_a_repo_never_mutated_says_so(target, capsys):
    target, _ = target
    assert sweep.stale_main([str(target)]) == 0
    assert capsys.readouterr().out == "never mutated\n"


def test_a_fresh_repo_prints_nothing(target, capsys):
    target, _ = target
    mutated(target)
    assert sweep.stale_main([str(target)]) == 0
    assert capsys.readouterr().out == ""


def test_stale_modules_are_listed_one_per_line(target, capsys):
    target, seed = target
    mutated(target, "src/pkg/b.py")
    push_change(seed, "src/pkg/a.py", FUNC + "\n")
    git(target, "fetch", "-q", "origin")
    assert sweep.stale_main([str(target)]) == 0
    assert capsys.readouterr().out == "src/pkg/a.py\nsrc/pkg/b.py\n"


def test_stale_fetches_only_when_asked(target, capsys, monkeypatch):
    target, seed = target
    mutated(target)
    push_change(seed, "src/pkg/a.py", FUNC + "\n")
    monkeypatch.chdir(target)
    assert sweep.stale_main([]) == 0
    assert capsys.readouterr().out == ""
    assert sweep.stale_main(["--fetch"]) == 0
    assert capsys.readouterr().out == "src/pkg/a.py\n"


def test_stale_refuses_with_a_code(target, capsys):
    target, _ = target
    mutated(target)
    git(target, "remote", "set-head", "origin", "-d")
    assert sweep.stale_main([str(target)]) == 2
    assert capsys.readouterr().err.startswith(f"{checkout.NO_DEFAULT_BRANCH.code}: ")


def test_stale_refuses_a_since_that_is_no_past_date(target, capsys):
    target, _ = target
    assert sweep.stale_main([str(target), "--since", "nonsense"]) == 2
    assert capsys.readouterr().err.startswith(f"{scope.SINCE_MALFORMED.code}: ")


# --- enrolment --------------------------------------------------------------------------------


def enrolled() -> list[str]:
    path = sweep.repos_path()
    return path.read_text().splitlines() if path.exists() else []


def test_the_enrolment_list_lives_under_the_config_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert sweep.repos_path() == tmp_path / "cfg" / "bakobo-mutate" / "repos"
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert sweep.repos_path() == tmp_path / ".config" / "bakobo-mutate" / "repos"


def test_enrol_records_the_absolute_repo_root_once(target, monkeypatch, capsys):
    target, _ = target
    (target / "sub").mkdir()
    monkeypatch.chdir(target / "sub")
    assert sweep.enroll_main([]) == 0
    assert sweep.enroll_main([str(target)]) == 0
    assert enrolled() == [str(target.resolve())]
    out = capsys.readouterr().out
    assert f"enrolled {target.resolve()}" in out and "already enrolled" in out


def test_enrolling_a_directory_outside_a_repo_is_refused(tmp_path, capsys):
    (tmp_path / "plain").mkdir()
    assert sweep.enroll_main([str(tmp_path / "plain")]) == 2
    assert capsys.readouterr().err.startswith(f"{checkout.NOT_A_REPO.code}: ")
    assert enrolled() == []


def test_unenrol_removes_a_repo_and_keeps_the_rest(target, tmp_path, capsys):
    target, _ = target
    sweep.repos_path().parent.mkdir(parents=True)
    sweep.repos_path().write_text(f"/gone/repo\n{target.resolve()}\n\n")
    assert sweep.unenroll_main([str(target)]) == 0
    assert enrolled() == ["/gone/repo"]
    # A repo that no longer exists can still be named.
    assert sweep.unenroll_main(["/gone/repo"]) == 0
    assert enrolled() == []
    assert sweep.unenroll_main(["/gone/repo"]) == 0
    assert "was not enrolled" in capsys.readouterr().out


def test_unenrol_defaults_to_the_repo_around_the_cwd(target, monkeypatch):
    target, _ = target
    monkeypatch.chdir(target)
    sweep.enroll_main([])
    assert sweep.unenroll_main([]) == 0
    assert enrolled() == []


def test_unenrol_outside_a_repo_without_a_target_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert sweep.unenroll_main([]) == 2
    assert capsys.readouterr().err.startswith(f"{checkout.NOT_A_REPO.code}: ")


# --- the sweep --------------------------------------------------------------------------------


def tick_stub(tmp_path, **answers):
    spec = {"ls": {"out": "(no ticks)\n"}, "add": {"out": "4abc  title\npaste the mark\n"},
            "note": {"out": "noted\n"}, "off": {"out": "ticked off\n"},
            "show": {"out": "# title\n"}}
    spec.update(answers)
    return recorder(tmp_path / "tickbin", "tick", spec)


def tick_calls(tmp_path) -> list[list[str]]:
    return [c["argv"] for c in recorded(tmp_path / "tickbin", "tick")]


def run_sweep(tick, *extra) -> int:
    return sweep.sweep_main(["--tick", str(tick), *extra])


def test_a_never_mutated_repo_gets_one_tick(target, tmp_path, capsys):
    target, _ = target
    sweep.enroll_main([str(target)])
    tick = tick_stub(tmp_path)
    assert run_sweep(tick) == 0
    calls = recorded(tmp_path / "tickbin", "tick")
    assert [c["argv"][0] for c in calls] == ["ls", "add", "note"]
    assert all(c["cwd"] == str(target.resolve()) for c in calls)
    assert calls[0]["argv"] == ["ls", "--tag", "mutation-stale"]
    add = calls[1]["argv"]
    assert add[:5] == ["add", "--kind", "debt", "--tag", "mutation-stale"]
    assert "never" in add[5] and "bakobo-mutate run" in add[5]
    assert calls[2]["argv"][1] == "4abc" and "stale-set: " in calls[2]["argv"][2]
    assert "filed 4abc" in capsys.readouterr().out


def test_a_stale_repo_gets_a_tick_listing_its_modules(target, tmp_path):
    target, _ = target
    mutated(target, "src/pkg/a.py")
    sweep.enroll_main([str(target)])
    tick = tick_stub(tmp_path)
    assert run_sweep(tick) == 0
    _, add, note = tick_calls(tmp_path)
    assert "1 module" in add[5] and "modules" not in add[5] and "bakobo-mutate run" in add[5]
    assert "- src/pkg/a.py" in note[2] and "src/pkg/b.py" not in note[2]
    assert f"stale-set: {sweep.digest(['src/pkg/a.py'])}" in note[2]


def test_several_stale_modules_are_counted_in_the_title(target, tmp_path):
    target, _ = target
    mutated(target, "src/pkg/a.py", "src/pkg/b.py")
    sweep.enroll_main([str(target)])
    run_sweep(tick_stub(tmp_path))
    assert "2 modules" in tick_calls(tmp_path)[1][5]


def open_tick(tmp_path, body, *ids):
    listing = "".join(f"{i}  debt   mutation results are stale\n" for i in ids)
    return tick_stub(tmp_path, ls={"out": listing}, show={"out": body})


def test_an_unchanged_list_is_not_noted_again(target, tmp_path, capsys):
    target, _ = target
    mutated(target, "src/pkg/a.py")
    sweep.enroll_main([str(target)])
    body = (f"# t\n\n- 2026-10-01 stale-set: {sweep.digest(['x.py'])}\n"
            f"- 2026-10-08 stale-set: {sweep.digest(['src/pkg/a.py'])}\n")
    assert run_sweep(open_tick(tmp_path, body, "2xyz")) == 0
    assert tick_calls(tmp_path) == [["ls", "--tag", "mutation-stale"], ["show", "2xyz"]]
    assert "unchanged 2xyz" in capsys.readouterr().out


def test_a_changed_list_is_noted_on_the_open_tick(target, tmp_path):
    target, _ = target
    mutated(target, "src/pkg/a.py")
    sweep.enroll_main([str(target)])
    body = (f"# t\n\n- 2026-10-01 stale-set: {sweep.digest(['src/pkg/a.py'])}\n"
            f"- 2026-10-08 stale-set: {sweep.digest(['x.py'])}\n")
    run_sweep(open_tick(tmp_path, body, "2xyz"))
    ls, show, note = tick_calls(tmp_path)
    assert note[:2] == ["note", "2xyz"] and "- src/pkg/a.py" in note[2]


def test_a_tick_without_a_stale_set_is_noted(target, tmp_path):
    target, _ = target
    mutated(target, "src/pkg/a.py")
    sweep.enroll_main([str(target)])
    run_sweep(open_tick(tmp_path, "# a tick someone edited by hand\n", "2xyz"))
    assert tick_calls(tmp_path)[-1][:2] == ["note", "2xyz"]


def test_extra_open_ticks_are_closed(target, tmp_path):
    target, _ = target
    mutated(target, "src/pkg/a.py")
    sweep.enroll_main([str(target)])
    body = f"- stale-set: {sweep.digest(['src/pkg/a.py'])}\n"
    run_sweep(open_tick(tmp_path, body, "2xyz", "3abc"))
    assert ["off", "3abc"] in tick_calls(tmp_path)
    assert ["off", "2xyz"] not in tick_calls(tmp_path)


def test_a_fresh_repo_closes_its_open_tick(target, tmp_path, capsys):
    target, _ = target
    mutated(target)
    sweep.enroll_main([str(target)])
    assert run_sweep(open_tick(tmp_path, "", "2xyz")) == 0
    assert tick_calls(tmp_path) == [["ls", "--tag", "mutation-stale"], ["off", "2xyz"]]
    assert "fresh; ticked off 2xyz" in capsys.readouterr().out


def test_a_fresh_repo_without_a_tick_is_left_alone(target, tmp_path, capsys):
    target, _ = target
    mutated(target)
    sweep.enroll_main([str(target)])
    assert run_sweep(tick_stub(tmp_path)) == 0
    assert tick_calls(tmp_path) == [["ls", "--tag", "mutation-stale"]]
    assert ": fresh" in capsys.readouterr().out


def test_the_sweep_fetches_before_judging(target, tmp_path):
    target, seed = target
    mutated(target)
    sweep.enroll_main([str(target)])
    push_change(seed, "src/pkg/b.py", FUNC + "\n")  # landed, not yet fetched
    run_sweep(tick_stub(tmp_path))
    assert "- src/pkg/b.py" in tick_calls(tmp_path)[2][2]


def test_a_failing_repo_is_reported_and_the_sweep_goes_on(target, tmp_path, capsys):
    target, _ = target
    mutated(target)
    plain = tmp_path / "plain"
    plain.mkdir()
    sweep.repos_path().parent.mkdir(parents=True)
    sweep.repos_path().write_text(f"{tmp_path / 'missing'}\n{plain}\n{target.resolve()}\n")
    assert run_sweep(tick_stub(tmp_path)) == 1
    captured = capsys.readouterr()
    assert captured.err.count(checkout.NOT_A_REPO.code) == 2
    assert f"{target.resolve()}: fresh" in captured.out
    assert "2 of 3 repos failed" in captured.err


def test_a_failed_fetch_is_a_failed_repo(target, tmp_path, capsys):
    target, _ = target
    sweep.enroll_main([str(target)])
    git(target, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    assert run_sweep(tick_stub(tmp_path)) == 1
    assert checkout.GIT_FAILED.code in capsys.readouterr().err
    assert tick_calls(tmp_path) == []


@pytest.mark.parametrize("answers", [{"ls": {"exit": 1, "err": "not initialized\n"}},
                                     {"add": {"exit": 1, "err": "ledger locked\n"}}],
                         ids=["ls", "add"])
def test_a_failing_tick_is_a_failed_repo(target, tmp_path, capsys, answers):
    target, _ = target
    sweep.enroll_main([str(target)])
    assert run_sweep(tick_stub(tmp_path, **answers)) == 1
    err = capsys.readouterr().err
    assert str(target.resolve()) in err and "tick" in err


def test_a_missing_tick_is_a_failed_repo(target, tmp_path, capsys):
    target, _ = target
    sweep.enroll_main([str(target)])
    assert sweep.sweep_main(["--tick", str(tmp_path / "no-such-tick")]) == 1
    assert "no-such-tick" in capsys.readouterr().err


def test_a_sweep_refuses_a_since_that_is_no_past_date(target, tmp_path, capsys):
    target, _ = target
    sweep.enroll_main([str(target)])
    assert run_sweep(tick_stub(tmp_path), "--since", "nonsense") == 1
    assert scope.SINCE_MALFORMED.code in capsys.readouterr().err
    assert tick_calls(tmp_path) == []


def test_an_empty_enrolment_sweeps_nothing(capsys):
    assert sweep.sweep_main([]) == 0
    assert "No repo is enrolled" in capsys.readouterr().out


def test_tick_runs_without_its_update_check(target, tmp_path, monkeypatch):
    target, _ = target
    sweep.enroll_main([str(target)])
    seen = []
    real = sweep.subprocess.run

    def spy(argv, **kwargs):
        if argv[0].endswith("tick"):
            seen.append(kwargs["env"].get("TICK_NO_UPDATE_CHECK"))
        return real(argv, **kwargs)

    monkeypatch.setattr(sweep.subprocess, "run", spy)
    run_sweep(tick_stub(tmp_path))
    assert seen and set(seen) == {"1"}


# --- the cron line ----------------------------------------------------------------------------


def test_the_cron_file_runs_the_sweep_weekly():
    cron = Path(__file__).resolve().parent.parent / "cron" / "bakobo-mutate-sweep.cron"
    lines = [x for x in cron.read_text().splitlines() if x.strip() and not x.startswith("#")]
    assert len(lines) == 1
    fields = lines[0].split(None, 5)
    assert fields[:5] == ["17", "6", "*", "*", "1"]  # Monday 06:17 local
    assert "$HOME/.local/bin/bakobo-mutate sweep" in fields[5]
    assert re.search(r"crontab", cron.read_text())  # its header says how to install it
    assert os.path.basename(cron) == "bakobo-mutate-sweep.cron"
