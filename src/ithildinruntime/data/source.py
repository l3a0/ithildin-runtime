"""The point-in-time read of daily bars from marketlake's sealed partitions, and of quotes.

`LakeSource` implements `core.DataSource` over the mirror, the live bucket, the
security master, the two kind contracts and `core.sessions`. Every mode runs it,
and the modes differ only in the buckets it reads and the clock the stage
waits on. A snapshot holds only bars whose end and knowable time are at or
before the decision.

How one decision is read, in order.

1. **The lookback.** Each instrument reads the `lookback` sessions the
   calendar holds up to the latest session ended by the decision, from the
   series' first session on where its settings name one. A first session that
   cuts a lookback halts where the lake lists an earlier partition, as
   `_refuse_late` says.
2. **One read per session.** marketlake's `load_bars` returns around a session
   missing inside a range with no error, and a row carries no session. So the
   source calls it once per session, with `start` and `end` both that
   session, and the call names the row's session. A session's answer is kept
   for the run, keyed by the SHA-256 of its partition and of every reader
   file, so a later decision reads it again only once marketlake changes one.
3. **Each call passes `lake_root=`** the decision's own root from the mirror,
   because `lake_root=None` reads a `config.yaml`, **and `adjust="none"`**,
   because the adjusted views refuse an imported row. `Bar.basis` copies the
   row's stored basis.

**Table:** What the source does with each answer from `load_bars`

| Answer | What happens |
| --- | --- |
| One row for the session | The bar, read as below |
| `BarsAbsent` | The session has no bar |
| Two or more rows | The decision halts, naming the session |
| `PartitionQuarantined` | The decision halts, naming the partition |
| Any other `LoadError`, `ConfigError`, a ledger error, `ManifestError` | The decision halts |

A bar's values stay as received. Each price goes through
`core.from_received`, and a price with more places than `Price` declares is
kept and named in `excess_places`. A captured bar's knowable time is its
`fetch_end_ts`, when the response landed, and one with none, or a stamp with no
stated zone, halts. An imported bar's knowable time is its end, as `core.Bar`
says.

Each instrument's status follows from the sessions that hold a knowable bar.

1. **`missing`** when no session of the lookback holds one, or a session older
   than the newest bar holds none. The halt names the session.
2. **`stale`** when the newest bar's session lags the latest session ended by
   more sessions than the written lag allows.
3. **`fresh`** otherwise. No gap is filled.

A bar the read can hold may still halt the decision, once its snapshot is
stored, for one of three reasons that `_bar_halts` finds in the bars alone.

1. Its prices contradict each other.
2. Its price kind is unstated.
3. It is of another series than the bar before it in its own instrument. Two
   series can be on two scales, and every reader of the bars would take the
   step between them for a move of the market. docs/design.md's `#### A
   lookback that crosses a change of series` says why, and what it costs.

A decision inside its session, at or after the open and before the close, reads
a quote of every instrument too, once its bars are read, from the capture
segments `segments` opens. Such a snapshot is provisional, whatever the segments
held. A decision at or after the close reads none. An instrument with no written
maximum age and spread halts before either bucket is called, because a quote
read under none is stale. A fault in the read of the quotes never takes the
snapshot's place. `_quote_halts` gives each one, and each reason a quote cannot
be read, once the snapshot is stored.
"""

from __future__ import annotations

import bisect
import itertools
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from lake.actions import LedgerLineError, LedgerNotUtf8
from lake.config import ConfigError
from lake.loader import BarsAbsent, LoadError, PartitionQuarantined, load_bars
from lake.manifest import ManifestError
from lake.paths import LakePaths, parse_bars_rel

from ithildinruntime.core import (
    DAILY,
    Bar,
    BarStatus,
    Bucket,
    Clock,
    CodecError,
    Halt,
    InstrumentId,
    Session,
    Snapshot,
    SpikeFlag,
    Timer,
    clock_faults,
    exceeds_places,
    from_received,
    load_sessions,
    places,
    require_quote,
    series_of,
    unstated_fields,
)
from ithildinruntime.core.names import is_unstated
from ithildinruntime.core.sessions import SessionTable

from .mirror import MANIFEST, Checked, LakeRoot, Mirror
from .reference import (
    CAPTURE_SOURCE,
    QUOTE_SOURCE,
    PriceKinds,
    QuoteKinds,
    SecurityMaster,
    load_master,
    load_price_kinds,
    load_quote_kinds,
)
from .segments import QuoteLimits, QuoteRead, read_quotes
from .spikes import (
    CONTRADICT,
    MOVE,
    below_low,
    contradictions,
    quote_contradictions,
    quote_flags,
    spike_flags,
)

