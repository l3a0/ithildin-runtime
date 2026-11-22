"""The read-only bucket's S3 adapter against the test environment's versioned bucket.

These cases run only in ci.yml's `aws` job, on a push to main, with the test
role. moto returns no checksum on a read, so this is where the S3 bucket is
shown reading S3's own version id and whole-object SHA-256 for an object
stored the way marketlake's upload stores one. The role already writes and
reads under `runs/ci/`, and the bucket keeps versions, so the run needs no new
grant. The bucket is create-only, so a new version of one key cannot be made
here, and tests/test_bucket_suite.py shows that case on moto.

Each key carries the run's own id, `ITHILDIN_TEST_RUN`, and sits under
`lake/`, which no route in infra/test/routes.json matches.
"""

from __future__ import annotations

import base64
import hashlib
import os

import boto3
import pytest
from botocore.config import Config

from ithildinruntime.adapters import S3Bucket
from ithildinruntime.core import BlobMissing

pytestmark = pytest.mark.aws

BUCKET = "ithildin-runtime-test"
_CONFIG = Config(region_name="us-east-1", retries={"mode": "standard", "total_max_attempts": 3})


@pytest.fixture(scope="module")
def run() -> str:
    value = os.environ.get("ITHILDIN_TEST_RUN", "")
    if not value:
        pytest.fail("ITHILDIN_TEST_RUN is empty. ci.yml's aws job sets it.")
    return value


@pytest.fixture(scope="module")
def s3():
    return boto3.client("s3", config=_CONFIG)


def test_the_s3_bucket_reads_s3s_version_and_sha256(s3, run) -> None:
    key = f"runs/ci/{run}/lake/bars/ticker=GLD/freq=1d/date=2012-04-09.parquet"
    data = b"a partition, stored as marketlake's upload stores one"
    digest = hashlib.sha256(data)
    reply = s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=data,
        ChecksumSHA256=base64.b64encode(digest.digest()).decode("ascii"),
        IfNoneMatch="*",
    )
    got = S3Bucket(s3, BUCKET).read(key)
    assert got.data == data
    assert got.sha256 == digest.hexdigest()
    assert got.version == reply["VersionId"]


def test_a_missing_key_reads_as_blob_missing(s3, run) -> None:
    with pytest.raises(BlobMissing):
        S3Bucket(s3, BUCKET).read(f"runs/ci/{run}/lake/manifest.jsonl")
