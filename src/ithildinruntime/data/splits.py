"""The record of splits, data/splits.csv, its loader, and the checks every decision runs on it.

A split changes a share's price by a fixed ratio and leaves its value alone,
so a reader that does not know of one reads the step as a move of the market.
docs/design.md's rule that a split reads as neither a gain nor a loss says why
the record is a file the platform owns, written by a person from published
sources, and docs/what-a-person-runs.md says how a row is written.

**The file.** Three kinds of row share one header. A `split` row names the
first session on the new scale, the new shares per old and when the split
became knowable. A `covered` row says the record holds every split of one
instrument over a span of sessions. A `withdrawn` row ends a false split row.
`load_splits` halts every decision, as missing input, on a file that is
missing, unreadable or empty, on a wrong header, and on any row that fails a
check, naming the row by its line.

**What a decision checks.** `LakeSource.read` gathers what covers each pair of
consecutive sessions into `Snapshot.splits`, and `check_splits` judges it from
the stored snapshot and the session table alone, so `source.judge` repeats it
on a redelivery. The pairs run, for each instrument whose window holds a `raw`
bar, from that bar's session, or from the imported bar before it where the
read passes the one change of series `one_scale` names, to the newest bar's,
and on to the quote's where the decision read one.

1. **Coverage.** A covered row whose span holds both sessions covers a pair.
   Otherwise a quote of the later session must state the close of the earlier
   one, the decision's own quote for its own session and the sealed day for
   any before it. A pair neither covers halts, and pairs that run on from one
   another give one reason, apart from the pair to the decision's own quote,
   which gives its own.
2. **The residual.** Each `close_price` a quote states, over the stored close
   of the earlier session, passes from 0.95 to 1.05, both ends allowed. The
   decision's own quote is checked under a covered row too.
3. **The step.** On a pair of two stored bars that a quote alone covers, a
   close-to-close fall of 30 percent or more, or a rise of 42.9 percent or
   more, halts. Those are the two steps of one size in the logarithm. On the
   pair to the decision's own quote, which only that quote covers, the
   quote's midpoint against the newest stored close halts at the same bounds,
   so a split on the decision's own session that Schwab does not restate halts
   that decision too.
4. **A recorded split.** A split row known by the decision halts it from the
   row's first session on, wherever a bar the decision reads of that
   instrument is not already adjusted for the split, until #190 and #191 let a
   decision trade through one. A raw bar on or after the first session counts
   as adjusted only where the split falls before the instrument's first
   covered row, because the record checked no captured bar before then.

A pair whose later session is a known split row's first session meets that
row's halt and not the residual's or the step's, because the row names it. Each
reason names what releases it and prints no price.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Context, Decimal, InvalidOperation
from pathlib import Path

from ithildinruntime.core import (
    DAILY,
    UNSTATED,
    Bar,
    Halt,
    InstrumentId,
    PairCover,
    Quote,
    Series,
    Snapshot,
    SplitRow,
    not_adjusted_for,
    series_of,
)
from ithildinruntime.core.sessions import SessionTable

from .reference import SCHWAB_IMPORT, SecurityMaster

__all__ = [
    "FALL",
    "HEADER",
    "HIGH",
    "LOW",
    "QUOTE",
    "RISE",
    "SEALED",
    "SPLITS",
    "Pair",
    "SplitRecord",
    "check_splits",
    "load_splits",
    "one_scale",
    "pairs",
    "split_halts",
]

SPLITS = Path(__file__).with_name("splits.csv")
HEADER = (
    "added",
    "kind",
    "instrument",
    "first_session",
    "last_session",
    "new_shares",
    "old_shares",
    "known_at",
    "read_from",
)
SPLIT, COVERED, WITHDRAWN = "split", "covered", "withdrawn"
_KINDS = (SPLIT, COVERED, WITHDRAWN)
# Where a pair's quote came from.
QUOTE, SEALED = "quote", "sealed"
# The residual's bounds, both allowed, and the step's, both halting.
LOW, HIGH = Decimal("0.95"), Decimal("1.05")
FALL, RISE = Decimal("0.70"), Decimal("1.429")
_CONTEXT = Context(prec=28)
_RAW = "raw"
# How many sessions before the day both vendor lists were read a covered row ends, at least.
_LATE = 5
_FILE = "data/splits.csv"
# What follows a halt on the decision's own pair, which no covered row known by a live
# decision can reach, since a row ends 5 sessions before the day it was added.
_NEXT_SESSION = (
    "From the next session the pair meets the sealed day's checks, which a covered row "
    "releases once one can reach it"
)
_OWN_SESSION = f"and a real move stays halted for this session. {_NEXT_SESSION}"


def _reached(pair: Pair, usual: str) -> str:
    """What releases a halt on a pair whose release is a covered row, `usual` unless it is own."""
    return f"It stays halted for this session. {_NEXT_SESSION}" if pair.quoted else usual


_RELEASED_BY_A_ROW = (
    "#190 and #191 build trading through a split, and a withdrawn row ends a false one"
)


@dataclass(frozen=True)
class SplitRecord:
    """The rows of data/splits.csv as read, and the SHA-256 of its bytes."""

    rows: tuple[SplitRow, ...]
    sha256: str

    def known(self, instrument: InstrumentId, at: datetime) -> tuple[SplitRow, ...]:
        """The split rows that stand on `instrument` for a decision at `at`, by first session.

        A split row is known by the decision where its `known_at` is at or before
        it. On each key the newest row added wins among the withdrawn rows and the
        split rows known, so a withdrawn row ends the split and a newer split row
        corrects its ratio. The decision reads no `added` date otherwise.
        """
        newest: dict[date, SplitRow] = {}
        for row in self.rows:
            if row.instrument != instrument or row.kind == COVERED:
                continue
            if row.kind == SPLIT and (row.known_at is None or row.known_at > at):
                continue
            held = newest.get(row.first_session)
            if held is None or row.added > held.added:
                newest[row.first_session] = row
        return tuple(row for _, row in sorted(newest.items()) if row.kind == SPLIT)

    def covering(self, instrument: InstrumentId, earlier: date, later: date) -> SplitRow | None:
        """The covered row added last whose span holds both sessions, or `None`."""
        found = [
            row
            for row in self.rows
            if row.kind == COVERED
            and row.instrument == instrument
            and row.last_session is not None
            and row.first_session <= earlier
            and later <= row.last_session
        ]
        return max(found, key=lambda r: (r.added, r.line)) if found else None

    def covered_from(self, instrument: InstrumentId) -> date | None:
        """The first session of the instrument's earliest covered row, or `None`."""
        firsts = [
            r.first_session for r in self.rows if r.kind == COVERED and r.instrument == instrument
        ]
        return min(firsts) if firsts else None


