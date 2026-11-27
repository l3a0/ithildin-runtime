"""Each environment and deployment the release knows, and the one stage table of a deployment.

07-deployment.md's rule that the environment is read once, at the edge, puts one
table in the release: each environment's bucket, queues and deployments, and
each deployment's account alias. The mode follows from the environment. A
VM's provisioning step names only which environment it is in, and the entry
points read everything else here, so a module is handed a store, a queue and
an alert channel and never the environment's name.

1. **What the table holds.** Names, and no secret. The account alias is the
   platform's own, and the broker account behind it stays under the
   environment's parameter path, because the image and this repository are
   public. marketlake's bucket and prefix are machine values the provisioning
   step writes, as `runtime/settings.py` says.
2. **The one stage table.** `stage_table` puts every worker's rows together
   for the strategies a deployment names, with a `Hears` row of the run's stop
   for each worker. Every worker builds its table here, and #202 generates a
   deployment's routes file from the same one, so no second table drifts.
3. **Schedules live here.** design.md's `### Strategies, schedules and
   decisions` leaves where schedules live to #11. A schedule here stays outside
   its strategy's hash, so changing one moves no hash, until #11 folds the
   schedule into the strategy's file.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from ithildinruntime.core import (
    STRATEGIES,
    BeforeClose,
    EventKind,
    Mode,
    ScheduleTime,
    StrategyConfig,
    load_strategies,
)

from .alpha_worker import alpha_rows
from .cost_worker import cost_rows
from .data_worker import DATA_ROW
from .execution_worker import RECOVER_ROW, SEND_ROW
from .loop import Hears, Table
from .portfolio_worker import portfolio_row
from .risk_worker import RISK_ROW

__all__ = [
    "ENVIRONMENTS",
    "STOP_ROUTE",
    "WORKERS",
    "Deployment",
    "Environment",
    "deployment_table",
    "environment",
    "stage_table",
]

# The route of the run's stop, which every worker hears.
STOP_ROUTE = "runs/{deployment}/*/stop.json"
# Every worker, by the component its rows name, in the order a person reads them.
WORKERS = ("data", "alpha", "cost", "risk", "portfolio", "execution")


@dataclass(frozen=True)
class Deployment:
    """One deployment: its account's alias, its strategies and when each decides."""

    name: str
    account: str
    schedules: Mapping[str, tuple[ScheduleTime, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "schedules", MappingProxyType({k: tuple(v) for k, v in self.schedules.items()})
        )
        if not self.schedules:
            raise ValueError(f"deployment {self.name} names no strategy")

    @property
    def strategies(self) -> tuple[str, ...]:
        return tuple(sorted(self.schedules))


@dataclass(frozen=True)
class Environment:
    """One environment: its mode, its bucket, its queues' prefix and its one deployment."""

    name: str
    mode: Mode
    region: str
    bucket: str
    queue_prefix: str
    deployment: Deployment
    # Seconds a received message stays hidden before SQS hands it out again.
    visibility: int = field(default=60)

    def queue_name(self, component: str) -> str:
        """Each reader's queue is `<prefix>-<reader>`, as the test environment's are."""
        return f"{self.queue_prefix}-{component}"


ENVIRONMENTS: Mapping[str, Environment] = MappingProxyType(
    {
        "shadow": Environment(
            name="shadow",
            mode=Mode.SHADOW,
            region="us-east-1",
            bucket="ithildin-runtime-shadow",
            queue_prefix="ithildin-shadow",
            deployment=Deployment("shadow", "shadow1", {"chan_bollinger": (BeforeClose(20),)}),
        ),
    }
)


def environment(name: str) -> Environment:
    """The environment the release's table names `name`, or a `KeyError` naming each it knows."""
    try:
        return ENVIRONMENTS[name]
    except KeyError:
        known = ", ".join(sorted(ENVIRONMENTS))
        raise KeyError(f"the release knows no environment {name!r}, only {known}") from None


def stage_table(strategies: Iterable[StrategyConfig]) -> Table:
    """Every worker's rows for `strategies`, and a `Hears` row of the run's stop for each worker."""
    strategies = tuple(strategies)
    rows = (
        DATA_ROW,
        *alpha_rows(strategies),
        *cost_rows(strategies),
        RISK_ROW,
        portfolio_row(strategies),
        RECOVER_ROW,
        SEND_ROW,
    )
    hears = tuple(Hears(component, EventKind.CREATED, STOP_ROUTE) for component in WORKERS)
    return Table(stages=rows, hears=hears, strategies=strategies)


def deployment_table(deployment: Deployment, folder: Path = STRATEGIES) -> Table:
    """The stage table of `deployment`, over the strategy files in `folder`."""
    return stage_table(load_strategies(folder, list(deployment.strategies)))
