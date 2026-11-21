"""The store suite: every rule a `BlobStore` keeps, run against the folder and against S3.

design.md's `### A worker works through a span of decisions` lists the cases,
and that list stays the one statement of them. Each test here runs once per
store through the `spec` fixture, so a rule one store breaks fails a test.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ConnectionClosedError

from ithildinruntime.core import (
    BlobMissing,
    ChecksumMismatch,
    KeyExists,
    StaleTag,
    sha256_of,
)
from tests.support.stores import StoreSpec

KEY = "runs/ci/2026-11-02/20261102T204000Z/alpha/forecasts.json"
LEASE = "leases/plans/ci/2026-11-02.json"


def _create(store, key: str, data: bytes) -> str:
    return store.create(key, data, sha256_of(data))


def test_a_create_is_refused_when_its_key_exists(spec: StoreSpec) -> None:
    store = spec.open()
    _create(store, KEY, b"first")
    with pytest.raises(KeyExists) as refused:
        _create(store, KEY, b"second")
    assert refused.value.key == KEY
    assert store.get(KEY) == b"first"
    with pytest.raises(KeyExists):
        _create(store, KEY, b"first")


def test_a_write_whose_bytes_differ_from_its_sha256_is_refused(
    spec: StoreSpec, request: pytest.FixtureRequest
) -> None:
    if not spec.checks_sha256:
        request.applymarker(
            pytest.mark.xfail(
                strict=True,
                reason="moto stores a blob whose SHA-256 differs; the bucket run on #88 covers S3",
            )
        )
    store = spec.open()
    with pytest.raises(ChecksumMismatch):
        store.create(KEY, b"bytes", sha256_of(b"other bytes"))
    with pytest.raises(BlobMissing):
        store.get(KEY)
    tag = _create(store, LEASE, b"lease one")
    with pytest.raises(ChecksumMismatch):
        store.replace_if_match(LEASE, b"lease two", sha256_of(b"lease three"), tag)
    assert store.get(LEASE) == b"lease one"


def test_keys_are_listed_in_order_after_a_key_under_a_prefix(spec: StoreSpec) -> None:
    store = spec.open()
    keys = [
        "runs/ci/b-2.json",
        "runs/ci/b-10.json",
        "runs/ci/Z.json",
        "runs/ci/bb/x.json",
        "runs/ci/b-1.json",
        "runs/cj/b-1.json",
        "journal/a/000000000001.json",
    ]
    for key in keys:
        _create(store, key, key.encode())
    assert store.list("runs/ci/b", None) == [
        "runs/ci/b-1.json",
        "runs/ci/b-10.json",
        "runs/ci/b-2.json",
        "runs/ci/bb/x.json",
    ]
    assert store.list("runs/ci/b", "runs/ci/b-10.json") == ["runs/ci/b-2.json", "runs/ci/bb/x.json"]
    assert store.list("runs/", None) == sorted(k for k in keys if k.startswith("runs/"))
    assert store.list("runs/ci/", "runs/ci/bb/x.json") == []
    assert store.list("accounts/", None) == []


def test_a_missing_key_answers_the_same_way_everywhere(spec: StoreSpec) -> None:
    store = spec.open()
    for call in (lambda: store.get(KEY), lambda: store.head(KEY)):
        with pytest.raises(BlobMissing) as missing:
            call()
        assert missing.value.key == KEY


def test_the_tag_a_create_returns_is_the_one_a_lookup_returns(spec: StoreSpec) -> None:
    store = spec.open()
    tag = _create(store, KEY, b"bytes")
    head = store.head(KEY)
    assert head.etag == tag
    assert head.ref.key == KEY
    assert head.ref.sha256 == sha256_of(b"bytes")


def test_a_lookup_returns_the_stored_time_in_whole_utc_seconds(spec: StoreSpec) -> None:
    store = spec.open()
    before = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)
    _create(store, KEY, b"bytes")
    after = datetime.now(UTC) + timedelta(seconds=1)
    stored_at = store.head(KEY).stored_at
    assert stored_at.utcoffset() == timedelta(0)
    assert stored_at.microsecond == 0
    assert before <= stored_at <= after


def test_a_rewrite_on_a_stale_tag_is_refused(spec: StoreSpec) -> None:
    store = spec.open()
    first = _create(store, LEASE, b"lease one")
    second = store.replace_if_match(LEASE, b"lease two", sha256_of(b"lease two"), first)
    with pytest.raises(StaleTag) as stale:
        store.replace_if_match(LEASE, b"lease three", sha256_of(b"lease three"), first)
    assert stale.value.key == LEASE
    assert store.get(LEASE) == b"lease two"
    assert store.head(LEASE).etag == second


def test_a_rewrite_of_a_key_that_no_longer_exists_is_refused(spec: StoreSpec) -> None:
    store = spec.open()
    tag = _create(store, LEASE, b"lease one")
    spec.remove(LEASE)
    with pytest.raises(BlobMissing):
        store.replace_if_match(LEASE, b"lease two", sha256_of(b"lease two"), tag)
    with pytest.raises(BlobMissing):
        store.replace_if_match("leases/plans/ci/never.json", b"x", sha256_of(b"x"), tag)
    with pytest.raises(BlobMissing):
        store.get(LEASE)


def test_a_rewrite_returns_the_new_tag(spec: StoreSpec) -> None:
    store = spec.open()
    first = _create(store, LEASE, b"lease one")
    second = store.replace_if_match(LEASE, b"lease two", sha256_of(b"lease two"), first)
    assert second != first
    assert store.head(LEASE).etag == second
    assert store.get(LEASE) == b"lease two"
    third = store.replace_if_match(LEASE, b"lease three", sha256_of(b"lease three"), second)
    assert third not in (first, second)


def _race(spec: StoreSpec, n: int, start, results) -> None:
    store = spec.open()
    data = f"writer {n}".encode()
    start.wait()
    try:
        store.create(KEY, data, sha256_of(data))
    except KeyExists:
        results.put((n, "refused"))
    else:
        results.put((n, "stored"))


def test_two_processes_racing_one_create_leave_one_winner(spec: StoreSpec) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    writers = [context.Process(target=_race, args=(spec, n, start, results)) for n in range(6)]
    for writer in writers:
        writer.start()
    start.set()
    outcomes = dict(results.get(timeout=60) for _ in writers)
    for writer in writers:
        writer.join(timeout=60)
    winners = [n for n, outcome in outcomes.items() if outcome == "stored"]
    assert len(winners) == 1, outcomes
    assert spec.open().get(KEY) == f"writer {winners[0]}".encode()


def test_a_read_during_a_write_sees_no_blob_or_the_whole_blob(spec: StoreSpec) -> None:
    store = spec.open()
    data = os.urandom(8 * 1024 * 1024)
    seen: list[int] = []
    done = threading.Event()

    def read() -> None:
        reader = spec.open()
        while not done.is_set():
            try:
                seen.append(len(reader.get(KEY)))
            except BlobMissing:
                seen.append(0)

    thread = threading.Thread(target=read)
    thread.start()
    store.create(KEY, data, sha256_of(data))
    time.sleep(0.2)
    done.set()
    thread.join()
    assert set(seen) <= {0, len(data)}
    assert seen[-1] == len(data)


_KILLED_FOLDER_WRITER = """
import os, sys, time
from ithildinruntime.adapters import folder
from ithildinruntime.core import sha256_of

