"""The storage contract: create-only writes, decision results and the reader's checks.

design.md's `### Rule 1: every write is a blob, created once` and `### Rule 2:
two kinds of stage, declared in one place` give the rules, and this module is
the one place they are applied, so no worker branches on its stage's kind.

1. Every write is a create that S3 refuses when the key exists. A refused
   create is judged by the stored blob's SHA-256. An equal hash means an
   earlier attempt already did this work. A different hash halts a
   deterministic stage. For a capturing stage the stored capture counts, and
   the new one is stored at the next attempt key, stepping past any attempt
   key that holds other bytes, so two writers racing one capture both land.
2. A write S3 refuses for any other reason, or does not answer, is retried
   until a deadline and then halts. The process keeps each capture it could
   not store, retries it while it lives, and every halt names each capture
   still held, as design.md's `### Rule 1: a write S3 refuses or does not
   answer` says.
3. A reader halts on a listed output file that is missing, one whose SHA-256
   differs from the listing, a blob whose header names another decision, and
   an output file with no entries and no "trade nothing" mark, as design.md's
   `### Rule 6: an absent, stale or empty input halts` says.

The refusals below are the answers every `BlobStore` adapter gives, so the
folder and S3 answer a refusal the same way.
"""

from __future__ import annotations

import dataclasses
import hashlib
import threading
from collections.abc import Callable, Sequence
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
    """A rewrite refused because the blob no longer carries the tag it named."""


class BlobMissing(StoreError):
    """A read or a rewrite of a key that holds no blob."""


class StoreUnavailable(StoreError):
    """Any other refusal, or no answer at all. Whether the write landed is unknown."""


class StageKind(StrEnum):
    """A stage's kind, which the one table in `runtime` declares beside its inputs."""

    DETERMINISTIC = "deterministic"
    CAPTURING = "capturing"


