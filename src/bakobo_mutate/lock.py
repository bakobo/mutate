"""One mutation run at a time per machine.

A run spawns one pytest per CPU, and Bakobo suites bind real ports, so two runs on one box
contend for both; a test that fails from contention is counted by mutmut as a killed mutant,
which hides survivors. The lock is a non-blocking ``flock`` on a file under the user's state
directory, so it covers runs in every repo, and a second run refuses by name rather than
queueing. The holder writes its pid and target into the file for that refusal to name.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bakobo.errors import ErrorCode  # type: ignore[import-untyped]

# The holder record is a pid and a path; anything longer than this is not one.
MAX_HOLDER_BYTES = 4096

LOCK_HELD = ErrorCode(
    code="e.state.conflict.mutation-lock.r",
    title="Another mutation run holds this machine's lock, so this one did not start.",
    detail=(
        "Process {pid} is running bakobo-mutate against {target}, and only one run may use "
        "this machine at a time. The lock is {path}."
    ),
    args=("pid", "target", "path"),
    hint=(
        "Wait for that run to finish, or stop it with Ctrl-C, and run this again. Its "
        "verdicts are saved as it goes, so stopping it loses only the mutants in flight."
    ),
)


def lock_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "bakobo-mutate" / "lock"


def _holder(text: str) -> tuple[str, str]:
    """(pid, target) from a holder record, or placeholders for one that cannot be read."""
    unknown = ("an unknown process", "an unknown repo")
    try:
        record = json.loads(text)
    except ValueError:
        return unknown
    if not (isinstance(record, dict) and isinstance(record.get("pid"), int)
            and isinstance(record.get("target"), str)):
        return unknown
    return str(record["pid"]), record["target"]


@contextmanager
def machine_lock(target: Path) -> Iterator[None]:
    """Hold the machine-wide lock for a run against ``target``, or raise LOCK_HELD."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            pid, holder = _holder(handle.read(MAX_HOLDER_BYTES + 1))
            raise LOCK_HELD(pid=pid, target=holder, path=path) from None
        handle.truncate(0)
        handle.write(json.dumps({"pid": os.getpid(), "target": str(target)}))
        handle.flush()
        yield
    finally:
        handle.close()  # closing the file releases the flock
