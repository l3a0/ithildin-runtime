"""The command that measures a strategy's stress move and prints the record of it.

    python -m ithildinruntime.runtime.stress_move --lake FOLDER --strategy NAME \
        --through DATE [--seam DATE ...]

It reads a folder that holds a copy of marketlake's lake, through
`data.LakeSource`, so it needs no credential. `--through` is the last session
and each `--seam` a session a new series starts on. How it reads, in order.

1. **Each leg's start.** A leg starts at its own `[data.first_sessions]` in the
   strategy's file, and no option moves it, because a later one lowers the
   stress move. So each leg is read alone over the two sessions that end at its
   start, where data's own check halts if the lake lists an earlier partition.
2. **Each series' span.** Each span between two seams is one read, so no read
   holds two series, and data halts on a seam nobody named.
3. **The moves**, over the spans joined, so a seam one leg does not change at
   leaves nothing out on it.

On any refusal it writes one line to stderr, nothing to stdout, and exits
nonzero. The line names the lake's folder, the mirror's and the checkout's by a
word for each.
docs/what-a-person-runs.md says when to run it and what to check in what it
prints.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import tempfile
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import NoReturn, TextIO

from ithildinruntime.adapters import FolderBucket, WallClock
from ithildinruntime.core import (
    DAILY,
    STRATEGIES,
    STRESS_MOVES,
    Bar,
    Bucket,
    Clock,
    FlaggedBar,
    Halt,
    InstrumentId,
    LargestMove,
    LeftOut,
    LegSeries,
    StrategyConfig,
    StressRecord,
    Timer,
    largest_move,
    leg_series,
    load_records,
    load_sessions,
    load_strategies,
    moves,
    series_of,
    stress_vintage,
)
from ithildinruntime.data import (
    DataSettings,
    LakeSource,
    Mirror,
    Reading,
    data_settings,
    load_price_kinds,
)
from ithildinruntime.data.mirror import MANIFEST
from ithildinruntime.data.reference import PriceKinds

__all__ = ["main", "measure"]


class _SystemTimer:
    """`core.Timer` on the machine's own clocks."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def after(self, seconds: float) -> object:
        raise NotImplementedError


def measure(
    bucket: Bucket,
    mirror: Path,
    strategy: StrategyConfig,
    through: date,
    seams: Sequence[date],
    kinds: PriceKinds,
    records: Sequence[StressRecord],
    *,
    clock: Clock,
    timer: Timer,
) -> str:
    """The record of `strategy`'s stress move through `through`, as TOML, or a `Halt`."""
    settings = data_settings([strategy])
    starts = {leg: settings.first_sessions[leg] for leg in sorted(strategy.instruments)}
    spans = _spans(starts, through, seams)
    held = Mirror(bucket, mirror)
    manifests: set[str] = set()

    def read(legs: Sequence[InstrumentId], last: date, lookback: int) -> Reading:
        reading = _read(
            held,
            replace(
                settings,
                instruments=tuple(legs),
                lookback=lookback,
                # At a written lag of 1 the record would name a session nobody read.
                stale_lag=0,
            ),
            last,
            kinds,
            clock,
            timer,
        )
        manifests.add(reading.snapshot.ledgers[MANIFEST])
        if len(manifests) > 1:
            raise Halt(f"{MANIFEST} changed between two reads, so run the command again")
        return reading

    for leg, start in starts.items():
        read((leg,), start, 2)
    bars: dict[InstrumentId, list[Bar]] = {leg: [] for leg in starts}
    flagged: list[FlaggedBar] = []
    for first, last in spans:
        begun = [leg for leg, start in starts.items() if start <= last]
        reading = read(begun, last, len(load_sessions().between(first, last)))
        for leg in begun:
            bars[leg].extend(reading.snapshot.bars[leg][DAILY])
            flagged += (
                FlaggedBar(leg, flag.session, flag.field, flag.rule, "")
                for flag in reading.snapshot.flags.get(leg, ())
            )
    for seam in seams:
        if not any(_changes(own, seam) for own in bars.values()):
            raise Halt(f"no leg changes series at the seam {seam.isoformat()}")
    two, one = largest_move(bars, 2), largest_move(bars, 1)
    (manifest,) = manifests
    record = StressRecord(
        strategy=strategy.name,
        instruments=tuple(bars),
        vintage=stress_vintage(bars),
        through=through,
        stress_move=two.move.size,
        leg=two.move.leg,
        moved_from=two.move.moved_from,
        moved_to=two.move.moved_to,
        one_session=one.move.size,
        one_session_leg=one.move.leg,
        one_session_on=one.move.moved_to,
        series=leg_series(bars),
        left_out=_left_out(one, two),
        flagged=tuple(sorted(flagged, key=lambda f: (f.session, f.instrument, f.rule, f.field))),
        manifest=manifest,
        measured_on=clock.now().date(),
    )
    counted = {m[1:] for own in bars.values() for m in moves(own, 2).kept}
    return _beside(record, records, counted) + _toml(record)


