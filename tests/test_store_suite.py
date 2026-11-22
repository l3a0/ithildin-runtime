"""The store suite: every rule a `BlobStore` keeps, run against the folder, moto and S3.

design.md's `### A worker works through a span of decisions` lists the cases,
and that list stays the one statement of them. Each test here runs once per
store through the `spec` fixture, so a rule one store breaks fails a test.

The third store is the test environment's bucket, marked `aws`, so it runs
only in ci.yml's `aws` job on a push to main, and every other run deselects
it. Every key a case writes goes through `spec.key` or `spec.lease`, which put
it under the run's own id on every store, because the bucket keeps each run's
keys until its lifecycle expires them and the test role deletes nothing.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
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
    Conflict,
    KeyExists,
    StaleTag,
    sha256_of,
)
from tests.support.stores import TEST_BUCKET, StoreSpec

# The key a case uses, which `spec.key` puts under the case's own prefix.
CASE_KEY = "runs/ci/2026-11-02/20261102T204000Z/alpha/forecasts.json"

# Named apart from conftest's buckets, which share the same moto server.
_buckets = itertools.count()


@pytest.fixture(params=["folder", "s3", pytest.param("bucket", marks=pytest.mark.aws)])
def spec(request: pytest.FixtureRequest, tmp_path) -> StoreSpec:
    """This module's own stores, which add the test bucket to conftest's folder and moto."""
    case = request.node.originalname
    if request.param == "bucket":
        run = os.environ.get("ITHILDIN_TEST_RUN", "")
        if not run:
            pytest.fail("ITHILDIN_TEST_RUN is empty. ci.yml's aws job sets it.")
        return StoreSpec("bucket", bucket=TEST_BUCKET, run=run, case=case)
    if request.param == "folder":
        return StoreSpec("folder", root=str(tmp_path / "store"), run="local", case=case)
    endpoint = request.getfixturevalue("emulator")
    bucket = f"store-suite-own-{next(_buckets)}"
    made = StoreSpec("s3", endpoint=endpoint, bucket=bucket, run="local", case=case)
    made.client().create_bucket(Bucket=bucket)
    return made


def test_a_key_a_test_route_would_hear_is_refused() -> None:
    """A store key under a route's pattern would put a message on a queue a suite reads."""
    spec = StoreSpec("folder", root="unused", run="local", case="case")
    with pytest.raises(ValueError, match=r"runs/ci/\*/smoke/\*"):
        spec.key("smoke/a.json")
    assert spec.key("alpha/a.json") == "runs/ci/local/store/case/alpha/a.json"


def test_a_routes_file_the_check_cannot_read_is_refused(tmp_path) -> None:
    from tests.support import stores

    for body, words in (
        ([{"pattern": [{"prefix": "runs/"}]}], "cannot read"),
        ([{"reader": "smoke"}], "holds no key pattern"),
    ):
        path = tmp_path / f"routes-{words.replace(' ', '-')}.json"
        path.write_text(json.dumps(body))
        with pytest.raises(ValueError, match=words):
            stores._routes(path)


def test_the_bucket_client_ignores_an_endpoint_the_environment_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A green bucket run cannot come from an endpoint a variable points elsewhere."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:1")
    spec = StoreSpec("bucket", bucket=TEST_BUCKET, run="local", case="case")
    assert spec.client().meta.endpoint_url == "https://s3.amazonaws.com"


def _create(store, key: str, data: bytes) -> str:
    return store.create(key, data, sha256_of(data))


def test_a_create_is_refused_when_its_key_exists(spec: StoreSpec) -> None:
    store, key = spec.open(), spec.key(CASE_KEY)
    _create(store, key, b"first")
    with pytest.raises(KeyExists) as refused:
        _create(store, key, b"second")
    assert refused.value.key == key
    assert store.get(key) == b"first"
    with pytest.raises(KeyExists):
        _create(store, key, b"first")