# The file


def load_splits(
    path: Path = SPLITS, *, master: SecurityMaster, sessions: SessionTable
) -> SplitRecord:
    """Read the record, raising `Halt` as missing input on any fault in the file or a row."""
    name = _FILE if path == SPLITS else path.name
    try:
        data = path.read_bytes()
    except OSError as error:
        raise Halt(
            f"{name} cannot be read ({type(error).__name__}), so every decision halts until a "
            "release ships it"
        ) from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Halt(
            f"{name} is not UTF-8, so every decision halts until a release corrects it"
        ) from None
    numbered = [
        (n, line) for n, line in enumerate(text.splitlines(), 1) if not line.startswith("#")
    ]
    if not numbered:
        raise Halt(f"{name} is empty, and an empty record of splits is a missing one")
    (header_line, header), *body = numbered
    if next(csv.reader([header])) != list(HEADER):
        raise Halt(
            f"{name} line {header_line} has the header {header!r}, not {','.join(HEADER)}, so "
            "every decision halts until a release corrects it"
        )
    rows = [_row(n, line, name, master, sessions) for n, line in body]
    if not rows:
        raise Halt(f"{name} holds no row, and an empty record of splits is a missing one")
    _refuse_keys(rows, name)
    return SplitRecord(tuple(rows), hashlib.sha256(data).hexdigest())


