"""The command that counts how often recorded quotes would be refused, at a step of maxima.

    python -m ithildinruntime.runtime.quote_refusals --lake FOLDER --instruments GLD USO \
        --from DATE --through DATE [--normal-status STATUS]

A decision before the close halts until its strategy writes a maximum quote age
and a widest spread. This prints what each choice would have refused on the
sealed sessions named, which bounds each limit from below. #133 names the bounds
from above, which this does not measure, and holds the steps, written there
before the first run. Every figure it prints is exploratory, and it prints no
price and no spread's value.

It reads a folder that holds a copy of marketlake's lake, through
`adapters.FolderBucket`, so it needs no credential. How it reads, in order.

1. **Each session is read once for each age**, at close−20, through
   `data.sealed.read_sealed`, the read a decision takes on a sealed day. The
   read cuts 15 seconds before the decision, and its walk stops a leg by the
   age, so a small age can refuse with no shared capture where a larger one
   finds an old quote. A spread changes no read.
2. **Each read is judged by `core.quote_faults`** at every spread, and at three
   moments: the decision, a send `SEND` after it, and the market-on-close
   cutoff at close−12, which design.md proposes for the hard caps. No send has
   been measured, so `SEND` stands in for one. On age alone, a quote refused at
   a send t seconds after the decision under a maximum a is refused at the
   decision under a − t. What it prints says both.
3. **The kind of each quote** comes from `data/quote_kinds.csv` where a row
   covers the session. Where none does, a stand-in row states the normal status
   given to `--normal-status`, and a placeholder for every other field, so no
   session it covers is refused for an unstated kind. What it prints says which
   sessions took it.
4. **A session it cannot measure stops the run.** Where the manifest lists no
   instrument's sealed quotes for a session, a decision that day would not take
   the sealed read. Where a listed partition is missing from the folder, or its
   bytes differ from the manifest, the copy is incomplete or damaged. None of
   these is a refusal to count.

On any refusal it writes one line to stderr, nothing to stdout, and exits
nonzero. docs/what-a-person-runs.md says when to run it.
"""

from __future__ import annotations

import argparse
import math
import re
import statistics
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NoReturn, TextIO

from ithildinruntime.adapters import FolderBucket, WallClock
from ithildinruntime.core import (
    BeforeClose,
    Bucket,
    Clock,
    Halt,
    InstrumentId,
    Session,
    load_sessions,
    quote_faults,
    to_utc,
)
from ithildinruntime.core.clock import CLOCK_BOUND
from ithildinruntime.data import Mirror, QuoteLimits, SecurityMaster, load_master, load_quote_kinds
from ithildinruntime.data.cycles import QuoteRead
from ithildinruntime.data.mirror import LakeRoot
from ithildinruntime.data.reference import (
    QUOTE_NOT_STATED,
    QUOTE_SOURCE,
    QuoteKind,
    QuoteKindRow,
    QuoteKinds,
)
from ithildinruntime.data.sealed import is_listed, partition_key, read_sealed

__all__ = ["AGES", "CUTOFF", "DECISION", "SEND", "SPREADS", "Measurement", "main", "measure"]

# The steps #133 wrote before the first run, which a test pins.
DECISION = BeforeClose(20)
CUTOFF = BeforeClose(12)
SEND = timedelta(seconds=30)
AGES = (10, 15, 30, 45, 60, 75, 90, 105, 120, 150, 180, 240, 300, 600, 900)
SPREADS = tuple(
    Decimal(s)
    for s in ("0.00002", "0.00005", "0.0001", "0.0002", "0.0005", "0.001", "0.002", "0.005", "0.01")
)
MOMENTS = (
    "the decision",
    f"a send {SEND.seconds} seconds after",
    "the cutoff at close−12, a proposal",
)
PLAN = "https://github.com/l3a0/ithildin-runtime/issues/133#issuecomment-7183936703"

_STAND_IN = "a placeholder of the measurement's"

