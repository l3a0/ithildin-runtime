"""The test environment's routes file, `infra/test/routes.json`, and the command that writes it.

The file holds the stand-in table's routes for the `ci` deployment, which the queue
suite runs on, and the smoke route, which no stage table holds and the smoke check in
`tests/aws/test_test_environment.py` writes to. `tests/test_routes.py` fails when the
committed file differs from what this module writes. Rewrite it with

    uv run python -m tests.support.routes_file
"""

from __future__ import annotations

from pathlib import Path

from ithildinruntime.core import EventKind, Route
from ithildinruntime.runtime.loop import QueueSettings, routes, routes_file
from tests.support.queues import DEPLOYMENT, TABLE

BUCKET = "ithildin-runtime-test"

PATH = Path(__file__).parents[2] / "infra" / "test" / "routes.json"
COMMAND = "uv run python -m tests.support.routes_file"

SMOKE = Route("smoke", EventKind.CREATED, f"runs/{DEPLOYMENT}/*/smoke/*")

# One entry per reader. `feed` reads nothing, so it has no queue. The rollback drill in
# docs/what-a-person-runs.md changes the smoke entry's timeout.
QUEUES = {
    "alpha": QueueSettings(visibility_timeout_seconds=30, max_receive_count=3),
    "book": QueueSettings(visibility_timeout_seconds=30, max_receive_count=3),
    "risk": QueueSettings(visibility_timeout_seconds=30, max_receive_count=3),
    "smoke": QueueSettings(visibility_timeout_seconds=30, max_receive_count=3),
}


def text() -> str:
    """The file's exact text."""
    return routes_file((*routes(TABLE, DEPLOYMENT), SMOKE), QUEUES, BUCKET)


if __name__ == "__main__":
    PATH.write_text(text())
    print(f"wrote {PATH}")
