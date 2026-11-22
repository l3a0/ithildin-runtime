"""The test environment's smoke check: the bucket and routes production uses, in AWS.

These cases run only in ci.yml's `aws` job, on a push to main, with the test role. They
show that the test bucket carries the refusals the store suite relies on, that an
object's event reaches its route's queue through EventBridge, and that the test role
can do nothing else. #78 holds the reasoning and the grants, and
infra/test/bootstrap/roles.tf the role.

Every key carries the run's own id, `ITHILDIN_TEST_RUN`, which is `GITHUB_RUN_ID` and
`GITHUB_RUN_ATTEMPT` joined by a hyphen, because the bucket is create-only and a rerun
must never meet its own earlier keys.

What fails when what. The refusal cases for an SSM read, an EC2 describe, an
EventBridge list and an S3 delete each sit under an explicit Deny, and an explicit Deny
overrides any Allow. So neither adding an Allow nor removing one Deny makes them fail.
infra/test/bootstrap's tofu test pins the role's inline policy exactly, and the
SendMessage case, the one refusal with no Deny behind it, fails when a send is granted
from any policy. Likewise two bucket statements refuse a write without If-None-Match,
so the case for it shows the deployed bucket refuses the write, and infra/test's tofu
test pins each statement.
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

from tests.support.aws import DELIVERY_SECONDS, REGION, TEST_BUCKET, TEST_QUEUE_PREFIX

pytestmark = pytest.mark.aws

BUCKET = TEST_BUCKET
QUEUE_PREFIX = TEST_QUEUE_PREFIX
ROUTES = json.loads((Path(__file__).parents[2] / "infra" / "test" / "routes.json").read_text())
READERS = [queue["reader"] for queue in ROUTES["queues"]]
SMOKE = "smoke"

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


def _receive(sqs, url: str, wait: int) -> list[str]:
    """One receive from a queue, deleting what arrives, as the keys its events name."""
    answer = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=wait)
    keys = []
    for message in answer.get("Messages", []):
        event = json.loads(message["Body"])
        keys.append(event["detail"]["object"]["key"])
        sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
    return keys


def _keys_on(sqs, name: str, wait: float, until: str | None = None) -> list[str]:
    """Receive from a queue, deleting what arrives, until `until` arrives or `wait` passes."""
    url = _queue_url(sqs, name)
    keys: list[str] = []
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        keys += _receive(sqs, url, min(20, max(1, int(deadline - time.monotonic()))))
        if until is not None and until in keys:
            return keys
    return keys


def _keys_on_each(sqs, names: list[str], wait: float) -> dict[str, list[str]]:
    """Receive from several queues in turn for one window of `wait` seconds, by queue.

    Each receive waits a second, so every queue is read about once in each pass, and
    the window costs `wait` seconds however many queues there are.
    """
    urls = {name: _queue_url(sqs, name) for name in names}
    keys: dict[str, list[str]] = {name: [] for name in names}
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        for name, url in urls.items():
            keys[name] += _receive(sqs, url, 1)
    return keys


def test_a_write_without_if_none_match_is_refused(s3, run):
    with pytest.raises(ClientError) as refused:
        s3.put_object(Bucket=BUCKET, Key=_key(run, "no-header"), Body=b"{}")
    assert _status(refused.value) == 403


def test_a_create_lands_once_and_its_event_reaches_its_route(s3, sqs, run):
    key = _key(run, "create")
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"{}", IfNoneMatch="*")

    with pytest.raises(ClientError) as second:
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"{}", IfNoneMatch="*")
    assert _status(second.value) == 412

    delivered = _keys_on(sqs, f"{QUEUE_PREFIX}-{SMOKE}", DELIVERY_SECONDS, until=key)
    assert key in delivered, (
        f"The {SMOKE} route delivered nothing for {key} in {DELIVERY_SECONDS} seconds. "
        "If this merge changed infra/test/routes.json, infra.yml's apply may not have finished."
    )


OTHER_READERS = [reader for reader in READERS if reader != SMOKE]


@pytest.mark.skipif(
    not OTHER_READERS,
    reason="infra/test/routes.json names no reader but the smoke route's",
)
def test_a_smoke_write_reaches_no_other_route(s3, sqs, run):
    key = _key(run, "no-other-route")
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"{}", IfNoneMatch="*")
    assert key in _keys_on(sqs, f"{QUEUE_PREFIX}-{SMOKE}", DELIVERY_SECONDS, until=key)
    # The smoke queue's event has arrived, so another route's would have had as long.
    # Every other queue is read for one more window, together rather than in turn.
    others = _keys_on_each(sqs, [f"{QUEUE_PREFIX}-{r}" for r in OTHER_READERS], DELIVERY_SECONDS)
    reached = sorted(name for name, keys in others.items() if key in keys)
    assert not reached, f"{key} reached {', '.join(reached)}, which no route of theirs names."


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


def test_a_retention_date_just_under_the_cap_lands(s3, run):
    # The policy refuses retention that exceeds one day, so a date 23 hours out, under
    # that, must land. How S3 counts a date between one and two days is not pinned.
    s3.put_object(
        Bucket=BUCKET,
        Key=_key(run, "retention-under-cap"),
        Body=b"{}",
        IfNoneMatch="*",
        ObjectLockMode="GOVERNANCE",
        ObjectLockRetainUntilDate=datetime.now(UTC) + timedelta(hours=23),
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


def test_the_test_role_is_refused_a_send_to_a_queue(sqs):
    with pytest.raises(ClientError) as refused:
        sqs.send_message(QueueUrl=_queue_url(sqs, f"{QUEUE_PREFIX}-{SMOKE}"), MessageBody="{}")
    assert _status(refused.value) == 403


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
