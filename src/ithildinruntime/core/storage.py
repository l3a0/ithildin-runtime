"""The storage contract: create-only writes, decision results and the reader's checks.

This module is the one place the storage rules are applied, so no worker
branches on its stage's kind. design.md holds the rules:

- `### Rule 1: every write is a blob, created once` and `### Rule 2: two kinds
  of stage, declared in one place`, for what a refused create means;
- `### Rule 1: a write S3 refuses or does not answer`, for the outage and the
  captures a process holds through it;
- `### Rule 6: an absent, stale or empty input halts`, for the reader's checks.

The refusals below are the answers every `BlobStore` adapter gives, so the
folder and S3 answer a refusal the same way.
"""

from __future__ import annotations

import dataclasses
import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import cache

from .codec import CodecError, decode, encode
from .halt import Halt
from .ports import Alerts, BlobStore, Timer
from .records import BlobHeader, BlobRef, DecisionResult, HaltRecord, RestartRecord, _Record

__all__ = [
    "BlobMissing",
    "ChecksumMismatch",
    "Conflict",
    "KeyExists",
    "OutputFile",
    "Retry",
    "StageKind",
    "StaleTag",
    "StoreError",
    "StoreUnavailable",
    "Storage",
    "Stored",
    "decode_output",
    "encode_output",
    "sha256_of",
]


class StoreError(Exception):
    """A store's answer to a call it did not carry out. `answer` is what the store said."""

    def __init__(self, key: str, answer: str) -> None:
        super().__init__(f"{key}: {answer}")
        self.key = key
        self.answer = answer


class KeyExists(StoreError):
    """A create refused because its key already holds a blob."""


class ChecksumMismatch(StoreError):
    """A write refused because its bytes do not hash to the SHA-256 it sent."""


class StaleTag(StoreError):
    """A rewrite refused because the blob did not carry the tag it named when S3 judged it.

    A client that resends a rewrite whose first try landed meets its own new
    tag, so it reads this too. #10's lease rules hold how a lease reads it.
    """


class BlobMissing(StoreError):
    """A read or a rewrite of a key that holds no blob."""


class StoreUnavailable(StoreError):
    """Any other refusal, or no answer at all. Whether the write landed is unknown."""


class Conflict(StoreUnavailable):
    """S3's 409: a conditional write raced another one, and this one did not land."""


class StageKind(StrEnum):
    """A stage's kind, which the one table in `runtime` declares beside its inputs."""

    DETERMINISTIC = "deterministic"
    CAPTURING = "capturing"


def sha256_of(data: bytes) -> str:
    """The lowercase hex SHA-256 every write sends and every listing names."""
    return hashlib.sha256(data).hexdigest()


def _no_entries_with_the_mark(trade_nothing: bool, entries: tuple) -> None:
    if trade_nothing and entries:
        raise ValueError("an output file that trades nothing holds no entries")


@dataclass(frozen=True)
class OutputFile[T]:
    """An output file: its header, its entries and the "trade nothing" mark.

    On the wire it is one object with two members, `header` and `body`, and
    `body` holds `entries` and `trade_nothing`. The mark means hold every
    position and send no order, never a target of zero, so a file that carries
    it holds no entries. Portfolio construction halts on one, because a target
    portfolio has no form for it.
    """

    header: BlobHeader
    entries: tuple[T, ...]
    trade_nothing: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        _no_entries_with_the_mark(self.trade_nothing, self.entries)


@cache
def _wire(entry: type) -> tuple[type, type]:
    """The two records an output file of `entry` travels as, built once per kind."""

    def check(body: object) -> None:
        _Record.__post_init__(body)  # type: ignore[arg-type]
        _no_entries_with_the_mark(body.trade_nothing, body.entries)  # type: ignore[attr-defined]

    body = dataclasses.make_dataclass(
        f"{entry.__name__}Body",
        [("entries", tuple[entry, ...]), ("trade_nothing", bool)],
        bases=(_Record,),
        frozen=True,
        namespace={"__post_init__": check},
    )
    file = dataclasses.make_dataclass(
        f"{entry.__name__}File",
        [("header", BlobHeader), ("body", body)],
        bases=(_Record,),
        frozen=True,
    )
    return file, body


def encode_output[T](entry: type[T], output: OutputFile[T]) -> bytes:
    """Write an output file whose entries are each an `entry`, through the one codec."""
    file, body = _wire(entry)
    return encode(file(output.header, body(output.entries, output.trade_nothing)))