__all__ = ["DAILY", "DataSettings", "LakeSource", "Reading", "judge"]

_PRICES = ("open", "high", "low", "close")
_PLACES = places(Bar, "close")
_CAPTURED = "captured"
_IMPORTED = "imported"
_LEDGER_ERRORS = (ConfigError, LedgerLineError, LedgerNotUtf8, ManifestError)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataSettings:
    """The data module's settings, which `settings.data_settings` builds from tracked files.

    The data worker's entry point builds them from the file of every strategy
    its table declares. A caller that reads no strategy's file builds them
    directly.

    - `instruments` are the instruments the run's strategies trade.
    - `lookback` is the number of sessions each instrument reads, counted on
      the calendar.
    - `first_sessions` names each series' first session in the lake, before
      which a lookback holds no session rather than reading `missing`. One
      later than a session the lake lists halts a decision whose lookback it
      cuts.
    - `stale_lag` is the written lag, in sessions. At 0, a bar that lags the
      newest session ended by the decision is stale. `None` halts every
      decision, because an absent lag is a missing input. No file can write it.
    - `spike_multiple` is how many standard deviations of the lookback's daily
      returns a close-to-close move may reach before it is flagged.
      `spikes.largest_multiple` measured the largest the Chan vintages reach,
      which tests/test_data_spikes.py pins and docs/design.md's `#### What the
      spike multiple is measured against` states, labelled exploratory.
    - `quotes` maps an instrument to the maximum age and spread its quote may
      be read under. One it leaves out halts each decision inside its session.

    docs/design.md's `## Configuration` states the values the first strategy
    writes.
    """

    instruments: tuple[InstrumentId, ...]
    lookback: int
    first_sessions: Mapping[InstrumentId, date]
    stale_lag: int | None
    spike_multiple: Decimal
    quotes: Mapping[InstrumentId, QuoteLimits]

    def __post_init__(self) -> None:
        if not self.instruments:
            raise ValueError("a run reads at least one instrument")
        if isinstance(self.lookback, bool) or not isinstance(self.lookback, int):
            raise ValueError("the lookback is a whole number of sessions")
        if self.lookback < 1:
            raise ValueError("the lookback holds at least one session")
        if self.stale_lag is not None and (type(self.stale_lag) is not int or self.stale_lag < 0):
            raise ValueError("the stale lag is a whole number of sessions, 0 or more")


@dataclass(frozen=True)
class Reading:
    """One decision's snapshot, and why it halts once stored, if it is the capture that counts.

    The rest is the load of the read and how far capture lagged it.
    `quote_seconds` and `cycle_age` are `None` where the decision read no quote,
    and the second one where the read found no capture its instruments share. It is
    the age of the newest such capture, so one instrument that lags shows in it.
    """

    snapshot: Snapshot
    halts: tuple[str, ...]
    manifest_bytes: int
    download_seconds: float
    segments_listed: int
    segments_opened: int
    quote_seconds: float | None
    cycle_age: timedelta | None


