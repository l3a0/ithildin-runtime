"""The table that declares each stage, and the routes generated from it.

docs/design.md's `### Rule 2: two kinds of stage, declared in one place` makes
one table the place each stage's kind and inputs are declared, and `### How a
component learns of a change` turns that table into routes. A stage's readers
are the components whose stages read it, so the table never lists them by
hand.

The table's form lives in `runtime.loop` rather than elsewhere in `runtime`
because the supervisor reads it, and the supervisor may import only `core`,
`journal`, `adapters` and `runtime.loop`. Each worker's entry point adds its
own stage's row, since each stage's inputs belong to its module's issue.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ithildinruntime.core import EventKind, Route, StageKind, result_key

__all__ = ["Hears", "QueueSettings", "Stage", "Table", "event_pattern", "routes", "routes_file"]

# A decision time no plan holds, used only to check a stage's name builds a key.
_ANY_DECISION = datetime(2000, 1, 3, tzinfo=UTC)


@dataclass(frozen=True)
class Stage:
    """One stage: its name, its kind, the stages whose results it reads, and its order.

    `keeps_order` marks a stage that reads a book the decision before changed,
    such as execution's two stages and monitoring's reconcile. Its decision N
    starts only once its own result for decision N−1 is stored.
    """

    name: str
    kind: StageKind
    inputs: tuple[str, ...] = ()
    keeps_order: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", StageKind(self.kind))
        object.__setattr__(self, "inputs", tuple(self.inputs))
        # The key builder refuses a name that cannot be part of a key.
        result_key("table", "2000-01-03", _ANY_DECISION, self.name)

    @property
    def component(self) -> str:
        """The component that runs the stage, the name's first part."""
        return self.name.split("/")[0]


@dataclass(frozen=True)
class Hears:
    """A control blob a component hears beside its inputs, such as the run's stop.

    `key` is a route's wildcard pattern, where `{deployment}` stands for the
    deployment the routes are generated for.
    """

    component: str
    kind: EventKind
    key: str


@dataclass(frozen=True)
class Table:
    """Every stage of a deployment, and the control blobs each component hears."""

    stages: tuple[Stage, ...]
    hears: tuple[Hears, ...] = ()
    _by_name: Mapping[str, Stage] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        stages = tuple(self.stages)
        object.__setattr__(self, "stages", stages)
        object.__setattr__(self, "hears", tuple(self.hears))
        by_name: dict[str, Stage] = {}
        for stage in stages:
            if stage.name in by_name:
                raise ValueError(f"stage {stage.name} is declared twice")
            by_name[stage.name] = stage
        for stage in stages:
            for other in by_name:
                # A route matches a stage's result by its name at the end of a
                # key, so no name may end with another stage's whole name.
                if stage.name != other and stage.name.endswith("/" + other):
                    raise ValueError(f"stage {stage.name} ends with stage {other}'s name")
            for name in stage.inputs:
                if name == stage.name:
                    raise ValueError(f"stage {stage.name} reads its own result as an input")
                if name not in by_name:
                    raise ValueError(f"stage {stage.name} reads {name}, which no row declares")
        for heard in self.hears:
            if heard.component not in {s.component for s in stages}:
                raise ValueError(f"{heard.component} hears a blob and runs no stage")
        object.__setattr__(self, "_by_name", by_name)
        self._refuse_cycles()

    def _refuse_cycles(self) -> None:
        done: set[str] = set()

        def visit(name: str, path: tuple[str, ...]) -> None:
            if name in path:
                raise ValueError(f"stages read each other in a cycle: {' -> '.join(path)}")
            if name in done:
                return
            for upstream in self._by_name[name].inputs:
                visit(upstream, (*path, name))
            done.add(name)

        for stage in self.stages:
            visit(stage.name, ())

    def stage(self, name: str) -> Stage:
        return self._by_name[name]

    def stages_of(self, component: str) -> tuple[Stage, ...]:
        return tuple(s for s in self.stages if s.component == component)


def _result_pattern(deployment: str, stage: str) -> str:
    # The run and the decision each fill one part of the key, and `*`
    # stands for both, so the pattern ends with the stage's whole name.
    return f"runs/{deployment}/*/{stage}/result.json"


