"""The persistent checkout a target is mutated in, and the mutmut that runs there.

Each target keeps ``<target>/.worktrees/mutation``, detached at ``origin/<default>`` and
refreshed before every run, so the tool measures what has landed rather than anyone's work in
progress, and mutmut's ``mutants/`` cache persists between runs. ``mutants/`` (in the checkout)
and ``.ignored/`` (where reports are written in the target) are added to the repo's
``.git/info/exclude`` when git does not already ignore them, so neither ever shows up as
something to commit.

The checkout is synced with ``uv sync --frozen --all-extras`` and runs its own mutmut, from
its own ``.venv``, so two targets on different mutmut versions never fight (this.i 4xz5c93n).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from bakobo.errors import ErrorCode  # type: ignore[import-untyped]

CHECKOUT = Path(".worktrees") / "mutation"
# Lines of a failing command's output carried into its error.
TAIL = 15

NOT_A_REPO = ErrorCode(
    code="e.state.missing.git-repo.f",
    title="The target is not inside a git checkout, so there is nothing to mutate.",
    detail="{path} is not inside a non-bare git repository.",
    args=("path",),
    hint="Run this from inside the repo you mean, or name its root as the target.",
)
NO_DEFAULT_BRANCH = ErrorCode(
    code="e.state.missing.origin-head.f",
    title="The target's default branch is unknown, so there is no landed code to mutate.",
    detail="{target} has no refs/remotes/origin/HEAD, which is how its default branch is found.",
    args=("target",),
    hint=(
        "Run 'git remote set-head origin --auto' in the target to record origin's default "
        "branch, then run this again."
    ),
)
NOT_A_WORKTREE = ErrorCode(
    code="e.state.conflict.mutation-checkout-path.f",
    title="The mutation checkout's path is taken by something that is not a git worktree.",
    detail="{path} exists but is not a worktree of {target}, so it was left alone.",
    args=("path", "target"),
    hint="Move or delete that directory; the next run will create the checkout there.",
)
CHECKOUT_DIRTY = ErrorCode(
    code="e.state.conflict.mutation-checkout.f",
    title="The mutation checkout has changes of its own, so it was not refreshed.",
    detail=(
        "{path} must hold nothing but origin's default branch and ignored files, and git "
        "reports:\n{status}"
    ),
    args=("path", "status"),
    hint=(
        "That checkout belongs to bakobo-mutate; nobody should edit it. Discard the changes "
        "there with 'git checkout -- . && git clean -fd', which keeps the ignored mutants/ "
        "cache, then run this again."
    ),
)
GIT_FAILED = ErrorCode(
    code="e.env.git.mutation-checkout.r",
    title="git could not prepare the mutation checkout.",
    detail="'{command}' exited {exit_code}:\n{output}",
    args=("command", "exit_code", "output"),
    hint=(
        "If that is a network failure, run this again. Otherwise run the same git command in "
        "the target, where its full error is visible."
    ),
)
SYNC_FAILED = ErrorCode(
    code="e.env.uv-sync.f",
    title="uv could not install the target's environment in the mutation checkout.",
    detail="'uv sync --frozen --all-extras' in {path} exited {exit_code}:\n{output}",
    args=("path", "exit_code", "output"),
    hint=(
        "Make the same command succeed in a clean clone of the target's default branch, "
        "usually by committing an up-to-date uv.lock; running this again will fail the "
        "same way."
    ),
)
NO_MUTMUT = ErrorCode(
    code="e.state.missing.mutmut.f",
    title="The target's environment has no mutmut, so nothing could be mutated.",
    detail="There is no {path} after the environment was synced.",
    args=("path",),
    hint=(
        "Add mutmut to one of the target's dependency groups (dev is synced by default) and "
        "commit uv.lock, or name an executable with --mutmut."
    ),
)


def _git(cwd: Path, *args: str, git: str = "git") -> subprocess.CompletedProcess:
    return subprocess.run([git, "-C", str(cwd), *args], capture_output=True, text=True)


def _tail(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stdout or "") + (proc.stderr or "")
    return "\n".join(text.strip().splitlines()[-TAIL:]) or "(it printed nothing)"


def _must(cwd: Path, *args: str, git: str = "git") -> str:
    proc = _git(cwd, *args, git=git)
    if proc.returncode != 0:
        raise GIT_FAILED(command=" ".join(["git", *args]), exit_code=proc.returncode,
                         output=_tail(proc))
    return proc.stdout


def target_root(start: Path, git: str = "git") -> Path:
    """The main checkout's root of the repo ``start`` is in, even from inside a worktree."""
    proc = _git(start, "rev-parse", "--path-format=absolute", "--git-common-dir", git=git)
    common = Path(proc.stdout.strip())
    if proc.returncode != 0 or common.name != ".git":
        raise NOT_A_REPO(path=start)
    return common.parent


def default_branch(target: Path, git: str = "git") -> str:
    proc = _git(target, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", git=git)
    if proc.returncode != 0:
        raise NO_DEFAULT_BRANCH(target=target)
    return proc.stdout.strip().removeprefix("refs/remotes/origin/")


def fetch(target: Path, git: str = "git") -> None:
    _must(target, "fetch", "--quiet", "origin", git=git)


def ensure_ignored(repo: Path, probe: str, pattern: str, git: str = "git") -> None:
    """Append ``pattern`` to the repo's info/exclude unless git already ignores ``probe``."""
    if _git(repo, "check-ignore", "-q", probe, git=git).returncode == 0:
        return
    exclude = Path(_must(repo, "rev-parse", "--path-format=absolute", "--git-path",
                         "info/exclude", git=git).strip())
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    exclude.write_text(text + pattern + "\n")


def ensure_checkout(target: Path, git: str = "git") -> tuple[Path, str]:
    """The checkout, fetched and detached at origin's default branch, and that branch's name."""
    wt = target / CHECKOUT
    fetch(target, git)
    branch = default_branch(target, git)
    ensure_ignored(target, ".ignored/mutation/report.json", ".ignored/", git)
    upstream = f"origin/{branch}"
    if not wt.exists():
        _must(target, "worktree", "add", "--quiet", "--detach", str(wt), upstream, git=git)
        ensure_ignored(wt, "mutants/probe.meta", "mutants/", git)
        return wt, branch
    top = _git(wt, "rev-parse", "--show-toplevel", git=git).stdout.strip()
    if not top or Path(top).resolve() != wt.resolve():
        raise NOT_A_WORKTREE(path=wt, target=target)
    ensure_ignored(wt, "mutants/probe.meta", "mutants/", git)
    status = _must(wt, "status", "--porcelain", git=git)
    if status.strip():
        raise CHECKOUT_DIRTY(path=wt, status=status.rstrip())
    _must(wt, "checkout", "--quiet", "--detach", upstream, git=git)
    return wt, branch


def clean_env() -> dict[str, str]:
    """This process's environment without anything that would point uv at another venv."""
    return {k: v for k, v in os.environ.items()
            if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")}


def sync(wt: Path, uv: str = "uv") -> None:
    proc = subprocess.run([uv, "sync", "--frozen", "--all-extras"], cwd=wt, env=clean_env(),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SYNC_FAILED(path=wt, exit_code=proc.returncode, output=_tail(proc))


def find_mutmut(wt: Path, override: str | None) -> str:
    if override:
        return override
    mutmut = wt / ".venv" / "bin" / "mutmut"
    if not mutmut.exists():
        raise NO_MUTMUT(path=mutmut)
    return str(mutmut)
