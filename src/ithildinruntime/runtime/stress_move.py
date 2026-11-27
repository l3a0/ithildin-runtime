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
   holds two series. Data halts on a seam nobody named, and this refuses the
   one change data passes, from Schwab's import to captured bars, until it is
   named too.
3. **The record of splits.** Every pair of consecutive sessions of a `raw`
   series, and the pair across the change from Schwab's import to it, must sit
   inside a covered row of data/splits.csv. A quote that states the close of
   the session before covers a decision's pair, and never a stress record's,
   because the record is what says no split fell there. A split row from a
   session the read reaches, on bars not already adjusted for it, halts the
   read as it halts a decision, until #190 builds the read that applies one.
   The record names the file's SHA-256 and every split row its instruments
   hold.
4. **The moves**, over the spans joined, so a seam one leg does not change at
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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NoReturn, TextIO

from ithildinruntime.adapters import FolderBucket, SystemTimer, WallClock
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
    KnownSplit,
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
from ithildinruntime.data.reference import PriceKinds, load_master
from ithildinruntime.data.splits import SplitRecord, load_splits, pairs

__all__ = ["main", "measure"]


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
    splits: SplitRecord | None = None,
) -> str:
    """The record of `strategy`'s stress move through `through`, as TOML, or a `Halt`.

    `splits` is the record of splits it reads, the tracked file's where none is given.
    """
    settings = data_settings([strategy])
    record_of_splits = splits or load_splits(master=load_master(), sessions=load_sessions())
    starts = {leg: settings.first_sessions[leg] for leg in sorted(strategy.instruments)}
    spans = _spans(starts, through, seams)
    held = Mirror(bucket, mirror)
    manifests: set[str] = set()

    def read(legs: Sequence[InstrumentId], last: date, lookback: int) -> Reading:
        reading = _read(
            held,
            bucket,
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
            record_of_splits,
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
            _one_series(leg, reading.snapshot.bars[leg][DAILY])
            bars[leg].extend(reading.snapshot.bars[leg][DAILY])
            flagged += (
                FlaggedBar(leg, flag.session, flag.field, flag.rule, "")
                for flag in reading.snapshot.flags.get(leg, ())
            )
    for seam in seams:
        if not any(_changes(own, seam) for own in bars.values()):
            raise Halt(f"no leg changes series at the seam {seam.isoformat()}")
    known = _splits_held(bars, record_of_splits, clock.now())
    two, one = largest_move(bars, 2), largest_move(bars, 1)
    # m is the larger of the two, and a tie names the two-session move.
    larger = one.move if one.move.size > two.move.size else two.move
    (manifest,) = manifests
    record = StressRecord(
        strategy=strategy.name,
        instruments=tuple(bars),
        vintage=stress_vintage(bars),
        through=through,
        stress_move=larger.size,
        leg=larger.leg,
        moved_from=larger.moved_from,
        moved_to=larger.moved_to,
        one_session=one.move.size,
        one_session_leg=one.move.leg,
        one_session_on=one.move.moved_to,
        series=leg_series(bars),
        left_out=_left_out(one, two),
        flagged=tuple(sorted(flagged, key=lambda f: (f.session, f.instrument, f.rule, f.field))),
        manifest=manifest,
        measured_on=clock.now().date(),
        splits=record_of_splits.sha256,
        split_rows=known,
    )
    counted = {m[1:] for own in bars.values() for n in (1, 2) for m in moves(own, n).kept}
    return _beside(record, records, counted) + _toml(record)


def _splits_held(
    bars: Mapping[InstrumentId, Sequence[Bar]], record: SplitRecord, now: datetime
) -> tuple[KnownSplit, ...]:
    """Every split row the legs hold, once no pair of raw bars refuses the read.

    A split row on bars not already adjusted for it halts the read itself, as
    it halts a decision, so the command refuses it before it gets here.
    """
    table = load_sessions()
    for leg, held in bars.items():
        for pair in pairs({leg: {DAILY: tuple(held)}}, (), None, table):
            if record.covering(leg, pair.earlier, pair.later) is None:
                raise Halt(
                    f"{leg}'s sessions {pair.earlier.isoformat()} and {pair.later.isoformat()} "
                    "read a raw series that no covered row of data/splits.csv holds, and a "
                    "stress record over captured bars rests on the record alone. A covered row "
                    f"reaching {pair.later.isoformat()} releases it"
                )
    known = []
    for leg in bars:
        for row in record.known(leg, now):
            assert row.new_shares is not None and row.old_shares is not None
            known.append(KnownSplit(leg, row.first_session, row.new_shares, row.old_shares))
    return tuple(known)


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
    bucket: Bucket,
    settings: DataSettings,
    last: date,
    kinds: PriceKinds,
    clock: Clock,
    timer: Timer,
    splits: SplitRecord,
) -> Reading:
    """One read just before the session after `last` opens, or a halt on any reason it holds.

    A read inside a session reads its quotes too, which this measures nothing of.
    """
    table = load_sessions()
    after = [day for day in table.sessions if day > last]
    if not after:
        raise Halt(f"the session table holds no session after {last.isoformat()}")
    # It reads no quote, so the bucket of segments is never asked for one.
    # It trades nothing, so its clock is read for the time alone and measured by nothing.
    source = LakeSource(
        mirror,
        settings,
        live=bucket,
        clock=clock,
        timer=timer,
        measured=False,
        kinds=kinds,
        splits=splits,
    )
    reading = source.read(table.session(min(after)).open - timedelta(microseconds=1))
    if reading.halts:
        raise Halt("; ".join(reading.halts))
    return reading


def _one_series(leg: InstrumentId, held: Sequence[Bar]) -> None:
    """Refuse a read of one span that holds two series, as at a seam nobody named.

    Data halts on such a read, but for the one change its rule passes because
    the two sit on one scale. The span is still to be named, so this refuses
    that one too.
    """
    for one, two in itertools.pairwise(held):
        if series_of(one) != series_of(two):
            raise Halt(
                f"{leg}'s bars for {one.session.isoformat()} and {two.session.isoformat()} are "
                f"of two series, {series_of(one)} and {series_of(two)}, at a seam nobody named"
            )


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
    if record.splits is not None:
        lines.append(f"splits = {_value(record.splits)}")
        lines += _rows("split_rows", [vars(row) for row in record.split_rows])
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
    splits: SplitRecord | None = None,
) -> int:
    timer = SystemTimer()
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
                clock=clock or WallClock(timer, servers=None),
                timer=timer,
                splits=splits,
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