# The reasons each step can lift. Every other reason refuses at every step.
OLD = "too old"
UNSHARED = "no capture every instrument shares"
WIDE = "too wide"
# Two reasons the counts below name. A read with no fault holds a quote of every
# instrument, so `MISSING` only guards a read that breaks that.
MISSING = "no quote"
AFTER = "a stamp after its capture"

# Each reason `core.quote_faults` gives, by what it starts or ends with. Order
# matters only where one text could match two, and none does.
_QUOTE_REASONS = (
    ("a maximum it cannot be read under", re.compile(r" is read under a maximum ")),
    ("now before the decision", re.compile(r"^now is ")),
    ("a side missing or not above zero", re.compile(r"and not a price above zero$")),
    ("crossed", re.compile(r"^its ask of \S+ is below its bid of ")),
    (WIDE, re.compile(r" are further apart than the written ")),
    ("not real-time", re.compile(r"^its realtime flag ")),
    ("status not normal", re.compile(r"^its status is ")),
    ("price kind unstated", re.compile(r"'s quote is of unstated price kind$")),
    (
        "a stamp that is no instant",
        re.compile(r"^its (bid|ask) has the stamp .*, which is no instant$"),
    ),
    (
        AFTER,
        re.compile(r"^its (bid|ask) has the stamp .* later than its capture at "),
    ),
    (OLD, re.compile(r"^its (bid|ask) has the stamp .* seconds old at ")),
    ("captured outside the session", re.compile(r"^it was captured at ")),
)
_READ_REASONS = (
    ("partitions not listed", re.compile(r"^the manifest lists ")),
    (UNSHARED, re.compile(r"^no capture by ")),
    ("no row of the equity close", re.compile(r" holds no row of the session's equity close$")),
    ("quarantined", re.compile(r" is quarantined in marketlake$")),
    ("two rows in one capture", re.compile(r" rows captured at ")),
    ("unreadable snap_ts values", re.compile(r" snap_ts values in it cannot be read$")),
    ("a partition that could not be linked or read", re.compile(r" could not be (linked|read)\b")),
    ("a row of another ticker", re.compile(r" holds a row of ticker ")),
    ("a column of another type", re.compile(r", and marketlake publishes ")),
    ("a capture stamp that is no time", re.compile(r" has a fetch_end_ts .* that is no time$")),
    ("no one ticker for an instrument", re.compile(r" security master rows? ")),
    ("no one quote-kind row", re.compile(r" quote-kind rows on ")),
)
UNCLASSED = "a reason this command does not class"

_MICROSECOND = timedelta(microseconds=1)


def quote_reason(fault: str) -> str:
    """The class of one reason `core.quote_faults` gave."""
    return _classed(fault, _QUOTE_REASONS)


def read_reason(fault: str) -> str:
    """The class of one fault of the read itself."""
    return _classed(fault, _READ_REASONS, "another fault of the read")


def _classed(
    fault: str, reasons: Sequence[tuple[str, re.Pattern[str]]], other: str = UNCLASSED
) -> str:
    return next((name for name, pattern in reasons if pattern.search(fault)), other)


@dataclass(frozen=True)
class Day:
    """One session's reads, one at each age, and the moments it is judged at."""

    session: Session
    decision: datetime
    moments: tuple[datetime, datetime, datetime]
    reads: Mapping[int, QuoteRead]
    stand_in: bool
    instruments: tuple[InstrumentId, ...]

    def refused(self, age: int, spread: Decimal, moment: int) -> Counter[tuple[str, str]]:
        """Each reason the decision is refused, by its class and instrument, or none.

        Each class counts once for each instrument, so a reason both sides of one
        quote give counts once. A fault of the read names no instrument, and
        stands beside an empty one.
        A read with no fault and no quote of an instrument refuses it, as data's
        stage does.
        """
        read = self.reads[age]
        found = {(read_reason(f), "") for f in read.faults}
        if not read.faults:
            found.update((MISSING, str(i)) for i in self.instruments if i not in read.quotes)
        for instrument, quote in sorted(read.quotes.items()):
            judged = replace(quote, max_age=age, max_spread_share=spread)
            for fault in quote_faults(judged, self.decision, self.moments[moment]):
                found.add((quote_reason(fault), str(instrument)))
        return Counter(found)

    @property
    def widest(self) -> QuoteRead:
        return self.reads[AGES[-1]]


