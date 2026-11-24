"""The point-in-time read of daily bars from marketlake's sealed partitions.

`LakeSource` implements `core.DataSource` over the mirror, the security
master, the price-kind contract and `core.sessions`. Every mode runs it, and
the modes differ only in the bucket the mirror reads and the clock the stage
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
kept and named in `excess_places`. A captured bar's knowable time is its fetch
stamp, and a stamp with no stated zone halts. An imported bar's knowable time
is its end, as `core.Bar` says.

Each instrument's status follows from the sessions that hold a knowable bar.

1. **`missing`** when no session of the lookback holds one, or a session older
   than the newest bar holds none. The halt names the session.
2. **`stale`** when the newest bar's session lags the latest session ended by
   more sessions than #7's written lag allows.
3. **`fresh`** otherwise. No gap is filled.
"""

from __future__ import annotations

import bisect
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from lake.actions import LedgerLineError, LedgerNotUtf8
from lake.config import ConfigError
from lake.loader import BarsAbsent, LoadError, PartitionQuarantined, load_bars
from lake.manifest import ManifestError
from lake.paths import LakePaths, parse_bars_rel

from ithildinruntime.core import (
    Bar,
    BarStatus,
    Clock,
    CodecError,
    Halt,
    InstrumentId,
    Snapshot,
    SpikeFlag,
    Timer,
    exceeds_places,
    from_received,
    load_sessions,
    places,
)
from ithildinruntime.core.sessions import SessionTable

from .mirror import MANIFEST, Checked, LakeRoot, Mirror
from .reference import (
    CAPTURE_SOURCE,
    PriceKinds,
    SecurityMaster,
    load_master,
    load_price_kinds,
)
from .spikes import CONTRADICT, MOVE, below_low, contradictions, spike_flags

__all__ = ["DAILY", "DataSettings", "LakeSource", "Reading", "judge"]

DAILY = "1d"
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
    - `stale_lag` is #7's written lag, in sessions. `None` halts every
      decision, because an absent lag is a missing input. No file can write it.
    - `spike_multiple` is how many standard deviations of the lookback's daily
      returns a close-to-close move may reach before it is flagged.
      `spikes.largest_multiple` measured the largest the Chan vintages reach,
      which tests/test_data_spikes.py pins and docs/design.md's `#### What the
      spike multiple is measured against` states, labelled exploratory.

    docs/design.md's `## Configuration` states the values the first strategy
    writes.
    """

    instruments: tuple[InstrumentId, ...]
    lookback: int
    first_sessions: Mapping[InstrumentId, date]
    stale_lag: int | None
    spike_multiple: Decimal

    def __post_init__(self) -> None:
        if not self.instruments:
            raise ValueError("a run reads at least one instrument")
        if isinstance(self.lookback, bool) or not isinstance(self.lookback, int):
            raise ValueError("the lookback is a whole number of sessions")
        if self.lookback < 1:
            raise ValueError("the lookback holds at least one session")


@dataclass(frozen=True)
class Reading:
    """One decision's snapshot, and why it halts once stored, if it is the capture that counts."""

    snapshot: Snapshot
    halts: tuple[str, ...]
    manifest_bytes: int
    download_seconds: float


@dataclass
class _Answers:
    """Each session's answer from `load_bars`, kept for the run, keyed by file hashes."""

    rows: dict[tuple[Any, ...], dict[str, Any] | None] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class LakeSource:
    """`core.DataSource` over marketlake's sealed daily bars, read through a mirror."""

    def __init__(
        self,
        mirror: Mirror,
        settings: DataSettings,
        *,
        clock: Clock,
        timer: Timer,
        sessions: SessionTable | None = None,
        master: SecurityMaster | None = None,
        kinds: PriceKinds | None = None,
    ) -> None:
        self._mirror = mirror
        self._settings = settings
        self._clock = clock
        self._timer = timer
        self._sessions = sessions or load_sessions()
        self._days = sorted(self._sessions.sessions)
        self._master = master or load_master()
        self._kinds = kinds or load_price_kinds()
        self._answers = _Answers()

    @property
    def instruments(self) -> tuple[InstrumentId, ...]:
        """The instruments every snapshot of this source must hold."""
        return tuple(self._settings.instruments)

    def snapshot(self, decision_at: datetime) -> Snapshot:
        """The port's read, which halts on any reason a stored snapshot would halt on.

        `core.DataSource` returns a snapshot alone, so a caller of this method
        never sees `Reading.halts`. A missing, stale or unstated input, or a bar
        whose prices contradict each other, therefore raises here rather than
        come back as a snapshot that reads as usable.
        Data's stage calls `read`, which stores the snapshot before it halts.
        """
        reading = self.read(decision_at)
        if reading.halts:
            raise Halt("; ".join(reading.halts))
        return reading.snapshot

    def read(self, decision_at: datetime) -> Reading:
        """Read one decision's snapshot, raising `Halt` where no snapshot can be stored."""
        settings = self._settings
        if settings.stale_lag is None:
            raise Halt("no stale lag is written, and #7's lag is a missing input")
        latest = self._latest_ended(decision_at)
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
            snapshot = Snapshot(
                decision_at=decision_at,
                bars=bars,
                quotes={},
                status=status,
                provisional=False,
                flags=flags,
                segments=(),
                ledgers=dict(sorted(ledgers.items())),
            )
            return Reading(snapshot, tuple(halts), root.manifest_bytes, seconds)
        finally:
            root.remove()

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
            if fetched_at is None:
                raise Halt(f"{where} was captured with no fetch stamp, so it has no knowable time")
            knowable_at = fetched_at
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


