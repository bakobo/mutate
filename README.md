# bakobo-mutate

[![CI](https://github.com/bakobo/mutate/actions/workflows/ci.yml/badge.svg)](https://github.com/bakobo/mutate/actions/workflows/ci.yml)

Branch coverage says a line ran under some test. A surviving mutant says no test would notice that line being wrong. `bakobo-mutate` mutation-tests the source modules a repo's default branch changed recently, using that repo's own [mutmut](https://github.com/boxed/mutmut), and reports each mutant that survived the repo's test suite.

It is a developer tool, installed once on a developer's machine and pointed at a repo. It is never a dependency of the repo it tests:

```sh
uv tool install git+https://github.com/bakobo/mutate
```

## Commands

`bakobo-mutate run` picks every `.py` file under `[tool.mutmut].source_paths` that a first-parent commit on `--ref` (default `HEAD`) touched since `--since` (default `7 days ago`), minus anything `[tool.mutmut].do_not_mutate` excludes, or the files named with `--module`. It runs mutmut over them within `--budget-minutes`, and writes `mutation-report.json` (one record per surviving mutant) and `summary.md` to `--out` (default `mutation-out`). It exits 0 whether or not any mutant survived, and 2 only when the run itself could not be carried out. Run it from the target repo's root, or pass `--root`.

`bakobo-mutate ticks` files one [`tick`](https://github.com/dhh1128/tick) per surviving mutant in a report, read from `--report`, or downloaded from a workflow run with `gh`. A survivor that already has a tick, open or closed, is not filed again. `--dry-run` says what would be filed and files nothing.

A survivor whose mutation changes only the text of a message, such as the string passed to an exception, `print`, a logging call or `warnings.warn`, is classed `string-only`. It is counted in the report and not ticked. Every other survivor is `behavioural`.

`bakobo-mutate <command> --help` lists each command's options.

## Enrolling a repo

A repo enrols by having mutmut in its own development environment and configuring it in its `pyproject.toml`; mutmut reads the pytest arguments from the same table. `bakobo-mutate run` uses the `mutmut` on `PATH`, so activate the repo's virtualenv first or name the executable with `--mutmut`.

```toml
[tool.mutmut]
source_paths = ["src/yourpkg"]
do_not_mutate = ["*/yourpkg/vendored.py"]
pytest_add_cli_args = ["-n0", "--no-cov"]
```

A repo may also declare its own message sinks, callables whose string arguments are message text rather than behaviour. `Name` makes every string argument of a call spelled `Name(...)` message text, and `Name.kwarg` makes only that keyword argument so:

```toml
[tool.mutation]
message_sinks = ["Refusal.missing", "Refusal.detail"]
```

A malformed declaration fails the run before mutmut starts.

## Development

```sh
uv sync
uv run pytest
```

The suite runs at 100% branch coverage. mutmut, `gh` and `tick` are replaced by stub executables in the tests, so nothing here runs a real mutation, reaches GitHub or touches a real ledger.

Licensed under Apache-2.0; see `LICENSE`.