def test_a_write_whose_bytes_differ_from_its_sha256_is_refused(
    spec: StoreSpec, request: pytest.FixtureRequest
) -> None:
    if not spec.checks_sha256:
        request.applymarker(
            pytest.mark.xfail(
                strict=True,
                reason="moto stores a blob whose SHA-256 differs; the run on the test bucket "
                "in ci.yml's aws job covers S3",
            )
        )
    store, key, lease = spec.open(), spec.key(CASE_KEY), spec.lease(1)
    with pytest.raises(ChecksumMismatch):
        store.create(key, b"bytes", sha256_of(b"other bytes"))
    with pytest.raises(BlobMissing):
        store.get(key)
    tag = _create(store, lease, b"lease one")
    with pytest.raises(ChecksumMismatch):
        store.replace_if_match(lease, b"lease two", sha256_of(b"lease three"), tag)
    assert store.get(lease) == b"lease one"


def test_keys_are_listed_in_order_after_a_key_under_a_prefix(spec: StoreSpec) -> None:
    store, at = spec.open(), spec.key
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
        _create(store, at(key), key.encode())
    assert store.list(at("runs/ci/b"), None) == [
        at("runs/ci/b-1.json"),
        at("runs/ci/b-10.json"),
        at("runs/ci/b-2.json"),
        at("runs/ci/bb/x.json"),
    ]
    assert store.list(at("runs/ci/b"), at("runs/ci/b-10.json")) == [
        at("runs/ci/b-2.json"),
        at("runs/ci/bb/x.json"),
    ]
    assert store.list(at("runs/"), None) == sorted(at(k) for k in keys if k.startswith("runs/"))
    assert store.list(at("runs/ci/"), at("runs/ci/bb/x.json")) == []
    assert store.list(at("accounts/"), None) == []


def test_a_missing_key_answers_the_same_way_everywhere(spec: StoreSpec) -> None:
    store, key = spec.open(), spec.key(CASE_KEY)
    for call in (lambda: store.get(key), lambda: store.head(key)):
        with pytest.raises(BlobMissing) as missing:
            call()
        assert missing.value.key == key


def test_the_tag_a_create_returns_is_the_one_a_lookup_returns(spec: StoreSpec) -> None:
    store, key = spec.open(), spec.key(CASE_KEY)
    tag = _create(store, key, b"bytes")
    head = store.head(key)
    assert head.etag == tag
    assert head.ref.key == key
    assert head.ref.sha256 == sha256_of(b"bytes")


def test_a_lookup_returns_the_stored_time_in_whole_utc_seconds(spec: StoreSpec) -> None:
    store, key = spec.open(), spec.key(CASE_KEY)
    before = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)
    _create(store, key, b"bytes")
    after = datetime.now(UTC) + timedelta(seconds=1)
    stored_at = store.head(key).stored_at
    assert stored_at.utcoffset() == timedelta(0)
    assert stored_at.microsecond == 0
    assert before <= stored_at <= after


def test_a_rewrite_on_a_stale_tag_is_refused(spec: StoreSpec) -> None:
    store, lease = spec.open(), spec.lease(1)
    first = _create(store, lease, b"lease one")
    second = store.replace_if_match(lease, b"lease two", sha256_of(b"lease two"), first)
    with pytest.raises(StaleTag) as stale:
        store.replace_if_match(lease, b"lease three", sha256_of(b"lease three"), first)
    assert stale.value.key == lease
    assert store.get(lease) == b"lease two"
    assert store.head(lease).etag == second


def test_a_rewrite_of_a_key_that_no_longer_exists_is_refused(spec: StoreSpec) -> None:
    store, lease, never = spec.open(), spec.lease(1), spec.lease(2)
    tag = _create(store, lease, b"lease one")
    with pytest.raises(BlobMissing):
        store.replace_if_match(never, b"x", sha256_of(b"x"), tag)
    if not spec.can_remove:
        # The test role deletes nothing, so on the bucket only a key never written runs.
        return
    spec.remove(lease)
    with pytest.raises(BlobMissing):
        store.replace_if_match(lease, b"lease two", sha256_of(b"lease two"), tag)
    with pytest.raises(BlobMissing):
        store.get(lease)


def test_a_rewrite_returns_the_new_tag(spec: StoreSpec) -> None:
    store, lease = spec.open(), spec.lease(1)
    first = _create(store, lease, b"lease one")
    second = store.replace_if_match(lease, b"lease two", sha256_of(b"lease two"), first)
    assert second != first
    assert store.head(lease).etag == second
    assert store.get(lease) == b"lease two"
    third = store.replace_if_match(lease, b"lease three", sha256_of(b"lease three"), second)
    assert third not in (first, second)