def judge(snapshot: Snapshot, instruments: Sequence[InstrumentId]) -> tuple[str, ...]:
    """Every reason a stored snapshot halts its decision, read from the snapshot alone.

    Data's stage uses it on the capture that counts when a later read differs,
    so the decision halts on what it will trade from. The stage judges only a
    capture this release wrote under the same strategy configuration hashes,
    and halts on any other, so `judge` repeats this release's read under the
    settings those hashes name, data's own among them. These each halt:

    1. an instrument the settings read and the snapshot does not hold;
    2. an instrument that is not fresh, or is fresh with no bar;
    3. each bar `LakeSource.read` halts on, through the same `_bar_halts`;
    4. each stored flag that is not the move rule's and that this release's
       rules do not find in the bars. Within one release the bars raise
       every flag stored beside them, so such a flag means the record is not
       what this release wrote. Its reason states the record as stored
       rather than guess what it meant. A flag the bars raise and the
       record lacks adds no halt of its own.

    A stored status keeps no session, so its reason names the stored snapshot
    rather than the session, and it is trusted as recorded.
    """
    reasons: list[str] = []
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
    for instrument, flags in snapshot.flags.items():
        for flag in flags:
            if flag.rule != MOVE and _key(instrument, flag) not in found:
                reasons.append(
                    f"{instrument}'s stored flag for {flag.session.isoformat()} halts as "
                    f"recorded: its {flag.field} of {flag.price} against {flag.against}, "
                    f"by the rule {flag.rule!r}"
                )
    return tuple(reasons)


def _recomputed(bar: Bar) -> list[SpikeFlag]:
    """The flags one bar raises by itself, which is every rule's but the move rule's."""
    return contradictions(bar) + below_low(bar)


def _key(instrument: InstrumentId, flag: SpikeFlag) -> tuple[object, ...]:
    return (instrument, flag.session, flag.field, flag.rule, flag.price, flag.against)


def _bar_halts(instrument: InstrumentId, bars: Sequence[Bar]) -> list[str]:
    """The halts one instrument's bars raise: each contradiction, then any unstated kind.

    The contradictions come in the order `spike_flags` gives them, by session
    and then field, keeping the order the checks found them in a tie.
    """
    found = sorted(
        (flag for bar in bars for flag in contradictions(bar)),
        key=lambda f: (f.session, f.field),
    )
    return _contradicted(instrument, found) + _unstated(instrument, bars)


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
        fields = {
            "price kind": bar.price_kind,
            "venues": bar.venues,
            "delay": bar.delay,
            "volume counting": bar.volume_counted,
        }
        silent = [name for name, value in fields.items() if value == "unstated"]
        if silent:
            source = bar.source or CAPTURE_SOURCE
            return [
                f"{instrument}'s bar for {bar.session.isoformat()} has unstated "
                f"{', '.join(silent)}, from {source}, which data/price_kinds.csv states nothing for"
            ]
    return []


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

    The bucket keeps every version of a partition, and a replaced manifest for
    30 days.
    """
    return f"Read from {checked.key} at SHA-256 {checked.sha256}, bucket version {checked.version}"


def _partition_key(ticker: str, day: date) -> str:
    root = Path("/lake")
    path = LakePaths(root).bars_partition_path(ticker, DAILY, day)
    return path.relative_to(root).as_posix()


def _is_bars(key: str) -> bool:
    return key.startswith("bars/")
