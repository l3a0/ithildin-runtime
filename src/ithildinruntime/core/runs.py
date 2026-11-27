"""The records the supervisor stores at keys of their own, and the cancel cutoff.

docs/architecture/08-crosscutting-concepts.md's `#### What an output file
holds` gives every blob stored at a key of its own a `format` of its own, so a
reader can tell one release's record from another's. Each record here is at
format 1.

1. **`RunStop`** is the run's clean-shutdown record, at `run_stop_key`. A run
   with no stop ended some other way, and the next start holds its strategies.
2. **`RunHalt`** halts the whole run, at `run_halt_key`. Execution's controls
   refuse every order while one exists, and the supervisor stores no stop
   after one.
3. **`SupervisorStart`** records one start of the supervisor on one boot of
   its machine, at `supervisor_start_key`. A start on another boot of the same
   run is a machine that was lost, and one on the same boot is a restart the
   host supervised. `RestartRecord` is the host's record of an exit and names
   no boot, so it cannot tell the two apart.
4. **`StrategyHalt`** is one halt of one strategy on one account, in the
   strategy's halts folder. Execution's controls refuse every new order of a
   strategy while its folder holds any blob, and read nothing inside it.

`CANCEL_CUTOFF` is how many minutes before the close the exchange's closing
auction stops taking cancels, as 06-runtime.md's `#### The cancel cutoff`
says. It errs early, and a change to it is a change to a limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .records import _Record

__all__ = [
    "CANCEL_CUTOFF",
    "RUN_RECORD_FORMAT",
    "RunHalt",
    "RunStop",
    "StrategyHalt",
    "SupervisorStart",
]

RUN_RECORD_FORMAT = 1
CANCEL_CUTOFF = 10


@dataclass(frozen=True)
class RunStop(_Record):
    """The run's clean-shutdown record: every decision had a result, and the run ended."""

    format: int
    deployment: str
    run: str
    release: str
    stopped_at: datetime


@dataclass(frozen=True)
class RunHalt(_Record):
    """A halt of the whole run, and why."""

    format: int
    deployment: str
    run: str
    reason: str
    halted_at: datetime


@dataclass(frozen=True)
class SupervisorStart(_Record):
    """One start of the supervisor on one boot of its machine, counted from 1 on that boot."""

    format: int
    deployment: str
    run: str
    boot: str
    count: int
    release: str
    started_at: datetime


@dataclass(frozen=True)
class StrategyHalt(_Record):
    """One halt of one strategy on one account, naming the run that caused it."""

    format: int
    account: str
    strategy: str
    cause: str
    reason: str
    run: str
    halted_at: datetime
