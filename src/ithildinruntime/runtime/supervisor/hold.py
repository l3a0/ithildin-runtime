"""The hold after an unclean stop: every strategy stays off until a person clears it.

#33's `## The running match and the strategy halts, for #11` item 3 sets the
rule. After a stop that left no clean-shutdown record, startup keeps every
strategy switched off and pages the owner to confirm, or on shadow and paper
the operator, naming each open position, and journals the stop as a pause. The
run's stop, `core.RunStop`, is the clean-shutdown record. A run the host's
timer ended stored none.

1. **Which runs it checks.** Every earlier run with a plan, walking back one
   session at a time from the session before today, until a run with a stop, a
   run whose end a pause covers, or the session of the account's opening entry.
   Today's run is checked only after a lost machine: its plan exists, a start
   record read before this start names another boot no pause covers, and none
   names this boot. A start records itself only once its hold has finished, so
   a record of this boot is a restart the host supervised, which holds nothing,
   and a start that died mid-hold leaves none. Two more cases read as a lost
   machine, each under a boot of its own so no pause of one covers another: a
   stored plan with no start record of any boot, whose start's record was lost,
   and start records that cannot be read. An account whose journal has no entry
   has never traded, so its first run holds nothing.
2. **Which strategies it holds.** Every strategy of the deployment's table,
   and every strategy the journal's book holds a position or an order that has
   not ended for.
3. **What it writes, in order.** First the halts of every stop it found: for
   each held strategy a `StrategyHalt` at `strategy_halt_key(account,
   strategy, "supervisor", "unclean_stop", at, n)`, created once, where `at` is
   the run's last journaled decision, or the plan's first decision where the
   journal names none, and `n` counts the run's pauses from 1, so each pause's
   halts have keys of their own. Then for each stop, the oldest first, one
   `pause` journal entry and its alarm. A crash anywhere leaves halts a rerun
   finds, as #33 asks of every strategy halt, and a pause of a newer run never
   lands before the older run's.
4. **Safe to repeat.** A pause covers only what it names: the boots of the lost
   machines it judged, or the run's own end. So a halt a person cleared stays
   cleared, while a later lost machine or the run's unclean end after a pause
   written in its session holds again. A halt already stored with no pause
   still gets its pause.
5. **A failure holds.** Only a store's answer of "missing" reads as absent.
   Any failure holds every strategy of the table at the open of today's
   session, alarms with what failed and raises `HoldFailed`, so the supervisor
   writes no plan and no release file. Workers this machine released before,
   on a restart in the session, run on with no supervisor, and no other worker
   starts. It journals no pause, because the check did not finish, and the
   next start runs it all again.

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
    Alarm,
    CodecError,
    Halt,
    Kind,
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

from .alarms import HOLD_UNFINISHED, UNCLEAN_HOLD, raised
from .deadline import cancel_cutoff

__all__ = [
    "CAUSE",
    "COMPONENT",
    "HoldFailed",
    "Starts",
    "Unclean",
    "hold_after_unclean_stops",
    "who",
    "write_halts",
]

COMPONENT = "supervisor"
CAUSE = "unclean_stop"
# How many times a pause is tried again after another writer took its number.
_TRIES = 3


class HoldFailed(Exception):
    """The hold check could not finish, so every strategy it could hold is held, and no plan
    or release file is written."""


@dataclass(frozen=True)
class Unclean:
    """One run that stopped with no clean-shutdown record, and why it counts as unclean."""

    run: str
    reason: str
    since: datetime | None
    at: datetime
    # The lost machines' boots this stop covers, or none where it is the run's own end.
    boots: tuple[str, ...] = ()
    # This pause's number among the run's, from 1, so each one's halts have keys of their own.
    count: int = 1

    @property
    def ended(self) -> bool:
        return not self.boots


@dataclass(frozen=True)
class Starts:
    """What today's start records said before this start wrote its own."""

    plan_stored: bool
    other_boots: tuple[str, ...]
    this_boot: bool
    # Whether this machine's local release file names this run and this boot, so its
    # workers already run.
    released: bool = False


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
    alarm: Callable[[Alarm], None],
    say: Callable[[str], None] = print,
) -> tuple[Unclean, ...]:
    """Hold every strategy after each unclean stop it finds, and return those stops.

    It raises `HoldFailed` where a read or a write fails, after holding every
    strategy of `strategies` at the open of today's session.
    """
    today_run = run_part(mode, today, today)
    # Who clears each alarm is read from the mode here, once, and no call site says it.
    live = mode is Mode.LIVE

    def raise_(kind: Kind, reason: str) -> None:
        alarm(raised(kind, reason, deployment=deployment, live=live))

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
            raise_,
            say,
        )
    except Exception as error:
        # Any failure holds, so an error nobody foresaw never reads as nothing to hold.
        reason = error.reason if isinstance(error, Halt) else f"{type(error).__name__}: {error}"
        at = sessions.session(today).open
        held = write_halts(
            storage,
            account,
            sorted(set(strategies)),
            today_run,
            f"the hold check could not finish: {reason}",
            at,
            now(),
            quiet=True,
        )
        if starts.released:
            # Started in the session, after the workers had their release file.
            after = "the workers run today's plan and no supervisor watches it"
        else:
            after = "no worker starts"
        free = sorted(set(strategies) - set(held))
        if held:
            unheld = (
                f" No halt of {', '.join(free)} could be stored, so it is not held: pull the kill "
                "switch of the account."
                if free
                else ""
            )
            raise_(
                HOLD_UNFINISHED,
                f"{deployment}: the check for an unclean stop could not finish, so "
                f"{', '.join(held)} stays held and {after}: {reason}.{unheld} The strategies "
                "held stay held after a later start succeeds. Start the supervisor's unit "
                "again once "
                f"the cause is fixed. {who(mode)}",
            )
        else:
            raise_(
                HOLD_UNFINISHED,
                f"{deployment}: the check for an unclean stop could not finish and stored no "
                f"halt of its own, so it holds no strategy and {after}: {reason}. A halt "
                "written earlier may still stand. Pull the kill switch of the account, then "
                "start the supervisor's unit again once the cause is "
                "fixed",
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
    raise_: Callable[[Kind, str], None],
    say: Callable[[str], None],
) -> tuple[Unclean, ...]:
    view = journal.view()
    if view.through.seq == 0:
        say(
            f"{deployment}: account {account} has no journal entry, so this first run holds nothing"
        )
        return ()
    found = list(_unclean(storage, view, mode, deployment, today, starts, sessions))
    if not found:
        return ()
    book = view.book
    held = set(strategies)
    held |= {s for s, positions in book.positions_by_strategy.items() if any(positions.values())}
    # Every order that has not ended, an open one or one in doubt among them.
    held |= {o.intent.strategy for o in view.live.values()}
    names = sorted(held)
    # Every halt lands before any pause, and the pauses go oldest first, because a pause
    # of a run's end covers every run before it.
    for unclean in found:
        write_halts(
            storage,
            account,
            names,
            unclean.run,
            unclean.reason,
            unclean.at,
            now(),
            count=unclean.count,
        )
    for unclean in reversed(found):
        view = _pause(journal, view, deployment, unclean, names, now)
        raise_(UNCLEAN_HOLD, _alarm(mode, deployment, unclean, names, view, today, sessions))
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
    """Each unclean stop no pause covers yet, newest first.

    A pause covers the boots it names, or the run's own end, and nothing else.
    A start on another boot reads as a lost machine only while one VM runs the
    environment. In a group of several VMs every VM has its own boot, so there
    a machine counts as lost only once the plan's lease has expired, which #210
    builds before a second supervisor runs.
    """
    today_run = run_part(mode, today, today)
    covered, _, made = _covered(view, deployment, today_run)
    lost = tuple(b for b in starts.other_boots if b not in covered)
    if starts.plan_stored and not starts.other_boots and not starts.this_boot:
        # The plan is stored and no start recorded itself, so the record of the start that
        # stored it was lost, and nothing tells its machine from this one. Each such stop
        # takes a name of its own, so a pause of one never covers a later one.
        lost = (f"unrecorded-{made + 1}",)
    if starts.plan_stored and lost and not starts.this_boot:
        boots = ", ".join(lost)
        yield _judged(
            storage,
            view,
            deployment,
            today_run,
            f"the machine running {today_run} was lost: it started on boot {boots}, "
            "and this start is on another",
            boots=tuple(sorted(lost)),
            count=made + 1,
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
        _, ended, made = _covered(view, deployment, run)
        if ended:
            # A pause written after the run ended covers it, and every run before it.
            return
        if not storage.exists(plan_key(deployment, run)):
            continue
        stop = storage.find_blob(run_stop_key(deployment, run))
        if stop is not None and _is_stop_of(stop, deployment, run):
            return
        why = "it stored no stop" if stop is None else "its stop is not this run's stop record"
        yield _judged(
            storage, view, deployment, run, f"{run} ended uncleanly: {why}", count=made + 1
        )


def _covered(view: View, deployment: str, run: str) -> tuple[set[str], bool, int]:
    """The boots the run's pauses cover, whether one covers its end, and how many there are."""
    boots: set[str] = set()
    ended, made = False, 0
    for pause in view.pauses.values():
        if (pause.deployment, pause.run) == (deployment, run):
            boots |= set(pause.boots)
            ended = ended or pause.ended
            made += 1
    return boots, ended, made


def _is_stop_of(data: bytes, deployment: str, run: str) -> bool:
    try:
        stop = decode(data, RunStop)
    except CodecError:
        return False
    return (stop.format, stop.deployment, stop.run) == (RUN_RECORD_FORMAT, deployment, run)


def _judged(
    storage: Storage,
    view: View,
    deployment: str,
    run: str,
    reason: str,
    *,
    boots: tuple[str, ...] = (),
    count: int = 1,
) -> Unclean:
    """The unclean run, from its last journaled decision or else its plan's first."""
    decided = [
        entry.decision_at
        for entry in view.decisions.values()
        if (entry.deployment, entry.run) == (deployment, run)
    ]
    if decided:
        return Unclean(run, reason, max(decided), max(decided), boots, count)
    data = storage.read_blob(plan_key(deployment, run))
    plan = decode(data, Plan)
    if not plan.decisions:
        raise Halt(f"the plan of {run} holds no decision, so no halt can name one")
    return Unclean(run, reason, None, plan.decisions[0].at, boots, count)


def write_halts(
    storage: Storage,
    account: str,
    strategies: Sequence[str],
    run: str,
    reason: str,
    at: datetime,
    written_at: datetime,
    *,
    count: int = 1,
    quiet: bool = False,
    cause: str = CAUSE,
) -> list[str]:
    """Create each strategy's halt once, and return the strategies whose halt stands."""
    held = []
    for strategy in strategies:
        record = StrategyHalt(
            format=RUN_RECORD_FORMAT,
            account=account,
            strategy=strategy,
            cause=cause,
            reason=reason,
            run=run,
            halted_at=written_at,
        )
        key = strategy_halt_key(account, strategy, COMPONENT, cause, at, count)
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
        boots, ended, _ = _covered(view, deployment, unclean.run)
        if (unclean.ended and ended) or (unclean.boots and set(unclean.boots) <= boots):
            # Another writer's pause covers this stop already.
            return view
        entry = PauseEntry(
            deployment,
            unclean.run,
            unclean.since,
            tuple(strategies),
            unclean.reason,
            unclean.boots,
            unclean.ended,
        )
        try:
            _, view = journal.record(view, entry, now())
            return view
        except NumberTaken:
            view = journal.view()
    raise Halt(f"the pause of {unclean.run} lost its number {_TRIES} times to other writers")


def who(mode: Mode) -> str:
    """Who clears a hold in `mode`, as an alarm says it."""
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
        who(mode),
    ]
    return ". ".join(parts)
