"""The stress move: how it is measured, the record a measurement leaves, and the hold to it.

The net cap is the share of V over the stress move, so a stress move too small
lets a lone leg lose more than the share the owner set. The stress move is the
largest two-session move, in either direction, across a strategy's legs over a
recorded vintage. `largest_move` is that calculation, `stress_vintage` names
the bars it read, `load_records` reads the tracked records, and
`hold_to_record` holds the owner's hard caps to them.

It sits in `core` because the gate, the risk model and portfolio construction
each hold the caps, and no worker module imports another. docs/architecture's
08-crosscutting-concepts.md holds the form of a record, and 05-building-blocks.md
what the hold halts on.
"""

from __future__ import annotations

import hashlib
import itertools
import re
import tomllib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import NamedTuple

from .codec import CodecError, from_received, places
from .exact import exact_total, finite, quotient
from .halt import Halt
from .limits import check_pair, shown
from .names import is_name, is_sha256
from .records import Bar, Constraints, HardCaps, InstrumentId
from .sessions import load_sessions
from .valuing import Series, series_of

__all__ = [
    "STRESS_MOVES",
    "FlaggedBar",
    "LargestMove",
    "LeftOut",
    "LegSeries",
    "Move",
    "Moves",
    "StressRecord",
    "StressRecords",
    "hold_to_record",
    "largest_move",
    "leg_series",
    "load_records",
    "moves",
    "read_records",
    "stress_vintage",
]

# The tracked records, which ship inside the package as the strategies do.
STRESS_MOVES = Path(__file__).parent / "stress_moves.toml"

_PLACES = places(HardCaps, "stress_move")
_VINTAGE_ID = re.compile(r"(?P<key>[^#\t\n]+)#sha256=(?P<sha>[0-9a-f]{64})#version=.*", re.DOTALL)
_STATISTIC = re.compile(rf"(0|[1-9][0-9]*)\.[0-9]{{{_PLACES}}}")


@dataclass(frozen=True)
class LeftOut:
    """A move no figure counts, because its bars are of more than one series."""

    instrument: InstrumentId
    sessions: int
    moved_from: date
    moved_to: date


class Move(NamedTuple):
    """How far one leg's close moved between two sessions, as a fraction of the first."""

    size: Decimal
    leg: InstrumentId
    moved_from: date
    moved_to: date


class Moves(NamedTuple):
    """One leg's moves over one count of sessions, oldest first, and each that was left out."""

    kept: tuple[Move, ...]
    left_out: tuple[LeftOut, ...]


@dataclass(frozen=True)
class LargestMove:
    """The largest of every leg's moves, and each move that was left out."""

    move: Move
    left_out: tuple[LeftOut, ...]


@dataclass(frozen=True)
class LegSeries:
    """One series of one leg, with the first and the last session of its run of bars."""

    instrument: InstrumentId
    first: date
    last: date
    series: Series


@dataclass(frozen=True)
class FlaggedBar:
    """A flag a read raised, and what the person who committed the record found it was."""

    instrument: InstrumentId
    session: date
    field: str
    rule: str
    what: str


@dataclass(frozen=True)
class StressRecord:
    """One measurement of one strategy's stress move over one vintage."""

    strategy: str
    instruments: tuple[InstrumentId, ...]
    vintage: str
    through: date
    stress_move: Decimal
    leg: InstrumentId
    moved_from: date
    moved_to: date
    one_session: Decimal
    one_session_leg: InstrumentId
    one_session_on: date
    series: tuple[LegSeries, ...]
    left_out: tuple[LeftOut, ...]
    flagged: tuple[FlaggedBar, ...]
    manifest: str
    measured_on: date
    withdrawn: str | None = None

    def counts_for(self, strategy: str, legs: Collection[InstrumentId]) -> bool:
        """Whether the record is of this strategy, is not withdrawn, and one of these legs set it.

        Its other legs do not matter, because that leg made the move.
        """
        return self.withdrawn is None and self.strategy == strategy and self.leg in legs


type StressRecords = tuple[StressRecord, ...] | Halt


# The measure


def moves(bars: Sequence[Bar], sessions: int) -> Moves:
    """One leg's |C(t+n) − C(t)| ÷ C(t) for each t, n being `sessions`, each rounded up.

    A move whose bars are of more than one series is left out and listed. Bars
    too few for one move give none, and a leg that did not move reads zero.
    """
    held = _ordered(bars)
    kept, left_out = [], []
    for n in range(len(held) - sessions):
        window = held[n : n + sessions + 1]
        before, after = window[0], window[-1]
        leg, span = before.instrument, (before.session, after.session)
        if len({series_of(bar) for bar in window}) > 1:
            left_out.append(LeftOut(leg, sessions, *span))
        else:
            kept.append(Move(_move(before, after), leg, *span))
    return Moves(tuple(kept), tuple(left_out))


