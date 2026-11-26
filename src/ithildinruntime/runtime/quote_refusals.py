"""The command that counts how often recorded quotes would be refused, at a step of maxima.

    python -m ithildinruntime.runtime.quote_refusals --lake FOLDER --instruments GLD USO \
        --from DATE --through DATE [--normal-status STATUS]

A decision before the close halts until its strategy writes a maximum quote age
and a widest spread. This prints what each choice would have refused on the
sealed sessions named, so the owner sets the two from a count rather than a
guess. #133 holds the steps, written there before the first run. Every figure
it prints is exploratory, and it prints no price and no spread's value.

It reads a folder that holds a copy of marketlake's lake, through
`adapters.FolderBucket`, so it needs no credential. How it reads, in order.

1. **Each session is read once for each age**, at close−20, through
   `data.sealed.read_sealed`, the read a decision takes on a sealed day. The
   read cuts 15 seconds before the decision, and its walk stops a leg by the
   age, so a small age can refuse with no shared capture where a larger one
   finds an old quote. A spread changes no read.
2. **Each read is judged by `core.quote_faults`** at every spread, and at three
   moments: the decision, a send `SEND` after it, and the market-on-close
   cutoff at close−12. No send has been measured, so `SEND` stands in for one.
   On age alone, a quote refused at a send t seconds after the decision under a
   maximum a is refused at the decision under a − t.
3. **The kind of each quote** comes from `data/quote_kinds.csv` where a row
   covers the session. Where none does, a stand-in row states the normal status
   given to `--normal-status`, and what it prints says which sessions took it.

On any refusal it writes one line to stderr, nothing to stdout, and exits
nonzero. docs/what-a-person-runs.md says when to run it.
"""

from __future__ import annotations

import argparse
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
from ithildinruntime.data import Mirror, QuoteLimits, load_master, load_quote_kinds
from ithildinruntime.data.cycles import QuoteRead
from ithildinruntime.data.reference import (
    QUOTE_NOT_STATED,
    QUOTE_SOURCE,
    QuoteKind,
    QuoteKindRow,
    QuoteKinds,
)
from ithildinruntime.data.sealed import read_sealed

__all__ = ["AGES", "CUTOFF", "DECISION", "SEND", "SPREADS", "Measurement", "main", "measure"]

# The steps #133 wrote before the first run. A change is a new plan there first.
DECISION = BeforeClose(20)
CUTOFF = BeforeClose(12)
SEND = timedelta(seconds=30)
AGES = (10, 15, 30, 45, 60, 75, 90, 105, 120, 150, 180, 240, 300, 600, 900)
SPREADS = tuple(
    Decimal(s)
    for s in ("0.00002", "0.00005", "0.0001", "0.0002", "0.0005", "0.001", "0.002", "0.005", "0.01")
)
MOMENTS = ("the decision", f"a send {SEND.seconds} seconds after", "the cutoff at close−12")

_STAND_IN = "a stand-in of the measurement's, which states only the normal status"