@dataclass(frozen=True)
class Measurement:
    instruments: tuple[InstrumentId, ...]
    days: tuple[Day, ...]
    manifest: str
    normal_status: str | None


def measure(
    bucket: Bucket,
    mirror: Path,
    instruments: Sequence[InstrumentId],
    first: date,
    last: date,
    *,
    normal_status: str | None,
    clock: Clock,
    kinds: QuoteKinds | None = None,
) -> Measurement:
    """Read every session from `first` to `last` at each age, or a `Halt`."""
    if not instruments or len(set(instruments)) != len(instruments):
        raise Halt("--instruments names no instrument, or one twice")
    table = load_sessions()
    days = [d for d in sorted(table.sessions) if first <= d <= last]
    if not days:
        raise Halt(f"no session falls from {first} to {last}")
    tracked = load_quote_kinds() if kinds is None else kinds
    stand_in = None
    if normal_status is not None:
        kind = QuoteKind(_STAND_IN, _STAND_IN, _STAND_IN, _STAND_IN, normal_status, _STAND_IN)
        stand_in = QuoteKinds(
            (QuoteKindRow(days[0], QUOTE_SOURCE, days[0], days[-1], kind, _STAND_IN),)
        )
    held = Mirror(bucket, mirror)
    root = held.open()
    master = load_master()
    out = []
    try:
        for day in days:
            session = table.session(day)
            covered = tracked.kind(day) != QUOTE_NOT_STATED
            if not covered and stand_in is None:
                raise Halt(
                    f"no row of quote_kinds.csv covers {day}, so give --normal-status for a "
                    "stand-in"
                )
            _check(held, root, day, instruments, master)
            decision = to_utc(day, DECISION, table)
            reads = {
                age: read_sealed(
                    held,
                    root,
                    session,
                    decision,
                    {i: QuoteLimits(age, SPREADS[-1]) for i in instruments},
                    master=master,
                    kinds=tracked if covered else stand_in,  # type: ignore[arg-type]
                    clock=clock,
                    report=lambda: None,
                )
                for age in AGES
            }
            moments = (decision, decision + SEND, to_utc(day, CUTOFF, table))
            out.append(Day(session, decision, moments, reads, not covered, tuple(instruments)))
    finally:
        root.remove()
    return Measurement(tuple(instruments), tuple(out), root.manifest.sha256, normal_status)


def _check(
    held: Mirror,
    root: LakeRoot,
    day: date,
    instruments: Sequence[InstrumentId],
    master: SecurityMaster,
) -> None:
    """Halt on a session a decision would not read sealed, and on a missing or damaged partition."""
    if not is_listed(root, day, instruments, master):
        raise Halt(
            f"the manifest lists no sealed quotes of {', '.join(instruments)} for {day}, so a "
            "decision that day would not take the sealed read"
        )
    keys = [
        partition_key(tickers[0], day)
        for i in instruments
        if len(tickers := master.tickers(i, day)) == 1
    ]
    try:
        held.partitions(root, keys)
    except Halt as halt:
        raise Halt(f"on {day}, {halt.reason}") from None


# What it prints