def _row(n: int, line: str, name: str, master: SecurityMaster, sessions: SessionTable) -> SplitRow:
    where = f"{name} line {n}"
    fields = next(csv.reader([line]), [])
    if len(fields) != len(HEADER):
        raise _bad(where, f"holds {len(fields)} fields, not {len(HEADER)}")
    raw = dict(zip(HEADER, fields, strict=True))
    kind = raw["kind"]
    if kind not in _KINDS:
        raise _bad(where, f"is of kind {kind!r}, not one of {', '.join(_KINDS)}")
    instrument = InstrumentId(raw["instrument"])
    if not any(row.id == instrument for row in master.rows):
        raise _bad(where, f"names {instrument!r}, which the security master does not name")
    added = _day(raw["added"], where, "added")
    first = _session(raw["first_session"], where, "first_session", sessions)
    if not raw["read_from"]:
        raise _bad(where, "names nothing it was read from")
    last, new, old, known = None, None, None, None
    if kind == SPLIT:
        new = _shares(raw["new_shares"], where, "new_shares")
        old = _shares(raw["old_shares"], where, "old_shares")
        if new == old:
            raise _bad(where, "has new shares equal to old, which is no split")
        known = _instant(raw["known_at"], where)
        opens = sessions.session(first).open
        if known > opens:
            raise _bad(
                where,
                f"has a known_at of {raw['known_at']}, after the open of its first session at "
                f"{opens.isoformat()}",
            )
        empty = ("last_session",)
    elif kind == COVERED:
        last = _session(raw["last_session"], where, "last_session", sessions)
        if first > last:
            raise _bad(where, "is a covered row whose first session is after its last")
        # A vendor can post a split days late, so a row stops 5 sessions before its lists.
        before = [d for d in sorted(sessions.sessions) if d < added]
        if len(before) < _LATE or last > before[-_LATE]:
            raise _bad(
                where,
                f"is a covered row whose last session, {last.isoformat()}, is not 5 sessions "
                f"before the day it was added, {added.isoformat()}",
            )
        empty = ("new_shares", "old_shares", "known_at")
    else:
        empty = ("last_session", "new_shares", "old_shares", "known_at")
    if filled := [field for field in empty if raw[field]]:
        raise _bad(where, f"is a {kind} row and fills {', '.join(filled)}")
    return SplitRow(
        added=added,
        kind=kind,
        instrument=instrument,
        first_session=first,
        last_session=last,
        new_shares=new,
        old_shares=old,
        known_at=known,
        read_from=raw["read_from"],
        line=n,
    )


def _refuse_keys(rows: Sequence[SplitRow], name: str) -> None:
    """Refuse two keyed rows of one key added on one date, and a withdrawal of nothing."""
    keyed = [r for r in rows if r.kind != COVERED]
    for row in keyed:
        same = [r for r in keyed if _key(r) == _key(row)]
        if any(r.added == row.added and r.line < row.line for r in same):
            raise _bad(
                f"{name} line {row.line}",
                f"is a second row of {row.instrument} from {row.first_session} added on "
                f"{row.added}",
            )
        if row.kind == WITHDRAWN and not any(r.kind == SPLIT and r.added < row.added for r in same):
            raise _bad(
                f"{name} line {row.line}",
                f"withdraws a split row of {row.instrument} from {row.first_session}, and no "
                "split row on that key was added before it",
            )


def _bad(where: str, what: str) -> Halt:
    return Halt(f"{where} {what}, so every decision halts until a release corrects the row")


def _key(row: SplitRow) -> tuple[InstrumentId, date]:
    return row.instrument, row.first_session


