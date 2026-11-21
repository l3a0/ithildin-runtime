"""S3's answers that moto cannot be made to give, through botocore's stubbed replies.

moto checks no SHA-256 and never answers 409, so these show what the S3
adapter does with each answer S3 itself gives. The run against the test
environment's bucket, once #78 builds it, shows S3 giving them.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.stub import Stubber

from ithildinruntime.adapters import S3Store
from ithildinruntime.core import (
    BlobMissing,
    BlobRef,
    ChecksumMismatch,
    KeyExists,
    Retry,
    StageKind,
    StaleTag,
    Storage,
    StoreUnavailable,
    sha256_of,
)
from tests.support.stores import EMULATOR_KEYS, REGION

BUCKET = "stubbed-bucket"
KEY = "runs/ci/2026-11-02/20261102T204000Z/data/snapshot.json"
DATA = b"snapshot"
SHA = sha256_of(DATA)
B64 = base64.b64encode(bytes.fromhex(SHA)).decode()


@pytest.fixture
def stubbed():
    client = boto3.client(
        "s3", region_name=REGION, endpoint_url="http://stubbed.invalid", **EMULATOR_KEYS
    )
    with Stubber(client) as stub:
        yield S3Store(client, BUCKET), stub
        stub.assert_no_pending_responses()


def _create_params() -> dict:
    return {"Bucket": BUCKET, "Key": KEY, "Body": DATA, "ChecksumSHA256": B64, "IfNoneMatch": "*"}


def _rewrite_params() -> dict:
    return {"Bucket": BUCKET, "Key": KEY, "Body": DATA, "ChecksumSHA256": B64, "IfMatch": '"old"'}


CREATE_ANSWERS = [
    (
        "PreconditionFailed",
        412,
        "At least one of the pre-conditions you specified did not hold",
        KeyExists,
    ),
    (
        "ConditionalRequestConflict",
        409,
        "A conflicting conditional operation is in progress",
        StoreUnavailable,
    ),
    (
        "BadDigest",
        400,
        "The SHA256 you specified did not match the calculated checksum.",
        ChecksumMismatch,
    ),
    ("InvalidRequest", 400, "Value for x-amz-checksum-sha256 header is invalid.", ChecksumMismatch),
    ("AccessDenied", 403, "Access Denied", StoreUnavailable),
    ("InternalError", 500, "We encountered an internal error. Please try again.", StoreUnavailable),
]


@pytest.mark.parametrize("code,status,message,refusal", CREATE_ANSWERS, ids=lambda v: str(v))
def test_each_answer_to_a_create_becomes_its_refusal(stubbed, code, status, message, refusal):
    store, stub = stubbed
    stub.add_client_error("put_object", code, message, status, expected_params=_create_params())
    with pytest.raises(refusal) as refused:
        store.create(KEY, DATA, SHA)
    assert refused.value.key == KEY
    assert str(status) in refused.value.answer and code in refused.value.answer


REWRITE_ANSWERS = [
    (
        "PreconditionFailed",
        412,
        "At least one of the pre-conditions you specified did not hold",
        StaleTag,
    ),
    ("NoSuchKey", 404, "The specified key does not exist.", BlobMissing),
    (
        "ConditionalRequestConflict",
        409,
        "A conflicting conditional operation is in progress",
        StoreUnavailable,
    ),
    (
        "BadDigest",
        400,
        "The SHA256 you specified did not match the calculated checksum.",
        ChecksumMismatch,
    ),
]


@pytest.mark.parametrize("code,status,message,refusal", REWRITE_ANSWERS, ids=lambda v: str(v))
def test_each_answer_to_a_rewrite_becomes_its_refusal(stubbed, code, status, message, refusal):
    store, stub = stubbed
    stub.add_client_error("put_object", code, message, status, expected_params=_rewrite_params())
    with pytest.raises(refusal):
        store.replace_if_match(KEY, DATA, SHA, '"old"')


def test_a_create_sends_the_condition_and_the_sha256(stubbed) -> None:
    store, stub = stubbed
    stub.add_response("put_object", {"ETag": '"tag"'}, expected_params=_create_params())
    assert store.create(KEY, DATA, SHA) == '"tag"'


def test_a_rewrite_sends_the_tag_and_the_sha256(stubbed) -> None:
    store, stub = stubbed
    stub.add_response("put_object", {"ETag": '"new"'}, expected_params=_rewrite_params())
    assert store.replace_if_match(KEY, DATA, SHA, '"old"') == '"new"'


def test_a_lookup_reads_the_stored_sha256_tag_and_time(stubbed) -> None:
    store, stub = stubbed
    stored = datetime(2026, 11, 2, 20, 40, 1, tzinfo=UTC)
    stub.add_response(
        "head_object",
        {
            "ETag": '"tag"',
            "LastModified": stored,
            "ChecksumSHA256": B64,
            "ChecksumType": "FULL_OBJECT",
        },
        expected_params={"Bucket": BUCKET, "Key": KEY, "ChecksumMode": "ENABLED"},
    )
    head = store.head(KEY)
    assert head.ref == BlobRef(KEY, SHA)
    assert head.etag == '"tag"' and head.stored_at == stored


def test_a_lookup_hashes_a_blob_stored_without_a_sha256(stubbed) -> None:
    import io

    from botocore.response import StreamingBody

    store, stub = stubbed
    stub.add_response(
        "head_object",
        {"ETag": '"tag"', "LastModified": datetime(2026, 11, 2, tzinfo=UTC)},
        expected_params={"Bucket": BUCKET, "Key": KEY, "ChecksumMode": "ENABLED"},
    )
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(DATA), len(DATA))},
        expected_params={"Bucket": BUCKET, "Key": KEY, "ChecksumMode": "ENABLED"},
    )
    assert store.head(KEY).ref.sha256 == SHA


def test_a_lookup_of_a_missing_key_is_refused_as_missing(stubbed) -> None:
    store, stub = stubbed
    stub.add_client_error("head_object", "404", "Not Found", 404)
    with pytest.raises(BlobMissing):
        store.head(KEY)


def test_a_409_on_a_create_is_retried_until_s3_answers(stubbed) -> None:
    """A create racing another gets 409, then 412 once the other lands, and the hashes match."""
    store, stub = stubbed
    stub.add_client_error("put_object", "ConditionalRequestConflict", "conflict", 409)
    stub.add_client_error("put_object", "PreconditionFailed", "pre-conditions", 412)
    stub.add_response(
        "head_object",
        {"ETag": '"tag"', "LastModified": datetime(2026, 11, 2, tzinfo=UTC), "ChecksumSHA256": B64},
    )
    pauses: list[float] = []
    now = [datetime(2026, 11, 2, 20, 40, tzinfo=UTC)]

    class Timer:
        def now(self):
            return now[0]

        def after(self, seconds):
            raise NotImplementedError

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        now[0] += timedelta(seconds=seconds)

    class Alerts:
        def alarm(self, reason):
            raise AssertionError(reason)

        def heartbeat(self):
            pass

    storage = Storage(
        store,
        timer=Timer(),
        pause=pause,
        retry=Retry(timedelta(seconds=30), 1.0, 8.0, 10),
        alerts=Alerts(),
        owner_step="x",
    )
    stored = storage.put(StageKind.CAPTURING, KEY, DATA)
    assert stored.ref == BlobRef(KEY, SHA) and pauses == [1.0]


def test_a_call_botocore_refuses_before_sending_is_a_bug_not_an_outage() -> None:
    from botocore.exceptions import ParamValidationError

    client = boto3.client(
        "s3", region_name=REGION, endpoint_url="http://stubbed.invalid", **EMULATOR_KEYS
    )
    with pytest.raises(ParamValidationError):
        S3Store(client, BUCKET).create(None, DATA, SHA)  # type: ignore[arg-type]