def _outcome(error: Exception) -> str:
    """A child's unexpected error by type and message, which names S3's status and code."""
    return f"{type(error).__name__}: {error}"


def _stop(writers) -> None:
    """Wait for each child, and end any still running, so a failed case cannot hang the job."""
    for writer in writers:
        writer.join(timeout=60)
        if writer.is_alive():
            writer.terminate()


def _race(spec: StoreSpec, n: int, start, results) -> None:
    store = spec.open()
    data = f"writer {n}".encode()
    if not start.wait(timeout=60):
        results.put((n, "never started"))
        return
    try:
        store.create(spec.key(CASE_KEY), data, sha256_of(data))
    except KeyExists:
        results.put((n, "refused"))
    except Conflict:
        results.put((n, "conflicted"))
    except Exception as error:  # noqa: BLE001, so the parent fails on what the child met
        results.put((n, _outcome(error)))
    else:
        results.put((n, "stored"))


def test_two_processes_racing_one_create_leave_one_winner(spec: StoreSpec) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    writers = [
        context.Process(target=_race, args=(spec, n, start, results), daemon=True) for n in range(6)
    ]
    try:
        for writer in writers:
            writer.start()
        start.set()
        outcomes = dict(results.get(timeout=60) for _ in writers)
    finally:
        start.set()
        _stop(writers)
    winners = [n for n, outcome in outcomes.items() if outcome == "stored"]
    assert len(winners) == 1, outcomes
    losers = {outcome for n, outcome in outcomes.items() if n != winners[0]}
    assert losers <= {"refused", "conflicted"}, outcomes
    assert spec.open().get(spec.key(CASE_KEY)) == f"writer {winners[0]}".encode()


# How many processes race each rewrite, and for how many rounds.
REWRITERS = 6
ROUNDS = 10


def _rewrite(spec: StoreSpec, n: int, inbox, barrier, results) -> None:
    """Rewrite the case's lease on each tag the parent sends, together with every other writer."""
    store = spec.open()
    lease = spec.lease(1)
    while (job := inbox.get(timeout=120)) is not None:
        round_, tag = job
        data = f"round {round_} writer {n}".encode()
        barrier.wait()
        try:
            new = store.replace_if_match(lease, data, sha256_of(data), tag)
        except StaleTag:
            results.put((round_, n, "stale", None))
        except Conflict:
            results.put((round_, n, "conflicted", None))
        except Exception as error:  # noqa: BLE001, so the parent fails on what the child met
            results.put((round_, n, _outcome(error), None))
        else:
            results.put((round_, n, "stored", new))


def test_processes_racing_one_rewrite_leave_one_winner(spec: StoreSpec) -> None:
    """One rewrite on a tag wins, and a rewrite S3 answers with 409 leaves nothing behind.

    S3 may answer racing `If-Match` writes with 409, which `S3Store` reads as
    `Conflict`. The writers start once, so later rounds race on warm connections.
    """
    if spec.kind == "s3":
        pytest.skip(
            "moto 5.2.5 let two of 8 racing rewrites on one tag win in 4 of 20 rounds, "
            "in a probe on 2026-11-22"
        )
    store, lease = spec.open(), spec.lease(1)
    tag = _create(store, lease, b"round 0")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(REWRITERS, timeout=60)
    results = context.Queue()
    inboxes = [context.Queue() for _ in range(REWRITERS)]
    writers = [
        context.Process(target=_rewrite, args=(spec, n, inboxes[n], barrier, results), daemon=True)
        for n in range(REWRITERS)
    ]
    rounds = []
    try:
        for writer in writers:
            writer.start()
        for round_ in range(1, ROUNDS + 1):
            for inbox in inboxes:
                inbox.put((round_, tag))
            answers = [results.get(timeout=60) for _ in writers]
            assert {answer[0] for answer in answers} == {round_}, answers
            outcomes = {n: outcome for _, n, outcome, _ in answers}
            rounds.append(sorted(outcomes.values()))
            winners = [(n, new) for _, n, outcome, new in answers if outcome == "stored"]
            assert len(winners) == 1, (round_, outcomes)
            ((winner, tag),) = winners
            losers = {outcome for n, outcome in outcomes.items() if n != winner}
            assert losers <= {"stale", "conflicted"}, (round_, outcomes)
            assert store.get(lease) == f"round {round_} writer {winner}".encode()
            assert store.head(lease).etag == tag
    finally:
        for inbox in inboxes:
            inbox.put(None)
        barrier.abort()
        _stop(writers)
    conflicts = sum(outcomes.count("conflicted") for outcomes in rounds)
    if spec.kind == "bucket" and conflicts == 0:
        pytest.xfail(
            f"S3 answered no 409 in {ROUNDS} rounds of {REWRITERS} racing rewrites on one tag; "
            f"each round's outcomes: {rounds}"
        )