def _day(text: str, where: str, name: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _bad(where, f"holds {name} {text!r}, which is no date") from None


def _session(text: str, where: str, name: str, sessions: SessionTable) -> date:
    day = _day(text, where, name)
    if day not in sessions.sessions:
        raise _bad(
            where,
            f"holds {name} {day.isoformat()}, which the exchange calendar holds no session on",
        )
    return day


def _shares(text: str, where: str, name: str) -> int:
    if not text.isascii() or not text.isdigit() or int(text) < 1:
        raise _bad(where, f"holds {name} {text!r}, which is no positive whole number")
    return int(text)


def _instant(text: str, where: str) -> datetime:
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        at = None
    if at is None or at.utcoffset() != timedelta(0):
        raise _bad(where, f"holds known_at {text!r}, which is no UTC instant")
    return at.astimezone(UTC)


# The pairs


def one_scale(one: Bar, two: Bar) -> bool:
    """Whether two consecutive bars of two series are the one change read as one scale.

    The earlier bar is imported from `SCHWAB_IMPORT`, `adjusted` as of the later
    bar's session, and the later bar is captured. Schwab's candles come back on
    the scale of the day they are fetched, on the two forward splits
    l3a0/marketlake#951 measured, so the import is on the scale of that
    session, and a captured bar is as traded on it. docs/design.md's
    `#### A lookback that crosses a change of series` says what it costs, and the
    pair across it is one the checks here read.
    """
    return (
        one.origin == "imported"
        and series_of(one) == Series("adjusted", SCHWAB_IMPORT, two.session)
        and two.origin == "captured"
        and series_of(two) == Series(_RAW, None, None)
    )


@dataclass(frozen=True)
class Pair:
    """Two consecutive sessions of one instrument that a decision checks."""

    instrument: InstrumentId
    earlier: date
    later: date
    # True for the pair from the newest bar's session to the decision's own quote's.
    quoted: bool


def pairs(
    bars: Mapping[InstrumentId, Mapping[str, Sequence[Bar]]],
    quoted: Iterable[InstrumentId],
    quote_day: date | None,
    sessions: SessionTable,
) -> list[Pair]:
    """Every pair a decision checks, in the order of the instruments and then the sessions."""
    days = sorted(sessions.sessions)
    with_quote = set(quoted)
    out: list[Pair] = []
    for instrument, by_interval in bars.items():
        held = list(by_interval.get(DAILY, ()))
        start = _start(held)
        if start is None:
            continue
        newest = held[-1].session
        span = [d for d in days if start <= d <= newest]
        out.extend(Pair(instrument, p, s, False) for p, s in itertools.pairwise(span))
        if quote_day is not None and instrument in with_quote:
            out.append(Pair(instrument, newest, quote_day, True))
    return out


def _start(held: Sequence[Bar]) -> date | None:
    """The first session a window's pairs run from: its first raw bar's, or the seam's before it."""
    for n, bar in enumerate(held):
        if bar.basis == _RAW:
            if n and one_scale(held[n - 1], bar):
                return held[n - 1].session
            return bar.session
    return None


# The checks


def check_splits(snapshot: Snapshot, sessions: SessionTable) -> list[str]:
    """Every reason the record of splits halts a stored snapshot's decision, from it alone."""
    read = snapshot.splits
    opened = sessions.opened_by(snapshot.decision_at)
    quote_day = opened.day if snapshot.decision_at < opened.close else None
    covers = {(c.instrument, c.earlier, c.later): c for c in read.pairs}
    found = pairs(snapshot.bars, snapshot.quotes, quote_day, sessions)
    reasons: list[str] = []
    for instrument, group in itertools.groupby(found, key=lambda p: p.instrument):
        firsts = {row.first_session for row in read.rows.get(instrument, ())}
        reasons.extend(_instrument(instrument, list(group), covers, snapshot, firsts, sessions))
    reasons.extend(split_halts(snapshot, opened.day))
    return reasons


def split_halts(snapshot: Snapshot, day: date) -> list[str]:
    """Check 4: a known split row, from its first session on, where a bar read is not adjusted."""
    read = snapshot.splits
    reasons = []
    for instrument, rows in read.rows.items():
        bars = snapshot.bars.get(instrument, {}).get(DAILY, ())
        for row in rows:
            if row.first_session > day:
                continue
            covered_from = read.covered_from.get(instrument)
            if not any(not_adjusted_for(b, row.first_session, covered_from) for b in bars):
                continue
            reasons.append(
                f"{instrument} has a recorded split of {row.new_shares} new shares for "
                f"{row.old_shares} old from {row.first_session.isoformat()}, at {_FILE} line "
                f"{row.line}, and the decision reads bars of {instrument} not adjusted for it. "
                f"{_RELEASED_BY_A_ROW}"
            )
    return reasons


def _instrument(
    instrument: InstrumentId,
    found: Sequence[Pair],
    covers: Mapping[tuple[InstrumentId, date, date], PairCover],
    snapshot: Snapshot,
    firsts: set[date],
    sessions: SessionTable,
) -> Iterator[str]:
    closes = {b.session: b.close for b in snapshot.bars.get(instrument, {}).get(DAILY, ())}
    short: list[Pair] = []
    if unread := [p for p in found if (p.instrument, p.earlier, p.later) not in covers]:
        yield (
            f"the stored snapshot names no cover for {len(unread)} pairs of {instrument}'s "
            f"sessions from {unread[0].earlier.isoformat()} to {unread[-1].later.isoformat()}, "
            "so it is not the read this release writes"
        )
    for pair in found:
        if pair.quoted and short:
            # The stored pairs keep the release a covered row gives them.
            yield _short(instrument, short)
            short = []
        cover = covers.get((pair.instrument, pair.earlier, pair.later))
        if pair.quoted and pair.earlier != sessions.before(pair.later).day:
            yield (
                f"{instrument}'s newest bar is of {pair.earlier.isoformat()}, not the session "
                f"before the quote's own of {pair.later.isoformat()}, so no quote states its "
                "close. A bar of the session before releases it"
            )
            continue
        if cover is None or cover.fault is not None:
            # A pair no run passes over, so the run before it ends there.
            if short:
                yield _short(instrument, short)
                short = []
            if cover is not None:
                yield f"{cover.fault}. " + _reached(
                    pair,
                    f"A read that succeeds, or a covered row reaching {pair.later.isoformat()}, "
                    "releases it",
                )
            continue
        usable = cover.close_kind != UNSTATED and bool(cover.close_prices)
        if cover.covered is None and not usable:
            short.append(pair)
            continue
        if short:
            yield _short(instrument, short)
            short = []
        if pair.later in firsts:
            continue
        yield from _residuals(instrument, pair, cover, closes)
        if cover.covered is None and cover.source == SEALED:
            yield from _step(instrument, pair, closes)
        elif cover.covered is None and pair.quoted:
            yield from _quoted_step(instrument, pair, closes, snapshot.quotes.get(instrument))
    if short:
        yield _short(instrument, short)


def _short(instrument: InstrumentId, short: Sequence[Pair]) -> str:
    first, last = short[0], short[-1]
    named = (
        f"the sessions {first.earlier.isoformat()} and {first.later.isoformat()}"
        if len(short) == 1
        else f"the {len(short)} pairs of sessions from {first.earlier.isoformat()} to "
        f"{last.later.isoformat()}"
    )
    return (
        f"the record of splits does not reach {instrument} on {named}: no covered row holds "
        "them, and no quote of a later session states the close of the one before under a "
        "kind data/quote_kinds.csv states. "
        + _reached(last, f"A covered row reaching {last.later.isoformat()} releases it")
    )


def _residuals(
    instrument: InstrumentId, pair: Pair, cover: PairCover, closes: Mapping[date, Decimal]
) -> Iterator[str]:
    where = (
        "own quote"
        if cover.source == QUOTE
        else f"the sealed day {cover.partition} at SHA-256 {cover.partition_sha256}"
    )
    if len(cover.close_prices) > 1:
        yield (
            f"{instrument}'s {where} states {len(cover.close_prices)} values of the close of "
            f"{pair.earlier.isoformat()}, so no one value checks the pair to "
            f"{pair.later.isoformat()}. "
            + _reached(pair, f"A covered row reaching {pair.later.isoformat()} releases it")
        )
        return
    close = closes.get(pair.earlier)
    if close is None or close <= 0 or cover.close_kind == UNSTATED:
        return
    for said in cover.close_prices:
        ratio = _ratio(said)
        if ratio is None:
            yield (
                f"{instrument}'s {where} states a close of {pair.earlier.isoformat()} that is no "
                "price. "
                + _reached(pair, f"A covered row reaching {pair.later.isoformat()} releases it")
            )
            continue
        residual = _CONTEXT.divide(ratio, close)
        if not LOW <= residual <= HIGH:
            released = (
                _OWN_SESSION
                if pair.quoted
                else "or a covered row reaching it where Schwab did not restate"
            )
            yield (
                f"{instrument}'s {where} of {pair.later.isoformat()} states a close of "
                f"{pair.earlier.isoformat()} at {_shown(residual)} times the stored close, outside "
                f"{LOW} to {HIGH}, as a split or a distribution with no row would. A split row "
                f"for {pair.later.isoformat()}, a correction or a withdrawn row in {_FILE} "
                f"releases it, {released}"
            )


def _step(instrument: InstrumentId, pair: Pair, closes: Mapping[date, Decimal]) -> Iterator[str]:
    before, after = closes.get(pair.earlier), closes.get(pair.later)
    if before is None or after is None or before <= 0:
        return
    step = _CONTEXT.divide(after, before)
    if step <= FALL or step >= RISE:
        yield (
            f"{instrument}'s close of {pair.later.isoformat()} is {_shown(step)} times its close "
            f"of {pair.earlier.isoformat()}, a fall of 30 percent or more or a rise of 42.9 "
            "percent or more that only a quote covers. A split row, or a covered row that says "
            "the move was real, releases it"
        )


def _quoted_step(
    instrument: InstrumentId, pair: Pair, closes: Mapping[date, Decimal], quote: Quote | None
) -> Iterator[str]:
    """The step from the newest stored close to the decision's own quote's midpoint.

    A split on the decision's own session that Schwab's `close_price` does not
    restate leaves the newest bar on the old scale and the quote on the new
    one, and only the quote covers the pair. A quote with no bid or no ask
    halts on its own check.
    """
    before = closes.get(pair.earlier)
    bid = quote.bid if quote is not None else None
    ask = quote.ask if quote is not None else None
    # A side that is missing, not finite or at or below zero halts on the quote's own check.
    if not _usable(bid) or not _usable(ask):
        return
    if before is None or before <= 0:
        return
    step = _CONTEXT.divide(_CONTEXT.add(bid, ask), _CONTEXT.multiply(before, 2))
    if step <= FALL or step >= RISE:
        yield (
            f"{instrument}'s own quote of {pair.later.isoformat()} has a midpoint {_shown(step)} "
            f"times its close of {pair.earlier.isoformat()}, a fall of 30 percent or more or a "
            f"rise of 42.9 percent or more that only a quote covers. A split row releases it, "
            f"{_OWN_SESSION}"
        )


def _usable(price: object) -> bool:
    return isinstance(price, Decimal) and price.is_finite() and price > 0


def _ratio(said: str) -> Decimal | None:
    try:
        value = Decimal(said)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _shown(ratio: Decimal) -> str:
    """A ratio to four places, which says how far off it is and names no price."""
    return str(ratio.quantize(Decimal("0.0001")))
