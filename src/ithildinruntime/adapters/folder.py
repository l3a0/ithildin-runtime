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
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ithildinruntime.core import (
    BlobMissing,
    BlobRef,
    ChecksumMismatch,
    KeyExists,
    StaleTag,
    StoredBlob,
    StoreUnavailable,
    sha256_of,
)

__all__ = ["FolderStore"]

_INCOMING = ".incoming"
_LOCKS = ".locks"
# A read of a key whose path is a folder, or runs through a file, finds no blob.
_NOT_A_BLOB = (FileNotFoundError, IsADirectoryError, NotADirectoryError)


class FolderStore:
    """A `BlobStore` on a folder, keeping S3's rules."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        (self._root / _INCOMING).mkdir(parents=True, exist_ok=True)
        (self._root / _LOCKS).mkdir(exist_ok=True)

    def create(self, key: str, data: bytes, sha256: str) -> str:
        path = self._path(key)
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
        return tag

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
