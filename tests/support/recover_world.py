"""A `SimWorld` whose recover is the real stage, run as the receive loop runs it.

Every figure is made up. `decide` stores what data stored, runs the stage, stores
its result and then journals the plan, so each decision starts from the last one's
recover. `attempt` runs the stage once more and returns its result unstored.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime

from ithildinruntime.core import (
    ACCOUNT_OUTPUT,
    BOOK_OUTPUT,
    CAPS_OUTPUT,
    CONSTRAINTS_OUTPUT,
    DATA_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    SNAPSHOT_OUTPUT,
    VALUATION_OUTPUT,
    AccountRead,
    BlobRef,
    Book,
    Broker,
    Constraints,
    DecisionResult,
    Halt,
    HardCaps,
    OrderIntent,
    OutputFile,
    Snapshot,
    StageKind,
    TargetPortfolio,
    Valuation,
    caps_key,
    decode,
    decode_output,
    encode,
    journal_entry_key,
    output_key,
    result_key,
    sha256_of,
)
from ithildinruntime.execution.account import Account
from ithildinruntime.execution.recover import RecoverStage
from ithildinruntime.journal import DecisionEntry, ReadEntry
from ithildinruntime.runtime.loop import Item
from tests.support import gates
from tests.support.order_world import caps_blob
from tests.support.sim_world import ACCOUNT, DEPLOYMENT, TABLE, SimWorld

HASHES = {gates.CHAN: "c" * 64}
RELEASE = "r1"
FILES = {
    ACCOUNT_OUTPUT: AccountRead,
    BOOK_OUTPUT: Book,
    CAPS_OUTPUT: HardCaps,
    VALUATION_OUTPUT: Valuation,
}


class Waits:
    """The report a handler is handed, which no stop ends."""

    def __init__(self) -> None:
        self.reports = 0

    def __call__(self) -> None:
        self.reports += 1

    def wait_for_stop(self, timeout: float) -> bool:
        raise AssertionError("a clock in a test waits for nothing")


@dataclass
class RecoverWorld(SimWorld):
    # The release and the strategy configuration the stage runs under.
    release: str = RELEASE
    hashes: dict[str, str] = field(default_factory=lambda: dict(HASHES))

    def __post_init__(self) -> None:
        super().__post_init__()
        clock = self.clock
        clock.wait_until = lambda at, report: setattr(clock, "at", max(at, clock.at))

    # The stage

    def stage(
        self,
        account: Account | None = None,
        brokers: Callable[[datetime], Broker] | Halt | None = None,
    ) -> RecoverStage:
        return RecoverStage(
            account or self.account,
            brokers or (lambda at: self.broker()),
            self.storage(),
            self.clock,  # type: ignore[arg-type]
            stage=RECOVER_STAGE,
            kind=StageKind.CAPTURING,
            data_stage=DATA_STAGE,
            data_hashes=HASHES,
            deployment=DEPLOYMENT,
            run=self.run,
            release=self.release,
            config_hashes=self.hashes,
        )

    def attempt(self, stage: RecoverStage | None = None) -> DecisionResult:
        """One attempt at the newest decision, whose result the loop has not stored."""
        return (stage or self.stage())(Item(self.at, RECOVER_STAGE), Waits())

    def recover(self) -> DecisionResult:
        """The stage for the newest decision, and its result stored as the loop stores it."""
        result = self.attempt()
        self.store.put(result_key(DEPLOYMENT, self.run, self.at, RECOVER_STAGE), encode(result))
        self.result = result
        return result

    # A decision

    def prepare(self, at: datetime, snapshot: Snapshot | None = None) -> None:
        """What stands before recover: the caps blob, and data's result and snapshot."""
        quiet, self.store.quiet = self.store.quiet, True
        self.store.put(caps_key(ACCOUNT), caps_blob(self.caps, ACCOUNT))
        session = TABLE.opened_by(at).day
        self.at, self.run, self.clock.at = at, session.isoformat(), max(at, self.clock.at)
        self._file(at, DATA_STAGE, SNAPSHOT_OUTPUT, Snapshot, snapshot or self.snapshot(at))
        self.store.quiet = quiet
        # How many entries the journal held when this decision was made ready.
        self.before = len(self.rows())

    def decide(
        self,
        at: datetime,
        orders: Iterable[OrderIntent] = (),
        snapshot: Snapshot | None = None,
        recover: bool = True,
    ) -> tuple[OrderIntent, ...]:
        self.prepare(at, snapshot)
        if recover:
            result = self.recover()
            assert result.halt is None, result.halt.reason
        return self.plan(orders)

    def plan(self, orders: Iterable[OrderIntent]) -> tuple[OrderIntent, ...]:
        """Risk's limits and the decision's plan, as the send stage journals it."""
        quiet, self.store.quiet = self.store.quiet, True
        planned = tuple(orders)
        self._file(self.at, RISK_STAGE, CONSTRAINTS_OUTPUT, Constraints, self.constraints)
        refs = {
            name: self.ref(stage, name)
            for name, stage in (
                (SNAPSHOT_OUTPUT, DATA_STAGE),
                (CONSTRAINTS_OUTPUT, RISK_STAGE),
                (VALUATION_OUTPUT, RECOVER_STAGE),
            )
        }
        ref = BlobRef(f"runs/{self.at.isoformat()}", "a" * 64)
        read = self._record(ReadEntry("execution/send", self.at, ref, self.at))
        target = TargetPortfolio(self.at, gates.CHAN, {gates.GLD: gates.d(1)})
        self._record(
            DecisionEntry(
                deployment=DEPLOYMENT,
                run=self.run,
                decision_at=self.at,
                targets=(target,),
                orders=planned,
                off=(),
                inputs=refs,
                read=read,
                synced=read,
            )
        )
        self.store.quiet = quiet
        return planned

    # What is stored

    def stored_snapshot(self, snapshot: Snapshot) -> None:
        """Put another snapshot in place of the newest decision's."""
        self._file(self.at, DATA_STAGE, SNAPSHOT_OUTPUT, Snapshot, snapshot)

    def ref(self, stage: str, name: str) -> BlobRef:
        key = output_key(DEPLOYMENT, self.run, self.at, stage, name)
        return BlobRef(key, sha256_of(self.store.blobs[key]))

    def key(self, name: str) -> str:
        return output_key(DEPLOYMENT, self.run, self.at, RECOVER_STAGE, name)

    def file(self, name: str) -> OutputFile:
        return decode_output(self.store.blobs[self.key(name)], FILES[name])

    def entry(self, name: str):
        (found,) = self.file(name).entries
        return found

    def stored(self) -> dict[str, bytes]:  # type: ignore[override]
        """Every file of this decision's recover, apart from its result."""
        prefix = self.key("x").rpartition("/")[0] + "/"
        blobs = self.store.blobs
        return {
            key.removeprefix(prefix): data
            for key, data in sorted(blobs.items())
            if key.startswith(prefix) and not key.endswith("/result.json")
        }

    def rows(self) -> list[dict]:
        """The stored journal, each entry as plain JSON."""
        prefix = journal_entry_key(ACCOUNT, 1).rpartition("/")[0] + "/"
        blobs = self.store.blobs
        return [json.loads(blobs[key]) for key in sorted(blobs) if key.startswith(prefix)]

    def kinds(self, since: int = 0) -> list[str]:
        return [row["body"]["kind"] for row in self.rows()[since:]]

    def result_of(self, at: datetime) -> DecisionResult:
        run = TABLE.opened_by(at).day.isoformat()
        key = result_key(DEPLOYMENT, run, at, RECOVER_STAGE)
        return decode(self.store.blobs[key], DecisionResult)

    def rewrite_caps(self, **changes: object) -> None:
        self.store.put(caps_key(ACCOUNT), caps_blob(replace(self.caps, **changes), ACCOUNT))


__all__ = ["FILES", "HASHES", "RELEASE", "RecoverWorld", "Waits"]