@dataclass
class _Answers:
    """Each session's answer from `load_bars`, kept for the run, keyed by file hashes."""

    rows: dict[tuple[Any, ...], dict[str, Any] | None] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class LakeSource:
    """`core.DataSource` over marketlake's sealed daily bars, read through a mirror, and its quotes.

    `live` is the bucket the capture segments of a day not yet sealed are in.
    """

    def __init__(
        self,
        mirror: Mirror,
        settings: DataSettings,
        *,
        live: Bucket,
        clock: Clock,
        timer: Timer,
        measured: bool,
        sessions: SessionTable | None = None,
        master: SecurityMaster | None = None,
        kinds: PriceKinds | None = None,
        quote_kinds: QuoteKinds | None = None,
    ) -> None:
        self._mirror = mirror
        self._live = live
        self._settings = settings
        self._clock = clock
        self._timer = timer
        self._measured = measured
        self._sessions = sessions or load_sessions()
        self._days = sorted(self._sessions.sessions)
        self._master = master or load_master()
        self._kinds = kinds or load_price_kinds()
        self._quote_kinds = quote_kinds or load_quote_kinds()
        self._answers = _Answers()

    @property
    def instruments(self) -> tuple[InstrumentId, ...]:
        """The instruments every snapshot of this source must hold."""
        return tuple(self._settings.instruments)

    @property
    def measured(self) -> bool:
        """Whether this run measures its clock, so a reading of nothing is a fault."""
        return self._measured

    def snapshot(self, decision_at: datetime) -> Snapshot:
        """The port's read, which halts on any reason a stored snapshot would halt on.

        `core.DataSource` returns a snapshot alone, so a caller of this method
        never sees `Reading.halts`. A missing, stale or unstated input, a bar
        whose prices contradict each other, two bars of one instrument that
        are of two series, or a quote that cannot be read, therefore raises here
        rather than come back as a snapshot that reads as usable.
        Data's stage calls `read`, which stores the snapshot before it halts.
        """
        reading = self.read(decision_at)
        if reading.halts:
            raise Halt("; ".join(reading.halts))
        return reading.snapshot

    def read(self, decision_at: datetime, report: Callable[[], None] = lambda: None) -> Reading:
        """Read one decision's snapshot, raising `Halt` where no snapshot can be stored.

        `report` is called after each call the read of the quotes makes.
        """
        settings = self._settings
        if settings.stale_lag is None:
            raise Halt("no stale lag is written, and an absent lag is a missing input")
        latest = self._latest_ended(decision_at)
        session = _session_read(self._sessions, decision_at)
        if session is not None:
            _require_limits(settings)
        plans = {i: self._plan(i, latest) for i in settings.instruments}
        start = self._timer.monotonic()
        root = self._mirror.open()
        try:
            self._refuse_late(root, latest)
            self._mirror.partitions(root, (key for plan in plans.values() for _, _, key in plan))
            seconds = self._timer.monotonic() - start
            log.info(
                "data read the manifest of %d bytes, and its downloads took %.3f seconds",
                root.manifest_bytes,
                seconds,
            )
            read_at = self._clock.now()
            bars: dict[InstrumentId, dict[str, tuple[Bar, ...]]] = {}
            status: dict[InstrumentId, BarStatus] = {}
            flags: dict[InstrumentId, tuple[SpikeFlag, ...]] = {}
            halts: list[str] = []
            for instrument, plan in plans.items():
                held = self._bars(root, instrument, plan, decision_at, read_at)
                state, reason = self._status(instrument, plan, held, latest)
                status[instrument] = state
                if reason:
                    halts.append(reason)
                if held:
                    bars[instrument] = {DAILY: tuple(held)}
                    found = spike_flags(held, settings.spike_multiple)
                    if found:
                        flags[instrument] = found
                    halts.extend(_bar_halts(instrument, held))
            ledgers = {MANIFEST: root.manifest.sha256}
            ledgers.update({k: c.sha256 for k, c in root.files.items() if not _is_bars(k)})
            quoted, quote_seconds = None, None
            if session is not None:
                quoted, quote_seconds = self._quotes(root, session, decision_at, report)
                for instrument, quote in quoted.quotes.items():
                    held = bars.get(instrument, {}).get(DAILY, ())
                    found = quote_flags(quote, session.day, held, settings.spike_multiple)
                    if found:
                        flags[instrument] = flags.get(instrument, ()) + found
            # After the last answer the read waited on, so it judges the clock the read ended on.
            clock = self._clock.reading()
            snapshot = Snapshot(
                decision_at=decision_at,
                bars=bars,
                quotes=quoted.quotes if quoted else {},
                status=status,
                provisional=quoted is not None,
                flags=flags,
                segments=quoted.segments if quoted else (),
                ledgers=dict(sorted(ledgers.items())),
                rows=quoted.rows if quoted else {},
                faults=quoted.faults if quoted else (),
                quotes_read_at=quoted.read_at if quoted else None,
                clock=clock,
            )
            halts.extend(_quote_halts(snapshot, settings.instruments, self._sessions))
            return Reading(
                snapshot,
                (*clock_faults(clock, measured=self._measured), *halts),
                root.manifest_bytes,
                seconds,
                segments_listed=quoted.listed if quoted else 0,
                segments_opened=len(snapshot.segments),
                quote_seconds=quote_seconds,
                cycle_age=quoted.cycle_age if quoted else None,
            )
        finally:
            root.remove()

    def _quotes(
        self,
        root: LakeRoot,
        session: Session,
        decision_at: datetime,
        report: Callable[[], None],
    ) -> tuple[QuoteRead, float]:
        start = self._timer.monotonic()
        quoted = read_quotes(
            self._live,
            root.path,
            session,
            decision_at,
            {i: self._settings.quotes[i] for i in self._settings.instruments},
            master=self._master,
            kinds=self._quote_kinds,
            clock=self._clock,
            report=report,
        )
        seconds = self._timer.monotonic() - start
        age = quoted.cycle_age
        log.info(
            "data opened %d of %d segments listed for the decision at %s, in %.3f seconds, and the "
            "newest capture its instruments share was %s",
            len(quoted.segments),
            quoted.listed,
            decision_at.isoformat(),
            seconds,
            "not found" if age is None else f"{age.total_seconds():.3f} seconds old",
        )
        return quoted, seconds

    # The calendar

    def _latest_ended(self, decision_at: datetime) -> date:
        if decision_at.tzinfo is None or decision_at.utcoffset() != UTC.utcoffset(None):
            raise ValueError(f"a decision time is UTC, not {decision_at!r}")
        day = decision_at.date()
        if not self._sessions.first <= day <= self._sessions.last:
            raise Halt(
                f"the decision at {decision_at.isoformat()} is outside the session table's "
                f"range, {self._sessions.first.isoformat()} to {self._sessions.last.isoformat()}"
            )
        closes = [self._sessions.sessions[d].close for d in self._days]
        i = bisect.bisect_right(closes, decision_at)
        if i == 0:
            raise Halt(f"no session in the table ended by {decision_at.isoformat()}")
        return self._days[i - 1]

    def _plan(self, instrument: InstrumentId, latest: date) -> list[tuple[date, str, str]]:
        """Each lookback session's day, ticker and partition key, oldest first."""
        days = self._lookback(latest)
        first = self._settings.first_sessions.get(instrument)
        if first is not None:
            days = [d for d in days if d >= first]
        plan = []
        for day in days:
            ticker = self._master.ticker(instrument, day)
            plan.append((day, ticker, _partition_key(ticker, day)))
        return plan

    def _lookback(self, latest: date) -> list[date]:
        """The sessions a lookback ending at `latest` counts, before a first session cuts it."""
        end = bisect.bisect_right(self._days, latest)
        return self._days[max(0, end - self._settings.lookback) : end]

    def _refuse_late(self, root: LakeRoot, latest: date) -> None:
        """Halt on a first session that cuts a lookback where the lake lists an earlier partition.

        A first session says where the lake's series of an instrument starts.
        Written later than that, it would drop a session the lake lost from the
        lookback, and the bars after the hole would read `fresh`. Only the
        manifest shows it, so this raises before any snapshot is stored, where
        a redelivery halts as the first delivery did, and names the manifest it
        read. The manifest is read rather than the calendar, which starts later
        than some series do.
        """
        oldest = self._lookback(latest)[0]
        firsts = {
            i: first
            for i in self._settings.instruments
            if (first := self._settings.first_sessions.get(i)) is not None and first > oldest
        }
        if not firsts:
            return
        listed = [r for r in map(parse_bars_rel, root.entries) if r and r.freq == DAILY]
        for instrument, first in firsts.items():
            earlier = [
                r
                for r in listed
                if r.day < first and r.ticker in self._master.tickers(instrument, r.day)
            ]
            if earlier:
                start = min(earlier, key=lambda r: r.day)
                raise Halt(
                    f"{instrument}'s first session is written as {first.isoformat()}, which cuts "
                    "the decision's lookback, and the lake lists "
                    f"{_partition_key(start.ticker, start.day)} before it. "
                    + _read_from(root.manifest)
                )

    # The read

    def _bars(
        self,
        root: LakeRoot,
        instrument: InstrumentId,
        plan: Sequence[tuple[date, str, str]],
        decision_at: datetime,
        read_at: datetime,
    ) -> list[Bar]:
        held: list[Bar] = []
        for day, ticker, key in plan:
            try:
                row = self._answer(root, ticker, day, key)
                if row is None:
                    continue
                bar = self._bar(instrument, day, key, root, row, read_at)
            except Halt as halt:
                checked = root.files.get(key)
                if checked is None:
                    raise
                # No snapshot is stored for this decision, so the halt names the
                # file it read.
                raise Halt(f"{halt}. {_read_from(checked)}") from None
            if bar.end <= decision_at and bar.knowable_at <= decision_at:
                held.append(bar)
        return held

    def _answer(self, root: LakeRoot, ticker: str, day: date, key: str) -> dict[str, Any] | None:
        partition = root.files.get(key)
        readers = tuple(sorted((k, c.sha256) for k, c in root.files.items() if not _is_bars(k)))
        cache_key = (ticker, day, partition.sha256 if partition else None, readers)
        with self._answers.lock:
            if cache_key in self._answers.rows:
                return self._answers.rows[cache_key]
        try:
            table = load_bars(ticker, DAILY, start=day, end=day, adjust="none", lake_root=root.path)
        except BarsAbsent:
            row = None
        except PartitionQuarantined as error:
            raise Halt(f"{key} is quarantined in marketlake: {error}") from None
        except (LoadError, *_LEDGER_ERRORS) as error:
            raise Halt(
                f"marketlake's reader refused {ticker} on {day.isoformat()}: "
                f"{type(error).__name__}: {error}"
            ) from None
        else:
            rows = table.to_pylist()
            if len(rows) != 1:
                raise Halt(
                    f"{ticker} holds {len(rows)} daily rows for the session of {day.isoformat()}"
                )
            row = rows[0]
        with self._answers.lock:
            self._answers.rows[cache_key] = row
        return row

    def _bar(
        self,
        instrument: InstrumentId,
        day: date,
        key: str,
        root: LakeRoot,
        row: Mapping[str, Any],
        read_at: datetime,
    ) -> Bar:
        where = f"{row.get('ticker')} on {day.isoformat()}"
        end = self._sessions.session(day).close
        origin = row.get("origin")
        fetched, fetched_at = _stamp(row.get("fetch_ts"), where, "fetch_ts")
        fetched_end, fetched_end_at = _stamp(row.get("fetch_end_ts"), where, "fetch_end_ts")
        if origin == _CAPTURED:
            if fetched_end_at is None:
                raise Halt(f"{where} was captured with no fetch_end_ts, so it has no knowable time")
            knowable_at = fetched_end_at
            kind_source = CAPTURE_SOURCE
        elif origin == _IMPORTED:
            knowable_at = end
            kind_source = str(row.get("source"))
        else:
            raise Halt(f"{where} has origin {origin!r}, neither captured nor imported")
        prices: dict[str, Decimal] = {}
        for name in _PRICES:
            value = row.get(name)
            if value is None:
                raise Halt(f"{where} has no {name}")
            try:
                prices[name] = from_received(value)
            except CodecError as error:
                raise Halt(f"{where} has a {name} {value!r} that is no price: {error}") from None
        volume = row.get("volume")
        if volume is not None and (isinstance(volume, bool) or not isinstance(volume, int)):
            raise Halt(f"{where} has a volume {volume!r} that is no whole number")
        # marketlake's reader refuses a source_as_of that is no strict date.
        source_as_of = row.get("source_as_of")
        as_of = date.fromisoformat(source_as_of) if source_as_of else None
        kind = self._kinds.kind(kind_source, day)
        checked = root.files[key]
        return Bar(
            instrument=instrument,
            session=day,
            **prices,
            volume=volume,
            basis=str(row.get("basis")),
            origin=origin,
            vintage_id=f"{key}#sha256={checked.sha256}#version={checked.version}",
            end=end,
            interval=DAILY,
            first_seen=read_at,
            fetched=fetched,
            fetched_at=fetched_at,
            fetched_end=fetched_end,
            fetched_end_at=fetched_end_at,
            knowable_at=knowable_at,
            source=None if origin == _CAPTURED else row.get("source"),
            source_as_of=as_of,
            price_kind=kind.price_kind,
            venues=kind.venues,
            delay=kind.delay,
            volume_counted=kind.volume_counted,
            excess_places=tuple(n for n in _PRICES if exceeds_places(prices[n], _PLACES)),
        )

    def _status(
        self,
        instrument: InstrumentId,
        plan: Sequence[tuple[date, str, str]],
        held: Sequence[Bar],
        latest: date,
    ) -> tuple[BarStatus, str | None]:
        if not held:
            if not plan:
                where = f"no lookback session on or after its first session by {latest}"
            else:
                where = f"no bar from {plan[0][0].isoformat()} to {plan[-1][0].isoformat()}"
            return BarStatus.MISSING, f"{instrument} missing for the decision: {where}"
        have = {bar.session for bar in held}
        newest = held[-1].session
        for day, _, _ in plan:
            if day < newest and day not in have:
                return (
                    BarStatus.MISSING,
                    f"{instrument} missing for the decision: no bar for {day.isoformat()}",
                )
        lag = sum(1 for d in self._days if newest < d <= latest)
        allowed = self._settings.stale_lag
        if allowed is not None and lag > allowed:
            return (
                BarStatus.STALE,
                f"{instrument} stale for the decision: its newest bar is for "
                f"{newest.isoformat()}, {lag} sessions behind {latest.isoformat()}, "
                f"past the written lag of {allowed}",
            )
        return BarStatus.FRESH, None


