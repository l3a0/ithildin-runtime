"""The loss stop's state as the journal holds it, worked out from four kinds of entry.

docs/architecture/06-runtime.md's `#### What happens when the stop fires` says
why each rule holds. The fold keeps every `stop_fired`, `stop_stored` and
`stop_cleared` entry, and every `verdict` that read the day's loss past its
stop, by its number, and `latched` reads them in order.

1. A past `verdict` belongs to the firing standing when it was journaled, or
   else to the first `stop_fired` after it. Until one comes it stands alone.
2. A `stop_fired` is refused while a firing stands, so at most one stands.
3. A `stop_stored` is refused unless it names the standing firing, which has
   none yet, and a tag no covered firing's blob had.
4. A `stop_cleared` is refused unless it names the standing firing, whose
   `stop_stored` is before it. It covers that firing and its verdicts.

The blob and what this process holds in memory are read by
`execution.latch`, which adds them to what is here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from ithildinruntime.core import Halt

from .entries import Record, StopClearedEntry, StopFiredEntry, StopStoredEntry, VerdictEntry

__all__ = ["LATCH_KINDS", "Firing", "Latched", "is_latch_entry", "latched", "refusal"]

LATCH_KINDS = (StopFiredEntry, StopStoredEntry, StopClearedEntry)


@dataclass(frozen=True)
class Firing:
    """The standing firing: its entry, its `stop_stored` where one came, and its verdicts."""

    seq: int
    entry: StopFiredEntry
    stored: StopStoredEntry | None = None
    verdicts: tuple[int, ...] = ()


@dataclass(frozen=True)
class Latched:
    """What the journal says of the loss stop.

    - `standing` is the firing no clear covers, or `None`.
    - `alone` holds each past verdict that belongs to no firing, by number.
    - `tags` maps each tag a `stop_stored` named to the firing it named, and
      `covered` holds those of the firings a clear covered.
    - `firings` maps each firing's id to its entry, and `entries` holds the
      number of each entry of the three kinds, which move no book.
    """

    standing: Firing | None = None
    alone: tuple[tuple[int, VerdictEntry], ...] = ()
    tags: dict[str, int] = field(default_factory=dict)
    covered: frozenset[str] = frozenset()
    firings: dict[str, int] = field(default_factory=dict)
    entries: frozenset[int] = frozenset()

    @property
    def fired(self) -> bool:
        """Whether the journal alone holds the stop fired."""
        return self.standing is not None or bool(self.alone)


def is_latch_entry(record: object) -> bool:
    """Whether the fold keeps this record for the stop: a stop kind, or a past verdict."""
    if isinstance(record, VerdictEntry):
        return record.verdict.loss.past
    return isinstance(record, LATCH_KINDS)


def refusal(state: Latched, seq: int, record: Record) -> str | None:
    """Why the fold refuses entry `seq`, of one of the stop's kinds, or `None` where it takes it."""
    standing = state.standing
    match record:
        case StopFiredEntry():
            if standing is not None:
                return f"entry {seq} fires the loss stop, and entry {standing.seq} stands already"
            if record.firing in state.firings:
                return (
                    f"entry {seq} fires the loss stop under the id entry "
                    f"{state.firings[record.firing]} took"
                )
        case StopStoredEntry():
            if standing is None or standing.seq != record.fired:
                return f"entry {seq} stores entry {record.fired}, which is no firing that stands"
            if standing.stored is not None:
                return f"entry {seq} stores entry {record.fired}, whose blob is stored already"
            if record.etag in state.covered:
                return f"entry {seq} names a tag a covered firing's blob had"
        case StopClearedEntry():
            if standing is None or standing.seq != record.fired:
                return f"entry {seq} clears entry {record.fired}, which is no firing that stands"
            if standing.stored is None:
                return f"entry {seq} clears entry {record.fired}, whose blob was never stored"
        case _:
            pass
    return None


def latched(kept: Iterable[tuple[int, Record]]) -> Latched:
    """The stop's state after each entry the fold kept for it, oldest first.

    Each was checked by `refusal` when it was folded, so none is refused here.
    """
    state = Latched()
    standing: Firing | None = None
    alone: list[tuple[int, VerdictEntry]] = []
    tags: dict[str, int] = {}
    covered: set[str] = set()
    firings: dict[str, int] = {}
    entries: set[int] = set()
    for seq, record in kept:
        match record:
            case VerdictEntry():
                if standing is not None:
                    standing = replace(standing, verdicts=(*standing.verdicts, seq))
                else:
                    alone.append((seq, record))
            case StopFiredEntry():
                entries.add(seq)
                firings[record.firing] = seq
                standing = Firing(seq, record, verdicts=tuple(n for n, _ in alone))
                alone = []
            case StopStoredEntry():
                entries.add(seq)
                tags[record.etag] = record.fired
                if standing is None:
                    raise Halt(f"entry {seq} stores a firing, and none stands")
                standing = replace(standing, stored=record)
            case StopClearedEntry():
                entries.add(seq)
                if standing is None or standing.stored is None:
                    raise Halt(f"entry {seq} clears a firing, and none stands with its blob")
                covered.add(standing.stored.etag)
                standing = None
            case _:
                raise Halt(f"entry {seq} is kept for the loss stop, and is of no kind it reads")
    return replace(
        state,
        standing=standing,
        alone=tuple(alone),
        tags=tags,
        covered=frozenset(covered),
        firings=firings,
        entries=frozenset(entries),
    )