def routes(table: Table, deployment: str) -> tuple[Route, ...]:
    """Every route a deployment needs, sorted by reader and key, each once.

    A component hears the result of every stage its stages read, and the
    result of each of its own stages that keeps the plan's order, so another
    instance of it learns that decision N−1 is stored. It also hears every
    control blob the table names for it.
    """
    result_key(deployment, "2000-01-03", _ANY_DECISION, "check")
    found: set[Route] = set()
    for stage in table.stages:
        heard: Iterable[str] = stage.inputs
        if stage.keeps_order:
            heard = (*stage.inputs, stage.name)
        for name in heard:
            found.add(Route(stage.component, EventKind.CREATED, _result_pattern(deployment, name)))
    for control in table.hears:
        found.add(Route(control.component, control.kind, control.key.format(deployment=deployment)))
    return tuple(sorted(found, key=lambda r: (r.reader, r.key, r.kind)))


def event_pattern(route: Route, bucket: str) -> dict[str, Any]:
    """The EventBridge rule's pattern for a route in one environment's bucket.

    It matches `aws.s3` as the source, because EventBridge refuses a custom
    event whose source starts with `aws.`, so nobody can put a forged S3 event
    on the bus that a rule would pass on.

    This is the one place a rule's pattern is built. The moto side of the queue
    suite builds its rules from it, and `routes_file` writes it for an
    environment's infrastructure, which applies it as written. `Route.name`
    hashes the reader, the kind and the key, and not the rest of this pattern,
    so a change to the rest renames no rule. A release that changes it gives
    its rules new names by another route.
    """
    return {
        "source": ["aws.s3"],
        "detail-type": [str(route.kind)],
        "detail": {
            "bucket": {"name": [bucket]},
            "object": {"key": [{"wildcard": route.key}]},
        },
    }


@dataclass(frozen=True)
class QueueSettings:
    """One reader's queue: how long a received message stays hidden, and how often.

    A message received `max_receive_count` times without being deleted moves to
    the reader's dead-letter queue on its next receive. SQS takes a visibility
    timeout of 0 to 43,200 seconds and a receive count of 1 to 1,000.
    """

    visibility_timeout_seconds: int
    max_receive_count: int

    def __post_init__(self) -> None:
        timeout, count = self.visibility_timeout_seconds, self.max_receive_count
        if type(timeout) is not int or not 0 <= timeout <= 43_200:
            raise ValueError(f"visibility timeout {timeout!r} is not 0 to 43200 seconds")
        if type(count) is not int or not 1 <= count <= 1_000:
            raise ValueError(f"receive count {count!r} is not 1 to 1000")


def routes_file(rules: Iterable[Route], queues: Mapping[str, QueueSettings], bucket: str) -> str:
    """The routes file an environment's infrastructure reads, as its exact text.

    It holds two lists. `queues` has one entry per reader, which becomes a
    queue and its dead-letter queue. `rules` has one entry per route, which
    becomes an EventBridge rule named by `Route.name`, matching its
    `event_pattern` on the environment's `bucket` and aimed at its reader's
    queue. Both are sorted, so one set of routes writes one text.

    Every route's reader needs an entry in `queues`, and every entry needs a
    route, because a queue no rule feeds would sit empty and look healthy.
    """
    rules = sorted(set(rules), key=lambda r: (r.reader, r.key, r.kind))
    readers = {r.reader for r in rules}
    if missing := sorted(readers - set(queues)):
        raise ValueError(f"no queue settings for {', '.join(missing)}, whose routes need a queue")
    if unfed := sorted(set(queues) - readers):
        raise ValueError(f"no route feeds the queue of {', '.join(unfed)}")
    document = {
        "queues": [
            {
                "reader": reader,
                "visibility_timeout_seconds": queues[reader].visibility_timeout_seconds,
                "max_receive_count": queues[reader].max_receive_count,
            }
            for reader in sorted(readers)
        ],
        "rules": [
            {
                "name": r.name,
                "reader": r.reader,
                "pattern": event_pattern(r, bucket),
            }
            for r in rules
        ],
    }
    return json.dumps(document, indent=2) + "\n"
