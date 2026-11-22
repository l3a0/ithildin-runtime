"""The shared fixtures.

Each store test runs once against the folder and once against S3 on moto. Each
data test reads its own copy of one marketlake lake, built once per session
from quantitative-trading's vintages, as tests/support/lake.py says.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support import lake as lakes
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


@pytest.fixture(scope="session")
def vintages(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """GLD's and USO's vintages from quantitative-trading, checked by their digests."""
    return lakes.fetch_vintages(tmp_path_factory.mktemp("vintages"))


@pytest.fixture(scope="session")
def built_lake(tmp_path_factory: pytest.TempPathFactory, vintages: Path) -> Path:
    """The lake both series sit in, built once and never changed."""
    return lakes.build_lake(tmp_path_factory.mktemp("built") / "lake", vintages)


@pytest.fixture
def lake(built_lake: Path, tmp_path: Path) -> Path:
    """This test's own copy of the lake, which the folder bucket serves as marketlake's bucket."""
    return lakes.copy_lake(built_lake, tmp_path / "bucket")