def judge(
    snapshot: Snapshot, instruments: Sequence[InstrumentId], *, measured: bool
) -> tuple[str, ...]:
    """Every reason a stored snapshot halts its decision, read from the snapshot alone.

    Data's stage uses it on the capture that counts when a later read differs,
    so the decision halts on what it will trade from. The stage judges only a
    capture this release wrote under the same strategy configuration hashes,
    and halts on any other, so `judge` repeats this release's read under the
    settings those hashes name, data's own among them. These each halt:

    1. each time server's fault in the stored reading of the clock, through the
       same `core.clock_faults`, first;
    2. an instrument the settings read and the snapshot does not hold;
    3. an instrument that is not fresh, or is fresh with no bar;
    4. each bar `LakeSource.read` halts on, through the same `_bar_halts`: one
       whose prices contradict each other, one of unstated kind, and one of
       another series than the bar before it in its own instrument;
    5. each stored flag that is not the move rule's and that this release's
       rules do not find in the bars and the quotes. Within one release they
       raise every flag stored beside them, so such a flag means the record is
       not what this release wrote. Its reason states the record as stored
       rather than guess what it meant. A flag the bars raise and the
       record lacks adds no halt of its own;
    6. each reason `_quote_halts` gives, which is what the first delivery judges
       its quotes by.

    A stored status keeps no session, so its reason names the stored snapshot
    rather than the session, and it is trusted as recorded.
    """
    reasons = list(clock_faults(snapshot.clock, measured=measured))
    for instrument in instruments:
        if instrument not in snapshot.status:
            reasons.append(f"{instrument} is not in the stored snapshot")
    for instrument, state in snapshot.status.items():
        if state is not BarStatus.FRESH:
            reasons.append(f"{instrument} {state} in the stored snapshot")
        elif not snapshot.bars.get(instrument, {}).get(DAILY):
            reasons.append(f"{instrument} is fresh and has no bar in the stored snapshot")
    found: set[tuple[object, ...]] = set()
    for instrument, by_interval in snapshot.bars.items():
        bars = by_interval.get(DAILY, ())
        reasons.extend(_bar_halts(instrument, bars))
        found.update(_key(instrument, f) for bar in bars for f in _recomputed(bar))
    sessions = load_sessions()
    session = _session_read(sessions, snapshot.decision_at)
    if session is not None:
        for instrument, quote in snapshot.quotes.items():
            found.update(_key(instrument, f) for f in quote_contradictions(quote, session.day))
    for instrument, flags in snapshot.flags.items():
        for flag in flags:
            if flag.rule != MOVE and _key(instrument, flag) not in found:
                reasons.append(
                    f"{instrument}'s stored flag for {flag.session.isoformat()} halts as "
                    f"recorded: its {flag.field} of {flag.price} against {flag.against}, "
                    f"by the rule {flag.rule!r}"
                )
    reasons.extend(_quote_halts(snapshot, instruments, sessions))
    return tuple(reasons)


