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
- 404 NoSuchKey is `BlobMissing`.
- BadDigest, or a checksum S3 says is invalid, is `ChecksumMismatch`.
- 409 ConditionalRequestConflict, a conditional write racing another, is
  `StoreUnavailable`, so a create is tried again and then meets 412.

A retried create whose first try landed gets 412, and the contract then finds
the same SHA-256 at the key, which is how a lost reply costs nothing.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC
from typing import Any, TypeVar

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from ithildinruntime.core import (
    BlobMissing,
    BlobRef,
    ChecksumMismatch,
    KeyExists,
    StaleTag,
    StoredBlob,
    StoreError,
    StoreUnavailable,
    sha256_of,
)

__all__ = ["S3Store"]

R = TypeVar("R")

_EXISTS = {"PreconditionFailed", "412"}
_MISSING = {"NoSuchKey", "404", "NotFound"}
_DIGEST = {"BadDigest", "XAmzContentSHA256Mismatch", "InvalidDigest"}


class S3Store:
    """A `BlobStore` on one S3 bucket. `client` is a boto3 S3 client."""

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def connect(
        cls,
        bucket: str,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        timeout: float = 10.0,
    ) -> S3Store:
        """A store on `bucket`, with botocore's standard retries and a timeout on each call."""
        config = Config(
            retries={"mode": "standard", "max_attempts": 3},
            connect_timeout=timeout,
            read_timeout=timeout,
        )
        client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url, config=config)
        return cls(client, bucket)

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
            raise StoreUnavailable(key, str(error)) from None

    def head(self, key: str) -> StoredBlob:
        reply = self._call(
            key,
            lambda: self._client.head_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED"),
        )
        checksum = reply.get("ChecksumSHA256")
        if checksum and reply.get("ChecksumType", "FULL_OBJECT") == "FULL_OBJECT":
            sha = base64.b64decode(checksum).hex()
        else:
            # A blob stored without a SHA-256 checksum, which no create here
            # writes, is hashed from its bytes.
            sha = sha256_of(self.get(key))
        stored_at = reply["LastModified"].astimezone(UTC)
        return StoredBlob(BlobRef(key, sha), reply["ETag"], stored_at)

    def list(self, prefix: str, after: str | None) -> list[str]:
        """Every key that starts with `prefix` and sorts after `after`, in S3's order."""
        pages = self._client.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=prefix, **({"StartAfter": after} if after else {})
        )
        try:
            return [item["Key"] for page in pages for item in page.get("Contents", ())]
        except (ClientError, BotoCoreError) as error:
            raise _refusal(prefix, error, None) from None

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

    def _call(
        self,
        key: str,
        call: Callable[[], R],
        on_precondition: type[StoreError] | None = None,
    ) -> R:
        try:
            return call()
        except ParamValidationError:
            # A call botocore refuses before sending is a bug here, never an outage.
            raise
        except (ClientError, BotoCoreError) as error:
            raise _refusal(key, error, on_precondition) from None


def _refusal(key: str, error: Exception, on_precondition: type[StoreError] | None) -> StoreError:
    if not isinstance(error, ClientError):
        return StoreUnavailable(key, f"no answer: {error}")
    detail = error.response.get("Error", {})
    code = str(detail.get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    message = str(detail.get("Message", ""))
    answer = f"{status} {code}: {message}".strip()
    if on_precondition is not None and (code in _EXISTS or status == 412):
        return on_precondition(key, answer)
    if code in _MISSING or status == 404:
        return BlobMissing(key, answer)
    if code in _DIGEST or (code == "InvalidRequest" and "checksum" in message.lower()):
        return ChecksumMismatch(key, answer)
    return StoreUnavailable(key, answer)


def _b64(sha256: str) -> str:
    return base64.b64encode(bytes.fromhex(sha256)).decode("ascii")