def test_a_read_during_a_write_sees_no_blob_or_the_whole_blob(spec: StoreSpec) -> None:
    store, key = spec.open(), spec.key(CASE_KEY)
    data = os.urandom(8 * 1024 * 1024)
    seen: list[int] = []
    done = threading.Event()

    def read() -> None:
        reader = spec.open()
        while not done.is_set():
            try:
                seen.append(len(reader.get(key)))
            except BlobMissing:
                seen.append(0)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    try:
        store.create(key, data, sha256_of(data))
        time.sleep(0.2)
    finally:
        done.set()
        thread.join(timeout=60)
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

# The S3 writer has `S3Store.create` sign its PUT, so the request carries the adapter's own
# headers, then sends it by hand with `Expect: 100-continue`, so it can stop partway through
# the body. On the bucket its headers carry the test role's session token, and the job's log
# is public. So it prints "written" once S3 has answered 100 Continue, or one fixed line
# saying where it stopped, naming only a status code or an exception's type, and on the
# bucket its stderr goes nowhere.
_KILLED_S3_WRITER = """
import os, sys, time
quiet = True
try:
    import http.client, json, urllib.parse
    from ithildinruntime.adapters import S3Store
    from ithildinruntime.core import sha256_of
    from tests.support.stores import StoreSpec

    spec = StoreSpec(**json.loads(sys.argv[1]))
    quiet = spec.kind == "bucket"
    key = sys.argv[2]
    client = spec.client()
    signed = {}

    class Signed(Exception):
        pass

    def keep(request, **_):
        signed["url"], signed["headers"] = request.url, dict(request.headers)
        raise Signed

    client.meta.events.register("before-send.s3.PutObject", keep)
    data = b"x" * 8192
    try:
        S3Store(client, spec.bucket).create(key, data, sha256_of(data))
    except Signed:
        pass
    url = urllib.parse.urlsplit(signed["url"])
    https = url.scheme == "https"
    connection = (http.client.HTTPSConnection if https else http.client.HTTPConnection)(
        url.hostname, url.port, timeout=60
    )
    target = url.path + (f"?{url.query}" if url.query else "")
    connection.putrequest("PUT", target, skip_accept_encoding=True)
    for name, value in {**signed["headers"], "Expect": "100-continue"}.items():
        connection.putheader(name, value)
    connection.endheaders()
    reply = connection.sock.makefile("rb")
    status = reply.readline()
    if not status.startswith(b"HTTP/1.1 100 "):
        code = status[9:12] if status[9:12].isdigit() else b"none"
        print(f"stopped: S3 answered {code.decode()} rather than 100 Continue", flush=True)
        os._exit(1)
    while reply.readline() not in (b"\\r\\n", b"\\n", b""):
        pass
    connection.send(data[:4096])
except BaseException as error:
    print(f"stopped: {type(error).__name__}", flush=True)
    if quiet:
        os._exit(1)
    raise
print("written", flush=True)
time.sleep(600)
"""


