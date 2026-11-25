"""The execution worker's entry point: its stages' rows, its one account and its broker.

The mode builds the broker, and this is the one file that does.

1. A backtest over a folder trades through the simulated broker, with no fault.
2. Every other mode, and a backtest over any other store, builds no broker. Each
   decision of each of this worker's stages halts with the reason stored, and the
   worker starts all the same, because a start that fails only restarts it. The
   reason names the parts of #10 that lift it.

A deploy to shadow starts on a merge, and nothing yet serves a kill switch, a
second instance or a lone leg there, so shadow waits for the parts that do. A
backtest's account goes to no real store, where its opening entry and its
simulated fills could never be deleted.

`core.Storage` reports nothing while it tries a read again, so the retry's
deadline has to be below the loop's heartbeat limit. Whoever builds both sets
them so (#11).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    DATA_STAGE,
    RECOVER_STAGE,
    SEND_STAGE,
    Alerts,
    BlobStore,
    Broker,
    Clock,
    Halt,
    Mode,
    Retry,
    StageKind,
    Storage,
    read_records,
)
from ithildinruntime.execution.account import Account
from ithildinruntime.execution.brokers.faults import Faults
from ithildinruntime.execution.brokers.log import SimulatedAccount
from ithildinruntime.execution.brokers.prices import StoredPrices
from ithildinruntime.execution.brokers.simulated import SimulatedBroker
from ithildinruntime.execution.recover import RecoverStage
from ithildinruntime.risk import Gate

from .loop import ReceiveLoop, Stage, Table

__all__ = ["RECOVER_ROW", "ExecutionWorker"]

RECOVER_ROW = Stage(
    RECOVER_STAGE,
    StageKind.CAPTURING,
    inputs=(DATA_STAGE,),
    keeps_order=True,
    follows=(SEND_STAGE,),
)

# The parts of #10 that shadow waits for, and the one paper waits for beside them.
_SHADOW = "#147, #148, #149, #150, #151, #155 and #156"
_PAPER = "#153"


class ExecutionWorker:
    """One execution process: its `Account`, its broker by the mode, and its stages' handlers.

    Nothing here raises while it is built but a table with no row of its own.
    `account` is the platform's alias, never the broker's number, and has no
    default. Two of these for one run draw two writers.
    """

    def __init__(
        self,
        *,
        mode: Mode | str,
        store: BlobStore,
        storage: Storage,
        account: str,
        clock: Clock,
        alerts: Alerts,
        retry: Retry,
        pause: Callable[[float], None],
        deployment: str,
        run: str,
        release: str,
        table: Table,
        stress_moves: Path,
    ) -> None:
        self._alerts = alerts
        self.account = Account(
            store=store,
            storage=storage,
            account=account,
            writer=secrets.token_hex(16),
            gate=Gate(read_records(stress_moves)),
            clock=clock,
            alerts=alerts,
            retry=retry,
            pause=pause,
        )
        self.brokers = _brokers(mode, store, storage, account, clock, deployment, run)
        stage = table.stage(RECOVER_STAGE)
        (data_stage,) = stage.inputs
        self.recover = RecoverStage(
            self.account,
            self.brokers,
            storage,
            clock,
            stage=stage.name,
            kind=stage.kind,
            data_stage=data_stage,
            data_hashes=table.hashes_for(data_stage),
            deployment=deployment,
            run=run,
            release=release,
            config_hashes=table.hashes_for(stage.name),
        )

    @property
    def handlers(self) -> Mapping[str, RecoverStage]:
        """Each of this worker's stages that is built, by its name."""
        return {RECOVER_STAGE: self.recover}

    def run(self, loop: ReceiveLoop) -> None:
        """Run the loop to its end, then store what the journal still lacks.

        An answer of a broker's that the journal has not taken is known to this
        process alone. One that still cannot be stored alarms, naming each entry
        that is lost with the process, and ends the process with an error, so
        the host's restart record says it too.
        """
        try:
            loop.run()
        finally:
            self._settle()

    def _settle(self) -> None:
        # The loop has ended, so there is nothing left to report to.
        try:
            self.account.settle(lambda: None)
            return
        except Halt as halt:
            said = halt.reason
        except Exception as error:
            said = type(error).__name__
        owed = "; ".join(self.account.unstored()) or "nothing it names"
        reason = f"the process ends and the journal was not given {owed}: {said}"
        try:
            self._alerts.alarm(reason)
        except BaseException as unsent:
            reason = f"{reason}. The alarm could not be sent: {type(unsent).__name__}"
        # Raised outside each handler, so what ended the loop is what it shows beside it.
        raise Halt(reason)


def _brokers(
    mode: Mode | str,
    store: BlobStore,
    storage: Storage,
    account: str,
    clock: Clock,
    deployment: str,
    run: str,
) -> Callable[[datetime], Broker] | Halt:
    """One decision's broker as the mode builds it, or why this run has none."""
    try:
        known = Mode(mode)
    except ValueError:
        return Halt("no broker is built: the mode is none of backtest, shadow, paper and live")
    match known:
        case Mode.SHADOW:
            return Halt(f"no broker is built on shadow until {_SHADOW} are closed")
        case Mode.PAPER:
            return Halt(f"no broker is built on paper until {_PAPER} is, once {_SHADOW} are closed")
        case Mode.LIVE:
            return Halt("no broker is built on live, which only the owner switches an account to")
        case Mode.BACKTEST:
            pass
    if not isinstance(store, FolderStore):
        return Halt(
            "no broker is built for a backtest over any store but a folder, since nothing its "
            "account stores elsewhere can be deleted"
        )
    log = SimulatedAccount.load(store, account, secrets.token_hex(16))

    def broker(at: datetime) -> Broker:
        prices = StoredPrices(storage, deployment, run, at)
        return SimulatedBroker(log, clock, prices, Faults())

    return broker
