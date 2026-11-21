"""The test environment's smoke check: the bucket and routes production uses, in AWS.

These cases run only in ci.yml's `aws` job, on a push to main, with the test role. They
show that the test bucket carries the refusals the store suite relies on, that an
object's event reaches its route's queue through EventBridge, and that the test role
can do nothing else. #78 holds the reasoning and the grants, and
infra/test/bootstrap/roles.tf the role.

Every key carries the run's own id, `ITHILDIN_TEST_RUN`, which is `GITHUB_RUN_ID` and
`GITHUB_RUN_ATTEMPT` joined by a hyphen, because the bucket is create-only and a rerun
must never meet its own earlier keys.

A refusal case fails when an Allow for its action is added to the role. Removing a Deny
row cannot make it fail, because no Allow stands behind it, so infra/test/bootstrap's
tofu test is what pins the Deny rows.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

pytestmark = pytest.mark.aws

REGION = "us-east-1"
BUCKET = "ithildin-runtime-test"
QUEUE_PREFIX = "ithildin-test"
ROUTES = json.loads((Path(__file__).parents[2] / "infra" / "test" / "routes.json").read_text())
SMOKE = "smoke"

# How long an event may take to reach its queue before the case fails.
DELIVERY_SECONDS = 60

_CONFIG = Config(region_name=REGION, retries={"mode": "standard", "total_max_attempts": 3})


@pytest.fixture(scope="module")
def run() -> str:
    value = os.environ.get("ITHILDIN_TEST_RUN", "")
    if not value:
        pytest.fail("ITHILDIN_TEST_RUN is empty. ci.yml's aws job sets it.")
    return value


@pytest.fixture(scope="module")
def s3():
    return boto3.client("s3", config=_CONFIG)


@pytest.fixture(scope="module")
def sqs():
    return boto3.client("sqs", config=_CONFIG)


def _key(run: str, case: str) -> str:
    return f"runs/ci/{run}/{SMOKE}/{case}.json"


def _code(error: ClientError) -> str:
    return error.response["Error"]["Code"]


def _status(error: ClientError) -> int:
    return error.response["ResponseMetadata"]["HTTPStatusCode"]


def _queue_url(sqs, name: str) -> str:
    return sqs.get_queue_url(QueueName=name)["QueueUrl"]


def _keys_on(sqs, name: str, wait: float) -> list[str]:
    """Receive from a queue until `wait` seconds pass, deleting what arrives."""
    url = _queue_url(sqs, name)
    keys: list[str] = []
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        answer = sqs.receive_message(
            QueueUrl=url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=min(20, max(1, int(deadline - time.monotonic()))),
        )
        for message in answer.get("Messages", []):
            event = json.loads(message["Body"])
            keys.append(event["detail"]["object"]["key"])
            sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
        if keys:
            return keys
    return keys


def test_a_write_without_if_none_match_is_refused(s3, run):
    with pytest.raises(ClientError) as refused:
        s3.put_object(Bucket=BUCKET, Key=_key(run, "no-header"), Body=b"{}")
    assert _status(refused.value) == 403


def test_a_create_lands_once_and_its_event_reaches_only_its_route(s3, sqs, run):
    key = _key(run, "create")
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"{}", IfNoneMatch="*")

    with pytest.raises(ClientError) as second:
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"{}", IfNoneMatch="*")
    assert _status(second.value) == 412

    delivered = _keys_on(sqs, f"{QUEUE_PREFIX}-{SMOKE}", DELIVERY_SECONDS)
    assert key in delivered, (
        f"The {SMOKE} route delivered nothing for {key} in {DELIVERY_SECONDS} seconds. "
        "If this merge changed infra/test/routes.json, infra.yml's apply may not have finished."
    )

    for route in ROUTES:
        if route["reader"] != SMOKE:
            assert key not in _keys_on(sqs, f"{QUEUE_PREFIX}-{route['reader']}", 1)


def test_a_retention_date_within_the_cap_lands_with_a_checksum(s3, run):
    s3.put_object(
        Bucket=BUCKET,
        Key=_key(run, "retention-within-cap"),
        Body=b"{}",
        IfNoneMatch="*",
        ObjectLockMode="GOVERNANCE",
        ObjectLockRetainUntilDate=datetime.now(UTC) + timedelta(hours=12),
        ChecksumAlgorithm="SHA256",
    )


def test_a_retention_date_past_the_cap_is_refused(s3, run):
    with pytest.raises(ClientError) as refused:
        s3.put_object(
            Bucket=BUCKET,
            Key=_key(run, "retention-past-cap"),
            Body=b"{}",
            IfNoneMatch="*",
            ObjectLockMode="GOVERNANCE",
            ObjectLockRetainUntilDate=datetime.now(UTC) + timedelta(days=2),
            ChecksumAlgorithm="SHA256",
        )
    assert _status(refused.value) == 403


def test_a_key_never_written_answers_404(s3, run):
    with pytest.raises(ClientError) as missing:
        s3.get_object(Bucket=BUCKET, Key=_key(run, "never-written"))
    assert _status(missing.value) == 404


def test_the_test_role_is_refused_an_ssm_read():
    ssm = boto3.client("ssm", config=_CONFIG)
    with pytest.raises(ClientError) as refused:
        ssm.get_parameter(Name="/ithildin/test/broker/key")
    assert _code(refused.value) == "AccessDeniedException"


def test_the_test_role_is_refused_an_ec2_describe():
    ec2 = boto3.client("ec2", config=_CONFIG)
    with pytest.raises(ClientError) as refused:
        ec2.describe_instances()
    assert _code(refused.value) == "UnauthorizedOperation"


def test_the_test_role_is_refused_an_eventbridge_list():
    events = boto3.client("events", config=_CONFIG)
    with pytest.raises(ClientError) as refused:
        events.list_rules()
    assert _code(refused.value) == "AccessDeniedException"


def test_the_test_role_is_refused_an_s3_delete(s3, run):
    key = _key(run, "delete")
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"{}", IfNoneMatch="*")
    with pytest.raises(ClientError) as refused:
        s3.delete_object(Bucket=BUCKET, Key=key)
    assert _status(refused.value) == 403
