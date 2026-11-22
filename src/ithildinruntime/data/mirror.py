"""The local mirror of marketlake's bucket, which every read of a lake goes through.

The mirror is a directory and a cache. Losing it costs a download, so the
machine holds no state a run needs. Each decision checks what it reads before
marketlake's reader opens any of it, because `lake.loader.load_bars` lists
partitions from disk and checks none against the manifest.

How one decision fills its lake root, in order.

1. **The manifest is downloaded whole, every decision.** marketlake's
   manifest reader drops a torn last line and still answers, so a download
   cut short would read an older entry and admit a superseded partition. So
   the bytes must match the SHA-256 the bucket stored with them, the file must
   end in a newline, and a download that fails halts rather than falling back
   to a copy already held. A whole line hidden behind damage inside the file
   halts too, through marketlake's own `refuse_damaged_manifest`, because
   the tolerant read would skip it and answer without it.
2. **Both ledgers must be listed and present.** `lake.paths.is_reader_file`
   says a missing `quarantine.jsonl` admits a withheld partition and a missing
   `actions/corporate_actions.jsonl` labels as-traded numbers adjusted, so
   both fail open inside the reader. A manifest that lists either not at all,
   or a listed reader file the bucket does not hold, halts before any read.
3. **Each file is checked against its manifest SHA-256.** A file the mirror
   already holds at that SHA-256 is used again once its bytes still hash to
   it, and a damaged copy is downloaded again. A download whose bytes differ
   from the manifest halts, naming both hashes. marketlake's README publishes
   that an upload stopped before its `manifest.jsonl` leaves a file newer than
   its manifest entry until the next night's repair, so the halt names that
   case.
4. **A partition the manifest lists and the bucket does not hold is "data
   not ready"**, which halts. A session the manifest does not list has no bar,
   which the reader answers as `BarsAbsent`.
5. **Each file is stored under its SHA-256 and never rewritten.** Data's
   stage runs several decisions side by side, so one decision's refresh could
   otherwise replace a ledger while another reads it, and that snapshot would
   record a hash for bytes it never read. A download is written to a
   temporary name and renamed into place, beside a record of the bucket
   version it came from, and each decision reads its own lake root of hard
   links to the files it checked.
6. **A file the bucket reports with no version, or with no stored SHA-256,
   halts**, because a bar's `vintage_id` names its partition by that version.
   A manifest SHA-256 that is not 64 lowercase hex digits halts as well,
   because the mirror names its stored files by it.

The mirror never writes to the bucket. `core.Bucket` has no call that could.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lake.manifest import ManifestError, latest_entries, read_manifest, refuse_damaged_manifest
from lake.paths import is_reader_file

from ithildinruntime.core import BlobMissing, Bucket, BucketObject, Halt, StoreError

__all__ = ["LEDGERS", "MANIFEST", "Checked", "LakeRoot", "Mirror"]

MANIFEST = "manifest.jsonl"
QUARANTINE = "quarantine.jsonl"
ACTIONS = "actions/corporate_actions.jsonl"
# The two reader files that fail open when missing, so both must be listed.
LEDGERS = (QUARANTINE, ACTIONS)

_NOT_READY = "is listed in the manifest and not in the bucket, so the data is not ready"
_STOPPED_UPLOAD = (
    "marketlake's README publishes that an upload stopped before its manifest.jsonl "
    "leaves a file newer than its manifest entry until the next night's repair, which "
    "reads this way"
)


@dataclass(frozen=True)
class Checked:
    """A file the mirror checked for one decision: its key, SHA-256 and bucket version."""

    key: str
    sha256: str
    version: str


@dataclass
class LakeRoot:
    """One decision's lake root, of hard links to the files the mirror checked."""

    path: Path
    manifest: Checked
    entries: Mapping[str, Mapping[str, object]]
    files: dict[str, Checked]
    manifest_bytes: int

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


