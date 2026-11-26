"""The execution worker's entry point: its stages' rows, its one account and its broker.

The mode builds the broker, and this is the one file that does. It also hands a
backtest's opening of its account the simulated broker's own, through
`open_backtest`, since `execution` imports no broker.

1. A backtest over a folder trades through the simulated broker, with no fault.
2. Every other mode, a backtest over any other store, and a stage table whose row of
   recover is not the one this file declares, build no broker. A row of send that is
   not the one this file declares halts send alone. Each
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
from decimal import Decimal
from pathlib import Path

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    DATA_STAGE,
    PORTFOLIO_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
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
from ithildinruntime.execution.opening import open_account
from ithildinruntime.execution.recover import RecoverStage
from ithildinruntime.execution.send import SendStage
from ithildinruntime.risk import Gate

from .loop import ReceiveLoop, Stage, Table

__all__ = ["RECOVER_ROW", "SEND_ROW", "ExecutionWorker", "open_backtest"]

RECOVER_ROW = Stage(
    RECOVER_STAGE,
    StageKind.CAPTURING,
    inputs=(DATA_STAGE,),
    keeps_order=True,
    follows=(SEND_STAGE,),
)

SEND_ROW = Stage(
    SEND_STAGE,
    StageKind.CAPTURING,
    inputs=(DATA_STAGE, RECOVER_STAGE, RISK_STAGE, PORTFOLIO_STAGE),
    keeps_order=True,
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
        self._owner_step = storage.owner_step
        writer = secrets.token_hex(16)
        self.account = Account(
            store=store,
            storage=storage,
            account=account,
            writer=writer,
            gate=Gate(read_records(stress_moves)),
            clock=clock,
            alerts=alerts,
            retry=retry,
            pause=pause,
        )
        self._brokers = _brokers(mode, store, storage, account, clock, deployment, run)
        stage = table.stage(RECOVER_STAGE)
        if stage != RECOVER_ROW:
            # Recover would read the broker before the last send went out.
            self._brokers = Halt(
                f"the table's row of {RECOVER_STAGE} is not the one it declares, so set "
                "recover's row in the deployment's stage table to "
                "ithildinruntime.runtime.execution_worker.RECOVER_ROW, which captures, reads "
                f"data, keeps the plan's order and follows {SEND_STAGE}"
            )
        # A row that reads nothing still builds the stage from a table that declares the
        # data row, and the stage then halts each decision before it reads data.
        data_stage = (stage.inputs or RECOVER_ROW.inputs)[0]
        self.recover = RecoverStage(
            self.account,
            self._brokers,
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

        self.send: SendStage | None = None
        if SEND_STAGE in {row.name for row in table.stages}:
            self.send = self._send(table, storage, clock, writer, deployment, run, release)

    def _send(
        self,
        table: Table,
        storage: Storage,
        clock: Clock,
        writer: str,
        deployment: str,
        run: str,
        release: str,
    ) -> SendStage:
        """The send stage, which halts each decision where the table's row is not `SEND_ROW`."""
        stage = table.stage(SEND_STAGE)
        brokers, inputs = self._brokers, {}
        if stage != SEND_ROW:
            # The stage would plan on a target nothing sized, or before recover's book.
            brokers = Halt(
                f"the table's row of {SEND_STAGE} is not the one it declares, so set send's row "
                "in the deployment's stage table to "
                "ithildinruntime.runtime.execution_worker.SEND_ROW, which captures, reads data, "
                "recover, risk and portfolio construction, and keeps the plan's order"
            )
        else:
            inputs = {name: table.hashes_for(name) for name in stage.inputs}
        return SendStage(
            self.account,
            brokers,
            storage,
            clock,
            strategies=table.strategies,
            writer=writer,
            stage=stage.name,
            kind=stage.kind,
            inputs=inputs,
            deployment=deployment,
            run=run,
            release=release,
            config_hashes=table.hashes_for(stage.name),
        )

    @property
    def handlers(self) -> Mapping[str, RecoverStage | SendStage]:
        """Each of this worker's stages that is built, by its name."""
        built: dict[str, RecoverStage | SendStage] = {RECOVER_STAGE: self.recover}
        if self.send is not None:
            built[SEND_STAGE] = self.send
        return built

    def run(self, loop: ReceiveLoop) -> None:
        """Run the loop to its end, then store what the journal still lacks.

        An answer of a broker's that the journal has not taken is known to this
        process alone. So the worker pages before its last tries, naming each
        entry it owes. One that still cannot be stored alarms again, naming each
        entry lost with the process, and ends the process with an error, so the
        host's restart record says it too. An alarm the channel would not take
        ends it the same way. A last settle that raised an error other than a halt pages
        with the error's type and the owner's step and ends it the same way, and the
        error says where that page was not sent.
        """
        try:
            loop.run()
        finally:
            self._settle()

    def _settle(self) -> None:
        # The loop has ended, so there is nothing left to report to, and nothing beats.
        account, step = self.account, f"The owner's step: {self._owner_step}"
        if owed := account.unstored():
            # Paged ahead of the tries, which can outlast the host's stop.
            account.alarm(
                f"the process is ending before the journal holds {'; '.join(owed)}, and "
                f"tries once more. {step}",
                lambda: None,
            )
        failed = False
        try:
            account.settle(lambda: None)
            said = ""
        except Halt as halt:
            said = f": {halt.reason}"
        except Exception as error:
            said, failed = f": {type(error).__name__}", True
        owed = account.unstored()
        if owed:
            reason = f"the process ends and the journal was not given {'; '.join(owed)}{said}"
            account.alarm(f"{reason}. {step}", lambda: None)
        unsent = account.unsent()
        if not owed and not unsent and not failed:
            # An account that cannot be used holds nothing, so it loses nothing.
            return
        lost = [*(f"the journal was not given {one}" for one in owed)]
        lost += [f"the alarm '{one}' was not sent" for one in unsent]
        reason = f"the process ends and {'; '.join(lost) or 'its last settle failed'}{said}"
        if failed and not owed:
            # A settle that failed cannot vouch for its own count of what it owes,
            # so the owner checks the broker's site as where something was lost.
            reason = f"{reason}. {step}"
            if account.alarm(reason, lambda: None):
                reason = f"{reason}. The alarm that says so was not sent"
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


def open_backtest(
    mode: Mode | str, store: BlobStore, account: str, cash: Decimal, at: datetime
) -> None:
    """Open a backtest's account with `cash`, in its journal and its simulated broker at once.

    `at` is the close of the session before the backtest's first decision. Every
    other mode halts, since a person opens a shadow or paper account (#11), and
    so does a store that is not a folder, where nothing the account stores could
    be deleted.
    """
    try:
        known = Mode(mode)
    except ValueError:
        known = None
    if known is not Mode.BACKTEST:
        raise Halt(f"an account is opened here only for a backtest, and the mode is {mode}")
    if not isinstance(store, FolderStore):
        raise Halt("a backtest's account is opened only over a folder")
    open_account(store, SimulatedBroker.open, account, cash, at, secrets.token_hex(16))
