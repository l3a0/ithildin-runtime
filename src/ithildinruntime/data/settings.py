"""Data's settings, built from the `[data]` section of every strategy's tracked file.

Every strategy of a deployment shares data's stage, so its settings come from
the files whose hashes its header names, and from nothing else. A section looks like this, and
its instruments are the file's own `instruments`, which it does not repeat.

    [data]
    lookback = 60
    stale_lag = 0
    spike_multiple = 10

    [data.first_sessions]
    GLD = 2006-04-26
    USO = 2006-04-26

`lookback` is how many sessions of bars each decision reads, and `stale_lag`
how many sessions an instrument's newest bar may be behind. `spike_multiple`
is how many standard deviations a close may move before it is flagged, and
`first_sessions` says where the lake's series of each instrument starts. A
date later than that start halts each decision whose lookback it cuts, as
`LakeSource._refuse_late` says.

Every key is written, because an absent value never reads as a default (hard
limit 3). A file is refused, naming the strategy, the file and the key, on
each of these.

1. No `[data]` section, or an empty one.
2. A key this form lacks, or one it has and the section does not. `core`'s
   reader gives a blank `[data.first_sessions]` as absent.
3. A `lookback` that is no whole number of 1 or more, or a `stale_lag` that is
   no whole number of 0 or more.
4. A `spike_multiple` that is no number above 0.
5. A `first_sessions` that is no table, or one that does not hold a plain date
   for exactly the instruments the file lists.

`lookback`, `stale_lag` and `spike_multiple` are one value each for a
deployment, and so is the first session of an instrument that two strategies
trade. Strategies that write different values are refused, naming the key and
each strategy with its value. `spike_multiple` is compared as written, so 10
and 10.0 differ, because a stored flag's note prints it.

Every refusal is a `Halt`, which data's stage raises at each decision.
tests/test_data_settings.py reads every tracked strategy file, so a bad section
fails CI before any run.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from ithildinruntime.core import Halt, InstrumentId, StrategyConfig

from .source import DataSettings

__all__ = ["SECTION", "data_settings"]

SECTION = "data"
_KEYS = ("lookback", "stale_lag", "spike_multiple", "first_sessions")


class _Section(NamedTuple):
    """One strategy's section, checked."""

    lookback: int
    stale_lag: int
    spike_multiple: Decimal
    first_sessions: Mapping[InstrumentId, date]


def data_settings(strategies: Iterable[StrategyConfig]) -> DataSettings:
    """The one `DataSettings` of a deployment that runs `strategies`, in any order."""
    read = [(s, _section(s)) for s in sorted(strategies, key=lambda s: s.name)]
    if not read:
        raise Halt("the deployment declares no strategy, so no file states data's settings")
    firsts: dict[InstrumentId, date] = {}
    for instrument in (i for strategy, _ in read for i in strategy.instruments):
        firsts[instrument] = _shared(
            f"first_sessions.{instrument}",
            [
                (strategy, section.first_sessions[instrument])
                for strategy, section in read
                if instrument in section.first_sessions
            ],
        )
    lookback = _shared("lookback", [(s, section.lookback) for s, section in read])
    stale_lag = _shared("stale_lag", [(s, section.stale_lag) for s, section in read])
    multiple = _shared("spike_multiple", [(s, section.spike_multiple) for s, section in read])
    try:
        return DataSettings(
            instruments=tuple(firsts),
            lookback=lookback,
            first_sessions=firsts,
            stale_lag=stale_lag,
            spike_multiple=multiple,
        )
    except ValueError as error:
        # A check `DataSettings` makes and this reader does not yet.
        files = ", ".join(strategy.source for strategy, _ in read)
        raise Halt(f"data's settings from {files} are refused: {error}") from None


def _shared[T](key: str, written: Sequence[tuple[StrategyConfig, T]]) -> T:
    """The one value every strategy writes for `key`, compared as it prints."""
    if len({str(value) for _, value in written}) > 1:
        raise Halt(
            f"the strategies of this deployment write different values of [{SECTION}] {key}, "
            "and data reads one for them all: "
            + ", ".join(f"{strategy.name} writes {value}" for strategy, value in written)
        )
    return written[0][1]


def _section(strategy: StrategyConfig) -> _Section:
    section = strategy.section(SECTION)

    def refuse(what: str) -> Halt:
        return Halt(
            f"strategy {strategy.name}'s configuration {strategy.source} has a "
            f"[{SECTION}] section that {what}"
        )

    if missing := [key for key in _KEYS if key not in section]:
        raise refuse(f"states no {', '.join(missing)}")
    if unknown := sorted(set(section) - set(_KEYS)):
        raise refuse(f"holds {', '.join(unknown)}, which this form does not have")
    return _Section(
        lookback=_count("lookback", 1, section, refuse),
        stale_lag=_count("stale_lag", 0, section, refuse),
        spike_multiple=_multiple(section["spike_multiple"], refuse),
        first_sessions=_first_sessions(strategy, section["first_sessions"], refuse),
    )


def _count(
    key: str, least: int, section: Mapping[str, object], refuse: Callable[[str], Halt]
) -> int:
    value = section[key]
    # A bool is an int to Python, and no count.
    if type(value) is not int or value < least:
        raise refuse(f"has {key} = {value!r}, which is not a whole number of {least} or more")
    return value


def _multiple(value: object, refuse: Callable[[str], Halt]) -> Decimal:
    if type(value) not in (int, Decimal) or value <= 0:
        raise refuse(f"has spike_multiple = {value!r}, which is not a number above 0")
    return Decimal(value)


def _first_sessions(
    strategy: StrategyConfig, value: object, refuse: Callable[[str], Halt]
) -> Mapping[InstrumentId, date]:
    if not isinstance(value, Mapping):
        raise refuse(f"has first_sessions = {value!r}, which is not a table")
    if missing := [i for i in strategy.instruments if i not in value]:
        raise refuse(f"states no first_sessions date for {', '.join(missing)}")
    if extra := sorted(set(value) - set(strategy.instruments)):
        raise refuse(
            f"holds a first_sessions date for {', '.join(extra)}, which the strategy does not trade"
        )
    for instrument, day in value.items():
        # A datetime is a date to Python, and no session.
        if type(day) is not date:
            raise refuse(f"has first_sessions.{instrument} = {day!r}, which is not a plain date")
    return value
