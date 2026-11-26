"""The hold after an unclean stop: every strategy stays off until a person clears it.

#33's `## The running match and the strategy halts, for #11` item 3 sets the
rule. After a stop that left no clean-shutdown record, startup keeps every
strategy switched off and pages the owner to confirm, or on shadow and paper
the operator, naming each open position, and journals the stop as a pause. The
run's stop, `core.RunStop`, is the clean-shutdown record. A run the host's
timer ended stored none.

1. **Which runs it checks.** Every earlier run with a plan, walking back one
   session at a time from the session before today, until a run with a stop, a
   run a pause entry names, or the session of the account's opening entry.
   Today's run is checked only after a lost machine: its plan exists, a start
   record read before this start's own names another boot, and none names this
   boot. A start record of this boot is a restart the host supervised, which
   holds nothing. An account whose journal has no entry has never traded, so
   its first run holds nothing.
2. **Which strategies it holds.** Every strategy of the deployment's table,
   and every strategy the journal's book holds a position, a working order or
   an order in doubt for.
3. **What it writes, in order.** For each held strategy, a `StrategyHalt` at
   `strategy_halt_key(account, strategy, "supervisor", "unclean_stop", at, 1)`,
   created once, where `at` is the run's last journaled decision, or the
   plan's first decision where the journal names none. Then one `pause`
   journal entry naming the run. Then an alarm. A crash between the halts and
   the pause leaves halts a rerun finds, as #33 asks of every strategy halt.
4. **Safe to repeat.** A pause entry naming the run ends the hold for good, so a
   halt a person cleared stays cleared. A halt already stored with no pause
   still gets its pause.
5. **A read that fails holds.** Only a store's answer of "missing" reads as
   absent. Any other failure holds every strategy of the table at the open of
   today's session, alarms with what failed and raises `HoldFailed`, so the
   supervisor writes no plan and no worker starts. It journals no pause,
   because the check did not finish, and the next start runs it all again.

Execution's `Controls.strategy` refuses every new order of a strategy whose
halts folder holds a blob, at three points on the order path, so the halt is
what stops the orders. Cancelling a held strategy's working orders that add to
a position is #149's, so until it lands the alarm names each one for a person
to cancel at the broker. Clearing the hold is #203's `halt clear`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from ithildinruntime.core import (
    RUN_RECORD_FORMAT,
    CodecError,
    Halt,
    Mode,
    Plan,
    RunStop,
    SessionTable,
    Storage,
    StoreError,
    StrategyHalt,
    decode,
    encode,
    plan_key,
    run_part,
    run_stop_key,
    strategy_halt_key,
)
from ithildinruntime.journal import DOUBTFUL, Journal, NumberTaken, PauseEntry, View

from .deadline import cancel_cutoff

__all__ = ["CAUSE", "COMPONENT", "HoldFailed", "Starts", "Unclean", "hold_after_unclean_stops"]

COMPONENT = "supervisor"
CAUSE = "unclean_stop"
# How many times a pause is tried again after another writer took its number.
_TRIES = 3


class HoldFailed(Exception):
    """The hold check could not finish, so every strategy is held and nothing starts."""


@dataclass(frozen=True)
class Unclean:
    """One run that stopped with no clean-shutdown record, and why it counts as unclean."""

    run: str
    reason: str
    since: datetime | None
    at: datetime


@dataclass(frozen=True)
class Starts:
    """What today's start records said before this start wrote its own."""

    plan_stored: bool
    other_boots: tuple[str, ...]
    this_boot: bool


def hold_after_unclean_stops(
    *,
    storage: Storage,
    journal: Journal,
    mode: Mode,
    deployment: str,
    account: str,
    strategies: Sequence[str],
    today: date,
    starts: Starts,
    sessions: SessionTable,
    now: Callable[[], datetime],
    alarm: Callable[[str], None],
) -> tuple[Unclean, ...]:
    """Hold every strategy after each unclean stop it finds, and return those stops.

    It raises `HoldFailed` where a read or a write fails, after holding every
    strategy of `strategies` at the open of today's session.
    """
    today_run = run_part(mode, today, today)
    try:
        return _hold(
            storage,
            journal,
            mode,
            deployment,
            account,
            strategies,
            today,
            starts,
            sessions,
            now,
            alarm,
        )
    except (Halt, StoreError, CodecError, NumberTaken, OSError) as error:
        reason = error.reason if isinstance(error, Halt) else f"{type(error).__name__}: {error}"
        at = sessions.session(today).open
        held = _write_halts(
            storage,
            account,
            sorted(set(strategies)),
            today_run,
            f"the hold check could not finish: {reason}",
            at,
            now(),
            quiet=True,
        )
        said = ", ".join(held) or "no strategy, since no halt could be stored"
        alarm(
            f"{deployment}: the check for an unclean stop could not finish, so {said} stays "
            f"held and no worker starts: {reason}. {_who(mode)}"
        )
        raise HoldFailed(reason) from error


def _hold(
    storage: Storage,
    journal: Journal,
    mode: Mode,
    deployment: str,
    account: str,
    strategies: Sequence[str],
    today: date,
    starts: Starts,
    sessions: SessionTable,
    now: Callable[[], datetime],
    alarm: Callable[[str], None],
) -> tuple[Unclean, ...]:
    view = journal.view()
    if view.through.seq == 0:
        # An account that has never been opened has nothing to hold.
        return ()
    found = list(_unclean(storage, view, mode, deployment, today, starts, sessions))
    if not found:
        return ()
    book = view.book
    held = set(strategies)
    held |= {s for s, positions in book.positions_by_strategy.items() if any(positions.values())}
    held |= {s for s, orders in book.open_orders_by_strategy.items() if orders}
    held |= {o.intent.strategy for o in view.live.values()}
    names = sorted(held)
    for unclean in found:
        _write_halts(storage, account, names, unclean.run, unclean.reason, unclean.at, now())
        view = _pause(journal, view, deployment, unclean, names, now)
        alarm(_alarm(mode, deployment, unclean, names, view, today, sessions))
    return tuple(found)