class Mirror:
    """A cache of marketlake's bucket in `directory`, read through a `core.Bucket`."""

    def __init__(self, bucket: Bucket, directory: str | Path) -> None:
        self._bucket = bucket
        self._dir = Path(directory)
        self._objects = self._dir / "objects"
        self._versions = self._dir / "versions"
        self._roots = self._dir / "roots"
        for path in (self._objects, self._versions, self._roots):
            path.mkdir(parents=True, exist_ok=True)

    def open(self) -> LakeRoot:
        """A new lake root holding a manifest checked whole and every reader file it lists."""
        path = self._roots / uuid.uuid4().hex
        path.mkdir()
        try:
            manifest = self._download(MANIFEST, None)
            data = self._object(manifest.sha256).read_bytes()
            if not data.endswith(b"\n"):
                raise Halt(
                    f"{MANIFEST} does not end in a newline, so its download was cut short "
                    "or its last entry is torn"
                )
            self._link(path, MANIFEST, manifest.sha256)
            try:
                refuse_damaged_manifest(path, data, read_manifest(path))
                entries = latest_entries(path)
            except ManifestError as error:
                raise Halt(f"{MANIFEST} cannot be read: {type(error).__name__}: {error}") from None
            root = LakeRoot(path, manifest, entries, {}, len(data))
            for ledger in LEDGERS:
                if ledger not in entries:
                    raise Halt(
                        f"{MANIFEST} lists no {ledger}, and a lake root without it fails open"
                    )
            for key in sorted(k for k in entries if is_reader_file(k)):
                self.add(root, key, missing="is a reader file the bucket does not hold")
            return root
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise

    def add(self, root: LakeRoot, key: str, *, missing: str = "is not in the bucket") -> None:
        """Check one file the manifest lists and link it into `root`."""
        sha = _manifest_sha(root.entries[key], key)
        checked = self._held(key, sha) or self._download(key, sha, missing=missing)
        self._link(root.path, key, checked.sha256)
        root.files[key] = checked

    def partitions(self, root: LakeRoot, keys: Iterable[str]) -> None:
        """Link each listed partition into `root`. A key the manifest does not list is skipped."""
        for key in keys:
            if key in root.entries and key not in root.files:
                self.add(root, key, missing=_NOT_READY)

    # The cache

    def _object(self, sha: str) -> Path:
        return self._objects / sha

    def _version_path(self, key: str, sha: str) -> Path:
        name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self._versions / f"{sha}-{name}.json"

    def _held(self, key: str, sha: str) -> Checked | None:
        """The mirror's copy at `sha`, if its bytes still hash to it and its version is known."""
        path = self._object(sha)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        if hashlib.sha256(data).hexdigest() != sha:
            # A damaged cache, so the file is downloaded again.
            path.unlink(missing_ok=True)
            return None
        try:
            record = json.loads(self._version_path(key, sha).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        version = record.get("version") if isinstance(record, dict) else None
        if not isinstance(version, str) or record.get("key") != key:
            return None
        return Checked(key, sha, version)

    def _download(self, key: str, want: str | None, *, missing: str = "") -> Checked:
        try:
            got = self._bucket.read(key)
        except BlobMissing:
            raise Halt(f"{key} {missing or 'is not in the bucket'}") from None
        except StoreError as error:
            raise Halt(f"{key} could not be downloaded: {type(error).__name__}: {error}") from None
        sha = _check(got, want)
        self._store(key, sha, got.version, got.data)  # type: ignore[arg-type]
        return Checked(key, sha, got.version)  # type: ignore[arg-type]

    def _store(self, key: str, sha: str, version: str, data: bytes) -> None:
        # The version record lands first, so a crash between the two renames
        # leaves a record with no file, which reads as not held.
        record = json.dumps({"key": key, "version": version}, sort_keys=True).encode("utf-8")
        _write_new(self._version_path(key, sha), record)
        _write_new(self._object(sha), data)

    def _link(self, root: Path, key: str, sha: str) -> None:
        target = root.joinpath(*key.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(self._object(sha), target)


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _manifest_sha(entry: Mapping[str, object], key: str) -> str:
    sha = entry.get("sha256")
    if not isinstance(sha, str) or _SHA256.fullmatch(sha) is None:
        raise Halt(f"{MANIFEST} records no SHA-256 for {key}, as 64 lowercase hex digits")
    return sha


def _check(got: BucketObject, want: str | None) -> str:
    key = got.key
    if got.version is None:
        raise Halt(f"{key} has no version in the bucket, so no bar read from it can name one")
    if got.sha256 is None:
        raise Halt(f"{key} has no SHA-256 stored in the bucket, so its download cannot be checked")
    actual = hashlib.sha256(got.data).hexdigest()
    if actual != got.sha256:
        raise Halt(
            f"{key} downloaded with SHA-256 {actual}, and the bucket stored {got.sha256} "
            "with it, so the download was damaged"
        )
    if want is not None and actual != want:
        raise Halt(
            f"{key} in the bucket has SHA-256 {actual}, and {MANIFEST} records {want}. "
            f"{_STOPPED_UPLOAD}"
        )
    return actual


def _write_new(path: Path, data: bytes) -> None:
    """Write `data` to `path` through a temporary name, so a reader sees all or nothing."""
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
