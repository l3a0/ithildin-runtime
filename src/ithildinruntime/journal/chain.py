"""The journal: one numbered, hash-chained log per broker account, read and added to.

docs/design.md's `### Rule 8: the journal is the one numbered log` gives
the rules, and docs/architecture/08-crosscutting-concepts.md's `#### How a
journal entry is hashed and checked` says how an entry is hashed. In short:

- An entry is one canonical blob at `journal/<account>/<seq>.json`, created
  once and never replaced, and an account's first entry is number 1.
- Its `prev_hash` is the SHA-256 of the stored bytes of the entry before it,
  and sixty-four zeros on the first entry. `JournalEntry.hash()` is the one
  function that computes it, and it equals the SHA-256 of the stored bytes
  only because `core.decode` refuses bytes that are not canonical.
- S3's refusal of an existing key keeps two writers from taking one number,
  and `record`, the one way an entry is added, never renumbers one itself. It
  adds the entry after the view it was handed or raises `NumberTaken`, and the
  refused writer views the journal again and decides again.
- A reader halts on bytes that are not canonical, a key whose number differs
  from the `seq` inside it, a `format` it does not know, a link that does not
  chain and a gap in the numbers.

What an entry's body holds is in `entries.py`, the moves of an order in
`orders.py`, and the book the entries replay to in `book.py`.
"""

from __future__ import annotations

import json
import re
import threading
from collections import deque
from collections.abc import Iterator, Mapping
from datetime import datetime

from ithildinruntime.core import (
    BlobMissing,
    BlobStore,
    Book,
    CodecError,
    Halt,
    JournalAck,
    JournalEntry,
    KeyExists,
    OrderRecord,
    decode,
    encode,
    journal_entry_key,
)

from .book import Step, View, after, empty
from .entries import Record, body_of, is_writer
from .orders import DOUBTFUL

__all__ = [
    "FIRST_PREV_HASH",
    "FIRST_SEQ",
    "FORMATS",
    "JOURNAL_FORMAT",
    "Journal",
    "NumberTaken",
]

# The format this release writes, and every format it reads. A format ever
# written stays in FORMATS for good, because the book is replayed from the
# first entry, and tests/fixtures/journal holds a committed journal for each.
JOURNAL_FORMAT = 1
FORMATS = frozenset({1})

FIRST_SEQ = 1
FIRST_PREV_HASH = "0" * 64

_ENTRY_NAME = re.compile(r"[0-9]{12}\.json")
_CHECKPOINTS = "checkpoints/"


class NumberTaken(Exception):
    """Another writer, or another call of this one, already holds this number.

    The refused writer decides its step again on what it reads, as design.md's
    `### Rule 8: the journal is the one numbered log` says. Resending the
    refused entry under another number would record a step decided on a book
    that has since moved.
    """

    def __init__(self, account: str, seq: int) -> None:
        super().__init__(f"journal {account}: entry {seq} is already taken")
        self.account = account
        self.seq = seq


