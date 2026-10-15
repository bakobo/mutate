# bakobo-mutate

[![CI](https://github.com/bakobo/mutate/actions/workflows/ci.yml/badge.svg)](https://github.com/bakobo/mutate/actions/workflows/ci.yml)

Branch coverage says a line ran under some test. A surviving mutant says no test would notice that line being wrong. `bakobo-mutate` mutation-tests what has landed on a repo's default branch, using that repo's own [mutmut](https://github.com/boxed/mutmut), and reports each mutant that survived the repo's test suite.

It is a developer tool that runs on a developer's machine, on demand, and never in hosted CI: mutmut's cache of verdicts is only useful where it persists, and a box that persists is a developer's. It is installed once and pointed at a repo, and it is never a dependency of the repo it tests:

```sh
uv tool install git+https://github.com/bakobo/mutate
```

## The on-demand workflow

Run it from inside the repo you want to test, or name the repo:

```sh
bakobo-mutate run              # mutate what is stale, re-check every survivor
bakobo-mutate ticks --dry-run  # see which survivors would become ticks
bakobo-mutate ticks            # file them
```

`bakobo-mutate run [TARGET]` works in a persistent checkout of the target at `<TARGET>/.worktrees/mutation`, detached at `origin/<default branch>` and refreshed before every run, so it measures what has landed rather than anyone's work in progress, and mutmut's `mutants/` cache survives between runs. It syncs that checkout with `uv sync --frozen --all-extras` and runs the checkout's own `.venv/bin/mutmut` (`--mutmut` overrides it). It refuses to touch a checkout that holds changes of its own.

A run covers the modules named with `--module`, or else the modules that are stale: those the default branch changed after the commit they were last mutated at, and those whose last run was interrupted. Before a repo's first run there is no record yet, so it covers the modules a first-parent commit touched since `--since` (default `7 days ago`). A module is a `.py` file under `[tool.mutmut].source_paths` that `do_not_mutate` does not exclude and that defines a function or method.

mutmut keeps a verdict per mutant and resets only the functions whose source changed, but it re-runs every mutant it is given by name, cached or not, and its cache is keyed on source rather than tests. So a run first lets mutmut generate mutants, then names to it only what needs running: every mutant in scope that has no verdict yet (never checked, or its check was interrupted), and every survivor and every "no tests" verdict anywhere in the repo, since only a new test can change those and re-running them is the only way it is noticed. `--no-survivors` skips the second part. A survivor or "no tests" verdict is a verdict, so it does not leave its module incomplete. Names are grouped into one glob per function where a whole function is needed and passed in chunks of `--chunk` (default 400). A run over unchanged code with `--no-survivors` runs no mutant at all.

Ctrl-C, or SIGTERM, stops mutmut. Every verdict mutmut saved before then is kept, the run is reported as incomplete and exits 130, and the next run starts with the mutants that were never checked. There is no time budget.

Only one run may use a machine at a time, across every repo, because a run starts one pytest per CPU and Bakobo suites bind real ports. A second run refuses with `e.state.conflict.mutation-lock.r`, naming the process and repo that hold the lock, which lives at `$XDG_STATE_HOME/bakobo-mutate/lock` (default `~/.local/state/bakobo-mutate/lock`).

Each run writes four files to `<TARGET>/.ignored/mutation/`: `report.json` (one record per surviving mutant), `summary.md`, `mutmut-run.log` and `stamp.json`, which records per module the commit it was mutated at and whether every one of its mutants now has a verdict. A run that fails leaves the previous `stamp.json` as it was, so a failure never makes a module look fresh; an interrupted run still writes it, marking what it did not finish as incomplete. `.worktrees/`, `.ignored/` and `mutants/` are added to the repo's `.git/info/exclude` when git does not already ignore them. A run exits 0 whether or not any mutant survived, 130 when interrupted, and 2 when it could not be carried out or was refused.

`bakobo-mutate ticks [TARGET]` files one [`tick`](https://github.com/dhh1128/tick) per behavioural survivor in `<TARGET>/.ignored/mutation/report.json`, or in the report named with `--report`. A survivor that already has a tick, open or closed, is not filed again. `--dry-run` says what would be filed and files nothing.

`bakobo-mutate stale [TARGET]` prints the modules a run would choose, one per line, and nothing when none is stale. A repo that has never been mutated prints `never mutated`. `--fetch` fetches origin first.

`bakobo-mutate <command> --help` lists each command's options.

## Survivor classes

Every survivor in a report carries one of three classes. Only `behavioural` survivors become ticks; the other two are counted in the report and the summary.

- `behavioural`: the default, and what anything the tool cannot vouch for falls back to. A survivor here is either a test gap or an equivalent mutant, and the tick it becomes says to decide which.
- `string-only`: the mutation changes only the text of a message, such as the string passed to an exception, `print`, a logging call, `warnings.warn` or a sink the repo declares. A literal compared, used as a key or carrying format placeholders is never message text, and neither is emptying or filling a literal.
- `diagnostic`: the mutation lies wholly inside the arguments of the exception a `raise` constructs, positional or keyword, or in that raise's `from` cause, and is inert there: it changes the text of a string constant that reaches the argument only through `+`, `%`, `.format` or an f-string's literal parts (a format string's placeholders excluded), replaces a whole argument, keyword value or cause with `None`, or drops keyword arguments. The refusal still happens and only what it says changes. Any other change inside the arguments, such as `1 / 0` becoming `1 * 0`, or a string used as a key or passed to another call, can change which exception escapes and stays behavioural, as does a mutation that changes which exception is raised, removes the raise, drops its cause or touches anything else. `string-only` takes precedence where both apply.

`ticks` decides each class again from the record's diff, its message sink and the source at the report's commit, read with `git show` in the target, never from the class stored in the report. A survivor whose source git cannot produce is filed.

## The weekly staleness sweep

Results exist only where someone ran the tool, so freshness is signalled rather than assumed. Enrol each repo once:

```sh
cd path/to/your-repo && bakobo-mutate enroll
```

Enrolment is a list of absolute repo roots in `$XDG_CONFIG_HOME/bakobo-mutate/repos` (default `~/.config/bakobo-mutate/repos`); `bakobo-mutate unenroll` removes one. `bakobo-mutate sweep` fetches every enrolled repo, works out what is stale, and keeps at most one open `mutation-stale` tick in that repo's own ledger. It files one when something is stale and none is open, notes the open one with the current list when the list has changed since its last note, and ticks it off when nothing is stale. A repo that cannot be swept is reported and skipped, and the sweep exits 1 at the end if any was.

[`cron/bakobo-mutate-sweep.cron`](cron/bakobo-mutate-sweep.cron) runs the sweep every Monday at 06:17 local time. Install it with:

```sh
(crontab -l 2>/dev/null; cat cron/bakobo-mutate-sweep.cron) | crontab -
```

It calls `$HOME/.local/bin/bakobo-mutate`, where `uv tool install` puts it, and appends its output to `~/.local/state/bakobo-mutate/sweep.log`.

## Enrolling a repo

A repo is ready for `bakobo-mutate run` when it has mutmut in one of its dependency groups, committed in `uv.lock`, and configures it in its `pyproject.toml`; mutmut reads the pytest arguments from the same table. The `dev` group is synced by default:

```toml
[dependency-groups]
dev = ["mutmut==3.8.0", "pytest"]

[tool.mutmut]
source_paths = ["src/yourpkg"]
do_not_mutate = ["*/yourpkg/vendored.py"]
pytest_add_cli_args = ["-n0", "--no-cov"]
```

A repo may also declare its own message sinks, callables whose string arguments are message text rather than behaviour. `Name` makes every string argument of a call spelled `Name(...)` message text, and `Name.kwarg` makes only that keyword argument so. A dotted callee of any length followed by `()`, such as `console.out.write()` or `lines.append()`, makes every string argument of a call spelled exactly that way message text; `x.write(...)` does not match `console.out.write()`.

```toml
[tool.mutation]
message_sinks = ["Refusal.missing", "Refusal.detail", "console.out.write()", "lines.append()"]
```

A malformed declaration fails the run before mutmut starts.

## Development

```sh
uv sync
uv run pytest
```

The suite runs at 100% branch coverage. In the unit tests mutmut, uv and `tick` are stub executables and git is the real one over scratch repositories, so nothing touches a real ledger. The tests marked `integration` drive the real mutmut 3.8.0 on a toy project built in a temporary directory; they run by default and need `uv` on `PATH`, plus network access unless the uv cache already holds mutmut, in which case set `UV_OFFLINE=1`.

Licensed under Apache-2.0; see `LICENSE`.