def sha256_of(data: bytes) -> str:
    """The lowercase hex SHA-256 every write sends and every listing names."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class OutputFile[T]:
    """An output file: its header, its entries and the "trade nothing" mark.

    On the wire it is one object with two members, `header` and `body`, and
    `body` holds `entries` and `trade_nothing`. The mark means hold every
    position and send no order, never a target of zero, so a file that carries
    it holds no entries.
    """

    header: BlobHeader
    entries: tuple[T, ...]
    trade_nothing: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        if self.trade_nothing and self.entries:
            raise ValueError("an output file that trades nothing holds no entries")


@cache
def _wire(entry: type) -> tuple[type, type]:
    """The two records an output file of `entry` travels as, built once per kind."""

    def check(body: object) -> None:
        _Record.__post_init__(body)  # type: ignore[arg-type]
        if body.trade_nothing and body.entries:  # type: ignore[attr-defined]
            raise ValueError("an output file that trades nothing holds no entries")

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
    """How long a write S3 refuses or does not answer is retried before it halts.

    The pause between tries doubles from `pause` up to `longest_pause`, in
    seconds. A capture steps through at most `attempts` keys, its own first,
    before it halts.
    """

    deadline: timedelta
    pause: float
    longest_pause: float
    attempts: int

    def __post_init__(self) -> None:
        if self.deadline <= timedelta(0) or self.pause <= 0 or self.longest_pause < self.pause:
            raise ValueError("a retry needs a positive deadline and pauses that grow")
        if self.attempts < 2:
            raise ValueError("a capture needs room for at least one attempt key")


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


@dataclass
class _Held:
    key: str
    data: bytes
    sha256: str
    attempt: Callable[[int], str] | None
    trying: str


class _Deadline(Exception):
    def __init__(self, key: str, answer: str) -> None:
        super().__init__(key)
        self.key = key
        self.answer = answer


class Storage:
    """The storage contract every worker writes and reads through.

    `timer` is the wall-time port a deadline reads, and `pause` waits between
    tries, so a test fakes both. `owner_step` names the broker's interface,
    which a halt over a store that will not answer gives the owner as the step
    to take. One `Storage` serves every handler of a worker, so it keeps each
    held capture behind a lock.
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
        self._lock = threading.Lock()

    # Writing

    def put(
        self,
        kind: StageKind,
        key: str,
        data: bytes,
        *,
        attempt: Callable[[int], str] | None = None,
    ) -> Stored:
        """Create `key` holding `data`, under the rule for the stage's kind.

        A capturing stage's output passes `attempt`, which builds its attempt
        key from a count starting at 2, such as `core.attempt_key` with every
        part but the count given. A capture stored at a key of its own, such as
        the send stage's broker read, passes none, and a different blob already
        at its key halts. A capture is held through an outage and a
        deterministic output is not, since its stage can compute it again.
        """
        kind = StageKind(kind)
        if kind is StageKind.DETERMINISTIC and attempt is not None:
            raise ValueError("a deterministic output has no attempt keys")
        sha = sha256_of(data)
        held = _Held(key, data, sha, attempt, key)
        try:
            return self._place(kind, held)
        except _Deadline as late:
            self._hold(kind, held)
            raise self._outage(late) from None
        except Halt as halt:
            if kind is StageKind.DETERMINISTIC:
                raise
            self._hold(kind, held)
            raise Halt(f"{halt.reason}. {self._listing()}") from None

    def _hold(self, kind: StageKind, held: _Held) -> None:
        if kind is StageKind.CAPTURING:
            with self._lock:
                self._held.append(held)

    def _listing(self) -> str:
        held = self.unstored()
        listed = "; ".join(f"{ref.key} with SHA-256 {ref.sha256}" for ref in held) or "none"
        return f"Captures not yet stored: {listed}"

    def put_result(self, key: str, result: DecisionResult) -> DecisionResult:
        """Create a decision's result, and return the result its key holds.

        A key that already holds another result is not an error here. The
        caller reads what is stored, such as the supervisor's halt, which is
        final for the decision.
        """
        data = encode(result)
        try:
            self._place(StageKind.CAPTURING, _Held(key, data, sha256_of(data), None, key), True)
        except _Deadline as late:
            raise self._outage(late) from None
        except _Differs:
            pass
        stored = self._read(key)
        if stored is None:
            raise Halt(f"{key} was refused as existing and then read as missing")
        try:
            return decode(stored, DecisionResult)
        except CodecError as error:
            raise Halt(f"{key} holds no decision result the codec reads: {error}") from None

    def unstored(self) -> tuple[BlobRef, ...]:
        """Each capture this process holds that S3 has not stored, by key and SHA-256."""
        with self._lock:
            return tuple(BlobRef(h.trying, h.sha256) for h in self._held)

    def retry_held(self) -> tuple[Stored, ...]:
        """Try once more to store each held capture, and return the ones now stored.

        A capture S3 still will not take stays held. The process that holds a
        halt calls this while it lives, so each capture is stored once S3
        answers.
        """
        with self._lock:
            waiting = list(self._held)
        stored: list[Stored] = []
        for held in waiting:
            try:
                done = self._place(StageKind.CAPTURING, held, once=True)
            except (_Deadline, Halt):
                continue
            with self._lock:
                self._held.remove(held)
            stored.append(done)
        return tuple(stored)

    def halt_record(
        self,
        halt: Halt,
        *,
        cause: BlobRef | None = None,
        restart_records: Sequence[RestartRecord] = (),
        found_outputs: Sequence[BlobRef] = (),
    ) -> HaltRecord:
        """The record a halt becomes at a worker's edge, naming each capture still held."""
        return HaltRecord(
            reason=halt.reason,
            cause=cause,
            restart_records=tuple(restart_records),
            found_outputs=tuple(found_outputs),
            unstored=self.unstored(),
        )

    # Reading

    def read_result(self, key: str, decision_at: datetime) -> DecisionResult | None:
        """The decision result at `key`, or `None` while the stage has written none."""
        data = self._read(key)
        if data is None:
            return None
        try:
            result = decode(data, DecisionResult)
        except CodecError as error:
            raise Halt(f"{key} holds no decision result the codec reads: {error}") from None
        self._same_decision(key, result.header, decision_at)
        return result

    def read_output[T](
        self, result_key: str, listed: BlobRef, decision_at: datetime, entry: type[T]
    ) -> OutputFile[T]:
        """An output file a decision result lists, after the reader's four checks."""
        data = self._read(listed.key)
        if data is None:
            raise Halt(f"output file {listed.key}, listed by {result_key}, is missing")
        actual = sha256_of(data)
        if actual != listed.sha256:
            raise Halt(
                f"output file {listed.key} has SHA-256 {actual}, and {result_key} "
                f"lists {listed.sha256}"
            )
        try:
            output = decode_output(data, entry)
        except CodecError as error:
            raise Halt(f"output file {listed.key} is no {entry.__name__} file: {error}") from None
        self._same_decision(listed.key, output.header, decision_at)
        if not output.entries and not output.trade_nothing:
            raise Halt(
                f"output file {listed.key} for {_when(decision_at)} has no entries "
                'and no "trade nothing" mark'
            )
        return output

    # The rules

    def _place(
        self, kind: StageKind, held: _Held, quiet: bool = False, once: bool = False
    ) -> Stored:
        counts = BlobRef(held.key, held.sha256)
        n = 1
        while True:
            key = held.key if n == 1 else held.attempt(n)  # type: ignore[misc]
            held.trying = key
            stored = self._create(key, held.data, held.sha256, once)
            if stored is None or stored == held.sha256:
                if n > 1 and stored is None:
                    self._alerts.alarm(
                        f"{held.key} holds a capture with SHA-256 {counts.sha256}, and a "
                        f"later capture with SHA-256 {held.sha256} is stored at {key}"
                    )
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
            if n > self._retry.attempts:
                raise Halt(
                    f"{held.key} has {self._retry.attempts} captures stored that differ, "
                    f"and a capture with SHA-256 {held.sha256} has no attempt key left"
                )

    def _create(self, key: str, data: bytes, sha: str, once: bool) -> str | None:
        """Create the key. `None` means stored, and a hash means the key holds that blob."""

        def call() -> str | None:
            try:
                self._store.create(key, data, sha)
            except KeyExists:
                return self._store.head(key).ref.sha256
            return None

        return self._retrying(key, call, once)

    def _read(self, key: str) -> bytes | None:
        def call() -> bytes | None:
            try:
                return self._store.get(key)
            except BlobMissing:
                return None

        try:
            return self._retrying(key, call, False)
        except _Deadline as late:
            raise Halt(
                f"S3 did not answer a read of {late.key} by the deadline: {late.answer}"
            ) from None

    def _retrying[R](self, key: str, call: Callable[[], R], once: bool) -> R:
        start = self._timer.now()
        pause = self._retry.pause
        while True:
            try:
                return call()
            except (StoreUnavailable, ChecksumMismatch) as error:
                answer = f"{type(error).__name__}: {error.answer}"
            if once or self._timer.now() - start >= self._retry.deadline:
                raise _Deadline(key, answer)
            self._pause(pause)
            pause = min(pause * 2, self._retry.longest_pause)

    def _outage(self, late: _Deadline) -> Halt:
        return Halt(
            f"S3 did not store {late.key} by the deadline: {late.answer}. "
            f"{self._listing()}. The owner's step: {self._owner_step}"
        )

    @staticmethod
    def _same_decision(key: str, header: BlobHeader, decision_at: datetime) -> None:
        if header.decision_at != decision_at:
            raise Halt(
                f"{key} names the decision at {_when(header.decision_at)}, "
                f"not {_when(decision_at)}, so it is stale"
            )


class _Differs(Exception):
    pass


def _when(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")