class Journal:
    """One account's journal, read and added to through a `BlobStore`.

    Execution builds the one `OrderPath` holds and monitoring the one
    `Monitoring` holds, each from the store and the account handed to it. No
    code outside execution, monitoring, the supervisor and the `ithildin` tool
    may import this package, which tests/test_import_rule.py holds.

    `view` reads the journal and `record` adds an entry on a view, and no
    writer in any worker adds one another way. An entry that halts a replay
    ends the account, because no entry can be deleted, so `record` applies each
    entry to its view in memory and stores nothing the fold would refuse.

    The `Journal` remembers the newest entry it has checked or stored, and the
    fold through it. A memory behind the store costs a refused create and a
    read forward, never a wrong entry, because the store's create-only rule
    decides who holds each number. The memory is trusted for the link to the
    entry before, so a store that lost entries after this `Journal` checked
    them is not caught here. The next full read halts on such a gap, and a lost
    newest entry is caught only by the bucket's refusal of deletes, its Object
    Lock, its alarm on a delete and the head comparison that is #154's.

    One `Journal` may be shared between threads. Of two that record on the same
    view, one is acknowledged and the other gets `NumberTaken`, even where both
    built the same bytes.
    """

    def __init__(self, store: BlobStore, account: str, writer: str | None = None) -> None:
        """`writer` is unique to each `Journal` that records, and a reader needs none.

        It is 32 lower case hex characters, drawn afresh at each process start,
        that name no host, path, user or account. Two that share one build the
        same bytes under one clock, and both hold the entry's acknowledgement.
        """
        self._store = store
        self._account = account
        self._writer = writer
        # The prefix every entry key of this account starts with, built by the
        # one key builder so the two cannot disagree.
        self._prefix = journal_entry_key(account, FIRST_SEQ).rpartition("/")[0] + "/"
        self._head: JournalAck | None = None
        # The newest view, and the numbers `record` has acknowledged. One
        # number is acknowledged once, so two calls never both hold it.
        self._view = empty(account, self, FIRST_PREV_HASH)
        self._acknowledged: set[int] = set()
        self._lock = threading.Lock()
        # Held across the storage reads a view needs, so `_lock`, which an
        # append takes, never is.
        self._reading = threading.Lock()

    def head(self) -> JournalAck | None:
        """The newest entry's number and hash, or `None` for a journal with no entry.

        It reads every entry after the newest one this `Journal` has checked,
        with all of `read`'s checks.
        """
        known = self._known()
        for _ in self.read(after=known.seq if known else 0):
            pass
        return self._known()

    def view(self, through: int | None = None) -> View:
        """One consistent reading of the journal, through its newest entry.

        It reads forward from what this `Journal` has folded, to the end,
        before it returns, so nothing is acted on ahead of a later entry's
        check. An entry that halts stays unfolded and halts the next view too.
        A call with nothing new costs one listing.

        `through` gives the reading at an earlier entry, folded from the first
        with nothing read past it, which is how a verdict is run again. It
        halts past the end.
        """
        if through is not None:
            return self._view_at(through)
        with self._reading:
            with self._lock:
                found = self._view
            for entry, digest in self._read_on(found.through):
                found = self._keep(after(found, entry), digest)
            return found

    def replay(self) -> Book:
        """The book through the newest entry."""
        return self.view().book

    def in_doubt(self) -> tuple[OrderRecord, ...]:
        """Each order that may or may not be with the broker, in the order they were planned."""
        return tuple(o for o in self.view().live.values() if o.state in DOUBTFUL)

    def record(self, view: View, record: Record, at: datetime) -> tuple[JournalAck, View]:
        """Add one record on `view`, and return its acknowledgement and the view after it.

        The entry is the one after `view` and no other, so a step is stored
        only on the reading it was decided on. `at` is the writer's own clock
        read. It halts, storing nothing, where the fold would refuse the entry,
        and where this `Journal` has no writer or did not give the view.
        Another writer's entry at that number, or another call's, raises
        `NumberTaken`, for the caller to view again and decide again. A store
        that does not answer raises `StoreUnavailable`, and the same call is
        then safe to repeat, because it builds the same bytes. Only an append
        the store acknowledged moves the fold.
        """
        if not is_writer(self._writer):
            raise Halt(f"journal {self._account}: its writer is {self._writer!r}, no writer")
        if not isinstance(view, View) or view._owner is not self:
            raise Halt(f"journal {self._account}: the view is not one this journal gave")
        try:
            entry = self._build(body_of(record, self._writer), at, view.through)
        except CodecError as error:
            raise Halt(
                f"journal {self._account}: entry {view.through.seq + 1} cannot be written: {error}"
            ) from None
        step = after(view, entry)
        ack = self._append(entry)
        with self._lock:
            if ack.seq in self._acknowledged:
                raise NumberTaken(self._account, ack.seq)
            self._acknowledged.add(ack.seq)
        return ack, self._keep(step, ack.entry_hash)

    def _view_at(self, through: int) -> View:
        if isinstance(through, bool) or not isinstance(through, int) or through < 0:
            raise ValueError(f"through {through!r} is not an entry number or 0")
        found = empty(self._account, self, FIRST_PREV_HASH)
        if through == 0:
            return found
        for entry, digest in self._read_on(found.through):
            found = after(found, entry).keep(digest)
            if entry.seq == through:
                return found
        raise Halt(f"journal {self._account}: entry {through} is missing")

    def _keep(self, step: Step, entry_hash: str) -> View:
        """The view after a stored entry, which becomes the newest where it is."""
        made = step.keep(entry_hash)
        with self._lock:
            if made.through.seq > self._view.through.seq:
                self._view = made
        return made

    def _build(
        self, body: Mapping[str, object], built_at: datetime, on: JournalAck | None = None
    ) -> JournalEntry:
        """The next entry for `body`, chained to the entry `on`.

        With no `on` it is the newest entry this `Journal` knows, which reads
        the journal only when it knows no entry yet. The entry is built once
        and resent unchanged on a retry, so a live writer recognises its own
        earlier attempt by its bytes. The codec refuses a body that is not
        plain JSON values, here rather than at the write.
        """
        head = on or self._known() or self.head()
        entry = JournalEntry(
            seq=head.seq + 1 if head else FIRST_SEQ,
            format=JOURNAL_FORMAT,
            built_at=built_at,
            body=body,
            prev_hash=head.entry_hash if head else FIRST_PREV_HASH,
        )
        encode(entry)
        return entry

    def _append(self, entry: JournalEntry) -> JournalAck:
        """Store `entry` at its number's key, and return its number and hash.

        It halts, storing nothing, on an entry that does not chain to the entry
        before it, as stored or as this `Journal` remembers it, since a
        create-only entry with a wrong link breaks the chain for good. An
        existing key holding these same bytes is this writer's own earlier
        attempt whose answer was lost, so the append is done. One holding other
        bytes raises `NumberTaken`, once this `Journal` has read forward past
        it, so its next entry takes the next free number. A key refused as
        existing and then missing halts, because the bucket refuses deletes. A
        store that does not answer raises `StoreUnavailable`, and resending the
        same entry is then safe.
        """
        if entry.format not in FORMATS:
            raise Halt(
                f"journal {self._account}: entry {entry.seq} has format {entry.format}, "
                f"which this release does not read"
            )
        self._check_link(entry)
        data = encode(entry)
        digest = entry.hash()
        key = journal_entry_key(self._account, entry.seq)
        try:
            self._store.create(key, data, digest)
        except KeyExists:
            try:
                stored = self._store.get(key)
            except BlobMissing:
                raise Halt(
                    f"journal {self._account}: entry {entry.seq} was refused as existing "
                    f"and then missing"
                ) from None
            if stored != data:
                self.head()
                raise NumberTaken(self._account, entry.seq) from None
        ack = JournalAck(seq=entry.seq, entry_hash=digest)
        self._advance(ack)
        return ack

    def read(self, after: int = 0) -> Iterator[JournalEntry]:
        """Every entry numbered above `after`, in order, each checked before it is yielded.

        With no `after` the walk starts at the first entry, which is the full
        check. A later `after` checks the first entry's link against the
        stored bytes of entry `after`, so the chain is checked from there on.
        It halts on bytes that are not canonical, a key whose number differs
        from the `seq` inside it, a format it does not know, a link that does
        not chain, a gap in the numbers and a key that is not an entry's. A
        number the listing left out is a gap where its own key holds nothing.
        Checkpoints, which #154 writes under the same prefix, are skipped.
        """
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError(f"after {after!r} is not an entry number or 0")
        for entry, _ in self._read_on(JournalAck(after, self._hash_of(after))):
            yield entry

    def _read_on(self, on: JournalAck) -> Iterator[tuple[JournalEntry, str]]:
        """Every entry after `on` with its hash. `on`'s is trusted as the first one's link."""
        seq, expected = on.seq, on.entry_hash
        start = journal_entry_key(self._account, seq) if seq else None
        keys = deque(self._store.list(self._prefix, start))
        while keys:
            key = keys.popleft()
            if not key.startswith(self._prefix):
                raise Halt(f"journal {self._account}: the listing returned {key}")
            name = key[len(self._prefix) :]
            if name.startswith(_CHECKPOINTS):
                continue
            if not _ENTRY_NAME.fullmatch(name):
                raise Halt(f"journal {self._account}: {key} is not an entry's key")
            number = int(name[:12])
            if number <= seq:
                raise Halt(f"journal {self._account}: the listing returned {key} after entry {seq}")
            if number == seq + 1:
                data = self._get(key)
            else:
                # A folder listed while a writer adds to it can show an entry
                # and miss the one before it, so the missing one is asked for
                # by its own key before its gap halts.
                keys.appendleft(key)
                try:
                    data = self._store.get(journal_entry_key(self._account, seq + 1))
                except BlobMissing:
                    raise Halt(
                        f"journal {self._account}: entry {seq + 1} is missing, "
                        f"and the next key is entry {number}"
                    ) from None
                number = seq + 1
            entry = self._checked(number, data)
            if entry.prev_hash != expected:
                raise Halt(
                    f"journal {self._account}: entry {number}'s prev_hash does not "
                    f"match the SHA-256 of entry {seq}'s stored bytes"
                )
            seq = number
            expected = entry.hash()
            self._advance(JournalAck(seq=seq, entry_hash=expected))
            yield entry, expected

    def _check_link(self, entry: JournalEntry) -> None:
        if isinstance(entry.seq, bool) or not isinstance(entry.seq, int) or entry.seq < FIRST_SEQ:
            raise Halt(f"journal {self._account}: {entry.seq!r} is not an entry number")
        if entry.prev_hash != self._hash_of(entry.seq - 1):
            raise Halt(
                f"journal {self._account}: entry {entry.seq}'s prev_hash does not "
                f"match the SHA-256 of entry {entry.seq - 1}'s stored bytes"
            )

    def _hash_of(self, seq: int) -> str:
        """The hash entry `seq + 1` must carry: entry `seq`'s, or the zeros before the first."""
        if seq == 0:
            return FIRST_PREV_HASH
        known = self._known()
        if known is not None and known.seq == seq:
            return known.entry_hash
        key = journal_entry_key(self._account, seq)
        try:
            data = self._store.get(key)
        except BlobMissing:
            raise Halt(f"journal {self._account}: entry {seq} is missing") from None
        return self._checked(seq, data).hash()

    def _get(self, key: str) -> bytes:
        try:
            return self._store.get(key)
        except BlobMissing:
            raise Halt(f"journal {self._account}: {key} was listed and then missing") from None

    def _checked(self, seq: int, data: bytes) -> JournalEntry:
        """Entry `seq`'s stored bytes, read back with the reader's three checks."""
        # A blob that decodes holds an integer format, so this one check
        # covers every format the decode below could accept.
        written = _written_format(data)
        if written is not None and written not in FORMATS:
            raise Halt(
                f"journal {self._account}: entry {seq} has format {written}, "
                f"which this release does not read"
            )
        try:
            entry = decode(data, JournalEntry)
        except CodecError as error:
            raise Halt(
                f"journal {self._account}: entry {seq} is not a canonical journal entry: {error}"
            ) from None
        if entry.seq != seq:
            raise Halt(f"journal {self._account}: the key of entry {seq} holds seq {entry.seq}")
        return entry

    def _known(self) -> JournalAck | None:
        with self._lock:
            return self._head

    def _advance(self, ack: JournalAck) -> None:
        with self._lock:
            if self._head is None or ack.seq > self._head.seq:
                self._head = ack


def _written_format(data: bytes) -> int | None:
    """The `format` a blob names, read before decoding, so a later format's halt names it.

    A blob in a format this release does not know may hold fields this
    release cannot decode, and its halt should say the format rather than
    that the bytes are not canonical.
    """
    try:
        parsed = json.loads(data)
    except (ValueError, RecursionError):
        return None
    if isinstance(parsed, dict):
        written = parsed.get("format")
        if isinstance(written, int) and not isinstance(written, bool):
            return written
    return None
