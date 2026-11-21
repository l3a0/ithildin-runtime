"""What a queue hands a worker, and the routes that decide which queue hears a write.

docs/design.md's `### How a component learns of a change` holds the rules. S3
announces every blob it stores, EventBridge copies each announcement to the
queue of every reader whose route matches it, and a worker treats the event
only as a reason to wake. The memory-backed folder plays S3's part from the
same routes in a backtest and in CI, so both match a key here, in one place.

An event's body is S3's own announcement as EventBridge sends it. The folder
writes the same shape, with only the fields a worker reads, so one parser
reads both.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "Event",
    "EventKind",
    "QueueUnavailable",
    "Route",
    "event_body",
    "parse_event",
]


class EventKind(StrEnum):
    """What an event says happened to its key, named as EventBridge's `detail-type` names it."""

    CREATED = "Object Created"
    REMOVED = "Object Deleted"
    # A body that is no S3 event about an object, which every worker drops.
    OTHER = "other"


@dataclass(frozen=True)
class Event:
    """One message a queue hands a worker.

    `handle` is the queue's own name for the message, which `done`, `extend`
    and `release` pass back, such as SQS's receipt handle. An event of kind
    `OTHER` names no bucket and no key.
    """

    bucket: str
    key: str
    kind: EventKind
    etag: str | None
    sequencer: str | None
    handle: str


class QueueUnavailable(Exception):
    """A queue that refused a call or did not answer. `answer` is what it said."""

    def __init__(self, answer: str) -> None:
        super().__init__(answer)
        self.answer = answer


_READER = re.compile(r"[a-z][a-z0-9-]*")


@dataclass(frozen=True)
class Route:
    """Which reader hears which events: a kind, and a key pattern.

    `key` is EventBridge's `wildcard` form, where `*` matches any run of
    characters, `/` included, and every other character matches itself. No key
    builder writes a `*` or a `\\`, so a pattern holds neither as a literal.
    """

    reader: str
    kind: EventKind
    key: str

    def __post_init__(self) -> None:
        if not isinstance(self.reader, str) or not _READER.fullmatch(self.reader):
            raise ValueError(f"reader {self.reader!r} cannot name a queue")
        kind = EventKind(self.kind)
        if kind is EventKind.OTHER:
            raise ValueError("a route hears created or removed blobs, never anything else")
        object.__setattr__(self, "kind", kind)
        if not isinstance(self.key, str) or not self.key or "\\" in self.key:
            raise ValueError(f"key pattern {self.key!r} is not one a route takes")
        if "**" in self.key:
            raise ValueError(f"key pattern {self.key!r} repeats a wildcard")

    def matches(self, kind: EventKind, key: str) -> bool:
        """Whether an event of `kind` on `key` reaches this route's reader."""
        return kind is self.kind and _pattern(self.key).fullmatch(key) is not None

    @property
    def name(self) -> str:
        """The rule's name, which changes whenever its pattern or its target changes.

        design.md's `### How a component learns of a change` keeps a rule's name
        only while its pattern and target stay the same, so a later release adds
        a rule rather than narrowing one.
        """
        digest = hashlib.sha256(f"{self.reader}\n{self.kind}\n{self.key}".encode()).hexdigest()
        return f"{self.reader}-{digest[:12]}"


def _pattern(wildcard: str) -> re.Pattern[str]:
    return re.compile(".*".join(re.escape(part) for part in wildcard.split("*")), re.DOTALL)


def event_body(bucket: str, key: str, kind: EventKind, etag: str, sequencer: str) -> bytes:
    """The announcement the folder sends, in the shape EventBridge sends S3's."""
    detail = {
        "bucket": {"name": bucket},
        "object": {"key": key, "etag": etag, "sequencer": sequencer},
    }
    body = {"source": "aws.s3", "detail-type": str(EventKind(kind)), "detail": detail}
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")


def parse_event(body: bytes | str, handle: str) -> Event:
    """Read an announcement, or an event of kind `OTHER` for any body that is not one.

    S3 names the tag `etag` in its EventBridge events, and moto names it
    `eTag`, so both are read.
    """
    try:
        value = json.loads(body)
        detail = value["detail"]
        kind = EventKind(value["detail-type"])
        if value["source"] != "aws.s3" or kind is EventKind.OTHER:
            raise ValueError(value["source"])
        bucket = detail["bucket"]["name"]
        found = detail["object"]
        key = found["key"]
        if not isinstance(bucket, str) or not isinstance(key, str):
            raise ValueError(key)
        etag = found.get("etag", found.get("eTag"))
        sequencer = found.get("sequencer")
    except (ValueError, TypeError, KeyError, AttributeError):
        return Event("", "", EventKind.OTHER, None, None, handle)
    return Event(
        bucket,
        key,
        kind,
        etag if isinstance(etag, str) else None,
        sequencer if isinstance(sequencer, str) else None,
        handle,
    )