def _unclean(
    storage: Storage,
    view: View,
    mode: Mode,
    deployment: str,
    today: date,
    starts: Starts,
    sessions: SessionTable,
):
    """Each run that left no clean-shutdown record and no pause names, newest first."""
    paused = view.paused
    today_run = run_part(mode, today, today)
    if starts.plan_stored and starts.other_boots and not starts.this_boot:
        if (deployment, today_run) not in paused:
            boots = ", ".join(starts.other_boots)
            yield _judged(
                storage,
                view,
                deployment,
                today_run,
                f"the machine running {today_run} was lost: it started on boot {boots}, "
                "and this start is on another",
            )
    opened = view.opened_at
    day = today
    while True:
        try:
            day = sessions.before(day).day
        except Halt:
            return
        if opened is not None and day < sessions.opened_by(opened).day:
            return
        run = run_part(mode, day, day)
        if (deployment, run) in paused:
            return
        if not storage.exists(plan_key(deployment, run)):
            continue
        stop = storage.find_blob(run_stop_key(deployment, run))
        if stop is not None and _is_stop_of(stop, deployment, run):
            return
        why = "it stored no stop" if stop is None else "its stop is not this run's stop record"
        yield _judged(storage, view, deployment, run, f"{run} ended uncleanly: {why}")


def _is_stop_of(data: bytes, deployment: str, run: str) -> bool:
    try:
        stop = decode(data, RunStop)
    except CodecError:
        return False
    return (stop.format, stop.deployment, stop.run) == (RUN_RECORD_FORMAT, deployment, run)


def _judged(storage: Storage, view: View, deployment: str, run: str, reason: str) -> Unclean:
    """The unclean run, from its last journaled decision or else its plan's first."""
    decided = [
        entry.decision_at
        for entry in view.decisions.values()
        if (entry.deployment, entry.run) == (deployment, run)
    ]
    if decided:
        return Unclean(run, reason, max(decided), max(decided))
    data = storage.read_blob(plan_key(deployment, run))
    plan = decode(data, Plan)
    if not plan.decisions:
        raise Halt(f"the plan of {run} holds no decision, so no halt can name one")
    return Unclean(run, reason, None, plan.decisions[0].at)


def _write_halts(
    storage: Storage,
    account: str,
    strategies: Sequence[str],
    run: str,
    reason: str,
    at: datetime,
    written_at: datetime,
    *,
    quiet: bool = False,
) -> list[str]:
    """Create each strategy's halt once, and return the strategies whose halt stands."""
    held = []
    for strategy in strategies:
        record = StrategyHalt(
            format=RUN_RECORD_FORMAT,
            account=account,
            strategy=strategy,
            cause=CAUSE,
            reason=reason,
            run=run,
            halted_at=written_at,
        )
        key = strategy_halt_key(account, strategy, COMPONENT, CAUSE, at, 1)
        try:
            storage.keep(key, encode(record))
        except (Halt, StoreError):
            if not quiet:
                raise
            continue
        held.append(strategy)
    return held


def _pause(
    journal: Journal,
    view: View,
    deployment: str,
    unclean: Unclean,
    strategies: Sequence[str],
    now: Callable[[], datetime],
) -> View:
    """Journal the stop as a pause once, viewing again where another writer took the number."""
    for _ in range(_TRIES):
        if (deployment, unclean.run) in view.paused:
            return view
        entry = PauseEntry(
            deployment, unclean.run, unclean.since, tuple(strategies), unclean.reason
        )
        try:
            _, view = journal.record(view, entry, now())
            return view
        except NumberTaken:
            view = journal.view()
    raise Halt(f"the pause of {unclean.run} lost its number {_TRIES} times to other writers")


def _who(mode: Mode) -> str:
    if mode is Mode.LIVE:
        return "The owner clears it, after confirming each position at the broker"
    return "The operator clears it, after confirming each position at the broker"


def _alarm(
    mode: Mode,
    deployment: str,
    unclean: Unclean,
    strategies: Sequence[str],
    view: View,
    today: date,
    sessions: SessionTable,
) -> str:
    book = view.book
    positions = [
        f"{strategy} {instrument} {qty:+f}"
        for strategy, held in sorted(book.positions_by_strategy.items())
        for instrument, qty in sorted(held.items())
        if qty
    ]
    working = [
        f"{o.intent.client_order_id} (the broker's {o.broker_order_id})"
        for o in view.live.values()
        if o.state not in DOUBTFUL and o.broker_order_id is not None
    ]
    doubtful = [o.intent.client_order_id for o in view.live.values() if o.state in DOUBTFUL]
    cutoff = cancel_cutoff(sessions.session(today).close, sessions)
    parts = [
        f"{deployment}: {unclean.reason}, so {', '.join(strategies)} stays held and sends "
        "no new order",
        f"Open positions: {'; '.join(positions) or 'none'}",
        f"Working orders: {'; '.join(working) or 'none'}",
        f"Orders in doubt, by client order id: {'; '.join(doubtful) or 'none'}",
        f"An order to cancel goes out at the broker by {cutoff:%H:%M} UTC today",
        _who(mode),
    ]
    return ". ".join(parts)