def _session_read(sessions: SessionTable, decision_at: datetime) -> Session | None:
    """The session a decision reads quotes of: the one it is inside, or none at or past its close.

    One at the open reads quotes and always halts, since nothing is captured by then.
    """
    session = sessions.opened_by(decision_at)
    return session if decision_at < session.close else None


def _require_limits(settings: DataSettings) -> None:
    """Halt on an instrument with no written maximum, before either bucket is called."""
    if bare := [i for i in settings.instruments if i not in settings.quotes]:
        raise Halt(
            f"{', '.join(bare)} has no max_age_seconds and max_spread_share written under "
            "[data.quotes], and a quote read under no maximum is stale"
        )


def _quote_halts(
    snapshot: Snapshot, instruments: Sequence[InstrumentId], sessions: SessionTable
) -> list[str]:
    """Why a snapshot's quotes halt its decision, on the first delivery and on a redelivery.

    Each fault of the read comes first, then each instrument whose quote is
    absent or cannot be read at the decision time, then where a kind the quotes
    need is unstated, which the judge names by instrument alone.
    """
    reasons = list(snapshot.faults)
    session = _session_read(sessions, snapshot.decision_at)
    if session is None:
        return reasons
    for instrument in instruments:
        try:
            require_quote(snapshot, instrument, snapshot.decision_at)
        except Halt as halt:
            reasons.append(halt.reason)
    if any(
        is_unstated(quote.price_kind) or is_unstated(quote.normal_status)
        for quote in snapshot.quotes.values()
    ):
        reasons.append(
            f"data/quote_kinds.csv states no price kind or no normal status of {QUOTE_SOURCE} on "
            f"{session.day.isoformat()}"
        )
    return reasons


