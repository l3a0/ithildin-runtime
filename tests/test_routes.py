"""The stage table, the routes generated from it, and the events a queue hands a worker."""

from __future__ import annotations

import json

import boto3
import pytest
from botocore.config import Config

from ithildinruntime.adapters import SqsQueue
from ithildinruntime.core import (
    EventKind,
    Route,
    StageKind,
    event_body,
    parse_event,
    result_key,
)
from ithildinruntime.runtime.loop import Hears, Stage, Table, event_pattern, routes
from tests.support.queues import DEPLOYMENT, FIRST, RUN, TABLE

D = StageKind.DETERMINISTIC


def test_the_routes_follow_each_stages_inputs_and_its_order() -> None:
    found = {(r.reader, r.kind, r.key) for r in routes(TABLE, "ci")}
    assert found == {
        ("alpha", EventKind.CREATED, "runs/ci/*/feed/result.json"),
        ("risk", EventKind.CREATED, "runs/ci/*/feed/result.json"),
        ("book", EventKind.CREATED, "runs/ci/*/alpha/result.json"),
        ("book", EventKind.CREATED, "runs/ci/*/risk/result.json"),
        # book keeps the plan's order, so its instances hear each other's results.
        ("book", EventKind.CREATED, "runs/ci/*/book/result.json"),
        ("book", EventKind.CREATED, "runs/ci/*/stop.json"),
    }


def test_a_route_matches_only_its_own_kind_and_keys() -> None:
    route = Route("alpha", EventKind.CREATED, "runs/ci/*/feed/result.json")
    key = result_key("ci", RUN, FIRST, "feed")
    assert route.matches(EventKind.CREATED, key)
    assert not route.matches(EventKind.REMOVED, key)
    assert not route.matches(EventKind.CREATED, result_key("ci", RUN, FIRST, "alpha"))
    assert not route.matches(EventKind.CREATED, result_key("cd", RUN, FIRST, "feed"))
    assert not route.matches(EventKind.CREATED, key + ".attempt-2")
    # A literal dot or bracket in a pattern matches only itself.
    assert not Route("alpha", EventKind.CREATED, "a.b").matches(EventKind.CREATED, "axb")


def test_a_rules_name_changes_with_its_pattern_or_its_target() -> None:
    one = Route("alpha", EventKind.CREATED, "runs/ci/*/feed/result.json")
    assert one.name == Route("alpha", EventKind.CREATED, one.key).name
    assert one.name.startswith("alpha-")
    assert one.name != Route("risk", EventKind.CREATED, one.key).name
    assert one.name != Route("alpha", EventKind.REMOVED, one.key).name
    assert one.name != Route("alpha", EventKind.CREATED, "runs/ci/*/risk/result.json").name


@pytest.mark.parametrize(
    ("reader", "kind", "key"),
    [
        ("Alpha", EventKind.CREATED, "k"),
        ("alpha", EventKind.OTHER, "k"),
        ("alpha", EventKind.CREATED, ""),
        ("alpha", EventKind.CREATED, "a\\*b"),
        ("alpha", EventKind.CREATED, "a**b"),
    ],
)
def test_a_route_refuses_what_no_rule_could_hold(reader, kind, key) -> None:
    with pytest.raises(ValueError):
        Route(reader, kind, key)


def test_the_rule_pattern_matches_s3_as_the_source_and_the_environments_bucket() -> None:
    route = Route("alpha", EventKind.CREATED, "runs/ci/*/feed/result.json")
    assert event_pattern(route, "ithildin-test") == {
        "source": ["aws.s3"],
        "detail-type": ["Object Created"],
        "detail": {
            "bucket": {"name": ["ithildin-test"]},
            "object": {"key": [{"wildcard": "runs/ci/*/feed/result.json"}]},
        },
    }


@pytest.mark.parametrize(
    ("stages", "message"),
    [
        ((Stage("a", D), Stage("a", D)), "declared twice"),
        ((Stage("a", D, ("b",)),), "no row declares"),
        ((Stage("a", D, ("a",)),), "its own result"),
        ((Stage("a", D, ("b",)), Stage("b", D, ("a",))), "cycle"),
        ((Stage("send", D), Stage("execution/send", D)), "ends with stage"),
    ],
)
def test_a_table_refuses_rows_that_disagree(stages, message) -> None:
    with pytest.raises(ValueError, match=message):
        Table(stages)


def test_a_table_refuses_a_heard_blob_for_a_component_with_no_stage() -> None:
    with pytest.raises(ValueError, match="runs no stage"):
        Table((Stage("a", D),), (Hears("b", EventKind.CREATED, "k"),))


def test_a_stage_name_must_build_a_key() -> None:
    with pytest.raises(ValueError):
        Stage("bad name", D)


