"""The plan builder, which turns schedules and the trading calendar into a plan's decisions.

docs/design.md's `### Strategies, schedules and decisions` gives the rules.
The supervisor calls it for one trading day and a backtest for its whole
span, so both build their plans the same way. A decision time stated as
minutes before the close moves with an early close, and one stated as a time
on the exchange's clock that falls after the close is dropped. The builder
also reports how many sessions the table holds after the plan's last, so an
alarm can fire before a run meets the table's end. #11 sets that bound and
builds the alarm.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from .keys import run_part
from .records import Mode, Plan, PlanDecision
from .sessions import ExchangeTime, ScheduleTime, SessionTable, load_sessions, to_utc

__all__ = ["BuiltPlan", "build_plan"]


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
    for session in table.between(first, last):
        for strategy, times in schedules.items():
            for when in times:
                at = to_utc(session.day, when, table)
                if isinstance(when, ExchangeTime) and at > session.close:
                    continue
                deciding.setdefault(at, set()).add(strategy)
    decisions = tuple(
        PlanDecision(at=at, strategies=tuple(sorted(deciding[at]))) for at in sorted(deciding)
    )
    plan = Plan(deployment=deployment, run=run, mode=mode, release=release, decisions=decisions)
    return BuiltPlan(plan=plan, sessions_left=table.sessions_after(last))
