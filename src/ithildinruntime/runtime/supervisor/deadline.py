"""The supervisor's deadlines: an order in doubt, the stop after the close, and a stalled run.

#147 item 6 says an order in doubt halts the run past the smaller of 30 minutes
and the time left to the cancel cutoff, and that a supervisor deadline enforces
it. halts-and-drills.md's `### How a drill counts toward a gate` sets each such
deadline beside the heartbeat's grace period, which has to exceed them, so the
dead-man check #201 builds fires only once a deadline has had its chance.

1. **An order is in doubt** while the journal shows it `Sending` or `InDoubt`,
   the states `journal.DOUBTFUL` names. Its deadline counts from its `sending`
   entry, `sent_at`, and is the earlier of 30 minutes later and the cancel
   cutoff of its own decision's session, `core.CANCEL_CUTOFF` minutes before
   that session's close. The cutoff comes from `core` and not from an account's
   caps, so a caps blob that is missing or unreadable cannot move it. An order
   with no `sent_at` counts from its decision. Only a time strictly past the
   deadline halts, since the rule says "past".
2. **The stop waits for the close.** Outside a backtest the run's stop is
   stored no earlier than `STOP_AFTER_CLOSE` past the close, so execution
   still serves a cancel until the last moment one could land.
3. **A stalled run alarms.** At `STALL_AFTER_CLOSE` past the close, a result
   still missing alarms, naming each stage and decision without one, and the
   supervisor exits with no stop. The host's timer, which #202 sets later than
   this, ends the run, and the next start holds its strategies.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from ithildinruntime.core import CANCEL_CUTOFF, BeforeClose, OrderRecord, SessionTable, to_utc

__all__ = [
    "HEARTBEAT_GRACE",
    "IN_DOUBT_LIMIT",
    "STALL_AFTER_CLOSE",
    "STOP_AFTER_CLOSE",
    "cancel_cutoff",
    "in_doubt_deadline",
    "past_deadline",
]

IN_DOUBT_LIMIT = timedelta(minutes=30)
STOP_AFTER_CLOSE = timedelta(minutes=5)
STALL_AFTER_CLOSE = timedelta(minutes=60)
# The dead-man check's grace, which #201 reads. It exceeds every deadline above
# that runs inside a session, and a test holds it there.
HEARTBEAT_GRACE = timedelta(minutes=45)


def cancel_cutoff(at: datetime, sessions: SessionTable) -> datetime:
    """The cancel cutoff of the session the time `at` falls in."""
    session = sessions.opened_by(at)
    return to_utc(session.day, BeforeClose(CANCEL_CUTOFF), sessions)


def in_doubt_deadline(
    order: OrderRecord, decision_at: datetime, sessions: SessionTable
) -> datetime:
    """When an order in doubt halts the run, past which it does."""
    sent = order.sent_at if order.sent_at is not None else decision_at
    return min(sent + IN_DOUBT_LIMIT, cancel_cutoff(decision_at, sessions))


def past_deadline(
    doubtful: Iterable[tuple[OrderRecord, datetime]], now: datetime, sessions: SessionTable
) -> list[str]:
    """Each order in doubt whose deadline `now` is past, as the halt names it."""
    late = []
    for order, decision_at in doubtful:
        deadline = in_doubt_deadline(order, decision_at, sessions)
        if now > deadline:
            name = order.intent.client_order_id
            late.append(
                f"{name} has been in doubt since {order.sent_at or decision_at:%H:%M:%S} UTC, "
                f"past its deadline of {deadline:%H:%M:%S} UTC"
            )
    return late
