"""The memory-backed folder store, which a backtest and CI use in place of S3.

It keeps S3's rules on a folder, which a backtest puts on a memory-backed file
system such as `/dev/shm`, so its blobs vanish when the run ends.

1. A create writes the bytes to a temporary file under `.incoming/` in the
   same folder, reads them back, checks their SHA-256, and links the file to
   its key. A link refuses a name that exists, so two processes racing one
   create leave one winner, and a reader sees no blob or the whole blob.
2. A rewrite takes a lock on the key, compares the blob's tag with the one it
   names, and renames a checked temporary file over the key, which a reader
   also sees whole.
3. A tag is the file's inode and its modification time in nanoseconds, read
   from the writer's own temporary file, so every create and rewrite gives a
   new one and a create never returns a later writer's tag. A stored time is
   the modification time in whole seconds, which is what S3 records.

4. After each create and rewrite, the folder plays S3's announcement. It
   sends the event down the named pipe of each reader whose route matches the
   key, as `adapters.pipe` describes, and `announce` sends it again for a
   stored blob. Under a seeded `Faults` it drops, repeats or delays each
   event, and a delay reorders it, so the queue suite shows a reader checks
   its keys rather than trusting its events. A writer killed between its
   write and the delivery leaves the blob unannounced, as a restarted writer
   finds.

Nothing here deletes a blob, and a writer killed partway leaves only a
temporary file, which no listing shows. Any error the file system raises
beyond the refusals `core` names is `StoreUnavailable`, as S3's are.

Two limits differ from S3. The folder must sit on a case-sensitive file
system, as Linux's memory-backed ones are, because on macOS's default volume
two keys that differ only in case share a file. And a key cannot sit below
another stored key, such as `a.json/b.json` beside `a.json`, which S3 stores
and the folder refuses as existing, so the storage contract finds no blob
there, holds the capture and halts. No key builder in `core` writes either.
The folder does not check for a symlink inside it, which only someone who can
already write the folder can plant.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import random
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ithildinruntime.core import (
    BlobMissing,
    BlobRef,
    ChecksumMismatch,
    EventKind,
    KeyExists,
    Route,
    StaleTag,
    StoredBlob,
    StoreUnavailable,
    event_body,
    sha256_of,
)

from . import pipe

__all__ = ["Faults", "FolderStore"]

# The longest tag and sequencer the folder writes, which a key's event must
# leave room for inside one pipe record.
_LONGEST_TAG = "f" * 40
_LONGEST_SEQUENCER = "f" * 16


@dataclass(frozen=True)
class Faults:
    """How the folder mistreats its events, drawn from one seeded generator.

    `drop` and `repeat` are each event's chance of being lost or sent twice,
    and `delay` the range in seconds each copy waits before it is sent. A delay
    long enough reorders events.
    """

    seed: int
    drop: float = 0.0
    repeat: float = 0.0
    delay: tuple[float, float] = (0.0, 0.0)

    def __post_init__(self) -> None:
        low, high = self.delay
        if not (0 <= self.drop <= 1 and 0 <= self.repeat <= 1 and 0 <= low <= high):
            raise ValueError("faults are chances from 0 to 1 and a delay range that grows")


_INCOMING = ".incoming"
_LOCKS = ".locks"
# A read of a key whose path is a folder, or runs through a file, finds no blob.
_NOT_A_BLOB = (FileNotFoundError, IsADirectoryError, NotADirectoryError)


class FolderStore:
    """A `BlobStore` on a folder, keeping S3's rules.

    With `routes` and `pipes`, the queue folder, it announces each write to
    the readers the routes name, as `bucket`, which every reader checks.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        bucket: str = "folder",
        routes: Sequence[Route] = (),
        pipes: Path | str | None = None,
        faults: Faults | None = None,
    ) -> None:
        if routes and pipes is None:
            raise ValueError("a folder that announces its writes needs the queue folder")
        self._root = Path(root)
        self._bucket = bucket
        self._routes = tuple(routes)
        self._pipes = None if pipes is None else Path(pipes)
        self._faults = faults
        self._chance = random.Random(faults.seed if faults else 0)
        self._chance_lock = threading.Lock()
        (self._root / _INCOMING).mkdir(parents=True, exist_ok=True)
        (self._root / _LOCKS).mkdir(exist_ok=True)

    def create(self, key: str, data: bytes, sha256: str) -> str:
        path = self._path(key)
        self._fits(key)
        incoming: Path | None = None
        try:
            incoming = self._incoming(key, data, sha256)
            tag = _tag(incoming.stat())
            path.parent.mkdir(parents=True, exist_ok=True)
            os.link(incoming, path)
        except FileExistsError:
            raise KeyExists(key, "the key already holds a blob") from None
        except OSError as error:
            raise StoreUnavailable(key, f"{type(error).__name__}: {error}") from None
        finally:
            if incoming is not None:
                incoming.unlink(missing_ok=True)
        self._deliver(key, tag)
        return tag

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except _NOT_A_BLOB:
            raise BlobMissing(key, "no blob at the key") from None
        except OSError as error:
            raise StoreUnavailable(key, f"{type(error).__name__}: {error}") from None

    def head(self, key: str) -> StoredBlob:
        path = self._path(key)
        try:
            with open(path, "rb") as file:
                info = os.fstat(file.fileno())
                sha = hashlib.sha256(file.read()).hexdigest()
        except _NOT_A_BLOB:
            raise BlobMissing(key, "no blob at the key") from None
        except OSError as error:
            raise StoreUnavailable(key, f"{type(error).__name__}: {error}") from None
        stored_at = datetime.fromtimestamp(int(info.st_mtime), UTC)
        return StoredBlob(BlobRef(key, sha), _tag(info), stored_at)

    def list(self, prefix: str, after: str | None) -> list[str]:
        """Every key that starts with `prefix` and sorts after `after`, in S3's order."""
        folder = prefix.rpartition("/")[0]
        start = self._path(folder) if folder else self._root
        keys = []
        try:
            for path in _files(start):
                key = path.relative_to(self._root).as_posix()
                if key.startswith(prefix) and (after is None or _order(key) > _order(after)):
                    keys.append(key)
        except OSError as error:
            raise StoreUnavailable(prefix, f"{type(error).__name__}: {error}") from None
        return sorted(keys, key=_order)

    def replace_if_match(self, key: str, data: bytes, sha256: str, etag: str) -> str:
        path = self._path(key)
        self._fits(key)
        try:
            with self._locked(key):
                incoming = self._incoming(key, data, sha256)
                try:
                    try:
                        info = path.stat()
                    except _NOT_A_BLOB:
                        raise BlobMissing(key, "no blob at the key to rewrite") from None
                    if _tag(info) != etag:
                        raise StaleTag(key, f"the blob's tag is {_tag(info)}, not {etag}")
                    tag = _tag(incoming.stat())
                    os.replace(incoming, path)
                finally:
                    incoming.unlink(missing_ok=True)
        except OSError as error:
            raise StoreUnavailable(key, f"{type(error).__name__}: {error}") from None
        self._deliver(key, tag)
        return tag

    def announce(self, key: str) -> None:
        """Send a stored blob's event again to each reader its routes name."""
        self._deliver(key, self.head(key).etag)

    def _readers(self, key: str) -> list[str]:
        readers = []
        for route in self._routes:
            if route.matches(EventKind.CREATED, key) and route.reader not in readers:
                readers.append(route.reader)
        return readers

    def _fits(self, key: str) -> None:
        """Refuse, before writing, a key whose event could not reach a reader whole."""
        if self._readers(key):
            pipe.record(
                event_body(self._bucket, key, EventKind.CREATED, _LONGEST_TAG, _LONGEST_SEQUENCER)
            )

    def _deliver(self, key: str, tag: str) -> None:
        readers = self._readers(key)
        if not readers:
            return
        sequencer = f"{time.time_ns():016x}"
        body = event_body(self._bucket, key, EventKind.CREATED, tag, sequencer)
        for reader in readers:
            for wait in self._copies():
                if wait > 0:
                    timer = threading.Timer(wait, self._send, (reader, body))
                    timer.daemon = True
                    timer.start()
                else:
                    self._send(reader, body)

    def _copies(self) -> list[float]:
        """How long each copy of one event waits, with no copy for a dropped event."""
        faults = self._faults
        if faults is None:
            return [0.0]
        with self._chance_lock:
            if self._chance.random() < faults.drop:
                return []
            copies = 2 if self._chance.random() < faults.repeat else 1
            return [self._chance.uniform(*faults.delay) for _ in range(copies)]

    def _send(self, reader: str, body: bytes) -> None:
        # The blob is stored whatever happens here, and an event is only a
        # reason to wake, so a pipe that refuses it loses the event alone.
        try:
            pipe.send(self._pipes, reader, body)  # type: ignore[arg-type]
        except OSError:
            pass

    def _path(self, key: str) -> Path:
        parts = key.split("/")
        if not key or any(not p or p in (".", "..") or p.startswith(".") for p in parts):
            raise ValueError(f"{key!r} is not a key the folder stores")
        return self._root.joinpath(*parts)

    def _incoming(self, key: str, data: bytes, sha256: str) -> Path:
        incoming = self._root / _INCOMING / uuid.uuid4().hex
        with open(incoming, "xb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        received = sha256_of(incoming.read_bytes())
        if received != sha256:
            incoming.unlink()
            raise ChecksumMismatch(key, f"the bytes hash to {received}, not {sha256}")
        return incoming

    @contextmanager
    def _locked(self, key: str) -> Iterator[None]:
        lock = self._root / _LOCKS / sha256_of(key.encode())
        with open(lock, "a") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _tag(info: os.stat_result) -> str:
    return f"{info.st_ino:x}-{info.st_mtime_ns:x}"


def _order(key: str) -> bytes:
    """S3 lists keys in the order of their UTF-8 bytes."""
    return key.encode("utf-8")


def _raise(error: OSError) -> None:
    # os.walk skips a folder it cannot read unless told otherwise, which
    # would leave keys out of a listing without a word.
    raise error


def _files(start: Path) -> Iterator[Path]:
    if not start.is_dir():
        return
    for directory, folders, files in os.walk(start, onerror=_raise):
        folders[:] = [f for f in folders if not f.startswith(".")]
        for name in files:
            yield Path(directory, name)