def largest_move(bars: Mapping[InstrumentId, Sequence[Bar]], sessions: int) -> LargestMove:
    """The largest move over `sessions` across the legs, which may begin on different sessions.

    Moves are compared as rounded, and a tie goes to the earliest date and then
    to the first leg by name.
    """
    if not bars:
        raise Halt("no leg has a bar, so no move can be measured")
    kept: list[Move] = []
    left_out: list[LeftOut] = []
    last: tuple[InstrumentId, date] | None = None
    for leg in sorted(bars):
        if not bars[leg]:
            raise Halt(f"{leg} has no bar, so its moves cannot be measured")
        found = moves(bars[leg], sessions)
        ends = max(bar.session for bar in bars[leg])
        if last is not None and ends != last[1]:
            raise Halt(
                f"{leg}'s last bar is for {ends.isoformat()}, and {last[0]}'s for "
                f"{last[1].isoformat()}"
            )
        last = last or (leg, ends)
        if not found.kept:
            raise Halt(f"{leg} has no {sessions}-session move left through {ends.isoformat()}")
        kept += found.kept
        left_out += found.left_out
    kept.sort(key=lambda move: (move.moved_from, move.leg))
    largest = max(kept, key=lambda move: move.size)
    if largest.size <= 0:
        raise Halt(f"the largest {sessions}-session move of {', '.join(sorted(bars))} is zero")
    left_out.sort(key=lambda move: (move.moved_from, move.instrument))
    return LargestMove(largest, tuple(left_out))


def _ordered(bars: Sequence[Bar]) -> list[Bar]:
    """One leg's bars by session, one for each session from the first to the last."""
    held = sorted(bars, key=lambda bar: bar.session)
    if not held:
        return held
    leg = held[0].instrument
    for one, two in itertools.pairwise(held):
        if one.session == two.session:
            raise Halt(f"{leg} has two bars for {one.session.isoformat()}")
    expected = [s.day for s in load_sessions().between(held[0].session, held[-1].session)]
    have = {bar.session for bar in held}
    if missing := [day for day in expected if day not in have]:
        raise Halt(f"{leg} has no bar for {missing[0].isoformat()}")
    if extra := sorted(have - set(expected)):
        raise Halt(f"{leg} has a bar for {extra[0].isoformat()}, which is no session")
    for bar in held:
        if finite(f"{leg}'s close for {bar.session.isoformat()}", bar.close) <= 0:
            raise Halt(
                f"{leg}'s close for {bar.session.isoformat()} is {bar.close}, not above zero"
            )
    return held


def _move(before: Bar, after: Bar) -> Decimal:
    what = (
        f"{before.instrument}'s move from {before.session.isoformat()} to "
        f"{after.session.isoformat()}"
    )
    size = exact_total(what, (after.close, before.close.copy_negate())).copy_abs()
    return quotient(what, size, before.close, _PLACES, ROUND_CEILING)


def leg_series(bars: Mapping[InstrumentId, Sequence[Bar]]) -> tuple[LegSeries, ...]:
    """Each leg's runs of one series, by leg and then by session."""
    runs = []
    for leg in sorted(bars):
        held = sorted(bars[leg], key=lambda bar: bar.session)
        for series, group in itertools.groupby(held, key=series_of):
            run = list(group)
            runs.append(LegSeries(leg, run[0].session, run[-1].session, series))
    return tuple(runs)


def stress_vintage(bars: Mapping[InstrumentId, Sequence[Bar]]) -> str:
    """The SHA-256 of the bars a measurement read, by instrument and then by session.

    Each bar is one line naming its instrument, its session, its partition's key
    and that partition's SHA-256. The bucket's version is left out, so a run on
    the bucket and one on a copy agree.
    """
    digest = hashlib.sha256()
    held = [bar for leg in bars for bar in bars[leg]]
    for bar in sorted(held, key=lambda bar: (bar.instrument, bar.session)):
        read = _VINTAGE_ID.fullmatch(bar.vintage_id)
        if read is None:
            raise Halt(
                f"{bar.instrument}'s bar for {bar.session.isoformat()} has a vintage_id "
                f"{bar.vintage_id!r}, which names no partition and its SHA-256"
            )
        line = (bar.instrument, bar.session.isoformat(), read["key"], read["sha"])
        digest.update(("\t".join(line) + "\n").encode("utf-8"))
    return digest.hexdigest()