def render(m: Measurement) -> str:
    """Every table, as Markdown, each labelled exploratory."""
    n = len(m.days)
    names = " and ".join(m.instruments)
    span = f"{m.days[0].session.day} to {m.days[-1].session.day}"
    took = sum(d.stand_in for d in m.days)
    lines = [
        f"## Quote refusals of {names}, {span}, exploratory",
        "",
        f"Every figure here is exploratory. {n} sessions, each read at close−20 through the "
        f"sealed read, at the steps posted on #133 before any run, {PLAN}. The manifest's "
        f"SHA-256 is `{m.manifest}`.",
        "",
        f"{n} sessions is a short record. A step that refuses none of them still allows a true "
        f"share as high as {_bound(n)}, the one-sided 95% bound 1 − 0.05^(1/n) for sessions "
        "drawn independently. Sessions in a row are not independent, so the true bound is "
        "likely wider.",
        "",
        _kinds_line(m, took, n),
        "",
        "### Refusals no step lifts",
        "",
        f"At the widest steps, {AGES[-1]} seconds and {SPREADS[-1]} of the midpoint, at the "
        f"decision, {_count(m, AGES[-1], SPREADS[-1], 0)} sessions are refused. What "
        "refuses at every step comes next, and the age and spread tables after it show only "
        "the reasons each step lifts. A reason a quote gives counts once for each instrument "
        "and session it refuses, and each kind of fault of the read once for each session.",
        "",
        *_floor(m),
        "",
        "### Refusals by age",
        "",
        f"No send has been measured, so the send {SEND.seconds} seconds after the decision is a "
        "stand-in. On age alone, a quote refused at a send t seconds after the decision under "
        "a maximum a is refused at the decision under a − t, so the decision's column reads "
        "for any send. The cutoff at close−12 is design.md's proposal for the hard caps' "
        "market-on-close cutoff. An order of another type, or one on a simulated account, can "
        "meet the gate up to the close, where the gate refuses a quote past its age.",
        "",
        "In each cell, the sessions refused for age include any that another reason "
        "refuses as well, and the same holds for the spread.",
        "",
        f"**Table:** Sessions refused at each age, with the spread at its widest step of "
        f"{SPREADS[-1]}, then those refused for age, with the reasons of age by instrument and "
        "session, or by session for a fault of the read, exploratory",
        "",
        "| Age, seconds | " + " | ".join(f"Refused at {w}" for w in MOMENTS) + " |",
        "| --- |" + " --- |" * len(MOMENTS),
    ]
    for age in AGES:
        cells = [_cell(m, age, SPREADS[-1], k, (OLD, UNSHARED), "age") for k in range(len(MOMENTS))]
        lines.append(f"| {age} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "### Refusals by spread",
        "",
        f"**Table:** Sessions refused at each spread, at the decision, with the age at its "
        f"widest step of {AGES[-1]} seconds, then those refused for the spread, with the "
        "reasons by instrument and session, exploratory",
        "",
        "| Spread, share of the midpoint | Refused at the decision |",
        "| --- | --- |",
    ]
    for spread in SPREADS:
        lines.append(f"| {spread} | {_cell(m, AGES[-1], spread, 0, (WIDE,), 'the spread')} |")
    lines += ["", *_quotes(m), "", *_ages(m), "", *_read_faults(m)]
    return "\n".join(lines) + "\n"


def _reasons(m: Measurement, age: int, spread: Decimal, moment: int) -> Counter[tuple[str, str]]:
    found: Counter[tuple[str, str]] = Counter()
    for d in m.days:
        found.update(d.refused(age, spread, moment))
    return found


def _count(m: Measurement, age: int, spread: Decimal, moment: int) -> str:
    refused = sum(bool(d.refused(age, spread, moment)) for d in m.days)
    return f"{refused} of {len(m.days)}"


def _grouped(found: Counter[tuple[str, str]]) -> list[tuple[str, str]]:
    """Each class, with its count by instrument, as `GLD 3, USO 1`, or its count alone."""
    by: dict[str, list[str]] = {}
    for (reason, instrument), c in sorted(found.items()):
        by.setdefault(reason, []).append(f"{instrument} {c}" if instrument else str(c))
    return [(reason, ", ".join(counts)) for reason, counts in by.items()]


