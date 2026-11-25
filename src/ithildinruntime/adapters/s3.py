"""The S3 store, which shadow, paper and live write through.

Every create sends `If-None-Match: *`, so S3 refuses it when the key exists,
and every rewrite sends `If-Match` on the tag it names. Every write sends its
SHA-256 as the upload's checksum, so S3 checks the bytes before it stores
them, which an Object Lock bucket also requires of a write that sets a
retention date.

S3's answers become the refusals `core` names, the same ones the folder store
gives. Any answer not listed here, and no answer at all, is
`StoreUnavailable`, which the storage contract retries until its deadline.

- 412 PreconditionFailed is `KeyExists` on a create and `StaleTag` on a
  rewrite.
- 404 NoSuchKey is `BlobMissing` on a read or a rewrite, and so is 404
  NoSuchVersion, which only the read-only bucket can meet, because only its
  read names a version. Any other 404, such as NoSuchBucket, and any 404 on a
  create or a listing, is `StoreUnavailable`, because a missing bucket says
  nothing about the key. A lookup's reply carries no code, so `head` reads
  every 404 as `BlobMissing`, a missing bucket included, and the storage
  contract retries a lookup that follows a refused create rather than trusting
  it.
- BadDigest, or a checksum S3 says is invalid, is `ChecksumMismatch`.
- 409 ConditionalRequestConflict, a conditional write racing another, is
  `Conflict`, a kind of `StoreUnavailable`, so a create is tried again and
  then meets 412.

The client is the caller's to build, and the store refuses one that resends.
The storage contract retries on its own deadline, so a client with botocore's
`total_max_attempts` of 1 sends each call once, which keeps a 409's meaning
that nothing landed and each try's worst case to one call. A client that resends a rewrite
whose first try landed reads its own write as `StaleTag`. The runtime builds
its clients when its entry points exist, and #10 holds how a lease reads that
answer.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC
from typing import Any, TypeVar

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from ithildinruntime.core import (
    BlobMissing,
    BlobRef,
    ChecksumMismatch,
    Conflict,
    KeyExists,
    StaleTag,
    StoredBlob,
    StoreError,
    StoreUnavailable,
    sha256_of,
)

__all__ = ["S3Store"]

R = TypeVar("R")

_EXISTS = {"PreconditionFailed"}
_MISSING_KEY = {"NoSuchKey", "NoSuchVersion", "404", "NotFound"}
_DIGEST = {"BadDigest"}


class S3Store:
    """A `BlobStore` on one S3 bucket. `client` is a boto3 S3 client."""

    def __init__(self, client: Any, bucket: str) -> None:
        retries = client.meta.config.retries or {}
        if retries.get("total_max_attempts") != 1:
            raise ValueError(
                "an S3 client for the store sends each call once: build it with "
                "botocore's retries set to total_max_attempts of 1"
            )
        self._client = client
        self._bucket = bucket

    def create(self, key: str, data: bytes, sha256: str) -> str:
        reply = self._call(
            key,
            lambda: self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ChecksumSHA256=_b64(sha256),
                IfNoneMatch="*",
            ),
            on_precondition=KeyExists,
            on_missing=StoreUnavailable,
        )
        return reply["ETag"]

    def get(self, key: str) -> bytes:
        reply = self._call(
            key,
            lambda: self._client.get_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED"),
        )
        try:
            return reply["Body"].read()
        except BotoCoreError as error:
            raise StoreUnavailable(key, f"no answer: {error}") from None

    def head(self, key: str) -> StoredBlob:
        reply = self._call(
            key,
            lambda: self._client.head_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED"),
        )
        checksum = reply.get("ChecksumSHA256")
        if checksum and reply.get("ChecksumType", "FULL_OBJECT") == "FULL_OBJECT":
            sha = base64.b64decode(checksum).hex()
        else:
            # A blob stored without a whole-object SHA-256, which no create
            # here writes, is hashed from its bytes.
            sha = sha256_of(self.get(key))
        stored_at = reply["LastModified"].astimezone(UTC)
        return StoredBlob(BlobRef(key, sha), reply["ETag"], stored_at)

    def list(self, prefix: str, after: str | None) -> list[str]:
        """Every key that starts with `prefix` and sorts after `after`, in S3's order."""
        pages = self._client.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=prefix, **({"StartAfter": after} if after else {})
        )
        return self._call(
            prefix,
            lambda: [item["Key"] for page in pages for item in page.get("Contents", ())],
            on_missing=StoreUnavailable,
        )

    def replace_if_match(self, key: str, data: bytes, sha256: str, etag: str) -> str:
        reply = self._call(
            key,
            lambda: self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ChecksumSHA256=_b64(sha256),
                IfMatch=etag,
            ),
            on_precondition=StaleTag,
        )
        return reply["ETag"]

    def announce(self, key: str) -> None:
        """S3 announced the blob when it stored it, so a second announcement is skipped."""

    def _call(
        self,
        key: str,
        call: Callable[[], R],
        on_precondition: type[StoreError] | None = None,
        on_missing: type[StoreError] = BlobMissing,
    ) -> R:
        try:
            return call()
        except ParamValidationError as error:
            # A call botocore refuses before sending is a bug here, never an
            # outage, so it is no store refusal to retry. It leaves the adapter
            # as Python's own error, so no botocore type reaches a caller.
            raise TypeError(f"botocore refused the call before sending it: {error}") from None
        except (ClientError, BotoCoreError) as error:
            raise _refusal(key, error, on_precondition, on_missing) from None


def _refusal(
    key: str,
    error: Exception,
    on_precondition: type[StoreError] | None,
    on_missing: type[StoreError],
) -> StoreError:
    if not isinstance(error, ClientError):
        return StoreUnavailable(key, f"no answer: {error}")
    detail = error.response.get("Error", {})
    code = str(detail.get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    message = str(detail.get("Message", ""))
    answer = f"{status} {code}: {message}".strip()
    if on_precondition is not None and (code in _EXISTS or status == 412):
        return on_precondition(key, answer)
    if code in _MISSING_KEY:
        return on_missing(key, answer)
    if code == "ConditionalRequestConflict" or status == 409:
        return Conflict(key, answer)
    if code in _DIGEST or (code == "InvalidRequest" and "checksum" in message.lower()):
        return ChecksumMismatch(key, answer)
    return StoreUnavailable(key, answer)


def _b64(sha256: str) -> str:
    return base64.b64encode(bytes.fromhex(sha256)).decode("ascii")