# The record


def load_records(path: Path) -> tuple[StressRecord, ...]:
    """The records in `path`, or a halt naming the file, and the record and key at fault."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise Halt(f"{path.name} is missing") from None
    except OSError as error:
        raise Halt(f"{path.name} cannot be read: {type(error).__name__}") from None
    try:
        return _records(path.name, tomllib.loads(data.decode("utf-8"), parse_float=Decimal))
    except (ValueError, ArithmeticError, RecursionError) as error:
        # `tomllib` lets through what a number too large or too many brackets raise, and an
        # int of too many digits raises once a halt shows it.
        why = f"{type(error).__name__}: {shown(str(error))}"
        raise Halt(f"{path.name} cannot be read: {why}") from None


def _records(name: str, document: Mapping[str, object]) -> tuple[StressRecord, ...]:
    if unknown := sorted(set(document) - {"record"}):
        raise Halt(f"{name} holds {shown(unknown[0])}, which this release does not read")
    written = document.get("record", [])
    if not isinstance(written, list):
        raise Halt(f"{name} holds record as a {type(written).__name__}, not a list")
    records = tuple(_record(f"{name} record {n}", r) for n, r in enumerate(written, 1))
    seen: dict[tuple[object, ...], int] = {}
    for n, record in enumerate(records, 1):
        key = (record.strategy, record.vintage)
        if key in seen:
            raise Halt(f"{name} record {n} has the strategy and vintage of record {seen[key]}")
        seen[key] = n
    return records


def read_records(path: Path) -> StressRecords:
    """The records in `path`, or the `Halt` that says why none can be read.

    An entry point hands either on, so a bad file halts each decision and never
    a worker's start.
    """
    try:
        return load_records(path)
    except Halt as halt:
        return halt


def _record(where: str, value: object) -> StressRecord:
    table = _Table(where, value, _RECORD, {"withdrawn"})
    fields = {name: table.read(name, form) for name, form in _PLAIN.items()}
    if fields["leg"] not in fields["instruments"]:
        # The hold counts a record by its leg, so a mistyped one would drop its move.
        raise Halt(f"{where}'s leg is {shown(fields['leg'])}, which is none of its instruments")
    series = tuple(
        LegSeries(
            t.read("instrument", _leg),
            t.read("first", _day),
            t.read("last", _day),
            Series(t.read("basis", _text), t.read("source", _text), t.read("source_as_of", _day)),
        )
        for t in table.tables(
            "series", {"instrument", "first", "last", "basis"}, {"source", "source_as_of"}
        )
    )
    ends = {}
    for run in series:
        ends[run.instrument] = max(run.last, ends.get(run.instrument, run.last))
    if ends != dict.fromkeys(fields["instruments"], fields["through"]):
        # The hold takes `through` as the vintage's last session, which the age halt counts from.
        raise Halt(f"{where}'s series do not end at its through for each of its instruments")
    left_out = tuple(
        LeftOut(
            t.read("instrument", _leg),
            t.read("sessions", _sessions),
            t.read("moved_from", _day),
            t.read("moved_to", _day),
        )
        for t in table.tables("left_out", {"instrument", "sessions", "moved_from", "moved_to"})
    )
    flagged = tuple(
        FlaggedBar(
            t.read("instrument", _leg),
            t.read("session", _day),
            t.read("field", _text),
            t.read("rule", _text),
            t.read("what", _text),
        )
        for t in table.tables("flagged", {"instrument", "session", "field", "rule", "what"})
    )
    return StressRecord(
        **fields,
        series=series,
        left_out=left_out,
        flagged=flagged,
        withdrawn=table.read("withdrawn", _text),
    )


class _Table:
    """One TOML table, with each key known and none it must hold left out."""

    def __init__(
        self, where: str, value: object, must: set[str], may: set[str] | None = None
    ) -> None:
        if not isinstance(value, dict):
            raise Halt(f"{where} is a {type(value).__name__}, not a table")
        if missing := sorted(must - set(value)):
            raise Halt(f"{where} holds no {missing[0]}")
        if unknown := sorted(set(value) - must - (may or set())):
            raise Halt(f"{where} holds {shown(unknown[0])}, which this release does not read")
        self._where, self._value = where, value

    def read(self, key: str, form):
        """The key's value in its form, and `None` for a key that may be left out and is."""
        if key not in self._value:
            return None
        value = self._value[key]
        found = form(value)
        if found is None:
            raise Halt(f"{self._where}'s {key} is {shown(value)}, not {_FORMS[form]}")
        return found

    def tables(self, key: str, must: set[str], may: set[str] | None = None) -> list[_Table]:
        value = self._value[key]
        if not isinstance(value, list):
            raise Halt(f"{self._where}'s {key} is {shown(value)}, not a list of tables")
        return [_Table(f"{self._where}'s {key} {n}", v, must, may) for n, v in enumerate(value, 1)]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _leg(value: object) -> InstrumentId | None:
    return InstrumentId(value) if isinstance(value, str) and value else None