def _recomputed(bar: Bar) -> list[SpikeFlag]:
    """The flags one bar raises by itself, which is every rule's but the move rule's."""
    return contradictions(bar) + below_low(bar)


def _key(instrument: InstrumentId, flag: SpikeFlag) -> tuple[object, ...]:
    return (instrument, flag.session, flag.field, flag.rule, flag.price, flag.against)


def _bar_halts(instrument: InstrumentId, bars: Sequence[Bar]) -> list[str]:
    """The halts one instrument's bars raise, in three kinds.

    Each contradiction comes first, then any unstated kind, then each change of
    series.
    The contradictions come in the order `spike_flags` gives them, by session
    and then field, keeping the order the checks found them in a tie.
    """
    found = sorted(
        (flag for bar in bars for flag in contradictions(bar)),
        key=lambda f: (f.session, f.field),
    )
    return (
        _contradicted(instrument, found) + _unstated(instrument, bars) + _changed(instrument, bars)
    )


def _contradicted(instrument: InstrumentId, found: Sequence[SpikeFlag]) -> list[str]:
    """A halt for each flag of the contradiction rule, which no real trade produces.

    A flag of the move rule or the below-the-low rule halts nothing, and
    monitoring alerts on it.
    """
    return [
        f"{instrument}'s bar for {f.session.isoformat()} has prices that contradict each "
        f"other: {_why(f)}"
        for f in found
        if f.rule == CONTRADICT
    ]