def test_a_writer_killed_partway_through_leaves_no_blob(spec: StoreSpec) -> None:
    key = spec.key(CASE_KEY)
    if spec.kind == "folder":
        args = [_KILLED_FOLDER_WRITER, spec.root, key]
    else:
        args = [_KILLED_S3_WRITER, json.dumps(dataclasses.asdict(spec)), key]
    writer = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(args[0]), *args[1:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if spec.kind == "bucket" else None,
        cwd=os.getcwd(),
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    try:
        line = writer.stdout.readline().strip()
        assert line == b"written", (
            f"the writer exited before its store took the first half of the body: {line!r}"
        )
    finally:
        writer.send_signal(signal.SIGKILL)
        writer.wait(timeout=30)
        writer.stdout.close()
    time.sleep(0.2)
    store = spec.open()
    with pytest.raises(BlobMissing):
        store.get(key)
    assert store.list(spec.key("runs/"), None) == []
    _create(store, key, b"the retry")
    assert store.get(key) == b"the retry"


def test_a_lost_reply_on_a_create_that_landed_reads_as_no_answer(spec: StoreSpec) -> None:
    """The adapter reports a reply it never got as no answer, and the key then holds the blob."""
    from ithildinruntime.core import StoreUnavailable

    if spec.kind == "folder":
        pytest.skip("a folder write has no reply to lose; tests/test_storage_contract.py fakes one")
    key = spec.key(CASE_KEY)
    client = spec.client()

    def lose(**_: object) -> None:
        raise ConnectionClosedError(endpoint_url=client.meta.endpoint_url)

    client.meta.events.register("after-call.s3.PutObject", lose)
    from ithildinruntime.adapters import S3Store

    with pytest.raises(StoreUnavailable):
        S3Store(client, spec.bucket).create(key, b"landed", sha256_of(b"landed"))
    store = spec.open()
    assert store.get(key) == b"landed"
    with pytest.raises(KeyExists):
        _create(store, key, b"landed")


def test_a_folder_create_leaves_no_temporary_file(spec: StoreSpec) -> None:
    if spec.kind != "folder":
        pytest.skip("only the folder writes a temporary file")
    store, key, lease = spec.open(), spec.key(CASE_KEY), spec.lease(1)
    _create(store, key, b"first")
    with pytest.raises(KeyExists):
        _create(store, key, b"second")
    with pytest.raises(ChecksumMismatch):
        store.create(spec.key("runs/ci/other.json"), b"x", sha256_of(b"y"))
    tag = _create(store, lease, b"lease one")
    store.replace_if_match(lease, b"lease two", sha256_of(b"lease two"), tag)
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
    store, key = spec.open(), spec.key(CASE_KEY)
    _create(store, key, b"bytes")
    folder = key.rpartition("/")[0]
    for path in (folder, f"{key}/below.json"):
        with pytest.raises(BlobMissing):
            store.get(path)
        with pytest.raises(BlobMissing):
            store.head(path)


def test_a_folder_create_returns_its_own_tag_when_a_rewrite_follows_at_once(
    spec: StoreSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewrite landing between the link and any later read leaves the creator its own tag."""
    if spec.kind != "folder":
        pytest.skip("S3 returns the tag of the write it answers")
    from ithildinruntime.adapters import folder

    store, rival, lease = spec.open(), spec.open(), spec.lease(1)
    real_link = os.link

    def link_then_rival(source, target) -> None:
        real_link(source, target)
        monkeypatch.setattr(folder.os, "link", real_link)
        rival.replace_if_match(lease, b"rival", sha256_of(b"rival"), rival.head(lease).etag)

    monkeypatch.setattr(folder.os, "link", link_then_rival)
    tag = _create(store, lease, b"creator")
    assert store.get(lease) == b"rival"
    assert tag != store.head(lease).etag
    with pytest.raises(StaleTag):
        store.replace_if_match(lease, b"creator two", sha256_of(b"creator two"), tag)


def test_a_folder_reports_a_file_system_fault_as_no_answer(spec: StoreSpec) -> None:
    """A fault beyond the refusals core names is StoreUnavailable, never a raw error."""
    if spec.kind != "folder":
        pytest.skip("tests/test_s3_answers.py shows S3's answers")
    from ithildinruntime.core import StoreUnavailable

    store, key, lease = spec.open(), spec.key(CASE_KEY), spec.lease(1)
    _create(store, key, b"bytes")
    tag = _create(store, lease, b"lease one")
    folder = os.path.join(spec.root, key.rpartition("/")[0])
    incoming = os.path.join(spec.root, ".incoming")
    os.chmod(folder, 0o000)
    os.chmod(incoming, 0o500)
    try:
        with pytest.raises(StoreUnavailable):
            store.get(key)
        with pytest.raises(StoreUnavailable):
            store.list(spec.key("runs/"), None)
        with pytest.raises(StoreUnavailable):
            store.replace_if_match(lease, b"lease two", sha256_of(b"lease two"), tag)
    finally:
        os.chmod(folder, 0o755)
        os.chmod(incoming, 0o755)