def decode_output[T](data: bytes, entry: type[T]) -> OutputFile[T]:
    """Read an output file back, refusing bytes the codec would not have written."""
    file, _ = _wire(entry)
    value = decode(data, file)
    return OutputFile(value.header, value.body.entries, value.body.trade_nothing)


@dataclass(frozen=True)
class Retry:
    """How long a call S3 refuses or does not answer is retried before it halts.

    The pause between tries doubles from `pause` up to `longest_pause`, in
    seconds. The deadline is checked after each try, so a halt can come one
    try's worst case after it, which a client's own timeout bounds.
    """

    deadline: timedelta
    pause: float
    longest_pause: float

    def __post_init__(self) -> None:
        if self.deadline <= timedelta(0) or self.pause <= 0 or self.longest_pause < self.pause:
            raise ValueError("a retry needs a positive deadline and pauses that grow")


@dataclass(frozen=True)
class Stored:
    """Where a write's bytes are stored, and the blob at its first key, which counts.

    For a deterministic write, and for a capture whose key was free or held the
    same bytes, the two are the same. For a capture that differed from the one
    already stored, `ref` is its attempt key and `counts` the first capture,
    which the stage derives every remaining output from. The decision result
    names both.
    """

    ref: BlobRef
    counts: BlobRef


@dataclass(eq=False)
class _Held:
    key: str
    data: bytes
    sha256: str
    attempt: Callable[[int], str] | None
    trying: str
    explain: Callable[[bytes], str] | None = None


class _Deadline(Exception):
    def __init__(self, key: str, answer: str) -> None:
        super().__init__(key)
        self.key = key
        self.answer = answer


class _Differs(Exception):
    pass


class _AlarmFailed(Exception):
    """A capture is stored, and the alarm that says so could not be sent."""

    def __init__(self, reason: str, error: BaseException, stored: Stored) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error = error
        self.stored = stored


