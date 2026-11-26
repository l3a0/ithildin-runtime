"""Execution's send stage: where each strategy's target becomes orders, and the orders go out.

It runs after recover, risk and portfolio construction, and is the one caller of
`OrderPath.send`. Its handler waits for the decision's time, having written
nothing, and then works in this order. docs/architecture/06-runtime.md's
`### Phase 3: send` says why each step comes where it does.

1. **The claim**, at `core.claim_key`, naming this process's writer and a token
   of this call. A claim already there that this call did not write halts, after
   the answers this process owes the journal, a view of the journal, an alarm for
   each order in doubt, and the day's loss, so the halt names where each planned
   order stands and the loss. That view comes before any input is read, so an
   unreadable snapshot hides no order.
2. Each input's result in the row's order, each halt passed on, and then the
   snapshot, the constraints, the targets, and recover's book, valuation and
   caps, all read before any file of this stage's is stored.
3. **The sync**: the five reads of the broker, stored afresh at
   `broker-read-<n>.json`, a `read` entry, an entry for each new record, fill
   and open order as `execution/outside.py` sorts them, a halt on anything kept
   as unmatched, and `core.reconcile`. An order in doubt, a difference, and an entry past
   recover's book that moved it and that this sync did not write each halt.
4. Each strategy's off switch and halts. One that is off gets no order. One that
   cannot be read halts.
5. `OrderPath.loss`, which halts past the stop with no order needed.
6. `plan_orders`, and then the `decision` entry, which names every file the plan
   was made from, before anything is asked or sent. A plan of this decision the
   journal already holds that is not this one halts, with an alarm naming both.
7. `OrderPath.ask` about the whole plan. Where any order is refused or halts,
   nothing is sent, and every order of the plan ends `Refused` or `Abandoned`.
8. `OrderPath.send` for each order in the plan's order. An answer other than
   `Acknowledged` ends that strategy's plan. An off switch with no leg of that
   strategy at the broker ends it as partly sent, and every other answer halts
   the stage, with each order not yet sent, from the one that failed on,
   abandoned and an alarm naming every leg.
9. `outcome.json`, and then the result.

A result holds a halt or an outcome, never both.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol

from ithildinruntime.core import (
    ALPHA_COMPONENT,
    BOOK_OUTPUT,
    CAPS_OUTPUT,
    CONSTRAINTS_OUTPUT,
    COST_COMPONENT,
    DATA_STAGE,
    FORECAST_OUTPUT,
    PORTFOLIO_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    SCHEDULE_OUTPUT,
    TARGET_OUTPUT,
    VALUATION_OUTPUT,
    AccountRead,
    BlobRef,
    Book,
    Broker,
    Clock,
    Constraints,
    DecisionResult,
    Halt,
    HardCaps,
    OrderIntent,
    OrderState,
    Progress,
    Remainder,
    SendOutcome,
    SentOrder,
    StageDecision,
    StageKind,
    StageRun,
    Storage,
    StrategyConfig,
    StrategyEnd,
    StrategySent,
    TargetPortfolio,
    Valuation,
    broker_read_key,
    claim_key,
    error_name,
    output_key,
    reconcile,
    result_key,
)
from ithildinruntime.journal import DOUBTFUL, NOT_SENT, DecisionEntry, View

from .account import Account
from .inputs import read_inputs
from .order_path import OrderPath, Outage
from .orders import order_type, plan_orders
from .recover import fills_since
from .sync import journal_read, read_broker, recorded

__all__ = ["OUTCOME_OUTPUT", "SendStage"]

# The format of the stage's outcome file, its broker read and its result header. Format 2's
# broker read holds the records read, which format 1's does not.
_FORMAT = 2
OUTCOME_OUTPUT = "outcome"


class _Item(Protocol):
    decision_at: datetime


class SendStage:
    """The handler the receive loop calls for the send stage.

    `writer` is this process's, the one its `Account` journals under, and each
    call draws a token of its own beside it, so a second call in one process
    never reads the first one's claim as its own. `inputs` maps each stage
    the row reads to the configuration hashes the stage table gives it.
    """

    def __init__(
        self,
        account: Account,
        brokers: Callable[[datetime], Broker] | Halt,
        storage: Storage,
        clock: Clock,
        *,
        strategies: tuple[StrategyConfig, ...],
        writer: str,
        stage: str,
        kind: StageKind,
        inputs: Mapping[str, Mapping[str, str]],
        deployment: str,
        run: str,
        release: str,
        config_hashes: Mapping[str, str],
    ) -> None:
        self._account = account
        self._brokers = brokers
        self._storage = storage
        self._clock = clock
        self._strategies = strategies
        self._writer = writer
        self._inputs = {name: dict(hashes) for name, hashes in inputs.items()}
        self._deployment = deployment
        self._run = StageRun(
            storage,
            format=_FORMAT,
            stage=stage,
            kind=kind,
            deployment=deployment,
            run=run,
            release=release,
            config_hashes=config_hashes,
        )

    def __call__(self, item: _Item, report: Progress) -> DecisionResult:
        # Nothing is written yet, so a stop may end this wait.
        self._clock.wait_until(item.decision_at, report)
        decision = self._run.decision(item.decision_at, report)
        try:
            outcome = self._sent(decision, item.decision_at, report)
            ref = decision.put(OUTCOME_OUTPUT, SendOutcome, (outcome,))
        except Halt as halt:
            return decision.halted(halt)
        return decision.done(ref)

    def _sent(
        self, decision: StageDecision, at: datetime, report: Callable[[], None]
    ) -> SendOutcome:
        account, brokers = self._account, self._brokers
        if isinstance(brokers, Halt):
            raise Halt(brokers.reason)
        self._claim(at, brokers, report)

        # Step 2
        for upstream in (DATA_STAGE, RECOVER_STAGE, RISK_STAGE, PORTFOLIO_STAGE):
            decision.result(upstream, self._inputs.get(upstream))
        decision.snapshot(DATA_STAGE)
        decision.entries(RISK_STAGE, CONSTRAINTS_OUTPUT, Constraints)
        targets = decision.entries(PORTFOLIO_STAGE, TARGET_OUTPUT, TargetPortfolio)
        recovered = decision.entry(RECOVER_STAGE, BOOK_OUTPUT, Book).replayed_through
        decision.entry(RECOVER_STAGE, VALUATION_OUTPUT, Valuation)
        caps = decision.entry(RECOVER_STAGE, CAPS_OUTPUT, HardCaps)
        named = self._named(decision, at, report)
        types = {s.name: order_type(s) for s in self._strategies}

        # Step 3
        account.settle(report)
        path = account.path(brokers(at), report)
        view = account.view(report)
        made = read_broker(path, fills_since(view))
        capture = decision.fresh(
            lambda n: broker_read_key(self._deployment, self._run.run, at, n), AccountRead, made
        )
        view, seq = journal_read(
            account,
            path,
            view,
            capture.ref,
            made,
            stage=self._run.stage,
            at=at,
            clock=self._clock,
            report=report,
        )
        view = account.view(report)
        _synced(view, recovered, seq, capture.ref, made)

        # Step 4
        off = {s.name: why for s in self._strategies if (why := path.off(s.name)) is not None}

        # Step 5
        path.loss(self._deployment, self._run.run, at)

        # Step 6
        plan = plan_orders(
            targets,
            view.book,
            types=types,
            share_step=caps.share_step,
            decision_at=at,
            off=off,
        )
        entry = DecisionEntry(
            deployment=self._deployment,
            run=self._run.run,
            decision_at=at,
            targets=targets,
            orders=plan.orders,
            off=tuple(sorted(off)),
            inputs=named,
            read=seq,
            synced=view.through.seq,
        )
        try:
            view = recorded(account, view, lambda v: _planned(v, at, entry), report)
        except _OtherPlan as other:
            added = account.alarm(other.reason, report)
            raise Halt(f"{other.reason}{added}") from None
        names = [order.client_order_id for order in plan.orders]

        # Step 7
        if names:
            self._asked(path, names, report)

        # Step 8
        ended = self._sends(path, plan.orders, report)
        view = account.view(report)
        return _outcome(view, at, plan.orders, off, ended, plan.remainders, self._strategies)

    # Step 1

    def _claim(self, at: datetime, brokers: Callable[[datetime], Broker], report) -> None:
        key = claim_key(self._deployment, self._run.run, at)
        mark = {"token": secrets.token_hex(16), "writer": self._writer}
        data = json.dumps(mark, sort_keys=True, separators=(",", ":")).encode()
        claimed = self._storage.claim(key, data)
        report()
        if claimed:
            return
        account = self._account
        account.settle(report)
        view = account.view(report)
        said, doubts = _standing(view, at)
        added = ""
        for doubt in doubts:
            added = account.alarm(f"{doubt}, and {key} holds another call's claim", report) or added
        try:
            account.path(brokers(at), report).loss(self._deployment, self._run.run, at)
            loss = ""
        except Halt as halt:
            loss = f". {halt.reason}"
        raise Halt(
            f"{key} holds another call's claim, so this one sends nothing: {said}{loss}{added}"
        )

    # Step 6

    def _named(
        self, decision: StageDecision, at: datetime, report: Callable[[], None]
    ) -> dict[str, BlobRef]:
        """Every file the plan is made from, by the name a `decision` entry gives it."""
        run = self._run
        inputs = read_inputs(self._storage, self._deployment, run.run, at, report)
        named = dict(inputs.refs)
        portfolio = decision.result(PORTFOLIO_STAGE)
        target = output_key(self._deployment, run.run, at, PORTFOLIO_STAGE, TARGET_OUTPUT)
        listed = [ref for ref in portfolio.outputs if ref.key == target]
        named["targets"] = listed[0]
        read = portfolio.header.inputs
        portfolio_key = result_key(self._deployment, run.run, at, PORTFOLIO_STAGE)
        for strategy in sorted(s.name for s in self._strategies):
            for component, output, name in (
                (ALPHA_COMPONENT, FORECAST_OUTPUT, "forecast"),
                (COST_COMPONENT, SCHEDULE_OUTPUT, "schedule"),
            ):
                key = output_key(self._deployment, run.run, at, f"{component}/{strategy}", output)
                sha256 = read.get(key)
                if sha256 is None:
                    raise Halt(f"{portfolio_key} names no {key}, which the plan is made from")
                named[f"{name}/{strategy}"] = BlobRef(key, sha256)
        return named

    # Step 7

    def _asked(self, path: OrderPath, names: list[str], report: Callable[[], None]) -> None:
        """Ask the gate about the whole plan, and end every order of it where any is refused.

        An order refused, or one whose check raised, sends nothing. The steps
        up to the `verdict` run again for each order up to it, which journals
        an `intent` and a `verdict` for each, or an `intent` and an `abandoned`
        for one that raised. Then each order still `Approved` and each with no
        entry is journaled `abandoned`. The caps can be rewritten between the
        two asks, so an order the second approves is abandoned too.
        """
        answers = path.ask(names)
        refused = next((a for a in answers if not a.approved), None)
        if refused is None:
            return
        reason = refused.reason
        said = [reason]
        why = f"a partner in its plan was refused: {reason}"
        # A store that stopped answering alarmed once already, and each later read or abandon
        # would page the same outage, so none is tried once one is met.
        down = False
        try:
            path.hold(names)
        except Outage as outage:
            said.append(outage.reason)
            down = True
        except Halt as halt:
            said.append(halt.reason)
        view = None if down else self._read(said, report)
        for name in [] if view is None else names:
            order = view.orders.get(name)
            if order is None or order.state in NOT_SENT:
                try:
                    path.abandon(name, why)
                except Outage as outage:
                    said.append(outage.reason)
                    break
                except Halt as halt:
                    said.append(halt.reason)
        reason = ". ".join(said)
        added = self._account.alarm(f"nothing is sent: {reason}", report)
        raise Halt(f"nothing is sent: {reason}{added}")

    # Step 8

    def _sends(
        self, path: OrderPath, orders: tuple[OrderIntent, ...], report: Callable[[], None]
    ) -> dict[str, str]:
        """Send each order in the plan's order, and return each strategy whose plan ended short.

        Each is mapped to why. A strategy switched off with no leg at the
        broker ends there, and the other strategies' orders go on. Anything
        else that stops an order halts the stage once each order not yet sent is
        journaled `abandoned`, with an alarm naming every leg's state.
        """
        ended: dict[str, str] = {}
        went: dict[str, list[str]] = {}
        for place, order in enumerate(orders):
            name, strategy = order.client_order_id, order.strategy
            sent = [n for names in went.values() for n in names]
            try:
                if strategy in ended:
                    path.abandon(name, f"{strategy}'s plan ended: {ended[strategy]}")
                    continue
                state = path.send(name)
            except Halt as halt:
                self._failed(path, orders, place, halt, sent, report)
            except Exception as error:
                why = Halt(f"the call for {name} raised {error_name(error)}")
                self._failed(path, orders, place, why, sent, report)
            if state is OrderState.ACKNOWLEDGED:
                went.setdefault(strategy, []).append(name)
                continue
            if state is OrderState.ABANDONED and not went.get(strategy):
                ended[strategy] = f"{name} was not sent, since {strategy} is off"
                continue
            self._failed(path, orders, place, Halt(f"the order {name} is {state}"), sent, report)
        return ended

    def _failed(
        self,
        path: OrderPath,
        orders: tuple[OrderIntent, ...],
        place: int,
        halt: Halt,
        sent: list[str],
        report: Callable[[], None],
    ) -> None:
        """Abandon every order not yet sent from `place` on, alarm naming each leg, and halt.

        The order at `place` is abandoned only where it has no entry or was
        left before its `sending`, as an error the path did not expect can leave
        it. An order the broker may hold is left as it stands. `sent` names each
        order this call saw acknowledged. A store that did not answer has
        alarmed once already, so once one is met no other write or read is tried,
        and the legs this call cannot read back are named from `sent`. The order
        at `place` may be at the broker unless the path's halt is an `Outage`,
        so where the journal cannot be read it is named not known, never unsent.
        """
        reason = halt.reason
        said = [reason]
        # The path raises an `Outage` only before the broker's call, so the order at `place`
        # was not sent where it is one.
        down = isinstance(halt, Outage)
        view = None if down else self._read(said, report)
        # A journal that cannot be read takes no abandon either, and each would say why again.
        down = down or view is None
        left: list[str] = []
        for at, order in enumerate(orders[place:], start=place):
            name = order.client_order_id
            found = None if view is None else view.orders.get(name)
            if found is not None and found.state not in NOT_SENT:
                continue
            if view is None and at == place and not isinstance(halt, Outage):
                # It may be at the broker, and only the journal could say.
                continue
            if down:
                left.append(name)
                continue
            try:
                path.abandon(name, f"an earlier order of its plan stopped: {reason}")
            except Outage as outage:
                said.append(outage.reason)
                down = True
                left.append(name)
            except Halt as failed:
                said.append(failed.reason)
        view = None if down else self._read(said, report)
        if left:
            said.append(
                f"{', '.join(left)} {'was' if len(left) == 1 else 'were'} not journaled abandoned, "
                "since the journal could not be read, and none of them was sent"
            )
        if view is None:

            def stands(name: str) -> str:
                if name in sent:
                    return f"{name} acknowledged"
                return f"{name} not sent" if name in left else f"{name} not known"

            named = ", ".join(stands(order.client_order_id) for order in orders)
            legs = f"{named}, since the journal could not be read"
        else:
            legs = "; ".join(
                _leg(order.client_order_id, view.orders.get(order.client_order_id))
                for order in orders
            )
        what = f"{'. '.join(said)}. The plan's orders stand so: {legs}"
        # The one alarm that names every leg, beside the outage's own where there was one.
        added = self._account.alarm(what, report)
        raise Halt(f"{what}{added}")

    def _read(self, said: list[str], report: Callable[[], None]) -> View | None:
        """A view of the journal, or `None` with the reason it could not be read added to `said`."""
        try:
            return self._account.view(report)
        except Halt as halt:
            said.append(halt.reason)
            return None


class _OtherPlan(Exception):
    """A plan of this decision in the journal that is not this stage's."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _planned(view: View, at: datetime, entry: DecisionEntry) -> DecisionEntry | None:
    """The plan to record at `at`, or `None` where the journal holds it already.

    A different plan journaled for this decision halts, naming both, since its
    orders share this plan's ids and would go out under them. An identical one is
    this stage's own, and the claim keeps any other writer out.
    """
    seq = view.decided.get(at)
    if seq is None:
        return entry
    held = view.decisions[seq]
    if held != entry:
        planned = ", ".join(f"{o.client_order_id} for {o.qty}" for o in entry.orders) or "none"
        found = ", ".join(f"{o.client_order_id} for {o.qty}" for o in held.orders) or "none"
        raise _OtherPlan(
            f"entry {seq} holds another plan of the decision at {at.isoformat()}, so nothing is "
            f"sent: it plans {found}, and this stage planned {planned}"
        )
    return None


