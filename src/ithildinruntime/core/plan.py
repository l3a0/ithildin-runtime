"""The plan builder, which turns schedules and the trading calendar into a plan's decisions.

docs/design.md's `### Strategies, schedules and decisions` gives the rules.
The supervisor calls it for one trading day and a backtest for its whole
span, so both build their plans the same way. A decision time stated as
minutes before the close moves with an early close, and one stated as a time
on the exchange's clock that falls after an early close is dropped. The
builder halts on a decision that falls outside its session in any other way,
such as a clock time after the regular close or a time before the open,
because a schedule that names one is wrong, and an order sent then would
trade at a later price than the decision read. One strategy's wrong time
halts the whole plan, so every strategy waits until it is fixed. The builder also reports
how many sessions the table holds after the plan's last, so an alarm can fire
before a run meets the table's end. #11 sets that bound and builds the alarm.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from .halt import Halt
from .keys import run_part
from .records import Mode, Plan, PlanDecision
from .sessions import ExchangeTime, ScheduleTime, SessionTable, load_sessions, to_utc

__all__ = ["PLAN_FORMAT", "BuiltPlan", "build_plan"]

PLAN_FORMAT = 1


@dataclass(frozen=True)
class BuiltPlan:
    plan: Plan
    sessions_left: int


def build_plan(
    *,
    deployment: str,
    mode: Mode,
    release: str,
    schedules: Mapping[str, Sequence[ScheduleTime]],
    first: date,
    last: date,
    table: SessionTable | None = None,
) -> BuiltPlan:
    """Build the plan for the sessions from `first` to `last`.

    `schedules` maps each strategy to its decision times on a trading day.
    Each decision holds its UTC time and the strategies deciding at it, in
    time order, with the strategies sorted.
    """
    table = table or load_sessions()
    run = run_part(mode, first, last)
    deciding: dict[datetime, set[str]] = {}
    regular_close = table.regular_close()
    for session in table.between(first, last):
        for strategy, times in schedules.items():
            for when in times:
                at = to_utc(session.day, when, table)
                if session.open <= at <= session.close:
                    deciding.setdefault(at, set()).add(strategy)
                    continue
                within_a_regular_day = (
                    isinstance(when, ExchangeTime) and when.local <= regular_close
                )
                if at > session.close and within_a_regular_day:
                    continue
                raise Halt(
                    f"{strategy} decides at {when!r} on {session.day.isoformat()}, "
                    f"outside that session's {session.open:%H:%M} to {session.close:%H:%M} UTC"
                )
    decisions = tuple(
        PlanDecision(at=at, strategies=tuple(sorted(deciding[at]))) for at in sorted(deciding)
    )
    plan = Plan(
        format=PLAN_FORMAT,
        deployment=deployment,
        run=run,
        mode=mode,
        release=release,
        decisions=decisions,
    )
    return BuiltPlan(plan=plan, sessions_left=table.sessions_after(last))