def test_a_component_holds_every_stage_named_under_it() -> None:
    table = Table(
        (
            Stage("execution/recover", StageKind.CAPTURING),
            Stage("execution/send", StageKind.CAPTURING, ("execution/recover",), True),
        )
    )
    assert [s.name for s in table.stages_of("execution")] == [
        "execution/recover",
        "execution/send",
    ]
    assert {r.key for r in routes(table, DEPLOYMENT)} == {
        "runs/ci/*/execution/recover/result.json",
        "runs/ci/*/execution/send/result.json",
    }


# Events


def test_an_event_body_reads_back_as_the_event_it_describes() -> None:
    body = event_body("b", "runs/k.json", EventKind.CREATED, "tag", "0001")
    event = parse_event(body, "h")
    assert (event.bucket, event.key, event.kind, event.etag, event.sequencer, event.handle) == (
        "b",
        "runs/k.json",
        EventKind.CREATED,
        "tag",
        "0001",
        "h",
    )


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        json.dumps({"source": "aws.s3", "detail-type": "Object Created"}).encode(),
        json.dumps(
            {
                "source": "my.app",
                "detail-type": "Object Created",
                "detail": {"bucket": {"name": "b"}, "object": {"key": "k"}},
            }
        ).encode(),
        json.dumps(
            {
                "source": "aws.s3",
                "detail-type": "Object Restore Completed",
                "detail": {"bucket": {"name": "b"}, "object": {"key": "k"}},
            }
        ).encode(),
        json.dumps(
            {
                "source": "aws.s3",
                "detail-type": "Object Created",
                "detail": {"bucket": {"name": "b"}, "object": {"key": 7}},
            }
        ).encode(),
    ],
)
def test_a_body_that_is_no_s3_object_event_reads_as_other(body) -> None:
    event = parse_event(body, "h")
    assert (event.kind, event.bucket, event.key) == (EventKind.OTHER, "", "")


def test_s3s_own_etag_spelling_and_motos_both_read() -> None:
    for name in ("etag", "eTag"):
        body = {
            "source": "aws.s3",
            "detail-type": "Object Deleted",
            "detail": {"bucket": {"name": "b"}, "object": {"key": "k", name: "t"}},
        }
        event = parse_event(json.dumps(body), "h")
        assert (event.kind, event.etag, event.sequencer) == (EventKind.REMOVED, "t", None)


# The SQS queue's own refusals


def _sqs(read_timeout: float):
    return boto3.client(
        "sqs",
        region_name="us-east-1",
        endpoint_url="http://127.0.0.1:1",
        aws_access_key_id="emulator",
        aws_secret_access_key="emulator",
        config=Config(read_timeout=read_timeout),
    )


@pytest.mark.parametrize("read_timeout", [20, 60, 31])
def test_the_sqs_queue_refuses_a_read_timeout_far_from_the_wait(read_timeout) -> None:
    with pytest.raises(ValueError, match="read timeout"):
        SqsQueue(_sqs(read_timeout), "u", visibility_timeout=30)


@pytest.mark.parametrize("read_timeout", [21, 22, 30])
def test_the_sqs_queue_takes_a_read_timeout_a_little_over_the_wait(read_timeout) -> None:
    SqsQueue(_sqs(read_timeout), "u", visibility_timeout=30)


@pytest.mark.parametrize(
    "settings", [{"visibility_timeout": 0}, {"visibility_timeout": 30, "wait_seconds": 21}]
)
def test_the_sqs_queue_refuses_limits_sqs_does_not_keep(settings) -> None:
    with pytest.raises(ValueError):
        SqsQueue(_sqs(22), "u", **settings)


def test_an_sqs_receive_asks_for_at_least_one_event() -> None:
    with pytest.raises(ValueError):
        SqsQueue(_sqs(22), "u", visibility_timeout=30).receive(0)


def test_an_sqs_queue_that_does_not_answer_is_unavailable() -> None:
    from ithildinruntime.core import QueueUnavailable

    client = boto3.client(
        "sqs",
        region_name="us-east-1",
        endpoint_url="http://127.0.0.1:1",
        aws_access_key_id="emulator",
        aws_secret_access_key="emulator",
        config=Config(read_timeout=3, connect_timeout=1, retries={"total_max_attempts": 1}),
    )
    with pytest.raises(QueueUnavailable, match="no answer"):
        SqsQueue(client, "http://127.0.0.1:1/q", visibility_timeout=30, wait_seconds=1).receive(1)


def test_an_sqs_call_botocore_refuses_before_sending_leaves_as_pythons_own_error() -> None:
    from botocore.exceptions import BotoCoreError

    queue = SqsQueue(_sqs(22), "u", visibility_timeout=30)
    event = parse_event(b"{}", None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="refused the call") as raised:
        queue.done(event)
    assert not isinstance(raised.value, BotoCoreError) and raised.value.__cause__ is None