def _leg(name: str, order: object) -> str:
    if order is None:
        return f"{name} has no entry"
    state = order.state  # type: ignore[attr-defined]
    sent = order.sent_at  # type: ignore[attr-defined]
    said = "in doubt" if state in DOUBTFUL else f"{state}"
    if sent is None:
        return f"{name} is {said}"
    return f"{name} is {said}, its sending stored at {sent.isoformat()}"


def _standing(view: View, at: datetime) -> tuple[str, list[str]]:
    """Where each order of the decision's plan stands, in words, and each in doubt.

    A leg whose partner did not go is named too, since #149 cancels it.
    """
    seq = view.decided.get(at)
    if seq is None:
        return "the journal holds no plan of this decision", []
    orders = view.decisions[seq].orders
    said, doubts = [], []
    for order in orders:
        name = order.client_order_id
        found = view.orders.get(name)
        line = _leg(name, found)
        said.append(line)
        if found is not None and found.state in DOUBTFUL:
            doubts.append(f"the order {line}")
    by_strategy: dict[str, tuple[list[str], list[str]]] = {}
    for order in orders:
        found = view.orders.get(order.client_order_id)
        sent, unsent = by_strategy.setdefault(order.strategy, ([], []))
        (sent if found is not None and found.sent_at is not None else unsent).append(
            order.client_order_id
        )
    for strategy, (sent, unsent) in sorted(by_strategy.items()):
        if sent and unsent:
            said.append(
                f"{', '.join(sent)} of {strategy} went and its partner {', '.join(unsent)} did not"
            )
    return "; ".join(said) or "the plan holds no order", doubts


