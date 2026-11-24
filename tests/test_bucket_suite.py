"""The read-only bucket suite: one set of cases, run against the folder and S3 on moto.

`core.Bucket` reads an object's bytes with the bucket's version and stored
SHA-256, and a missing key reads as `BlobMissing` from every adapter, so the
mirror above it never branches on the mode. tests/aws/test_bucket_read.py runs
the S3 cases against the test environment's versioned bucket too.
"""

from __future__ import annotations

import base64
import hashlib
import io
import itertools

import boto3
import pytest
from botocore.config import Config

from ithildinruntime.core import BlobMissing, StoreUnavailable
from tests.support.buckets import BucketSpec
from tests.support.stores import EMULATOR_KEYS, REGION

_names = itertools.count()


def _client(endpoint: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=REGION,
        config=Config(retries={"mode": "standard", "total_max_attempts": 2}),
        **EMULATOR_KEYS,
    )


@pytest.fixture(params=["folder", "s3"])
def bucket(request: pytest.FixtureRequest, tmp_path) -> BucketSpec:
    if request.param == "folder":
        return BucketSpec("folder", root=tmp_path / "bucket")
    client = _client(request.getfixturevalue("emulator"))
    name = f"bucket-suite-{next(_names)}"
    client.create_bucket(Bucket=name)
    client.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    return BucketSpec("s3", client=client, name=name)


def check_reads_bytes_version_and_sha256(spec: BucketSpec) -> None:
    key = spec.key("bars/ticker=GLD/freq=1d/date=2012-04-09.parquet")
    spec.put(key, b"a partition")
    got = spec.open().read(key)
    assert (got.key, got.data) == (key, b"a partition")
    assert got.sha256 == hashlib.sha256(b"a partition").hexdigest()
    assert isinstance(got.version, str) and got.version


def check_a_new_version_reads_as_another_version(spec: BucketSpec) -> None:
    key = spec.key("quarantine.jsonl")
    spec.put(key, b"one\n")
    first = spec.open().read(key)
    spec.put(key, b"one\ntwo\n")
    second = spec.open().read(key)
    assert second.data == b"one\ntwo\n"
    assert second.version != first.version


def check_a_missing_key_reads_as_blob_missing(spec: BucketSpec) -> None:
    with pytest.raises(BlobMissing):
        spec.open().read(spec.key("manifest.jsonl"))


def test_the_bucket_reads_an_objects_bytes_version_and_sha256(
    bucket: BucketSpec, request: pytest.FixtureRequest
) -> None:
    if bucket.kind == "s3":
        # moto stores the checksum and returns none on a read. The stubbed
        # answers below show the adapter reading S3's own, and the test
        # environment's bucket shows S3 giving it.
        request.applymarker(pytest.mark.xfail(strict=True, reason="moto returns no checksum"))
    check_reads_bytes_version_and_sha256(bucket)


def test_a_new_version_of_a_key_reads_as_another_version(bucket: BucketSpec) -> None:
    check_a_new_version_reads_as_another_version(bucket)


def test_a_missing_key_reads_as_blob_missing(bucket: BucketSpec) -> None:
    check_a_missing_key_reads_as_blob_missing(bucket)


@pytest.mark.parametrize("key", ["../outside", "/root", "a/../../b", "a\\b", ""])
def test_a_key_that_could_climb_out_of_the_bucket_is_refused(bucket: BucketSpec, key: str) -> None:
    with pytest.raises(ValueError):
        bucket.open().read(key)


def _stubbed(reply: dict):
    from botocore.response import StreamingBody
    from botocore.stub import Stubber

    client = boto3.client(
        "s3", region_name=REGION, endpoint_url="http://stubbed.invalid", **EMULATOR_KEYS
    )
    body = {"Body": StreamingBody(io.BytesIO(b"a partition"), len(b"a partition")), **reply}
    stub = Stubber(client)
    stub.add_response("get_object", body, {"Bucket": "b", "Key": "k", "ChecksumMode": "ENABLED"})
    stub.activate()
    return client


SHA = hashlib.sha256(b"a partition")
B64 = base64.b64encode(SHA.digest()).decode()


@pytest.mark.parametrize(
    ("reply", "version", "sha256"),
    [
        (
            {
                "VersionId": "v1",
                "ChecksumSHA256": __import__("base64").b64encode(SHA.digest()).decode(),
                "ChecksumType": "FULL_OBJECT",
            },
            "v1",
            SHA.hexdigest(),
        ),
        ({"VersionId": "v1", "ChecksumSHA256": "AAAA", "ChecksumType": "COMPOSITE"}, "v1", None),
        ({"VersionId": "null"}, None, None),
        ({}, None, None),
    ],
)
def test_the_s3_bucket_reads_s3s_version_and_whole_object_sha256(
    reply: dict, version: str | None, sha256: str | None
) -> None:
    from ithildinruntime.adapters import S3Bucket

    got = S3Bucket(_stubbed(reply), "b").read("k")
    assert (got.data, got.version, got.sha256) == (b"a partition", version, sha256)


def test_an_object_stored_with_no_sha256_reads_with_none(emulator: str) -> None:
    spec = BucketSpec("s3", client=_client(emulator), name=f"bucket-suite-{next(_names)}")
    spec.client.create_bucket(Bucket=spec.name)
    spec.client.put_bucket_versioning(
        Bucket=spec.name, VersioningConfiguration={"Status": "Enabled"}
    )
    spec.put("manifest.jsonl", b"{}\n", checksum=False)
    assert spec.open().read("manifest.jsonl").sha256 is None


def test_an_unversioned_bucket_reads_with_no_version(emulator: str) -> None:
    spec = BucketSpec("s3", client=_client(emulator), name=f"bucket-suite-{next(_names)}")
    spec.client.create_bucket(Bucket=spec.name)
    spec.put("manifest.jsonl", b"{}\n")
    assert spec.open().read("manifest.jsonl").version is None


def test_a_missing_bucket_reads_as_unavailable_not_as_a_missing_key(emulator: str) -> None:
    spec = BucketSpec("s3", client=_client(emulator), name="no-such-bucket")
    with pytest.raises(StoreUnavailable) as refused:
        spec.open().read("manifest.jsonl")
    assert not isinstance(refused.value, BlobMissing)