def _cell(
    m: Measurement, age: int, spread: Decimal, moment: int, lifted: Sequence[str], what: str
) -> str:
    """Sessions refused, then those refused for a reason the step lifts, and those reasons."""
    said = _count(m, age, spread, moment)
    shown: Counter[tuple[str, str]] = Counter()
    sessions = 0
    for d in m.days:
        mine = Counter({k: c for k, c in d.refused(age, spread, moment).items() if k[0] in lifted})
        sessions += bool(mine)
        shown.update(mine)
    if shown:
        said += f", {sessions} for {what}: " + "; ".join(f"{r}, {c}" for r, c in _grouped(shown))
    return said


def _bound(n: int) -> str:
    """The share n sessions with none refused still allow at 95%, rounded up to a tenth."""
    return f"{math.ceil((1 - 0.05 ** (1 / n)) * 1000) / 10:.1f}%"


def _floor(m: Measurement) -> list[str]:
    """The table of what refuses a session at every step, or a line saying nothing does."""
    rows = _grouped(_reasons(m, AGES[-1], SPREADS[-1], 0))
    if not rows:
        return ["No reason refuses a session at every step."]
    return [
        "**Table:** What refuses a session at every step, exploratory",
        "",
        "| Reason | Count |",
        "| --- | --- |",
        *(f"| {r} | {c} |" for r, c in rows),
    ]


def _kinds_line(m: Measurement, took: int, n: int) -> str:
    if not took:
        return "The tracked row of quote_kinds.csv covers every session."
    return (
        f"The quote kind was a stand-in on {took} of {n} sessions. It states the normal status "
        f"{m.normal_status!r}, and a placeholder for the price kind, the last sale's kind, the "
        "venues and the volume, so no session it covers is refused for an unstated kind."
        + ("" if took == n else " The tracked row of quote_kinds.csv covers the rest.")
    )


def _quotes(m: Measurement) -> list[str]:
    """Stamps after the capture, sides with no size, and what Schwab wrote."""
    quotes = [(d, q) for d in m.days for q in d.widest.quotes.values()]
    later = past = zero = none = unsized = unsized_passed = 0
    status: Counter[str] = Counter()
    realtime: Counter[str] = Counter()
    for d, q in quotes:
        gaps = [s - q.captured_at for s in (q.bid_at, q.ask_at) if s is not None]
        later += any(g > timedelta(0) for g in gaps)
        judged = replace(q, max_age=AGES[-1], max_spread_share=SPREADS[-1])
        reasons = {quote_reason(f) for f in quote_faults(judged, d.decision, d.decision)}
        past += AFTER in reasons
        sizes = (q.received.get("bid_size"), q.received.get("ask_size"))
        zero += "0" in sizes
        none += None in sizes
        if "0" in sizes or None in sizes:
            unsized += 1
            # A read with a fault gives no quote, so only the quote's own reasons count.
            unsized_passed += not reasons
        status[repr(q.status)] += 1
        realtime[repr(q.realtime)] += 1
    total = len(quotes)
    return [
        "### What the quotes read hold",
        "",
        f"Counted over the {total} quotes read at the widest age, one per instrument and "
        "session that gave one, and not over every recorded row.",
        "",
        "**Table:** Stamps, sizes and what Schwab wrote, exploratory",
        "",
        "| Count | Quotes |",
        "| --- | --- |",
        f"| A bid or ask stamp later than its capture | {later} of {total} |",
        f"| The same, by more than the clock bound of {CLOCK_BOUND.seconds} second, which the "
        f"check refuses | {past} of {total} |",
        f"| A bid or ask size of zero | {zero} of {total} |",
        f"| A bid or ask size that is missing | {none} of {total} |",
        *(
            [
                f"| Either, passing every check at the widest steps at the decision | "
                f"{unsized_passed} of {unsized} |"
            ]
            if unsized
            else []
        ),
        *(f"| `security_status` of {s} | {c} of {total} |" for s, c in sorted(status.items())),
        *(f"| `realtime` of {s} | {c} of {total} |" for s, c in sorted(realtime.items())),
    ]


