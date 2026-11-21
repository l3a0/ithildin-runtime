"""The journal's writer and reader: one numbered, hash-chained log per broker account.

docs/design.md's paragraph opening "The journal is the one numbered log" gives
the rules, and docs/architecture/08-crosscutting-concepts.md's `#### How a
journal entry is hashed and checked` says how an entry is hashed. In short:

- An entry is one canonical blob at `journal/<account>/<seq>.json`, created
  once and never replaced, and an account's first entry is number 1.
- Its `prev_hash` is the SHA-256 of the stored bytes of the entry before it,
  and sixty-four zeros on the first entry.
- S3's refusal of an existing key keeps two writers from taking one number.
  The refused writer reads the new entries and decides its step again, so
  `append` never renumbers an entry itself.
- A reader halts on bytes that are not canonical, a key whose number differs
  from the `seq` inside it, a `format` it does not know, a link that does not
  chain and a gap in the numbers.

What an entry's body holds, `in_doubt()` and `replay()` belong to #10, which
defines the bodies they read.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterator, Mapping
from datetime import datetime

from ithildinruntime.core import (
    BlobMissing,
    BlobStore,
    CodecError,
    Halt,
    JournalAck,
    JournalEntry,
    KeyExists,
    decode,
    encode,
    journal_entry_key,
)

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
    """Another writer's entry already holds this number.

    The refused writer reads the new entries, checks the kill switch and any
    halt among them, decides its step again against what it has read, and
    builds a new entry. Resending the refused entry under another number would
    record a step decided on a book that has since moved.
    """

    def __init__(self, account: str, seq: int) -> None:
        super().__init__(f"journal {account}: entry {seq} is another writer's")
        self.account = account
        self.seq = seq


class Journal:
    """One account's journal, read and appended through a `BlobStore`.

    Execution and monitoring each build their own from the store and the
    account handed to them, and so do the supervisor and the `ithildin` tool.
    No other code may import this package, which tests/test_import_rule.py
    holds.

    The `Journal` remembers the newest entry it has checked, so a later read
    or append starts there rather than from the first entry. That memory is
    only a starting point: a stale one costs a refused create and a re-read,
    never a wrong entry, because the store's create-only rule decides who
    holds each number.
    """

    def __init__(self, store: BlobStore, account: str) -> None:
        self._store = store
        self._account = account
        # The prefix every entry key of this account starts with, built by the
        # one key builder so the two cannot disagree.
        self._prefix = journal_entry_key(account, FIRST_SEQ).rpartition("/")[0] + "/"
        self._head: JournalAck | None = None
        self._lock = threading.Lock()

    @property
    def account(self) -> str:
        return self._account

    def head(self) -> JournalAck | None:
        """The newest entry's number and hash, or `None` for a journal with no entry.

        It reads every entry after the newest one this `Journal` has checked,
        with all of `read`'s checks.
        """
        known = self._known()
        for _ in self.read(after=known.seq if known else 0):
            pass
        return self._known()

    def build(self, body: Mapping[str, object], built_at: datetime) -> JournalEntry:
        """The next entry for `body`, chained to the newest entry stored.

        `built_at` is the writer's own clock read. The entry is built once and
        resent unchanged on a retry, so a live writer recognises its own
        earlier attempt by its bytes. The codec refuses a body that is not
        plain JSON values, here rather than at the write.
        """
        head = self.head()
        entry = JournalEntry(
            seq=head.seq + 1 if head else FIRST_SEQ,
            format=JOURNAL_FORMAT,
            built_at=built_at,
            body=body,
            prev_hash=head.entry_hash if head else FIRST_PREV_HASH,
        )
        encode(entry)
        return entry

    def append(self, entry: JournalEntry) -> JournalAck:
        """Store `entry` at its number's key, and return its number and hash.

        It halts, storing nothing, on an entry that does not chain to the
        stored entry before it, since a create-only entry with a wrong link
        breaks the chain for good. An existing key holding these same bytes is
        this writer's own earlier attempt whose answer was lost, so the append
        is done. One holding other bytes raises `NumberTaken`. A store that
        does not answer raises `StoreUnavailable`, and resending the same
        entry is then safe.
        """
        if entry.format not in FORMATS:
            raise Halt(
                f"journal {self._account}: entry {entry.seq} has format {entry.format}, "
                f"which this release does not read"
            )
        data = encode(entry)
        digest = hashlib.sha256(data).hexdigest()
        self._check_link(entry)
        key = journal_entry_key(self._account, entry.seq)
        try:
            self._store.create(key, data, digest)
        except KeyExists:
            if self._store.get(key) != data:
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
        not chain, a gap in the numbers and a key that is not an entry's.
        Checkpoints, which #10 writes under the same prefix, are skipped.
        """
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError(f"after {after!r} is not an entry number or 0")
        expected = self._hash_of(after)
        start = journal_entry_key(self._account, after) if after else None
        seq = after
        for key in self._store.list(self._prefix, start):
            if not key.startswith(self._prefix):
                raise Halt(f"journal {self._account}: the listing returned {key}")
            name = key[len(self._prefix) :]
            if name.startswith(_CHECKPOINTS):
                continue
            if not _ENTRY_NAME.fullmatch(name):
                raise Halt(f"journal {self._account}: {key} is not an entry's key")
            number = int(name[:12])
            if number != seq + 1:
                raise Halt(
                    f"journal {self._account}: entry {seq + 1} is missing, "
                    f"and the next key is entry {number}"
                )
            data = self._get(key)
            entry = self._checked(number, data)
            if entry.prev_hash != expected:
                raise Halt(
                    f"journal {self._account}: entry {number}'s prev_hash does not "
                    f"match the SHA-256 of entry {seq}'s stored bytes"
                )
            seq = number
            expected = hashlib.sha256(data).hexdigest()
            self._advance(JournalAck(seq=seq, entry_hash=expected))
            yield entry

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
        self._checked(seq, data)
        return hashlib.sha256(data).hexdigest()

    def _get(self, key: str) -> bytes:
        try:
            return self._store.get(key)
        except BlobMissing:
            raise Halt(f"journal {self._account}: {key} was listed and then missing") from None

    def _checked(self, seq: int, data: bytes) -> JournalEntry:
        """Entry `seq`'s stored bytes, read back with the reader's three checks."""
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
        if entry.format not in FORMATS:
            raise Halt(
                f"journal {self._account}: entry {seq} has format {entry.format}, "
                f"which this release does not read"
            )
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
