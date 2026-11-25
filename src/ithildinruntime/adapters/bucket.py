"""The two read-only buckets marketlake's lake is read through: a folder, and S3.

`core.Bucket` is the port. It lists keys, reads an object's bytes with the
bucket's version and stored SHA-256, and holds no call that writes, because
the data worker's grant on marketlake's bucket allows only reads. Each bucket
is rooted at a prefix, and every key is relative to it, so a key reads the
same in a folder and in S3.

1. **The folder bucket** serves a directory laid out as marketlake's lake, as
   a backtest on a laptop and CI read it. A folder keeps no versions and
   stores no checksum, so it reports the SHA-256 of the bytes it read as
   both. A run over the same folder therefore names the same versions every
   time, and the check of bytes against the stored SHA-256 holds by
   construction here, which the S3 bucket's suite run covers. A read at a
   version the bytes no longer hash to reads as `BlobMissing`.
2. **The S3 bucket** reads with checksum mode on, so botocore checks the bytes
   against the SHA-256 S3 stored with them, and reports S3's version id. An
   object stored with no whole-object SHA-256, or in a bucket without
   versioning, reports `None` for that field, which its readers refuse. Its
   refusals go through the S3 store's own mapping, so a missing key reads as
   `BlobMissing` here as it does there, and so does a missing version. Bytes
   that fail botocore's check read as `ChecksumMismatch`.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path, PurePosixPath
from typing import Any

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    FlexibleChecksumError,
    ParamValidationError,
)

from ithildinruntime.core import (
    BlobMissing,
    BucketObject,
    ChecksumMismatch,
    StoreUnavailable,
    sha256_of,
)

from .s3 import _refusal

__all__ = ["FolderBucket", "S3Bucket"]


class FolderBucket:
    """A `Bucket` over a directory that holds marketlake's lake. The directory is its root."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def list(self, prefix: str) -> list[str]:
        if prefix:
            _parts(prefix)
        # A prefix need not end at a folder, so the walk starts at the last whole one.
        folder = prefix.rpartition("/")[0]
        found = []
        try:
            for here, _, names in os.walk(self._root / folder, onerror=_raise):
                at = Path(here).relative_to(self._root).as_posix()
                found.extend(name if at == "." else f"{at}/{name}" for name in names)
        except OSError as error:
            raise StoreUnavailable(prefix, f"{type(error).__name__}: {error}") from None
        return sorted((key for key in found if key.startswith(prefix)), key=os.fsencode)

    def read(self, key: str, version: str | None = None) -> BucketObject:
        path = self._root.joinpath(*_parts(key))
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise BlobMissing(key, "no such file in the folder") from None
        except OSError as error:
            raise StoreUnavailable(key, f"{type(error).__name__}: {error}") from None
        sha = sha256_of(data)
        if version not in (None, sha):
            raise BlobMissing(key, f"the folder holds version {sha}, not {version}")
        return BucketObject(key=key, data=data, version=sha, sha256=sha)


class S3Bucket:
    """A `Bucket` over one prefix of an S3 bucket. `client` is a boto3 S3 client.

    A read is safe to repeat, so unlike the S3 store this takes a client that
    resends a call botocore's retry mode allows.
    """

    def __init__(self, client: Any, bucket: str, prefix: str) -> None:
        if not prefix or prefix != prefix.strip("/"):
            raise ValueError(f"{prefix!r} is not a prefix: it is empty, or a slash is at an end")
        self._client = client
        self._bucket = bucket
        self._root = f"{prefix}/"

    def list(self, prefix: str) -> list[str]:
        if prefix:
            _parts(prefix)
        try:
            pages = self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self._bucket, Prefix=self._root + prefix
            )
            keys = [item["Key"] for page in pages for item in page.get("Contents", ())]
        except ParamValidationError as error:
            raise TypeError(f"botocore refused the call before sending it: {error}") from None
        except (ClientError, BotoCoreError) as error:
            raise _refusal(prefix, error, None, StoreUnavailable) from None
        return [key.removeprefix(self._root) for key in keys]

    def read(self, key: str, version: str | None = None) -> BucketObject:
        _parts(key)
        named = {} if version is None else {"VersionId": version}
        try:
            reply = self._client.get_object(
                Bucket=self._bucket, Key=self._root + key, ChecksumMode="ENABLED", **named
            )
        except ParamValidationError as error:
            raise TypeError(f"botocore refused the call before sending it: {error}") from None
        except (ClientError, BotoCoreError) as error:
            # The S3 store's own mapping, so a missing key reads the same here.
            raise _refusal(key, error, None, BlobMissing) from None
        try:
            data = reply["Body"].read()
        except FlexibleChecksumError as error:
            raise ChecksumMismatch(key, str(error)) from None
        except BotoCoreError as error:
            raise StoreUnavailable(key, f"no answer: {error}") from None
        checksum = reply.get("ChecksumSHA256")
        sha = None
        if checksum and reply.get("ChecksumType", "FULL_OBJECT") == "FULL_OBJECT":
            sha = base64.b64decode(checksum).hex()
        held = reply.get("VersionId")
        if held in (None, "", "null"):
            held = None
        return BucketObject(key=key, data=data, version=held, sha256=sha)


def _parts(key: str) -> tuple[str, ...]:
    """The key's path parts, refusing one that could climb out of the bucket's root."""
    parts = PurePosixPath(key).parts
    if not key or key.startswith("/") or any(p in ("..", ".") for p in parts) or "\\" in key:
        raise ValueError(f"{key!r} is not a key in the bucket")
    return parts


def _raise(error: OSError) -> None:
    """Let a walk stop at a folder it cannot read. A folder that is not there holds nothing."""
    if not isinstance(error, FileNotFoundError | NotADirectoryError):
        raise error
