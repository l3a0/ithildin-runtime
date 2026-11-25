"""The steps every model's stage takes around its model: read, write and return.

Alpha, cost, risk and portfolio construction each read other stages' results
and the files they list, write files under one header, and return a result.
No worker module imports another, so the steps live here, once.

A handler builds one `StageDecision` per decision, does its whole work inside
one `try`, and turns any `Halt` it meets into its result through `halted`. The
header of that result names each blob the stage had read and storage had
accepted, so a stored halt says what it was judged from. docs/design.md's
`### Rule 7: a halt is passed on` says what a stage does with a halt it reads.

It serves a deterministic stage, and a capturing one that stores a capture before
it judges it and, in a later attempt, goes on from what it stored in the first. A
stage that needs another form extends this class rather than writing beside it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from .codec import CodecError, encode
from .halt import Halt
from .keys import attempt_key, output_key, result_key
from .names import SNAPSHOT_OUTPUT
from .records import BlobHeader, BlobRef, DecisionResult, Snapshot
from .storage import (
    OutputFile,
    StageKind,
    Storage,
    decode_output,
    encode_output,
    sha256_of,
)

__all__ = ["Own", "StageDecision", "StageRun"]


@dataclass(frozen=True)
class Own[T]:
    """One of the stage's own files as it is stored: its key and SHA-256, its header, its entry."""

    ref: BlobRef
    header: BlobHeader
    entry: T


class StageRun:
    """What a stage's every header names, and the storage it reads and writes through.

    `format` is the number of the stage's own files and result, which the
    stage raises when their fields change.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        format: int,
        stage: str,
        kind: StageKind,
        deployment: str,
        run: str,
        release: str,
        config_hashes: Mapping[str, str],
    ) -> None:
        self.storage = storage
        self.format = format
        self.stage = stage
        self.kind = StageKind(kind)
        self.deployment = deployment
        self.run = run
        self.release = release
        self.config_hashes = dict(config_hashes)

    def decision(self, at: datetime, report: Callable[[], None]) -> StageDecision:
        """One decision, which calls `report`, the loop's, after each read and each write."""
        return StageDecision(self, at, report)


