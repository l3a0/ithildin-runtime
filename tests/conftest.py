"""The store fixture: each store test runs once against the folder and once against S3 on moto."""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pytest

from tests.support.stores import StoreSpec

_buckets = itertools.count()


@pytest.fixture(scope="session")
def emulator() -> Iterator[str]:
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture(params=["folder", "s3"])
def spec(request: pytest.FixtureRequest, tmp_path) -> StoreSpec:
    if request.param == "folder":
        return StoreSpec("folder", root=str(tmp_path / "store"))
    endpoint = request.getfixturevalue("emulator")
    bucket = f"store-suite-{next(_buckets)}"
    made = StoreSpec("s3", endpoint=endpoint, bucket=bucket)
    made.client().create_bucket(Bucket=bucket)
    return made