# The reasons each step can lift. Every other reason refuses at every step.
OLD = "too old"
UNSHARED = "no capture every instrument shares"
WIDE = "too wide"

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
        "a stamp after its capture",
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

    def refused(self, age: int, spread: Decimal, moment: int) -> Counter[tuple[str, str]]:
        """Each reason the decision is refused, by its class and instrument, or none.

        A fault of the read names no instrument, and stands beside an empty one.
        """
        read = self.reads[age]
        found: Counter[tuple[str, str]] = Counter((read_reason(f), "") for f in read.faults)
        for instrument, quote in sorted(read.quotes.items()):
            judged = replace(quote, max_age=age, max_spread_share=spread)
            for fault in quote_faults(judged, self.decision, self.moments[moment]):
                found[(quote_reason(fault), str(instrument))] += 1
        return found

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
        kind = QuoteKind(_STAND_IN, _STAND_IN, _STAND_IN, _STAND_IN, normal_status)
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
            out.append(Day(session, decision, moments, reads, not covered))
    finally:
        root.remove()
    return Measurement(tuple(instruments), tuple(out), root.manifest.sha256, normal_status)


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
        f"sealed read. The manifest's SHA-256 is `{m.manifest}`.",
        "",
        f"The quote kind was a stand-in on {took} of {n} sessions, stating the normal status "
        f"{m.normal_status!r}, and the tracked row of quote_kinds.csv on the rest.",
        "",
        "### Refusals no step lifts",
        "",
        f"At the widest steps, {AGES[-1]} seconds and {SPREADS[-1]} of the midpoint, at the "
        f"decision, {_count(m, AGES[-1], SPREADS[-1], 0)} sessions are refused. These "
        "reasons refuse at every step, so the tables after this one show only the reasons "
        "each step lifts.",
        "",
        "**Table:** What refuses a session at every step, exploratory",
        "",
        "| Reason | Count |",
        "| --- | --- |",
        *(f"| {r} | {c} |" for r, c in _grouped(_reasons(m, AGES[-1], SPREADS[-1], 0))),
        "",
        "### Refusals by age",
        "",
        f"**Table:** Sessions refused at each age, with the spread at its widest step of "
        f"{SPREADS[-1]}, and the reasons of age, exploratory",
        "",
        "| Age, seconds | " + " | ".join(f"Refused at {w}" for w in MOMENTS) + " |",
        "| --- |" + " --- |" * len(MOMENTS),
    ]
    for age in AGES:
        cells = [_cell(m, age, SPREADS[-1], k, (OLD, UNSHARED)) for k in range(len(MOMENTS))]
        lines.append(f"| {age} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "### Refusals by spread",
        "",
        f"**Table:** Sessions refused at each spread, at the decision, with the age at its "
        f"widest step of {AGES[-1]} seconds, and the reasons of the spread, exploratory",
        "",
        "| Spread, share of the midpoint | Refused at the decision |",
        "| --- | --- |",
    ]
    for spread in SPREADS:
        lines.append(f"| {spread} | {_cell(m, AGES[-1], spread, 0, (WIDE,))} |")
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


def _cell(m: Measurement, age: int, spread: Decimal, moment: int, lifted: Sequence[str]) -> str:
    said = _count(m, age, spread, moment)
    shown = Counter({k: c for k, c in _reasons(m, age, spread, moment).items() if k[0] in lifted})
    if shown:
        said += ": " + "; ".join(f"{r}, {c}" for r, c in _grouped(shown))
    return said


def _quotes(m: Measurement) -> list[str]:
    """Stamps after the capture, sides of size zero, and what Schwab wrote."""
    quotes = [(d, q) for d in m.days for q in d.widest.quotes.values()]
    later = past = zero = zero_passed = 0
    status: Counter[str] = Counter()
    realtime: Counter[str] = Counter()
    for d, q in quotes:
        gaps = [s - q.captured_at for s in (q.bid_at, q.ask_at) if s is not None]
        later += any(g > timedelta(0) for g in gaps)
        past += any(g > CLOCK_BOUND for g in gaps)
        if "0" in (q.received.get("bid_size"), q.received.get("ask_size")):
            zero += 1
            judged = replace(q, max_age=AGES[-1], max_spread_share=SPREADS[-1])
            zero_passed += not d.widest.faults and not quote_faults(judged, d.decision, d.decision)
        status[repr(q.status)] += 1
        realtime[repr(q.realtime)] += 1
    total = len(quotes)
    return [
        "### What the quotes read hold",
        "",
        f"Counted over the {total} quotes read at the widest age, one per instrument and "
        "session that gave one.",
        "",
        "**Table:** Stamps, sizes and what Schwab wrote, exploratory",
        "",
        "| Count | Quotes |",
        "| --- | --- |",
        f"| A bid or ask stamp later than its capture | {later} of {total} |",
        f"| The same, by more than the clock bound of {CLOCK_BOUND.seconds} second, which is "
        f"refused | {past} of {total} |",
        f"| A bid or ask size of zero | {zero} of {total} |",
        f"| The same, passing every check at the widest steps | {zero_passed} of {zero} |",
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