def _synced(view: View, recovered: int, seq: int, ref: BlobRef, read: AccountRead) -> None:
    """Halt where the journal after the sync differs from the broker, or another writer moved it.

    Between recover's book and this sync's `read` entry the journal may hold
    reads alone, since anything else moved the book after recover stored it.
    """
    if moved := [n for n in range(recovered + 1, seq) if n not in view.reads]:
        raise Halt(
            f"entries {', '.join(map(str, moved))} came after the book recover stored, and before "
            "this stage's read, so another writer moved the book"
        )
    book = view.book
    # Recover halts on any order in doubt, and the check above on any entry but a read before
    # this sync's own. So only another writer's entries after it reach this, which the
    # account's lease (#148) is to rule out.
    if book.in_doubt:
        raise Halt(f"an order is in doubt in {', '.join(book.in_doubt)}")
    if found := reconcile(book, view.live.values(), read):
        said = "; ".join(str(difference) for difference in found)
        raise Halt(f"the book and the broker differ, as {ref.key} holds it: {said}")


def _outcome(
    view: View,
    at: datetime,
    orders: tuple[OrderIntent, ...],
    off: Mapping[str, str],
    ended: Mapping[str, str],
    remainders: tuple[Remainder, ...],
    strategies: tuple[StrategyConfig, ...],
) -> SendOutcome:
    sent = tuple(
        SentOrder(order.client_order_id, view.orders[order.client_order_id].state)
        for order in orders
    )
    planned = {order.strategy for order in orders}
    lines = []
    for strategy in sorted(s.name for s in strategies):
        if strategy in off:
            lines.append(StrategySent(strategy, StrategyEnd.OFF, off[strategy]))
        elif strategy in ended:
            lines.append(StrategySent(strategy, StrategyEnd.PARTLY_SENT, ended[strategy]))
        elif strategy not in planned:
            lines.append(StrategySent(strategy, StrategyEnd.NO_ORDER_NEEDED, ""))
        else:
            lines.append(StrategySent(strategy, StrategyEnd.SENT, ""))
    return SendOutcome(at, sent, tuple(lines), remainders)