def _ages(m: Measurement) -> list[str]:
    lines = [
        "### Ages at the decision",
        "",
        "**Table:** How old each quote and its capture were at close−20, in seconds, from the "
        "read at the widest age, exploratory",
        "",
        "A quote missing a stamp counts in its capture's ages and not in the quote's.",
        "",
        "| Instrument | Quotes | Quote age, least | Median | Most "
        "| Capture age, least | Median | Most |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i in m.instruments:
        quoted, captured = [], []
        for d in m.days:
            q = d.widest.quotes.get(i)
            if q is None:
                continue
            captured.append((d.decision - q.captured_at) // _MICROSECOND)
            if q.bid_at is not None and q.ask_at is not None:
                quoted.append((d.decision - min(q.bid_at, q.ask_at)) // _MICROSECOND)
        lines.append(f"| {i} | {len(captured)} | {_spread_of(quoted)} | {_spread_of(captured)} |")
    return lines


def _spread_of(values: list[int]) -> str:
    if not values:
        return " | ".join(["none"] * 3)
    shown = (min(values), statistics.median(values), max(values))
    return " | ".join(f"{Decimal(v) / 1_000_000:.3f}" for v in shown)


def _read_faults(m: Measurement) -> list[str]:
    by: dict[tuple[str, date], list[int]] = {}
    for d in m.days:
        for age in AGES:
            for f in d.reads[age].faults:
                by.setdefault((read_reason(f), d.session.day), []).append(age)
    lines = ["### Faults of the read", ""]
    if not by:
        return [*lines, "The read gave no fault at any age."]
    lines += [
        "**Table:** Each fault of the read, the session it fell on and the ages it fell at, "
        "exploratory",
        "",
        "| Fault | Session | Ages, seconds |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {f} | {day} | {', '.join(str(a) for a in sorted(set(ages)))} |"
        for (f, day), ages in sorted(by.items())
    ]
    return lines


# The command


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise Halt(message)


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _Parser(prog="quote_refusals", allow_abbrev=False)
    parser.add_argument("--lake", required=True, type=Path)
    parser.add_argument("--instruments", required=True, nargs="+")
    parser.add_argument("--from", dest="first", required=True, type=_day)
    parser.add_argument("--through", required=True, type=_day)
    parser.add_argument("--normal-status")
    return parser.parse_args(argv)


def _day(text: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        raise argparse.ArgumentTypeError(f"{text!r} is no date")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is no date") from None


class _SystemTimer:
    """`core.Timer` on the machine's own clocks."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def after(self, seconds: float) -> object:
        raise NotImplementedError


def main(
    argv: Sequence[str] | None = None,
    *,
    kinds: QuoteKinds | None = None,
    clock: Clock | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    hidden: dict[Path, str] = {_CHECKOUT: "<checkout>"}
    try:
        given = _arguments(argv)
        lake = given.lake.absolute()
        hidden[lake] = "<lake>"
        with tempfile.TemporaryDirectory() as folder:
            hidden[Path(folder)] = "<mirror>"
            measured = measure(
                FolderBucket(lake),
                Path(folder),
                [InstrumentId(t) for t in given.instruments],
                given.first,
                given.through,
                normal_status=given.normal_status,
                clock=clock or WallClock(_SystemTimer(), servers=None),
                kinds=kinds,
            )
        print(render(measured), end="", file=out or sys.stdout)
    except Exception as error:
        said = error.reason if isinstance(error, Halt) else f"{type(error).__name__}: {error}"
        print(f"quote_refusals: {_one_line(said, hidden)}", file=err or sys.stderr)
        return 1
    return 0


_CHECKOUT = Path(__file__).parents[3]
_ABSOLUTE = re.compile(r"(?<![\w.>/])/[^\s'\":,]+")


def _one_line(said: str, hidden: Mapping[Path, str]) -> str:
    """The reason on one line, with each folder the command knows replaced by a word for it."""
    for path in sorted(hidden, key=lambda p: len(str(p)), reverse=True):
        if len(str(path)) > 1:
            said = said.replace(str(path), hidden[path])
    return " ".join(_ABSOLUTE.sub("<path>", said).split())


if __name__ == "__main__":
    sys.exit(main())