class StageDecision:
    """One decision of one stage: the blobs it has read, what it writes and its result.

    A stage reads everything before it writes, because a file's header names
    the blobs read by the time it is written. A read of another blob after the
    first write raises `RuntimeError`, which the loop alarms on and stores as a
    halt. A capture is a read of something outside, so it does not end the
    reads, and neither does a read of the stage's own stored file, which no
    header names.
    """

    def __init__(self, run: StageRun, at: datetime, report: Callable[[], None]) -> None:
        self._run = run
        self._at = at
        self._report = report
        self._read: dict[str, BlobRef] = {}
        self._results: dict[str, DecisionResult] = {}
        # Each halt `result` raised for an upstream halt, with the result that holds it.
        self._passed_on: list[tuple[Halt, BlobRef]] = []
        # Each capture this decision stored, the one that counts first.
        self._captures: tuple[BlobRef, ...] = ()
        self._wrote = False

    def result(
        self, upstream: str, config_hashes: Mapping[str, str] | None = None
    ) -> DecisionResult:
        """The result stage `upstream` stored for this decision.

        It raises `Halt` where none is stored, where storage refuses the read,
        and where the result holds a halt, which `halted` then passes on. Given
        `config_hashes`, the ones the stage table gives `upstream`, it also
        halts on a result that names any other. Storage compares a file only
        with the result that lists it, so a backtest rerun into one folder would
        otherwise read a file that an older configuration wrote.
        """
        key = result_key(self._run.deployment, self._run.run, self._at, upstream)
        if upstream not in self._results:
            found = self._run.storage.read_result(key, self._at)
            if found is None:
                raise Halt(f"{key} is not stored, and stage {self._run.stage} reads it")
            # The codec reads only canonical bytes, so this is the stored blob's SHA-256.
            ref = BlobRef(key, sha256_of(encode(found)))
            self._note(ref)
            if found.halt is not None:
                halt = Halt(f"{key} holds a halt: {found.halt.reason}")
                self._passed_on.append((halt, ref))
                raise halt
            self._results[upstream] = found
        result = self._results[upstream]
        if config_hashes is not None:
            carried = result.header.config_hashes
            names = sorted({*carried, *config_hashes})
            if odd := [n for n in names if carried.get(n) != config_hashes.get(n)]:
                raise Halt(
                    f"{key} does not carry the configuration of {', '.join(odd)} that stage "
                    f"{self._run.stage} runs"
                )
        return result

    def entry[T](self, upstream: str, name: str, kind: type[T]) -> T:
        """The one `kind` in the file `name` that stage `upstream` stored for this decision.

        It raises `Halt` where `entries` does, and where the file holds other
        than one entry.
        """
        counts, found = self._opened(upstream, name, kind)
        if len(found) != 1:
            raise Halt(f"{counts} holds {len(found)} entries, not one")
        return found[0]

    def entries[T](self, upstream: str, name: str, kind: type[T]) -> tuple[T, ...]:
        """Every `kind` in the file `name` that stage `upstream` stored for this decision.

        It raises `Halt` where `upstream`'s result lists that file other than
        once, where storage refuses the read, and where the file holds no entry,
        as one that carries the "trade nothing" mark does.
        """
        counts, found = self._opened(upstream, name, kind)
        if not found:
            raise Halt(f"{counts} holds no entries")
        return found

    def _opened[T](self, upstream: str, name: str, kind: type[T]) -> tuple[str, tuple[T, ...]]:
        """The key that counts of a file, and its entries."""
        result = self.result(upstream)
        run = self._run
        key = result_key(run.deployment, run.run, self._at, upstream)
        # A capturing stage lists the capture that counts under its own key, and
        # any later capture that differed under an attempt key.
        counts = output_key(run.deployment, run.run, self._at, upstream, name)
        listed = [ref for ref in result.outputs if ref.key == counts]
        if len(listed) != 1:
            raise Halt(f"{key} lists {counts} {len(listed)} times, not once")
        file = run.storage.read_output(key, result, listed[0], self._at, kind)
        self._note(listed[0])
        return counts, file.entries

    def snapshot(self, data_stage: str) -> Snapshot:
        """The snapshot `data_stage` stored, which halts where it is of another decision."""
        snapshot = self.entry(data_stage, SNAPSHOT_OUTPUT, Snapshot)
        # Storage checks the decision the file's header names. A reader goes by the
        # snapshot's own, which decides what is stale.
        if snapshot.decision_at != self._at:
            key = output_key(
                self._run.deployment, self._run.run, self._at, data_stage, SNAPSHOT_OUTPUT
            )
            raise Halt(
                f"{key} holds a snapshot of {snapshot.decision_at.isoformat()}, not of "
                f"{self._at.isoformat()}"
            )
        return snapshot

    def blob(self, key: str) -> bytes:
        """A blob with a reader of its own, such as the hard caps, named in every later header."""
        data = self._run.storage.read_blob(key)
        self._note(BlobRef(key, sha256_of(data)))
        return data

    def names(self, ref: BlobRef) -> None:
        """Name in every later header a blob the stage read in an earlier attempt."""
        self._note(ref)

    def capture[T](
        self, name: str, kind: type[T], entry: T, explain: Callable[[T, T], str]
    ) -> Own[T]:
        """Store one capture before anything in it is judged, and give the one that counts.

        The first capture of a decision counts. A later one that differs is
        stored at its attempt number, with an alarm that ends with what
        `explain` says of the first and it. A first capture this stage cannot
        read back, or that another release or configuration wrote, halts. The
        capture that counts is named in every header from here on, and the
        result lists it first, a halt's too.
        """
        run = self._run
        key = self._key(name)
        try:
            data = encode_output(kind, OutputFile(self._header(), (entry,), False))
        except CodecError:
            # Its text quotes a field, and the field came from outside.
            raise Halt(f"{key} was not stored: what was read is no {kind.__name__}") from None

        def described(held: bytes) -> str:
            return self._differ(key, held, kind, entry, explain)

        stored = run.storage.put(
            run.kind,
            key,
            data,
            attempt=lambda n: attempt_key(run.deployment, run.run, self._at, run.stage, name, n),
            explain=described,
        )
        both = (stored.counts, stored.ref)
        self._captures = both[:1] if stored.ref == stored.counts else both
        self._note(stored.counts)
        if stored.ref == stored.counts:
            return Own(stored.counts, self._header(), entry)
        return self._read_back(stored.counts, kind)

    def own[T](self, name: str, kind: type[T]) -> Own[T] | None:
        """A file this stage stored in an earlier attempt at this decision, or `None`.

        It halts on one this stage cannot read, on one of another release,
        format or configuration, and on one that holds other than one entry.
        """
        key = self._key(name)
        data = self._run.storage.find_blob(key)
        self._report()
        return None if data is None else self._checked(key, data, kind)

    def keep[T](self, name: str, kind: type[T], entry: T) -> Own[T]:
        """Store a file of the stage's, or give the one an earlier attempt stored under its name.

        So a second attempt, on this instance or another, goes on from the
        first one's file rather than its own.
        """
        key = self._key(name)
        data = encode_output(kind, OutputFile(self._header(), (entry,), False))
        self._wrote = True
        ref = self._run.storage.keep(key, data)
        self._report()
        if ref.sha256 == sha256_of(data):
            return Own(ref, self._header(), entry)
        return self._read_back(ref, kind)

    def put[T](self, name: str, kind: type[T], entries: tuple[T, ...]) -> BlobRef:
        """Store an output file of the stage's, whose header names each blob read so far."""
        run = self._run
        key = self._key(name)
        data = encode_output(kind, OutputFile(self._header(), entries, False))
        self._wrote = True
        ref = run.storage.put(run.kind, key, data).ref
        self._report()
        return ref

    def done(self, *outputs: BlobRef) -> DecisionResult:
        """The result that lists each capture and then `outputs`, which the loop stores last."""
        listed = (*self._captures, *outputs)
        return DecisionResult(header=self._header(), outputs=listed, halt=None)

    def halted(self, halt: Halt) -> DecisionResult:
        """The result that holds `halt`, and lists each capture the decision stored.

        Its `cause` is the result that holds the upstream halt `result` raised
        as `halt`, and empty for any other halt, which is what monitoring alarms
        on.
        """
        cause = next((ref for passed, ref in self._passed_on if passed is halt), None)
        record = self._run.storage.halt_record(halt, cause=cause)
        return DecisionResult(header=self._header(), outputs=self._captures, halt=record)

    def _key(self, name: str) -> str:
        run = self._run
        return output_key(run.deployment, run.run, self._at, run.stage, name)

    def _read_back[T](self, ref: BlobRef, kind: type[T]) -> Own[T]:
        """The file that stands at a key this attempt's own bytes did not take."""
        data = self._run.storage.read_blob(ref.key)
        self._report()
        if (found := sha256_of(data)) != ref.sha256:
            raise Halt(f"{ref.key} was read back with SHA-256 {found}, not the stored {ref.sha256}")
        return self._checked(ref.key, data, kind)

    def _checked[T](self, key: str, data: bytes, kind: type[T]) -> Own[T]:
        try:
            file = decode_output(data, kind)
        except CodecError:
            raise Halt(f"{key} is no {kind.__name__} file this release reads") from None
        if odd := self._odd(file.header):
            raise Halt(
                f"{key} was stored under another {', '.join(odd)}, and nothing goes on from it"
            )
        if len(file.entries) != 1:
            raise Halt(f"{key} holds {len(file.entries)} entries, not one")
        return Own(BlobRef(key, sha256_of(data)), file.header, file.entries[0])

    def _odd(self, header: BlobHeader) -> list[str]:
        """Each field but the inputs in which a header is not this stage's for this decision."""
        mine = self._header()
        same = ("format", "deployment", "run", "stage", "kind", "decision_at", "release")
        odd = [
            name.replace("_", " ") for name in same if getattr(header, name) != getattr(mine, name)
        ]
        if dict(header.config_hashes) != dict(mine.config_hashes):
            odd.append("configuration")
        return odd

    def _differ[T](
        self, key: str, held: bytes, kind: type[T], entry: T, explain: Callable[[T, T], str]
    ) -> str:
        """What a later capture's alarm says of the first."""
        try:
            first = self._checked(key, held, kind)
        except Halt as halt:
            return halt.reason
        if first.header.inputs != self._header().inputs:
            return f"What the stage had read differs. {explain(first.entry, entry)}"
        return explain(first.entry, entry)

    def _note(self, ref: BlobRef) -> None:
        """Name a blob the stage has read in every header from here on."""
        if self._wrote and ref.key not in self._read:
            raise RuntimeError(f"stage {self._run.stage} read {ref.key} after it had written")
        self._read[ref.key] = ref
        self._report()

    def _header(self) -> BlobHeader:
        run = self._run
        return BlobHeader(
            format=run.format,
            deployment=run.deployment,
            run=run.run,
            stage=run.stage,
            kind=str(run.kind),
            decision_at=self._at,
            inputs={ref.key: ref.sha256 for ref in self._read.values()},
            release=run.release,
            config_hashes=run.config_hashes,
        )
