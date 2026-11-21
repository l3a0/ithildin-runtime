"""The journal's writer and reader: the hash chain, the append's refusals and the reader's checks.

docs/design.md's `### Rule 8: the journal is the one numbered log` and
docs/architecture/08-crosscutting-concepts.md's `#### How a journal entry is
hashed and checked` hold the rules these tests pin. The tests that matter for
both stores run through the `spec` fixture, once on the folder and once on S3
under moto.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    Halt,
    JournalEntry,
    StoreUnavailable,
    checkpoint_key,
    encode,
    journal_entry_key,
    sha256_of,
)
from ithildinruntime.journal import (
    FIRST_PREV_HASH,
    FORMATS,
    JOURNAL_FORMAT,
    Journal,
    NumberTaken,
)
from tests.support.stores import StoreSpec

ACCOUNT = "paper-1"
AT = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "journal" / "format-1"


def _body(n: int) -> dict[str, object]:
    return {"kind": "fixture", "n": n}


def _append(journal: Journal, n: int) -> None:
    journal.append(journal.build(_body(n), AT + timedelta(seconds=n)))


def _plant(store, seq: int, data: bytes, account: str = ACCOUNT) -> None:
    store.create(journal_entry_key(account, seq), data, sha256_of(data))


def _entry(seq: int, prev_hash: str, **changes: object) -> JournalEntry:
    fields = {
        "seq": seq,
        "format": JOURNAL_FORMAT,
        "built_at": AT,
        "body": _body(seq),
        "prev_hash": prev_hash,
    }
    fields.update(changes)
    return JournalEntry(**fields)


def _stored(store, seq: int) -> bytes:
    return store.get(journal_entry_key(ACCOUNT, seq))


# Acceptance 1: the chain.


def test_the_first_entry_carries_sixty_four_zeros(spec: StoreSpec) -> None:
    store = spec.open()
    journal = Journal(store, ACCOUNT)
    assert journal.head() is None
    entry = journal.build(_body(1), AT)
    assert entry.seq == 1
    assert entry.prev_hash == "0" * 64 == FIRST_PREV_HASH
    ack = journal.append(entry)
    data = _stored(store, 1)
    assert data == encode(entry)
    assert ack.seq == 1
    assert ack.entry_hash == hashlib.sha256(data).hexdigest() == entry.hash()
    assert store.list("journal/", None) == ["journal/paper-1/000000000001.json"]


def test_each_later_entry_carries_the_sha256_of_the_stored_bytes_before_it(
    spec: StoreSpec,
) -> None:
    store = spec.open()
    journal = Journal(store, ACCOUNT)
    for n in range(1, 5):
        _append(journal, n)
    for seq in range(2, 5):
        before = _stored(store, seq - 1)
        entry = next(Journal(store, ACCOUNT).read(after=seq - 1))
        assert entry.seq == seq
        assert entry.prev_hash == hashlib.sha256(before).hexdigest()
    assert [e.seq for e in Journal(store, ACCOUNT).read()] == [1, 2, 3, 4]


def test_a_fresh_journal_builds_on_the_stored_end(spec: StoreSpec) -> None:
    store = spec.open()
    _append(Journal(store, ACCOUNT), 1)
    _append(Journal(store, ACCOUNT), 2)
    head = Journal(store, ACCOUNT).head()
    assert head.seq == 2
    assert head.entry_hash == hashlib.sha256(_stored(store, 2)).hexdigest()


def test_accounts_keep_separate_chains(tmp_path: Path) -> None:
    store = FolderStore(tmp_path)
    _append(Journal(store, "a"), 1)
    _append(Journal(store, "a2"), 1)
    _append(Journal(store, "a2"), 2)
    assert Journal(store, "a").head().seq == 1
    assert Journal(store, "a2").head().seq == 2


# The append: a resend, a taken number, and an entry that would break the chain.


def test_a_resend_of_the_same_entry_is_its_own_earlier_attempt(spec: StoreSpec) -> None:
    store = spec.open()
    journal = Journal(store, ACCOUNT)
    entry = journal.build(_body(1), AT)
    first = journal.append(entry)
    assert Journal(store, ACCOUNT).append(entry) == first
    assert len(store.list("journal/", None)) == 1


class _LostReply:
    """A store whose first create lands and then answers as if S3 had not."""

    def __init__(self, store) -> None:
        self._store = store
        self.lost = 0

    def __getattr__(self, name):
        return getattr(self._store, name)

    def create(self, key: str, data: bytes, sha256: str) -> str:
        tag = self._store.create(key, data, sha256)
        if not self.lost:
            self.lost += 1
            raise StoreUnavailable(key, "the reply was lost")
        return tag


def test_a_lost_reply_is_settled_by_resending_the_same_entry(tmp_path: Path) -> None:
    store = _LostReply(FolderStore(tmp_path))
    journal = Journal(store, ACCOUNT)
    entry = journal.build(_body(1), AT)
    with pytest.raises(StoreUnavailable):
        journal.append(entry)
    ack = journal.append(entry)
    assert ack.seq == 1
    assert [e.seq for e in Journal(store, ACCOUNT).read()] == [1]


def test_a_number_another_writer_took_is_refused_and_never_renumbered(
    spec: StoreSpec,
) -> None:
    store = spec.open()
    ours, theirs = Journal(store, ACCOUNT), Journal(store, ACCOUNT)
    _append(ours, 1)
    late = ours.build(_body(2), AT)
    _append(theirs, 99)
    with pytest.raises(NumberTaken) as refused:
        ours.append(late)
    assert refused.value.seq == 2
    assert refused.value.account == ACCOUNT
    assert Journal(store, ACCOUNT).head().seq == 2
    again = ours.build(_body(2), AT)
    assert again.seq == 3
    assert again.prev_hash == hashlib.sha256(_stored(store, 2)).hexdigest()
    ours.append(again)
    assert [e.body["n"] for e in Journal(store, ACCOUNT).read()] == [1, 99, 2]


def test_an_entry_with_a_wrong_link_is_refused_before_it_is_stored(tmp_path: Path) -> None:
    store = FolderStore(tmp_path)
    journal = Journal(store, ACCOUNT)
    _append(journal, 1)
    for wrong in (FIRST_PREV_HASH, "f" * 64):
        with pytest.raises(Halt, match="prev_hash"):
            journal.append(_entry(2, wrong))
        with pytest.raises(Halt, match="prev_hash"):
            Journal(store, ACCOUNT).append(_entry(2, wrong))
    with pytest.raises(Halt, match="prev_hash"):
        Journal(store, "fresh").append(_entry(1, "f" * 64))
    assert store.list("journal/", None) == ["journal/paper-1/000000000001.json"]


def test_an_entry_past_a_gap_is_refused_before_it_is_stored(tmp_path: Path) -> None:
    store = FolderStore(tmp_path)
    journal = Journal(store, ACCOUNT)
    _append(journal, 1)
    with pytest.raises(Halt, match="entry 2 is missing"):
        Journal(store, ACCOUNT).append(_entry(3, "f" * 64))
    assert len(store.list("journal/", None)) == 1


@pytest.mark.parametrize("seq", [0, -1, True])
def test_an_entry_numbered_below_one_is_refused(tmp_path: Path, seq: object) -> None:
    with pytest.raises(Halt, match="not an entry number"):
        Journal(FolderStore(tmp_path), ACCOUNT).append(_entry(seq, FIRST_PREV_HASH))
    assert FolderStore(tmp_path).list("journal/", None) == []


def test_an_entry_in_a_format_this_release_does_not_read_is_refused(tmp_path: Path) -> None:
    unknown = max(FORMATS) + 1
    store = FolderStore(tmp_path)
    with pytest.raises(Halt, match=f"format {unknown}"):
        Journal(store, ACCOUNT).append(_entry(1, FIRST_PREV_HASH, format=unknown))
    assert store.list("journal/", None) == []


def test_a_body_the_codec_cannot_write_is_refused_at_build(tmp_path: Path) -> None:
    from decimal import Decimal

    from ithildinruntime.core import CodecError

    with pytest.raises(CodecError):
        Journal(FolderStore(tmp_path), ACCOUNT).build({"qty": Decimal("1.5")}, AT)


def test_racing_writers_each_take_their_own_numbers(spec: StoreSpec) -> None:
    """Writers that rebuild after `NumberTaken` leave one unbroken chain holding every entry."""
    store = spec.open()
    writers, each = 4, 6
    failures: list[BaseException] = []
    start = threading.Barrier(writers)

    def write(w: int) -> None:
        journal = Journal(spec.open(), ACCOUNT)
        try:
            start.wait()
            for i in range(each):
                while True:
                    try:
                        journal.append(journal.build({"kind": "fixture", "w": w, "i": i}, AT))
                        break
                    except NumberTaken:
                        continue
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    threads = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures, failures
    entries = list(Journal(store, ACCOUNT).read())
    assert [e.seq for e in entries] == list(range(1, writers * each + 1))
    written = sorted((e.body["w"], e.body["i"]) for e in entries)
    assert written == [(w, i) for w in range(writers) for i in range(each)]


class _Counting:
    """A store that counts the blobs read, to show what a `Journal` re-reads."""

    def __init__(self, store) -> None:
        self._store = store
        self.gets: list[str] = []

    def __getattr__(self, name):
        return getattr(self._store, name)

    def get(self, key: str) -> bytes:
        self.gets.append(key)
        return self._store.get(key)


def test_a_journal_reads_no_entry_twice_once_it_has_checked_it(tmp_path: Path) -> None:
    store = _Counting(FolderStore(tmp_path))
    journal = Journal(store, ACCOUNT)
    for n in range(1, 4):
        _append(journal, n)
    assert store.gets == []
    assert next(journal.read()).seq == 1
    store.gets.clear()
    assert journal.head().seq == 3
    _append(journal, 4)
    assert store.gets == []


# Acceptance 2: the reader's checks.


def test_the_reader_halts_on_bytes_that_are_not_canonical(spec: StoreSpec) -> None:
    store = spec.open()
    canonical = encode(_entry(1, FIRST_PREV_HASH))
    _plant(store, 1, canonical.replace(b'"seq":1', b'"seq": 1'))
    with pytest.raises(Halt, match="not a canonical"):
        list(Journal(store, ACCOUNT).read())


@pytest.mark.parametrize(
    "bytes_",
    [
        b'{"body":{},"built_at":"2026-11-21T15:00:00Z","format":1,"prev_hash":"'
        + FIRST_PREV_HASH.encode()
        + b'","seq":1}',
        b"not json",
        b"[1]",
        b'{"format":1}',
    ],
)
def test_the_reader_halts_on_any_blob_that_is_not_an_entry(tmp_path: Path, bytes_: bytes) -> None:
    store = FolderStore(tmp_path)
    _plant(store, 1, bytes_)
    with pytest.raises(Halt, match="not a canonical"):
        list(Journal(store, ACCOUNT).read())


def test_the_reader_halts_on_a_key_whose_number_differs_from_the_seq_inside(
    spec: StoreSpec,
) -> None:
    store = spec.open()
    _plant(store, 1, encode(_entry(2, FIRST_PREV_HASH)))
    with pytest.raises(Halt, match="key of entry 1 holds seq 2"):
        list(Journal(store, ACCOUNT).read())


def test_the_reader_halts_on_a_format_it_does_not_know(spec: StoreSpec) -> None:
    store = spec.open()
    unknown = max(FORMATS) + 1
    _plant(store, 1, encode(_entry(1, FIRST_PREV_HASH, format=unknown)))
    with pytest.raises(Halt, match=f"format {unknown}"):
        list(Journal(store, ACCOUNT).read())


def test_a_later_format_with_fields_this_release_cannot_read_halts_by_its_format(
    tmp_path: Path,
) -> None:
    store = FolderStore(tmp_path)
    unknown = max(FORMATS) + 1
    _plant(store, 1, b'{"format":%d,"new_field":"x","seq":1}' % unknown)
    with pytest.raises(Halt, match=f"format {unknown}"):
        list(Journal(store, ACCOUNT).read())


def test_the_reader_halts_on_a_link_that_does_not_chain(spec: StoreSpec) -> None:
    store = spec.open()
    first = encode(_entry(1, FIRST_PREV_HASH))
    _plant(store, 1, first)
    _plant(store, 2, encode(_entry(2, "f" * 64)))
    with pytest.raises(Halt, match="entry 2's prev_hash"):
        list(Journal(store, ACCOUNT).read())


def test_the_reader_halts_on_a_first_entry_that_does_not_start_at_zeros(tmp_path: Path) -> None:
    store = FolderStore(tmp_path)
    _plant(store, 1, encode(_entry(1, "f" * 64)))
    with pytest.raises(Halt, match="entry 1's prev_hash"):
        list(Journal(store, ACCOUNT).read())


def test_the_reader_halts_on_a_gap_in_the_numbers(spec: StoreSpec) -> None:
    store = spec.open()
    journal = Journal(store, ACCOUNT)
    _append(journal, 1)
    first = _stored(store, 1)
    _plant(store, 3, encode(_entry(3, hashlib.sha256(first).hexdigest())))
    with pytest.raises(Halt, match="entry 2 is missing"):
        list(Journal(store, ACCOUNT).read())


def test_the_reader_halts_on_a_journal_that_does_not_start_at_one(tmp_path: Path) -> None:
    store = FolderStore(tmp_path)
    _plant(store, 2, encode(_entry(2, FIRST_PREV_HASH)))
    with pytest.raises(Halt, match="entry 1 is missing"):
        list(Journal(store, ACCOUNT).read())


def test_the_reader_halts_after_yielding_every_entry_before_the_fault(tmp_path: Path) -> None:
    store = FolderStore(tmp_path)
    journal = Journal(store, ACCOUNT)
    _append(journal, 1)
    _append(journal, 2)
    _plant(store, 3, encode(_entry(3, "f" * 64)))
    seen = []
    with pytest.raises(Halt):
        for entry in Journal(store, ACCOUNT).read():
            seen.append(entry.seq)
    assert seen == [1, 2]


def test_the_reader_skips_checkpoints_and_halts_on_any_other_key(tmp_path: Path) -> None:
    store = FolderStore(tmp_path)
    journal = Journal(store, ACCOUNT)
    _append(journal, 1)
    store.create(checkpoint_key(ACCOUNT, 1), b"{}", sha256_of(b"{}"))
    _append(Journal(store, ACCOUNT), 2)
    assert [e.seq for e in Journal(store, ACCOUNT).read()] == [1, 2]
    store.create(f"journal/{ACCOUNT}/stray.json", b"{}", sha256_of(b"{}"))
    with pytest.raises(Halt, match="stray.json is not an entry's key"):
        list(Journal(store, ACCOUNT).read())


def test_a_read_from_a_later_entry_checks_its_link_against_the_stored_entry(
    tmp_path: Path,
) -> None:
    store = FolderStore(tmp_path)
    journal = Journal(store, ACCOUNT)
    for n in (1, 2):
        _append(journal, n)
    _plant(store, 3, encode(_entry(3, "f" * 64)))
    with pytest.raises(Halt, match="entry 3's prev_hash"):
        list(Journal(store, ACCOUNT).read(after=2))
    with pytest.raises(Halt, match="entry 9 is missing"):
        list(Journal(store, ACCOUNT).read(after=9))


def test_a_read_from_a_later_entry_checks_that_entry_too(tmp_path: Path) -> None:
    store = FolderStore(tmp_path)
    _plant(store, 1, encode(_entry(2, FIRST_PREV_HASH)))
    with pytest.raises(Halt, match="key of entry 1 holds seq 2"):
        list(Journal(store, ACCOUNT).read(after=1))


def test_head_checks_what_another_writer_appended(tmp_path: Path) -> None:
    store = FolderStore(tmp_path)
    journal = Journal(store, ACCOUNT)
    _append(journal, 1)
    _plant(store, 2, encode(_entry(2, "f" * 64)))
    with pytest.raises(Halt, match="entry 2's prev_hash"):
        journal.head()
    with pytest.raises(Halt, match="entry 2's prev_hash"):
        journal.build(_body(3), AT)


# Every format ever written stays readable.


def test_the_committed_format_1_journal_reads_and_chains(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    entries = list(Journal(FolderStore(tmp_path), "fixture").read())
    assert [(e.seq, e.format) for e in entries] == [(1, 1), (2, 1), (3, 1)]
    assert entries[0].prev_hash == FIRST_PREV_HASH
    for before, after in zip(entries, entries[1:], strict=False):
        assert after.prev_hash == before.hash()


def test_this_release_writes_the_committed_format_1_bytes(tmp_path: Path) -> None:
    committed = sorted((FIXTURE / "journal" / "fixture").iterdir())
    copy = tmp_path / "copy"
    shutil.copytree(FIXTURE, copy)
    entries = list(Journal(FolderStore(copy), "fixture").read())
    fresh = Journal(FolderStore(tmp_path / "fresh"), "fixture")
    for entry, path in zip(entries, committed, strict=True):
        fresh.append(fresh.build(entry.body, entry.built_at))
        assert (tmp_path / "fresh" / "journal" / "fixture" / path.name).read_bytes() == (
            path.read_bytes()
        )


def test_every_format_this_release_reads_has_a_committed_journal() -> None:
    committed = {int(path.name.removeprefix("format-")) for path in FIXTURE.parent.iterdir()}
    assert committed == set(FORMATS)
    assert JOURNAL_FORMAT in FORMATS