def _why(flag: SpikeFlag) -> str:
    if flag.price <= 0 and flag.against == 0:
        return f"its {flag.field} of {flag.price} is at or below zero"
    return (
        f"its high of {flag.price} is below the largest of its open, close and low, {flag.against}"
    )


def _unstated(instrument: InstrumentId, bars: Sequence[Bar]) -> list[str]:
    for bar in bars:
        if silent := unstated_fields(bar):
            source = bar.source or CAPTURE_SOURCE
            return [
                f"{instrument}'s bar for {bar.session.isoformat()} has unstated "
                f"{', '.join(silent)}, from {source}, which data/price_kinds.csv states nothing for"
            ]
    return []


def _changed(instrument: InstrumentId, bars: Sequence[Bar]) -> list[str]:
    """A halt for each two consecutive bars of two series, which can be on two scales."""
    return [
        f"{instrument}'s bars for {one.session.isoformat()} and {two.session.isoformat()} are of "
        f"two series, {series_of(one)} and {series_of(two)}"
        for one, two in itertools.pairwise(bars)
        if series_of(one) != series_of(two)
    ]


def _stamp(value: object, where: str, name: str) -> tuple[str | None, datetime | None]:
    """A fetch stamp as received, beside its UTC instant. A stamp with no zone halts."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        raise Halt(f"{where} has a {name} {value!r} that is no text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise Halt(f"{where} has a {name} {value!r} that is no time") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Halt(f"{where} has a {name} {value!r} with no zone, so it is not usable")
    return value, parsed.astimezone(UTC)


def _read_from(checked: Checked) -> str:
    """The file a halt read, as the bucket versions it.

    marketlake's bucket keeps every version of a partition, and a replaced
    manifest for 30 days.
    """
    return f"Read from {checked.key} at SHA-256 {checked.sha256}, bucket version {checked.version}"


def _partition_key(ticker: str, day: date) -> str:
    root = Path("/lake")
    path = LakePaths(root).bars_partition_path(ticker, DAILY, day)
    return path.relative_to(root).as_posix()


def _is_bars(key: str) -> bool:
    return key.startswith("bars/")