def _name(value: object) -> str | None:
    return value if is_name(value) else None


def _sha(value: object) -> str | None:
    return value if is_sha256(value) else None


def _day(value: object) -> date | None:
    # A datetime is a date to Python, and no session.
    return value if type(value) is date else None


def _sessions(value: object) -> int | None:
    return value if type(value) is int and value in (1, 2) else None


def _statistic(value: object) -> Decimal | None:
    if not isinstance(value, str) or not _STATISTIC.fullmatch(value):
        return None
    try:
        read = from_received(value)
    except CodecError:
        return None
    return read if read > 0 else None


def _instruments(value: object) -> tuple[InstrumentId, ...] | None:
    if not isinstance(value, list) or not value or any(_leg(v) is None for v in value):
        return None
    return tuple(value) if value == sorted(set(value)) else None


_FORMS = {
    _text: "text",
    _leg: "an instrument",
    _name: "a strategy's name",
    _sha: "a SHA-256",
    _day: "a date",
    _sessions: "1 or 2",
    _statistic: f"a decimal above zero, written as a string at {_PLACES} places",
    _instruments: "a list of instruments in order of name",
}
_PLAIN = {
    "strategy": _name,
    "instruments": _instruments,
    "vintage": _sha,
    "through": _day,
    "stress_move": _statistic,
    "leg": _leg,
    "moved_from": _day,
    "moved_to": _day,
    "one_session": _statistic,
    "one_session_leg": _leg,
    "one_session_on": _day,
    "manifest": _sha,
    "measured_on": _day,
}
_RECORD = {*_PLAIN, "series", "left_out", "flagged"}


# The hold


def hold_to_record(constraints: Constraints, caps: HardCaps, records: StressRecords) -> HardCaps:
    """The caps held to the record they name, or a halt naming the field that is not.

    The stress move is the largest of the caps' own and of every record of the
    strategy that is not withdrawn and that one of these legs set, so a record
    can only tighten. Applied twice it changes nothing.
    """
    check_pair(constraints, caps)
    if isinstance(records, Halt):
        raise Halt(records.reason)
    if not isinstance(records, tuple) or not records:
        raise Halt("there is no stress record, so none holds the hard caps' stress_vintage")
    strategy = constraints.strategy
    legs = tuple(sorted(constraints.max_position))
    vintage = caps.stress_vintage
    record = next((r for r in records if r.strategy == strategy and r.vintage == vintage), None)
    if record is None:
        raise Halt(f"the hard caps' stress_vintage is {vintage}, of no record of {strategy}")
    if record.withdrawn is not None:
        raise Halt(
            f"the hard caps' stress_vintage is {vintage}, whose record is withdrawn: "
            f"{record.withdrawn}"
        )
    if odd := sorted(set(record.instruments) ^ set(legs)):
        raise Halt(
            f"the hard caps' stress_vintage is {vintage}, whose record and the constraints do not "
            f"both name {odd[0]}"
        )
    if caps.stress_through != record.through:
        raise Halt(
            f"the hard caps' stress_through is {caps.stress_through.isoformat()}, and its "
            f"record's is {record.through.isoformat()}"
        )
    if caps.stress_move < record.stress_move:
        raise Halt(
            f"the hard caps' stress_move is {caps.stress_move}, below its record's "
            f"{record.stress_move}"
        )
    largest = max(
        (r for r in records if r.counts_for(strategy, legs)),
        key=lambda r: r.stress_move,
    )
    if largest.stress_move > caps.stress_high:
        raise Halt(
            f"the hard caps' stress_high is {caps.stress_high}, below the stress_move of "
            f"{largest.stress_move} that the record of vintage {largest.vintage} holds"
        )
    return replace(caps, stress_move=max(caps.stress_move, largest.stress_move))
