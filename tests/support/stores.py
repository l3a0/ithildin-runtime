"""The stores the store suite runs against, described so a second process can open one.

A test that races two processes hands each a `StoreSpec`, which pickles,
rather than a store, which holds a client or a folder handle.

The S3 store runs against moto's S3 server, started in the test process.
moto honours `If-None-Match` and `If-Match`, and a probe on 2026-11-21 saw one
of 40 racing creates win. It does not check the SHA-256 a write sends, so the
suite marks that case as failing on moto, strictly, and botocore's stubbed
answers in tests/test_s3_answers.py show the adapter's handling of S3's own
refusal. A probe of moto 5.2.5 on 2026-11-22, with 8 spawned processes
rewriting one lease on one tag for 20 rounds, let two writers win in 4 of the
20 rounds, so the suite skips its racing-rewrite case on moto.

The bucket is the test environment's `ithildin-runtime-test`, which ci.yml's
`aws` job reaches with the test role on a push to main. It checks the SHA-256
and can answer 409, which closes both of moto's gaps. The test role may create
and read only under `runs/ci/` and `leases/plans/ci/`, and may delete nothing,
so `key` and `lease` put every key the suite writes under the run's own id,
and `remove` and `overwrite` refuse on the bucket. The bucket's client names
no endpoint and no keys, ignores any endpoint the environment or a shared
config file sets, and refuses one outside AWS, so nothing points it elsewhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.config import Config

from ithildinruntime.adapters import FolderStore, S3Store
from ithildinruntime.core import EventKind, Route

# Credentials for the emulator only. They reach no AWS endpoint, because
# every client here that passes them names the emulator's URL.
EMULATOR_KEYS = {"aws_access_key_id": "emulator", "aws_secret_access_key": "emulator"}
REGION = "us-east-1"
TEST_BUCKET = "ithildin-runtime-test"
ROUTES_FILE = Path(__file__).parents[2] / "infra" / "test" / "routes.json"


@dataclass(frozen=True)
class StoreSpec:
    """One store the suite runs against.

    `kind` is `folder`, `s3` for moto, or `bucket` for the test bucket. `run`
    and `case` put a case's keys under `runs/ci/<run>/store/<case>/`. On a spec
    without a `run`, `key` leaves a key as the test wrote it, which the modules
    that share `tests/conftest.py`'s fixture rely on.
    """

    kind: str
    root: str | None = None
    endpoint: str | None = None
    bucket: str | None = None
    run: str | None = None
    case: str | None = None

    @property
    def checks_sha256(self) -> bool:
        """Whether the store itself refuses bytes whose SHA-256 differs. moto does not."""
        return self.kind in ("folder", "bucket")

    @property
    def can_remove(self) -> bool:
        """Whether `remove` and `overwrite` work. The test role can do neither."""
        return self.kind != "bucket"

    def key(self, key: str) -> str:
        """The key a case's `key` is stored under, refused when a test route would hear it."""
        if self.run is None:
            return key
        return _unrouted(f"runs/ci/{self.run}/store/{self.case}/{key}")

    def lease(self, n: int) -> str:
        """The case's lease number `n`, under the prefix the test role may write leases."""
        return _unrouted(f"leases/plans/ci/{self.run}-{self.case}-{n}.json")

    def open(self) -> FolderStore | S3Store:
        if self.kind == "folder":
            return FolderStore(self.root)
        return S3Store(self.client(), self.bucket)

    def client(self):
        retries = {"mode": "standard", "total_max_attempts": 1}
        if self.kind == "bucket":
            # The job's own credentials, from the environment, and AWS's own endpoint
            # whatever AWS_ENDPOINT_URL or a config file says. Ten seconds to connect and
            # thirty to read bound a case on a slow S3 well inside the job's limit.
            config = Config(
                retries=retries,
                ignore_configured_endpoint_urls=True,
                connect_timeout=10,
                read_timeout=30,
            )
            client = boto3.client("s3", region_name=REGION, config=config)
            host = urlsplit(client.meta.endpoint_url).hostname or ""
            if not host.endswith(".amazonaws.com"):
                raise RuntimeError(f"the test bucket's client points at {host}, not AWS")
            return client
        config = Config(retries=retries)
        return boto3.client(
            "s3", endpoint_url=self.endpoint, region_name=REGION, config=config, **EMULATOR_KEYS
        )

    def remove(self, key: str) -> None:
        """Delete a blob by hand, which no worker can do through the port."""
        self._refuse_on_the_bucket("delete a blob")
        if self.kind == "folder":
            Path(self.root, key).unlink()
        else:
            self.client().delete_object(Bucket=self.bucket, Key=key)

    def overwrite(self, key: str, data: bytes) -> None:
        """Change a stored blob by hand, around every rule the store keeps."""
        self._refuse_on_the_bucket("rewrite a blob without a condition")
        if self.kind == "folder":
            Path(self.root, key).write_bytes(data)
        else:
            self.client().put_object(Bucket=self.bucket, Key=key, Body=data)

    def _refuse_on_the_bucket(self, what: str) -> None:
        if not self.can_remove:
            raise RuntimeError(f"the test role cannot {what}, so no bucket case may ask to")


@cache
def _routes(path: Path = ROUTES_FILE) -> tuple[Route, ...]:
    """Every route in infra/test/routes.json, read from each rule's EventBridge pattern.

    `routes_file` in `runtime.loop` writes each rule's `pattern` as `event_pattern`
    builds it: its kinds under `detail-type` and its key patterns as `wildcard`
    entries under `detail.object.key`. Anything else in a rule's pattern is one
    the store suite cannot read, so it refuses the file rather than skip a route.
    """
    found: list[Route] = []
    for rule in json.loads(path.read_text()).get("rules", []):
        pattern = rule.get("pattern") if isinstance(rule, dict) else None
        try:
            kinds = pattern["detail-type"]
            keys = pattern["detail"]["object"]["key"]
        except (KeyError, TypeError):
            raise ValueError(
                f"routes.json holds a pattern the store suite cannot read: {pattern}"
            ) from None
        if not isinstance(kinds, list) or not isinstance(keys, list) or not kinds or not keys:
            raise ValueError(f"routes.json holds a pattern the store suite cannot read: {pattern}")
        for entry in keys:
            if not isinstance(entry, dict) or set(entry) != {"wildcard"}:
                raise ValueError(
                    f"routes.json holds a pattern the store suite cannot read: {entry}"
                )
            for kind in kinds:
                found.append(Route("store-suite", EventKind(kind), entry["wildcard"]))
    if not found:
        raise ValueError(f"{path} holds no key pattern, so the store suite cannot check its keys")
    return tuple(found)


def _unrouted(key: str) -> str:
    """Refuse a key a test route hears, which would put a message on a queue a suite reads."""
    for route in _routes():
        if route.matches(EventKind.CREATED, key):
            raise ValueError(f"{key} matches the test route {route.key}, so a queue would hear it")
    return key