def stop(source, target):
    print("written", flush=True)
    time.sleep(600)

folder.os.link = stop
data = b"x" * 4096
folder.FolderStore(sys.argv[1]).create(sys.argv[2], data, sha256_of(data))
"""

_KILLED_S3_WRITER = """
import sys, time, urllib.parse, http.client
endpoint, bucket, key = sys.argv[1:4]
url = urllib.parse.urlparse(endpoint)
connection = http.client.HTTPConnection(url.hostname, url.port)
connection.putrequest("PUT", f"/{bucket}/{key}")
connection.putheader("Content-Length", "8192")
connection.putheader("If-None-Match", "*")
connection.endheaders()
connection.send(b"x" * 4096)
print("written", flush=True)
time.sleep(600)
"""


def test_a_writer_killed_partway_through_leaves_no_blob(spec: StoreSpec) -> None:
    if spec.kind == "folder":
        args = [_KILLED_FOLDER_WRITER, spec.root, KEY]
    else:
        args = [_KILLED_S3_WRITER, spec.endpoint, spec.bucket, KEY]
    writer = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(args[0]), *args[1:]],
        stdout=subprocess.PIPE,
        cwd=os.getcwd(),
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    try:
        assert writer.stdout.readline().strip() == b"written"
    finally:
        writer.send_signal(signal.SIGKILL)
        writer.wait(timeout=30)
        writer.stdout.close()
    time.sleep(0.2)
    store = spec.open()
    with pytest.raises(BlobMissing):
        store.get(KEY)
    assert store.list("runs/", None) == []
    _create(store, KEY, b"the retry")
    assert store.get(KEY) == b"the retry"


def test_a_lost_reply_on_a_create_that_landed_reads_as_no_answer(spec: StoreSpec) -> None:
    """The adapter reports a reply it never got as no answer, and the key then holds the blob."""
    from ithildinruntime.core import StoreUnavailable

    if spec.kind == "folder":
        pytest.skip("a folder write has no reply to lose; tests/test_storage_contract.py fakes one")
    client = spec.client()

    def lose(**_: object) -> None:
        raise ConnectionClosedError(endpoint_url=spec.endpoint)

    client.meta.events.register("after-call.s3.PutObject", lose)
    from ithildinruntime.adapters import S3Store

    with pytest.raises(StoreUnavailable):
        S3Store(client, spec.bucket).create(KEY, b"landed", sha256_of(b"landed"))
    store = spec.open()
    assert store.get(KEY) == b"landed"
    with pytest.raises(KeyExists):
        _create(store, KEY, b"landed")


def test_a_folder_create_leaves_no_temporary_file(spec: StoreSpec) -> None:
    if spec.kind != "folder":
        pytest.skip("only the folder writes a temporary file")
    store = spec.open()
    _create(store, KEY, b"first")
    with pytest.raises(KeyExists):
        _create(store, KEY, b"second")
    with pytest.raises(ChecksumMismatch):
        store.create("runs/ci/other.json", b"x", sha256_of(b"y"))
    tag = _create(store, LEASE, b"lease one")
    store.replace_if_match(LEASE, b"lease two", sha256_of(b"lease two"), tag)
    assert os.listdir(os.path.join(spec.root, ".incoming")) == []


def test_a_folder_lists_nothing_outside_its_own_folder(spec: StoreSpec, tmp_path) -> None:
    if spec.kind != "folder":
        pytest.skip("S3 has no folder to leave")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("x")
    store = spec.open()
    for prefix in ("../", "../outside/", "/etc/", "runs/../../"):
        with pytest.raises(ValueError):
            store.list(prefix, None)


def test_a_folder_reads_a_path_that_holds_no_blob_as_missing(spec: StoreSpec) -> None:
    if spec.kind != "folder":
        pytest.skip("S3 has no folders")
    store = spec.open()
    _create(store, KEY, b"bytes")
    folder = KEY.rpartition("/")[0]
    for key in (folder, f"{KEY}/below.json"):
        with pytest.raises(BlobMissing):
            store.get(key)
        with pytest.raises(BlobMissing):
            store.head(key)
