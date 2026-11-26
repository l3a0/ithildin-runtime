"""A `RecoverWorld` whose send stage is the real one, after the real recover.

Every figure is made up. `ready` stores what stands before the send stage at a
decision: the caps, data's snapshot, recover's files from the real stage, risk's
constraints and portfolio construction's targets, whose header names each
strategy's forecast and schedule. `send_attempt` runs the send stage once and returns
its result unstored, and `send` stores it as the loop does.

`Gatekeeper` stands in for the gate where a case needs it to answer one way at
the plan's ask and another at the send, or to raise.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from ithildinruntime.core import (
    CONSTRAINTS_OUTPUT,
    DATA_STAGE,
    FORECAST_OUTPUT,
    PORTFOLIO_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    SCHEDULE_OUTPUT,
    SEND_STAGE,
    TARGET_OUTPUT,
    BlobHeader,
    BlobRef,
    Broker,
    Constraints,
    DecisionResult,
    Halt,
    Limit,
    OutputFile,
    Retry,
    SendOutcome,
    StageKind,
    StrategyConfig,
    TargetPortfolio,
    Verdict,
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
from ithildinruntime.risk import Gate
from ithildinruntime.runtime.loop import Item
from tests.support import gates
from tests.support.recover_world import HASHES, RecoverWorld, Waits
from tests.support.sim_world import ACCOUNT, DEPLOYMENT

CHAN = gates.CHAN
EXECUTION = '[execution]\norder = "market_on_close"\n'


def strategy(name: str = CHAN, execution: str = EXECUTION) -> StrategyConfig:
    return parse_strategy(name, f'instruments = ["GLD", "USO"]\n{execution}'.encode())


class Gatekeeper:
    """The real gate, but for the answers a case plants by order and by call.

    `planted` maps an order's id to a list of answers, one per call of
    `check` for it, oldest first: `None` asks the real gate, a `Halt` is
    raised, and `"refuse"` turns the real answer into a refusal on the order's
    size. A call past the list asks the real gate.
    """

    def __init__(self, gate: Gate) -> None:
        self._gate = gate
        self.planted: dict[str, list[object]] = {}
        self.asked: list[str] = []

    def check(self, intent, book, snapshot, constraints, inputs, now) -> Verdict:
        name = intent.client_order_id
        self.asked.append(name)
        answers = self.planted.get(name, [])
        answer = answers.pop(0) if answers else None
        if isinstance(answer, Halt):
            raise answer
        verdict = self._gate.check(intent, book, snapshot, constraints, inputs, now)
        if answer == "refuse":
            size = abs(intent.qty)
            return replace(
                verdict, approved=False, limit=Limit.MAX_ORDER_QTY, value=size, bound=size / 2
            )
        return verdict

    def loss(self, *args, **kwargs):
        return self._gate.loss(*args, **kwargs)


@dataclass
class SendWorld(RecoverWorld):
    strategies: tuple[StrategyConfig, ...] = field(default_factory=lambda: (strategy(),))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.keeper = Gatekeeper(Gate(gates.fitting(self.constraints, self.caps)))
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
        self._file(at, RISK_STAGE, CONSTRAINTS_OUTPUT, Constraints, self.constraints)
        wanted = targets if targets is not None else {CHAN: {gates.GLD: 0, gates.USO: 0}}
        self.targets(at, wanted)
        self.store.quiet = quiet

    def targets(self, at: datetime, wanted: Mapping[str, Mapping[str, object]]) -> BlobRef:
        """Store the targets, under a header naming each strategy's forecast and schedule."""
        inputs = {}
        for name in sorted(wanted):
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
            strategies=self.strategies,
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

    def states(self) -> dict[str, str]:
        """Each order of the journal by its id, in its state."""
        return {name: str(o.state) for name, o in self.view().orders.items()}

    def submits(self) -> list[str]:
        """Each order id the simulated broker placed, in the order it came."""
        found = []
        for data in self.blobs().values():
            found += [o["client_order_id"] for o in json.loads(data)["events"]["placed"]]
        return found


__all__ = ["CHAN", "Gatekeeper", "SendWorld", "strategy"]