class Storage:
    """The storage contract every worker writes and reads through.

    `timer` is the time port whose monotonic reading a deadline measures, and
    `pause` waits between tries, so a test fakes both. `alerts` sends the
    alarm for a later capture stored at its attempt key, and for every halt
    raised because S3 would not answer, since no halt blob reaches monitoring
    while S3 is down.
    `owner_step` names the broker's interface, which such a halt gives the
    owner as the step to take. One `Storage` serves every handler of a worker,
    so it keeps each held capture behind a lock.
    """

    def __init__(
        self,
        store: BlobStore,
        *,
        timer: Timer,
        pause: Callable[[float], None],
        retry: Retry,
        alerts: Alerts,
        owner_step: str,
    ) -> None:
        self._store = store
        self._timer = timer
        self._pause = pause
        self._retry = retry
        self._alerts = alerts
        self._owner_step = owner_step
        self._held: list[_Held] = []
        # A held capture one handler is retrying stays in `_held`, so every
        # halt still names it, and sits here so a second handler skips it.
        self._claimed: list[_Held] = []
        self._lock = threading.Lock()

    @property
    def owner_step(self) -> str:
        """The step an outage gives the owner, the broker's interface."""
        return self._owner_step

    # Writing

    def put(
        self,
        kind: StageKind,
        key: str,
        data: bytes,
        *,
        attempt: Callable[[int], str] | None = None,
        explain: Callable[[bytes], str] | None = None,
    ) -> Stored:
        """Create `key` holding `data`, under the rule for the stage's kind.

        A capturing stage's output passes `attempt`, which builds its attempt
        key from a count starting at 2, such as `core.attempt_key` with every
        part but the count given. A capture stored at a key of its own, such as
        the send stage's broker read, passes none, and a different blob already
        at its key halts. A capture that is not stored, for any reason, is held
        and named by every later halt. A deterministic output is not held,
        since its stage can compute it again.

        `explain` says how a later capture differs from the first. It is handed
        the first capture's bytes, and what it returns ends the alarm that a
        later capture is stored at its attempt key. An `explain` that raises
        leaves the alarm saying so.
        """
        kind = StageKind(kind)
        if kind is StageKind.DETERMINISTIC and (attempt is not None or explain is not None):
            raise ValueError("a deterministic output has no attempt keys")
        held = _Held(key, data, sha256_of(data), attempt, key, explain)
        try:
            return self._place(kind, held)
        except _AlarmFailed as failed:
            # The capture is stored, so it is not held, and the halt says so.
            raise self._halt(
                f"{failed.reason}, and that alarm could not be sent: "
                f"{type(failed.error).__name__}: {failed.error}"
            ) from failed.error
        except _Deadline as late:
            self._hold(kind, held)
            raise self._outage(f"S3 did not store {late.key}", late) from None
        except Halt as halt:
            self._hold(kind, held)
            raise self._halt(halt.reason) from None
        except Exception as error:
            if kind is StageKind.DETERMINISTIC:
                raise
            self._hold(kind, held)
            raise self._halt(
                f"{held.trying} was not stored: {type(error).__name__}: {error}"
            ) from error
        except BaseException:
            # Raised on as it is, such as `KeyboardInterrupt`. The capture is
            # held all the same, so the halt its handler ends in names it.
            self._hold(kind, held)
            raise

    def put_result(self, key: str, result: DecisionResult, *, once: bool = False) -> DecisionResult:
        """Create a decision's result, and return the result its key holds.

        A key that already holds another result is not an error here. The
        caller reads what is stored, such as the supervisor's halt, which is
        final for the decision.

        `once` tries the write, and the read of a key that already holds a
        blob, once each with no pause, for a process retrying a halt it holds.
        S3 refusing either or not answering then raises `StoreUnavailable`
        and sends no alarm, since the first try sent one, and so does a key
        refused as existing and then read as missing.
        """
        data = encode(result)
        try:
            self._place(
                StageKind.CAPTURING, _Held(key, data, sha256_of(data), None, key), True, once
            )
        except _Deadline as late:
            if once:
                raise StoreUnavailable(late.key, late.answer) from None
            raise self._outage(f"S3 did not store {late.key}", late) from None
        except _Differs:
            pass
        stored = self._read(key, once)
        if stored is None:
            if once:
                raise StoreUnavailable(key, "refused as existing and then read as missing")
            raise self._halt(f"{key} was refused as existing and then read as missing")
        try:
            return decode(stored, DecisionResult)
        except CodecError as error:
            raise self._halt(f"{key} holds no decision result the codec reads: {error}") from None

    def announce(self, key: str) -> bool:
        """Announce a stored blob again, once, and say whether the store took the call.

        A restarted writer announces its newest result, because it may have
        died between storing it and the folder's delivery. An event is only a
        reason to wake, so a store that refuses the call costs a reader time
        and nothing else, and the writer does not retry it.
        """
        try:
            self._store.announce(key)
        except StoreError:
            return False
        return True

    def unstored(self) -> tuple[BlobRef, ...]:
        """Each capture this process holds that S3 has not stored, by key and SHA-256.

        The key is the one the capture was last trying, an attempt key where
        its first key holds another capture.
        """
        with self._lock:
            return tuple(BlobRef(h.trying, h.sha256) for h in self._held)

    def retry_held(self) -> tuple[Stored, ...]:
        """Try each held capture once more, with no pause, and return the ones now stored.

        A capture stored at its attempt key is returned even when the alarm
        that says so could not be sent.

        A capture S3 still will not take stays held. The process that holds a
        halt calls this while it lives, so each capture is stored once S3
        answers. Each capture is claimed under the lock, so two handlers
        calling this at once store it once.
        """
        stored: list[Stored] = []
        with self._lock:
            waiting = list(self._held)
        for held in waiting:
            with self._lock:
                if held not in self._held or held in self._claimed:
                    continue
                self._claimed.append(held)
            try:
                done = self._place(StageKind.CAPTURING, held, once=True)
            except _AlarmFailed as failed:
                # Stored at its attempt key, and returned, so the caller names
                # it even though the alarm that says so could not be sent.
                with self._lock:
                    self._held.remove(held)
                stored.append(failed.stored)
                continue
            except BaseException:
                # A capture S3 still will not take stays held and named by
                # every halt, whatever the error, so one cannot block the rest.
                continue
            else:
                with self._lock:
                    self._held.remove(held)
                stored.append(done)
            finally:
                with self._lock:
                    self._claimed.remove(held)
        return tuple(stored)

    def halt_record(
        self,
        halt: Halt,
        *,
        cause: BlobRef | None = None,
        restart_records: Sequence[RestartRecord] = (),
        found_outputs: Sequence[BlobRef] = (),
    ) -> HaltRecord:
        """The record a halt becomes at a worker's edge, naming each capture still held.

        A lone surrogate, as in a file name Python could not decode, has no
        UTF-8 form, and the codec refuses a reason that holds one, so it is
        escaped.
        """
        return HaltRecord(
            reason=halt.reason.encode("utf-8", "backslashreplace").decode(),
            cause=cause,
            restart_records=tuple(restart_records),
            found_outputs=tuple(found_outputs),
            unstored=self.unstored(),
        )

    # Reading

    def exists(self, key: str) -> bool:
        """Whether `key` holds a blob, which a reader checks before it starts a decision.

        A store that does not answer by the deadline halts, as every read does,
        so an outage never reads as a missing blob.
        """
        return self._read(key) is not None

    def read_blob(self, key: str) -> bytes:
        """The bytes at `key`, for a blob with a reader of its own, such as the hard caps.

        A missing blob halts, and so does a store that does not answer by the
        deadline, so neither ever reads as a blob that says nothing.
        """
        data = self._read(key)
        if data is None:
            raise self._halt(f"{key} is missing")
        return data

    def read_result(self, key: str, decision_at: datetime) -> DecisionResult | None:
        """The decision result at `key`, or `None` while the stage has written none.

        `None` means not yet. The caller waits for the result and halts at its
        deadline, and never reads `None` as nothing to trade.
        """
        data = self._read(key)
        if data is None:
            return None
        try:
            result = decode(data, DecisionResult)
        except CodecError as error:
            raise self._halt(f"{key} holds no decision result the codec reads: {error}") from None
        self._same_decision(key, result.header, decision_at)
        return result

    def read_output[T](
        self,
        result_key: str,
        result: DecisionResult,
        listed: BlobRef,
        decision_at: datetime,
        entry: type[T],
    ) -> OutputFile[T]:
        """An output file that `result` lists, after the reader's checks.

        A result that holds a halt lists no output to read, so reading one
        halts, and so does a file the result does not list, and one whose
        header names other configuration hashes than the result's, so one
        strategy's version cannot pass from one stage to the next as another's.
        """
        if result.halt is not None:
            raise self._halt(f"{result_key} holds a halt: {result.halt.reason}")
        if listed not in result.outputs:
            raise self._halt(f"{listed.key} is not an output file {result_key} lists")
        data = self._read(listed.key)
        if data is None:
            raise self._halt(f"output file {listed.key}, listed by {result_key}, is missing")
        actual = sha256_of(data)
        if actual != listed.sha256:
            raise self._halt(
                f"output file {listed.key} has SHA-256 {actual}, and {result_key} "
                f"lists {listed.sha256}"
            )
        try:
            output = decode_output(data, entry)
        except CodecError as error:
            raise self._halt(
                f"output file {listed.key} is no {entry.__name__} file: {error}"
            ) from None
        self._same_decision(listed.key, output.header, decision_at)
        if dict(output.header.config_hashes) != dict(result.header.config_hashes):
            raise self._halt(
                f"output file {listed.key} names the configuration hashes "
                f"{_hashes(output.header.config_hashes)}, and {result_key} names "
                f"{_hashes(result.header.config_hashes)}"
            )
        if not output.entries and not output.trade_nothing:
            raise self._halt(
                f"output file {listed.key} for {_when(decision_at)} has no entries "
                'and no "trade nothing" mark'
            )
        return output

    # The rules

    def _place(
        self, kind: StageKind, held: _Held, quiet: bool = False, once: bool = False
    ) -> Stored:
        counts = BlobRef(held.key, held.sha256)
        tried: set[str] = set()
        n = 1
        while True:
            key = held.key if n == 1 else held.attempt(n)  # type: ignore[misc]
            if key in tried:
                raise Halt(f"the attempt key for {held.key} at count {n} repeats {key}")
            tried.add(key)
            held.trying = key
            stored = self._create(key, held.data, held.sha256, once)
            if stored is None or stored == held.sha256:
                if n > 1:
                    # Raised whether this call stored it or found it stored by
                    # a try whose reply was lost, so the alarm is never missed.
                    reason = (
                        f"{held.key} holds a capture with SHA-256 {counts.sha256}, and a "
                        f"later capture with SHA-256 {held.sha256} is stored at {key}"
                        f"{self._explained(held)}"
                    )
                    done = Stored(BlobRef(key, held.sha256), counts)
                    try:
                        self._alerts.alarm(reason)
                    except BaseException as error:
                        raise _AlarmFailed(reason, error, done) from error
                    return done
                return Stored(BlobRef(key, held.sha256), counts)
            if n == 1:
                counts = BlobRef(held.key, stored)
            if kind is StageKind.DETERMINISTIC:
                raise Halt(
                    f"{key} is stored with SHA-256 {stored}, and the step wrote "
                    f"{held.sha256}, so the step is not deterministic"
                )
            if quiet:
                raise _Differs()
            if held.attempt is None:
                raise Halt(
                    f"{key} is stored with SHA-256 {stored}, and a capture with SHA-256 "
                    f"{held.sha256} has no other key to go to"
                )
            n += 1

    def _explained(self, held: _Held) -> str:
        """What `held.explain` says of the difference, as the end of an alarm's reason."""
        if held.explain is None:
            return ""
        try:
            # One try with no retry, so a read that fails sends no outage alarm
            # and does not hold back the alarm this reason belongs to.
            first = self._read(held.key, True)
            if first is None:
                return f". {held.key} read as missing, so the difference is not known"
            return f". {held.explain(first)}"
        except BaseException as error:
            return f". The difference could not be described: {type(error).__name__}: {error}"

    def _create(self, key: str, data: bytes, sha: str, once: bool) -> str | None:
        """Create the key. `None` means stored, and a hash means the key holds that blob."""

        def call() -> str | None:
            try:
                self._store.create(key, data, sha)
            except KeyExists:
                try:
                    return self._store.head(key).ref.sha256
                except BlobMissing as missing:
                    # Refused as existing, then not found: no answer to trust yet.
                    raise StoreUnavailable(
                        key, f"refused as existing, then {missing.answer}"
                    ) from None
            return None

        return self._retrying(key, call, once)

    def _read(self, key: str, once: bool = False) -> bytes | None:
        def call() -> bytes | None:
            try:
                return self._store.get(key)
            except BlobMissing:
                return None

        try:
            return self._retrying(key, call, once)
        except _Deadline as late:
            if once:
                raise StoreUnavailable(late.key, late.answer) from None
            raise self._outage(f"S3 did not answer a read of {late.key}", late) from None

    def _retrying[R](self, key: str, call: Callable[[], R], once: bool) -> R:
        # Elapsed time is read on the monotonic clock, so a wall clock stepped
        # back never stretches the deadline.
        start = self._timer.monotonic()
        deadline = self._retry.deadline.total_seconds()
        pause = self._retry.pause
        while True:
            try:
                return call()
            except (StoreUnavailable, ChecksumMismatch) as error:
                answer = f"{type(error).__name__}: {error.answer}"
            if once or self._timer.monotonic() - start >= deadline:
                raise _Deadline(key, answer)
            self._pause(pause)
            pause = min(pause * 2, self._retry.longest_pause)

    def _hold(self, kind: StageKind, held: _Held) -> None:
        if kind is StageKind.CAPTURING:
            with self._lock:
                self._held.append(held)

    def _halt(self, reason: str) -> Halt:
        """A halt whose reason names each capture still held, when there is one."""
        held = self.unstored()
        if not held:
            return Halt(reason)
        listed = "; ".join(f"{ref.key} with SHA-256 {ref.sha256}" for ref in held)
        return Halt(f"{reason}. Captures not yet stored: {listed}")

    def _outage(self, what: str, late: _Deadline) -> Halt:
        listed = "; ".join(f"{r.key} with SHA-256 {r.sha256}" for r in self.unstored()) or "none"
        halt = Halt(
            f"{what} by the deadline: {late.answer}. Captures not yet stored: {listed}. "
            f"The owner's step: {self._owner_step}"
        )
        try:
            self._alerts.alarm(halt.reason)
        except BaseException as error:
            return Halt(
                f"{halt.reason}. The alarm could not be sent: {type(error).__name__}: {error}"
            )
        return halt

    def _same_decision(self, key: str, header: BlobHeader, decision_at: datetime) -> None:
        if header.decision_at != decision_at:
            raise self._halt(
                f"{key} names the decision at {_when(header.decision_at)}, "
                f"not {_when(decision_at)}, so it is stale"
            )


def _when(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _hashes(hashes: Mapping[str, str]) -> str:
    return "{" + ", ".join(f"{k}: {v}" for k, v in sorted(hashes.items())) + "}"