def _spans(
    starts: Mapping[InstrumentId, date], through: date, seams: Sequence[date]
) -> list[tuple[date, date]]:
    """Each series' first and last session, which meet with no session between."""
    table = load_sessions()
    for what, day in (
        *((f"{leg}'s start", start) for leg, start in starts.items()),
        ("--through", through),
        *(("--seam", seam) for seam in seams),
    ):
        try:
            table.session(day)
        except Halt as halt:
            raise Halt(f"{what} is {day.isoformat()}: {halt.reason}") from None
    first = min(table.sessions)
    for leg, start in starts.items():
        if start == first:
            raise Halt(
                f"{leg}'s start is {start.isoformat()}, the session table's first, so no read "
                "can look for an earlier bar"
            )
    start = min(starts.values())
    if through <= start:
        raise Halt(f"--through is {through.isoformat()}, not after the start {start.isoformat()}")
    before = start
    for seam in seams:
        if not before < seam <= through:
            raise Halt(
                f"--seam is {seam.isoformat()}, not after {before.isoformat()} and at or before "
                f"{through.isoformat()}"
            )
        before = seam
    ends = [*(table.before(seam).day for seam in seams), through]
    return list(zip((start, *seams), ends, strict=True))


def _read(
    mirror: Mirror,
    settings: DataSettings,
    last: date,
    kinds: PriceKinds,
    clock: Clock,
    timer: Timer,
) -> Reading:
    """One read at the open of the session after `last`, or a halt on any reason it holds."""
    table = load_sessions()
    after = [day for day in table.sessions if day > last]
    if not after:
        raise Halt(f"the session table holds no session after {last.isoformat()}")
    source = LakeSource(mirror, settings, clock=clock, timer=timer, kinds=kinds)
    reading = source.read(table.session(min(after)).open)
    if reading.halts:
        raise Halt("; ".join(reading.halts))
    return reading


def _changes(bars: Sequence[Bar], seam: date) -> bool:
    return any(
        two.session == seam and series_of(one) != series_of(two)
        for one, two in itertools.pairwise(bars)
    )


def _left_out(*measured: LargestMove) -> tuple[LeftOut, ...]:
    return tuple(
        sorted(
            (move for found in measured for move in found.left_out),
            key=lambda m: (m.instrument, m.sessions, m.moved_from),
        )
    )


def _beside(
    record: StressRecord,
    records: Sequence[StressRecord],
    counted: Collection[tuple[InstrumentId, date, date]],
) -> str:
    """A comment for each record of the strategy that is larger, or whose move this run left out."""
    lines = []
    for other in records:
        if other.strategy != record.strategy:
            continue
        said = []
        if record.stress_move < other.stress_move:
            said.append("is larger")
        if (other.leg, other.moved_from, other.moved_to) not in counted:
            said.append(
                f"was set by {other.leg}'s move from {other.moved_from.isoformat()} to "
                f"{other.moved_to.isoformat()}, which this run did not count"
            )
        if said:
            which = "the withdrawn record" if other.withdrawn is not None else "the record"
            lines.append(
                f"# {which} of vintage {other.vintage}, with a stress_move of "
                f"{other.stress_move}, {' and '.join(said)}\n"
            )
    return "".join(lines)


