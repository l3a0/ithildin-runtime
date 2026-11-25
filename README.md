# ithildin-runtime

A systematic trading platform built after the one Rishi Narang outlines in
*Inside the Black Box* (3rd ed., Wiley, 2024). One runtime runs backtest, paper
and live trading, so what is tested is what runs live.

## Status

Only `core`, the types and shared code every module imports, the two stores, the two read-only buckets, the two queues and the two clocks in `adapters`, the journal's writer and reader, what an entry holds, an order's states and the book the entries replay to in `journal`, the daily bars of `data`, the alpha model of the first strategy in `alpha`, the transaction cost model in `cost`, the risk gate, the risk model and its stage in `risk`, the sizing of a target and its stage in `portfolio`, the one path an order takes to a broker in `execution`, which no worker calls yet, and in `runtime` the receive loop and the data, alpha, cost, risk and portfolio workers' entry points are built so far. The first milestone is a walking skeleton that runs in
shadow mode and trades on Alpaca's paper account, and [its issue](https://github.com/l3a0/ithildin-runtime/issues/4) says what it must leave usable and how
its tests show it works.

## Where things live

- [CLAUDE.md](CLAUDE.md) holds the hard limits and the rules agents work by.
- [docs/team-charter.md](docs/team-charter.md) holds the team's roles, the
  lifecycle from plan to operate, and why the MVP is a walking skeleton.
- [docs/design.md](docs/design.md) holds the premise and the reasoning.
- [docs/architecture/](docs/architecture/00-README.md) draws how slice 1's modules
  fit together, as diagrams, one page per section of the arc42 template.
- [docs/what-a-person-runs.md](docs/what-a-person-runs.md) holds the command line
  tool the owner and the operator run, how an alarm leads to a command, and the
  command that measures a stress move.
- [docs/halts-and-drills.md](docs/halts-and-drills.md) holds the steps of a sort
  and every drill.
- [infra/](infra/) holds the test environment's OpenTofu configuration, which
  [.github/workflows/infra.yml](.github/workflows/infra.yml) checks on every pull
  request, as the required `infra` check, and applies after each merge that
  changes it.
- The issue tracker holds the scope of everything unbuilt.
- The [build board](https://l3a0.github.io/ithildin-runtime/) shows what is
  in flight and what to take next, rebuilt from the tracker.

The repo started from [repo-template](https://github.com/l3a0/repo-template),
and will share code with its siblings through
[ithildin-core](https://github.com/l3a0/ithildin-core) once it has code to share.

## Running the checks locally

```bash
uv sync --dev
uv run ruff check
uv run ruff format --check
uv run pytest
```

markdownlint has no Python package, so it runs in CI rather than locally. The
prose checks it cannot do run in the test suite.

The cases marked `aws` run against the test environment in AWS, so the default
run leaves them out and only `ci.yml`'s `aws` job runs them, on a push to `main`.
The OpenTofu checks need OpenTofu 1.13 and no AWS credential.

```bash
tofu fmt -check -recursive infra/
tofu -chdir=infra/test/bootstrap init -backend=false && tofu -chdir=infra/test/bootstrap test
tofu -chdir=infra/test init -backend=false && tofu -chdir=infra/test test
tofu -chdir=infra/modules/bucket init -backend=false && tofu -chdir=infra/modules/bucket test
tofu -chdir=infra/modules/routes init -backend=false && tofu -chdir=infra/modules/routes test
```
