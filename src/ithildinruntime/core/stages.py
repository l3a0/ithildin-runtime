"""The steps every model's stage takes around its model: read, write and return.

Alpha, cost, risk and portfolio construction each read other stages' results
and the files they list, write files under one header, and return a result.
No worker module imports another, so the steps live here, once.

A handler builds one `StageDecision` per decision, does its whole work inside
one `try`, and turns any `Halt` it meets into its result through `halted`. The
header of that result names each blob the stage had read and storage had
accepted, so a stored halt says what it was judged from. docs/design.md's
`### Rule 7: a halt is passed on` says what a stage does with a halt it reads.

It serves a deterministic stage. A stage that needs another form, such as a
capture stored at an attempt key, extends this class rather than writing beside
it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from .codec import encode
from .halt import Halt
from .keys import output_key, result_key
from .names import SNAPSHOT_OUTPUT
from .records import BlobHeader, BlobRef, DecisionResult, Snapshot
from .storage import OutputFile, StageKind, Storage, encode_output, sha256_of

__all__ = ["StageDecision", "StageRun"]


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
    halt.
    """

    def __init__(self, run: StageRun, at: datetime, report: Callable[[], None]) -> None:
        self._run = run
        self._at = at
        self._report = report
        self._read: dict[str, BlobRef] = {}
        self._results: dict[str, DecisionResult] = {}
        # Each halt `result` raised for an upstream halt, with the result that holds it.
        self._passed_on: list[tuple[Halt, BlobRef]] = []
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

    def put[T](self, name: str, kind: type[T], entries: tuple[T, ...]) -> BlobRef:
        """Store an output file of the stage's, whose header names each blob read so far."""
        run = self._run
        key = output_key(run.deployment, run.run, self._at, run.stage, name)
        data = encode_output(kind, OutputFile(self._header(), entries, False))
        self._wrote = True
        ref = run.storage.put(run.kind, key, data).ref
        self._report()
        return ref

    def done(self, *outputs: BlobRef) -> DecisionResult:
        """The result that lists `outputs`, which the loop stores last."""
        return DecisionResult(header=self._header(), outputs=outputs, halt=None)

    def halted(self, halt: Halt) -> DecisionResult:
        """The result that holds `halt`.

        Its `cause` is the result that holds the upstream halt `result` raised
        as `halt`, and empty for any other halt, which is what monitoring alarms
        on.
        """
        cause = next((ref for passed, ref in self._passed_on if passed is halt), None)
        record = self._run.storage.halt_record(halt, cause=cause)
        return DecisionResult(header=self._header(), outputs=(), halt=record)

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
