"""The two read-only buckets marketlake's lake is read through: a folder, and S3.

`core.Bucket` is the port. It reads an object's bytes with the bucket's
version and stored SHA-256, and holds no call that writes, because the data
worker's grant on marketlake's bucket allows only reads.

1. **The folder bucket** serves a directory laid out as marketlake's lake, as
   a backtest on a laptop and CI read it. A folder keeps no versions and
   stores no checksum, so it reports the SHA-256 of the bytes it read as
   both. A run over the same folder therefore names the same versions every
   time, and the check of bytes against the stored SHA-256 holds by
   construction here, which the S3 bucket's suite run covers.
2. **The S3 bucket** reads with checksum mode on, so botocore checks the bytes
   against the SHA-256 S3 stored with them, and reports S3's version id. An
   object stored with no whole-object SHA-256, or in a bucket without
   versioning, reports `None` for that field, which the mirror refuses. Its
   refusals go through the S3 store's own mapping, so a missing key reads as
   `BlobMissing` here as it does there, and a bytes check botocore fails reads
   as `StoreUnavailable`.
"""

from __future__ import annotations

import base64
from pathlib import Path, PurePosixPath
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from ithildinruntime.core import BlobMissing, BucketObject, StoreUnavailable, sha256_of

from .s3 import _refusal

__all__ = ["FolderBucket", "S3Bucket"]


class FolderBucket:
    """A `Bucket` over a directory that holds marketlake's lake."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def read(self, key: str) -> BucketObject:
        path = self._root.joinpath(*_parts(key))
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise BlobMissing(key, "no such file in the folder") from None
        except OSError as error:
            raise StoreUnavailable(key, f"{type(error).__name__}: {error}") from None
        sha = sha256_of(data)
        return BucketObject(key=key, data=data, version=sha, sha256=sha)


class S3Bucket:
    """A `Bucket` over one S3 bucket. `client` is a boto3 S3 client.

    A read is safe to repeat, so unlike the S3 store this takes a client that
    resends a call botocore's retry mode allows.
    """

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def read(self, key: str) -> BucketObject:
        _parts(key)
        try:
            reply = self._client.get_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED")
        except ParamValidationError as error:
            raise TypeError(f"botocore refused the call before sending it: {error}") from None
        except (ClientError, BotoCoreError) as error:
            # The S3 store's own mapping, so a missing key reads the same here.
            raise _refusal(key, error, None, BlobMissing) from None
        try:
            data = reply["Body"].read()
        except BotoCoreError as error:
            raise StoreUnavailable(key, f"no answer: {error}") from None
        checksum = reply.get("ChecksumSHA256")
        sha = None
        if checksum and reply.get("ChecksumType", "FULL_OBJECT") == "FULL_OBJECT":
            sha = base64.b64decode(checksum).hex()
        version = reply.get("VersionId")
        if version in (None, "", "null"):
            version = None
        return BucketObject(key=key, data=data, version=version, sha256=sha)


def _parts(key: str) -> tuple[str, ...]:
    """The key's path parts, refusing one that could climb out of the bucket's root."""
    parts = PurePosixPath(key).parts
    if not key or key.startswith("/") or any(p in ("..", ".") for p in parts) or "\\" in key:
        raise ValueError(f"{key!r} is not a key in the bucket")
    return parts
