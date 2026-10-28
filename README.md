# ithildin-runtime

A systematic trading platform built after the one Rishi Narang outlines in
*Inside the Black Box* (3rd ed., Wiley, 2024). One runtime runs backtest, paper
and live trading, so what is tested is what runs live.

## Status

Nothing is built yet. The first milestone is a walking skeleton that trades on
paper, and [docs/build-plan.md](docs/build-plan.md) says what it must leave
usable.

## Where things live

- [CLAUDE.md](CLAUDE.md) holds the hard limits and the rules agents work by.
- [docs/team-charter.md](docs/team-charter.md) holds the team's roles, the
  lifecycle from plan to operate, and the MVP.
- [docs/design.md](docs/design.md) holds the premise and the reasoning.
- The issue tracker holds the scope of everything unbuilt.

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