# The record as TOML


def _toml(record: StressRecord) -> str:
    lines = ["[[record]]"]
    for name in (
        "strategy",
        "instruments",
        "vintage",
        "through",
        "stress_move",
        "leg",
        "moved_from",
        "moved_to",
        "one_session",
        "one_session_leg",
        "one_session_on",
    ):
        lines.append(f"{name} = {_value(getattr(record, name))}")
    for name, rows in (
        ("series", [_series(s) for s in record.series]),
        ("left_out", [vars(m) for m in record.left_out]),
        ("flagged", [vars(f) for f in record.flagged]),
    ):
        lines += _rows(name, rows)
    lines += [f"{name} = {_value(getattr(record, name))}" for name in ("manifest", "measured_on")]
    return "\n".join(lines) + "\n"


def _series(held: LegSeries) -> Mapping[str, object]:
    run = {"instrument": held.instrument, "first": held.first, "last": held.last}
    written = {**run, **held.series._asdict()}
    return {name: value for name, value in written.items() if value is not None}


def _rows(name: str, rows: Iterable[Mapping[str, object]]) -> list[str]:
    inside = [
        "    { " + ", ".join(f"{k} = {_value(v)}" for k, v in row.items()) + " }," for row in rows
    ]
    return [f"{name} = [", *inside, "]"] if inside else [f"{name} = []"]


def _value(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    return json.dumps(f"{value:f}" if isinstance(value, Decimal) else value, ensure_ascii=False)


# The command


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise Halt(message)


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _Parser(prog="stress_move", allow_abbrev=False)
    parser.add_argument("--lake", required=True, type=Path)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--through", required=True, type=_day)
    parser.add_argument("--seam", action="append", default=[], type=_day)
    return parser.parse_args(argv)


def _day(text: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        raise argparse.ArgumentTypeError(f"{text!r} is no date")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is no date") from None


def main(
    argv: Sequence[str] | None = None,
    *,
    kinds: PriceKinds | None = None,
    records: Path = STRESS_MOVES,
    strategies: Path = STRATEGIES,
    clock: Clock | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    timer = _SystemTimer()
    hidden = {_CHECKOUT: "<checkout>"}
    try:
        given = _arguments(argv)
        lake = given.lake.absolute()
        hidden[lake] = "<lake>"
        with tempfile.TemporaryDirectory() as folder:
            mirror = Path(folder)
            hidden[mirror] = "<mirror>"
            (strategy,) = load_strategies(strategies, [given.strategy])
            text = measure(
                FolderBucket(lake),
                mirror,
                strategy,
                given.through,
                given.seam,
                load_price_kinds() if kinds is None else kinds,
                load_records(records),
                clock=clock or WallClock(timer),
                timer=timer,
            )
        print(text, end="", file=out or sys.stdout)
    except Exception as error:
        said = error.reason if isinstance(error, Halt) else f"{type(error).__name__}: {error}"
        print(f"stress_move: {_one_line(said, hidden)}", file=err or sys.stderr)
        return 1
    return 0


_CHECKOUT = Path(__file__).parents[3]
_ABSOLUTE = re.compile(r"(?<![\w.>/])/[^\s'\":,]+")


def _one_line(said: str, hidden: Mapping[Path, str]) -> str:
    """The reason on one line, with each folder the command knows replaced by a word for it.

    Any other absolute path it recognises is left out too.
    """
    for path in sorted(hidden, key=lambda p: len(str(p)), reverse=True):
        # A lake at the machine's root would otherwise be every slash.
        if len(str(path)) > 1:
            said = said.replace(str(path), hidden[path])
    return " ".join(_ABSOLUTE.sub("<path>", said).split())


if __name__ == "__main__":
    sys.exit(main())
