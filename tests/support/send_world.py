"""A `RecoverWorld` whose send stage is the real one, after the real recover.

Every figure is made up. `ready` stores what stands before the send stage at a
decision: the caps, data's snapshot, recover's files from the real stage, risk's
constraints and portfolio construction's targets, whose header names each
strategy's forecast and schedule. `send_attempt` runs the send stage once and returns
its result unstored, and `send` stores it as the loop does.

`Gatekeeper` stands in for the gate where a case needs it to answer one way at
the plan's ask and another at the send, or to raise. `rebuilt` asks the real gate
again about every `verdict` the journal holds, from what that entry names alone.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal

from ithildinruntime.core import (
    CONSTRAINTS_OUTPUT,
    DATA_STAGE,
    FORECAST_OUTPUT,
    PORTFOLIO_STAGE,
    RECOVER_STAGE,
    RISK_SECTION,
    RISK_STAGE,
    SCHEDULE_OUTPUT,
    SEND_STAGE,
    TARGET_OUTPUT,
    BlobHeader,
    BlobRef,
    Book,
    Broker,
    Constraints,
    DecisionResult,
    GateInputs,
    Halt,
    HaltRecord,
    HardCaps,
    Limit,
    OutputFile,
    Retry,
    SendOutcome,
    Snapshot,
    StageKind,
    StopState,
    StrategyConfig,
    TargetPortfolio,
    Valuation,
    Verdict,
    decode,
    decode_output,
    encode,
    encode_output,
    output_key,
    parse_strategy,
    result_key,
    sha256_of,
)
from ithildinruntime.execution.account import Account
from ithildinruntime.execution.send import OUTCOME_OUTPUT, SendStage
from ithildinruntime.journal import Journal, VerdictEntry, record_of
from ithildinruntime.risk import Gate
from ithildinruntime.runtime.loop import Item
from tests.support import gates
from tests.support.recover_world import HASHES, RecoverWorld, Waits
from tests.support.sim_world import ACCOUNT, DEPLOYMENT

CHAN = gates.CHAN
EXECUTION = '[execution]\norder = "market_on_close"\n'


def strategy(name: str = CHAN, execution: str = EXECUTION) -> StrategyConfig:
    return parse_strategy(name, f'instruments = ["GLD", "USO"]\n{execution}'.encode())


def risked(made: StrategyConfig, stop: Decimal) -> StrategyConfig:
    """`made` with a `[risk]` section whose `loss_stop` is `stop`, as the day's loss reads it.

    Only the field the loss reads is written, since the risk stage's own file
    stands in for the rest of the section here.
    """
    return replace(made, sections={**made.sections, RISK_SECTION: {"loss_stop": stop}})


class Gatekeeper:
    """The real gate, but for the answers a case plants by order and by call.

    `planted` maps an order's id to a list of answers, one per call of
    `check` for it, oldest first: `None` asks the real gate, a `Halt` is
    raised, `"refuse"` turns the real answer into a refusal on the order's
    size, and `"loss_past"` reads the day's loss past its stop. A call past
    the list asks the real gate.
    """

    def __init__(self, gate: Gate) -> None:
        self._gate = gate
        self.planted: dict[str, list[object]] = {}
        self.asked: list[str] = []
        # Each moment the day's loss was read at with no order, as the order path reads it.
        self.losses: list[datetime] = []

    def check(self, intent, book, snapshot, constraints, inputs, now) -> Verdict:
        name = intent.client_order_id
        self.asked.append(name)
        answers = self.planted.get(name, [])
        answer = answers.pop(0) if answers else None
        if isinstance(answer, Halt):
            raise answer
        verdict = self._gate.check(intent, book, snapshot, constraints, inputs, now)
        if answer == "loss_past":
            loss = verdict.loss
            past = replace(loss, loss=loss.bound + 1, past=True)
            return replace(verdict, loss=past)
        if answer == "refuse":
            size = abs(intent.qty)
            return replace(
                verdict, approved=False, limit=Limit.MAX_ORDER_QTY, value=size, bound=size / 2
            )
        return verdict

    def loss(self, *args, **kwargs):
        return self._gate.loss(*args, **kwargs)

    def loss_against(self, book, snapshot, stop, valuation, state, now):
        self.losses.append(now)
        return self._gate.loss_against(book, snapshot, stop, valuation, state, now)


@dataclass
class SendWorld(RecoverWorld):
    strategies: tuple[StrategyConfig, ...] = field(default_factory=lambda: (strategy(),))
    # The constraints of each strategy but the first, which `constraints` holds, in risk's file.
    others: tuple[Constraints, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        records = [r for c in (self.constraints, *self.others) for r in gates.fitting(c, self.caps)]
        self.gate = Gate(tuple(records))
        self.keeper = Gatekeeper(self.gate)
        self.account = self.fresh_account()

    def fresh_account(self, writer: str | None = None) -> Account:
        """An `Account` as a new process builds it, over the same store and the planted gate."""
        made = Account(
            store=self.store,  # type: ignore[arg-type]
            storage=self.storage(),
            account=ACCOUNT,
            writer=writer or next(self.writers, "d" * 32),
            gate=self.keeper,  # type: ignore[arg-type]
            clock=self.clock,  # type: ignore[arg-type]
            alerts=self.alarms,
            retry=self.retry,
            pause=self.clock.wait,
        )
        return made

    @property
    def retry(self) -> Retry:
        return Retry(timedelta(seconds=10), 1.0, 4.0)

    def restart(self, sim_writer: str | None = None) -> None:
        super().restart(sim_writer)
        if hasattr(self, "keeper"):
            self.account = self.fresh_account()

    # A decision

    def ready(
        self,
        at: datetime,
        targets: Mapping[str, Mapping[str, object]] | None = None,
        recover: bool = True,
    ) -> None:
        """Everything the send stage reads at `at`, with recover run for real."""
        self.prepare(at)
        if recover:
            result = self.recover()
            assert result.halt is None, result.halt.reason
        quiet, self.store.quiet = self.store.quiet, True
        self._files(
            at, RISK_STAGE, CONSTRAINTS_OUTPUT, Constraints, (self.constraints, *self.others)
        )
        wanted = targets if targets is not None else {CHAN: {gates.GLD: 0, gates.USO: 0}}
        self.targets(at, wanted)
        self.store.quiet = quiet

    def _files(self, at: datetime, stage: str, name: str, kind: type, made: tuple) -> BlobRef:
        """A stage's file of several entries, and its result, stored as `_file` stores one."""
        ref = self._file(at, stage, name, kind, made[0])
        if len(made) == 1:
            return ref
        data = self.store.blobs[ref.key]
        header = decode_output(data, kind).header
        data = encode_output(kind, OutputFile(header, made, False))
        self.store.put(ref.key, data)
        ref = BlobRef(ref.key, sha256_of(data))
        result = DecisionResult(header, (ref,), None)
        self.store.put(result_key(DEPLOYMENT, self.run, at, stage), encode(result))
        return ref

    def targets(
        self, at: datetime, wanted: Mapping[str, Mapping[str, object]], named: bool = True
    ) -> BlobRef:
        """Store the targets, under a header naming each strategy's forecast and schedule."""
        inputs = {}
        for name in sorted(wanted) if named else ():
            for component, output in (("alpha", FORECAST_OUTPUT), ("cost", SCHEDULE_OUTPUT)):
                key = output_key(DEPLOYMENT, self.run, at, f"{component}/{name}", output)
                inputs[key] = sha256_of(key.encode())
        header = BlobHeader(
            format=1,
            deployment=DEPLOYMENT,
            run=self.run,
            stage=PORTFOLIO_STAGE,
            kind=str(StageKind.DETERMINISTIC),
            decision_at=at,
            inputs=inputs,
            release="r1",
            config_hashes=HASHES,
        )
        made = tuple(
            TargetPortfolio(at, name, {i: gates.d(q) for i, q in shares.items()})
            for name, shares in sorted(wanted.items())
        )
        key = output_key(DEPLOYMENT, self.run, at, PORTFOLIO_STAGE, TARGET_OUTPUT)
        data = encode_output(TargetPortfolio, OutputFile(header, made, False))
        self.store.put(key, data)
        ref = BlobRef(key, sha256_of(data))
        result = DecisionResult(header, (ref,), None)
        self.store.put(result_key(DEPLOYMENT, self.run, at, PORTFOLIO_STAGE), encode(result))
        return ref

    def portfolio_halted(self, at: datetime, reason: str) -> None:
        """Store portfolio construction's result as a halt, as the loop stores a stage's halt."""
        self.targets(at, {})
        self.stage_halted(at, PORTFOLIO_STAGE, reason)

    def result_key(self, stage: str) -> str:
        """The key of a stage's result at the newest decision."""
        return result_key(DEPLOYMENT, self.run, self.at, stage)

    def result_ref(self, stage: str) -> BlobRef:
        """A stage's stored result at the newest decision, as a halt's `cause` names it."""
        key = self.result_key(stage)
        return BlobRef(key, sha256_of(self.store.blobs[key]))

    def stage_halted(self, at: datetime, stage: str, reason: str) -> BlobRef:
        """Store a stage's stored result again as a halt, and give the halted result's ref."""
        key = result_key(DEPLOYMENT, self.run, at, stage)
        header = decode(self.store.blobs[key], DecisionResult).header
        halted = DecisionResult(header, (), HaltRecord(reason, None, (), (), ()))
        data = encode(halted)
        self.store.put(key, data)
        return BlobRef(key, sha256_of(data))

    def risked(self) -> tuple[StrategyConfig, ...]:
        """Each strategy, given the `loss_stop` its constraints in risk's file hold.

        A strategy with a `[risk]` section of its own keeps it, and one that no
        constraints name is left with none.
        """
        stops = {c.strategy: c.loss_stop for c in (self.constraints, *self.others)}
        return tuple(
            s if RISK_SECTION in s.sections or s.name not in stops else risked(s, stops[s.name])
            for s in self.strategies
        )

    def sender(
        self,
        account: Account | None = None,
        brokers: Callable[[datetime], Broker] | Halt | None = None,
        writer: str = "e" * 32,
    ) -> SendStage:
        inputs = {name: HASHES for name in (DATA_STAGE, RECOVER_STAGE, RISK_STAGE, PORTFOLIO_STAGE)}
        return SendStage(
            account or self.account,
            brokers or (lambda at: self.broker()),
            self.storage(),
            self.clock,  # type: ignore[arg-type]
            strategies=self.risked(),
            writer=writer,
            stage=SEND_STAGE,
            kind=StageKind.CAPTURING,
            inputs=inputs,
            deployment=DEPLOYMENT,
            run=self.run,
            release=self.release,
            config_hashes=self.hashes,
        )

    def send_attempt(self, stage: SendStage | None = None) -> DecisionResult:
        """One attempt of the send stage at the newest decision, unstored."""
        return (stage or self.sender())(Item(self.at, SEND_STAGE), Waits())

    def send_stage(self, stage: SendStage | None = None) -> DecisionResult:
        """The send stage at the newest decision, its result stored as the loop stores it."""
        result = self.send_attempt(stage)
        self.store.put(result_key(DEPLOYMENT, self.run, self.at, SEND_STAGE), encode(result))
        return result

    def outcome(self, result: DecisionResult) -> SendOutcome:
        assert result.halt is None, result.halt.reason
        key = output_key(DEPLOYMENT, self.run, self.at, SEND_STAGE, OUTCOME_OUTPUT)
        (made,) = decode_output(self.store.blobs[key], SendOutcome).entries
        return made

    def rebuilt(self) -> int:
        """Ask the real gate again about every `verdict` the journal holds, and count them.

        Each is rebuilt from what its entry names alone: the book through its
        entry number, the snapshot, the constraints and the valuation by their
        SHA-256, the caps it holds, its time and the loss stop's state. The
        gate is the world's, built with the stress records no entry holds. A
        verdict the gate would now give otherwise fails the test.
        """
        reader = Journal(self.store, ACCOUNT)  # type: ignore[arg-type]
        entries = [record_of(entry) for entry in reader.read()]
        count = 0
        for entry in entries:
            if not isinstance(entry, VerdictEntry):
                continue
            name = entry.client_order_id
            order = reader.view().orders[name].intent
            book: Book = reader.view(through=entry.book).book
            (snapshot,) = self._entries(entry.snapshot, Snapshot)
            (limits,) = [
                c
                for c in self._entries(entry.constraints, Constraints)
                if c.strategy == order.strategy
            ]
            (valuation,) = self._entries(entry.valuation, Valuation)
            handed = GateInputs(
                caps=decode(entry.caps, HardCaps),
                valuation=valuation,
                stop=StopState(entry.stop),
                close_entry=entry.close_entry,
            )
            again = self.gate.check(order, book, snapshot, limits, handed, entry.now)
            assert again == entry.verdict, name
            count += 1
        return count

    def _entries[T](self, ref: BlobRef, kind: type[T]) -> tuple[T, ...]:
        data = self.store.blobs[ref.key]
        assert sha256_of(data) == ref.sha256, ref.key
        return decode_output(data, kind).entries

    def states(self) -> dict[str, str]:
        """Each order of the journal by its id, in its state."""
        return {name: str(o.state) for name, o in self.view().orders.items()}

    def submits(self) -> list[str]:
        """Each order id the simulated broker placed, in the order it came."""
        found = []
        for data in self.blobs().values():
            found += [o["client_order_id"] for o in json.loads(data)["events"]["placed"]]
        return found


__all__ = ["CHAN", "Gatekeeper", "SendWorld", "risked", "strategy"]
