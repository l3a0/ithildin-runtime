"""The read-only bucket suite: one set of cases, run against the folder and S3 on moto.

`core.Bucket` reads an object's bytes with the bucket's version and stored
SHA-256, and a missing key reads as `BlobMissing` from every adapter, so the
mirror above it never branches on the mode. tests/aws/test_bucket_read.py runs
the S3 cases against the test environment's versioned bucket too.
"""

from __future__ import annotations

import base64
import hashlib
import io
import itertools
import ssl

import boto3
import pytest
from botocore.config import Config

from ithildinruntime.adapters import S3Bucket
from ithildinruntime.core import BlobMissing, ChecksumMismatch, StoreUnavailable
from tests.support.buckets import BucketSpec
from tests.support.stores import EMULATOR_KEYS, REGION

_names = itertools.count()


def _client(endpoint: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=REGION,
        config=Config(retries={"mode": "standard", "total_max_attempts": 2}),
        **EMULATOR_KEYS,
    )


@pytest.fixture(params=["folder", "s3"])
def bucket(request: pytest.FixtureRequest, tmp_path) -> BucketSpec:
    if request.param == "folder":
        return BucketSpec("folder", root=tmp_path / "bucket")
    client = _client(request.getfixturevalue("emulator"))
    name = f"bucket-suite-{next(_names)}"
    client.create_bucket(Bucket=name)
    client.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    return BucketSpec("s3", client=client, name=name)


def check_reads_bytes_version_and_sha256(spec: BucketSpec) -> None:
    key = spec.key("bars/ticker=GLD/freq=1d/date=2012-04-09.parquet")
    spec.put(key, b"a partition")
    got = spec.open().read(key)
    assert (got.key, got.data) == (key, b"a partition")
    assert got.sha256 == hashlib.sha256(b"a partition").hexdigest()
    assert isinstance(got.version, str) and got.version


def check_a_new_version_reads_as_another_version(spec: BucketSpec) -> None:
    key = spec.key("quarantine.jsonl")
    spec.put(key, b"one\n")
    first = spec.open().read(key)
    spec.put(key, b"one\ntwo\n")
    second = spec.open().read(key)
    assert second.data == b"one\ntwo\n"
    assert second.version != first.version


def check_a_read_at_a_version_gives_that_version_or_none(spec: BucketSpec) -> None:
    key = spec.key("journal/seg-1.arrows")
    spec.put(key, b"one")
    first = spec.open().read(key)
    assert spec.open().read(key, first.version).data == b"one"
    spec.put(key, b"two")
    newest = spec.open().read(key)
    assert spec.open().read(key, newest.version).data == b"two"
    if spec.kind == "s3":
        old = spec.open().read(key, first.version)
        assert (old.data, old.version) == (b"one", first.version)
    else:
        # A folder holds one version of a file, the one its bytes hash to.
        with pytest.raises(BlobMissing):
            spec.open().read(key, first.version)
    for missing, version in ((key, spec.missing_version()), (spec.key("absent"), first.version)):
        with pytest.raises(BlobMissing):
            spec.open().read(missing, version)


def check_a_listing_is_sorted_by_bytes_and_holds_a_prefix_alone(spec: BucketSpec) -> None:
    keys = ["a-b", "a/b", "a/bc/d", "ab", "a/é", "a/\U0001f600", "a/\ue000", "b"]
    for key in keys:
        spec.put(spec.key(key), key.encode())
    bucket = spec.open()
    everything = [spec.key(k) for k in sorted(keys, key=str.encode)]
    assert everything.index(spec.key("a-b")) < everything.index(spec.key("a/b"))
    assert list(bucket.list(spec.key("a"))) == everything[:-1]
    under = [k for k in everything if k.startswith(spec.key("a/"))]
    assert list(bucket.list(spec.key("a/"))) == under
    assert list(bucket.list(spec.key("a/b"))) == [spec.key("a/b"), spec.key("a/bc/d")]
    assert list(bucket.list(spec.key("b"))) == [spec.key("b")]


def check_a_prefix_that_holds_nothing_is_an_empty_list(spec: BucketSpec) -> None:
    spec.put(spec.key("a/b"), b"1")
    for prefix in ("absent/", "a/c", "a/b/", "c"):
        assert list(spec.open().list(spec.key(prefix))) == []


def check_a_missing_key_reads_as_blob_missing(spec: BucketSpec) -> None:
    with pytest.raises(BlobMissing):
        spec.open().read(spec.key("manifest.jsonl"))


def test_the_bucket_reads_an_objects_bytes_version_and_sha256(
    bucket: BucketSpec, request: pytest.FixtureRequest
) -> None:
    if bucket.kind == "s3":
        # moto stores the checksum and returns none on a read. The stubbed
        # answers below show the adapter reading S3's own, and the test
        # environment's bucket shows S3 giving it.
        request.applymarker(pytest.mark.xfail(strict=True, reason="moto returns no checksum"))
    check_reads_bytes_version_and_sha256(bucket)


def test_a_new_version_of_a_key_reads_as_another_version(bucket: BucketSpec) -> None:
    check_a_new_version_reads_as_another_version(bucket)


def test_a_missing_key_reads_as_blob_missing(bucket: BucketSpec) -> None:
    check_a_missing_key_reads_as_blob_missing(bucket)


def test_a_read_at_a_version_gives_that_version_or_none(bucket: BucketSpec) -> None:
    check_a_read_at_a_version_gives_that_version_or_none(bucket)


def test_a_listing_is_sorted_by_bytes_and_holds_a_prefix_alone(bucket: BucketSpec) -> None:
    check_a_listing_is_sorted_by_bytes_and_holds_a_prefix_alone(bucket)


def test_a_prefix_that_holds_nothing_is_an_empty_list(bucket: BucketSpec) -> None:
    check_a_prefix_that_holds_nothing_is_an_empty_list(bucket)
    assert list(bucket.open().list("")) == ["a/b"]


def test_a_bucket_that_holds_nothing_lists_nothing(bucket: BucketSpec) -> None:
    assert list(bucket.open().list("")) == []
    assert list(bucket.open().list("journal/")) == []


def test_a_listing_follows_every_page(bucket: BucketSpec) -> None:
    keys = [f"journal/{n:04d}" for n in range(1001)]
    for key in keys:
        bucket.put(key, b"")
    assert list(bucket.open().list("journal/")) == keys


def test_a_bucket_reads_and_lists_only_its_own_prefix(bucket: BucketSpec) -> None:
    """marketlake's live segments sit under `<prefix>-live/`, beside its lake under `<prefix>/`."""
    bucket.put("a/one", b"1")
    bucket.put_beside("a/two", b"2")
    assert list(bucket.open().list("")) == ["a/one"]
    assert list(bucket.open_beside().list("a/")) == ["a/two"]
    assert bucket.open_beside().read("a/two").data == b"2"
    with pytest.raises(BlobMissing):
        bucket.open().read("a/two")


@pytest.mark.parametrize("prefix", ["", "/", "lake/", "/lake", "/lake/"])
def test_an_s3_bucket_is_rooted_at_a_prefix_with_no_slash_at_either_end(prefix: str) -> None:
    with pytest.raises(ValueError, match="is not a prefix"):
        S3Bucket(object(), "b", prefix)
    with pytest.raises(TypeError):
        S3Bucket(object(), "b")  # type: ignore[call-arg]


@pytest.mark.parametrize("key", ["../outside", "/root", "a/../../b", "a\\b", ""])
def test_a_key_that_could_climb_out_of_the_bucket_is_refused(bucket: BucketSpec, key: str) -> None:
    with pytest.raises(ValueError):
        bucket.open().read(key)
    if key:
        with pytest.raises(ValueError):
            bucket.open().list(key)


def _stubbed(reply: dict):
    from botocore.response import StreamingBody
    from botocore.stub import Stubber

    client = boto3.client(
        "s3", region_name=REGION, endpoint_url="http://stubbed.invalid", **EMULATOR_KEYS
    )
    body = {"Body": StreamingBody(io.BytesIO(b"a partition"), len(b"a partition")), **reply}
    stub = Stubber(client)
    stub.add_response(
        "get_object", body, {"Bucket": "b", "Key": "lake/k", "ChecksumMode": "ENABLED"}
    )
    stub.activate()
    return client


SHA = hashlib.sha256(b"a partition")
B64 = base64.b64encode(SHA.digest()).decode()


@pytest.mark.parametrize(
    ("reply", "version", "sha256"),
    [
        (
            {
                "VersionId": "v1",
                "ChecksumSHA256": B64,
                "ChecksumType": "FULL_OBJECT",
            },
            "v1",
            SHA.hexdigest(),
        ),
        ({"VersionId": "v1", "ChecksumSHA256": "AAAA", "ChecksumType": "COMPOSITE"}, "v1", None),
        ({"VersionId": "null"}, None, None),
        ({}, None, None),
    ],
)
def test_the_s3_bucket_reads_s3s_version_and_whole_object_sha256(
    reply: dict, version: str | None, sha256: str | None
) -> None:
    got = S3Bucket(_stubbed(reply), "b", "lake").read("k")
    assert (got.data, got.version, got.sha256) == (b"a partition", version, sha256)


def test_an_object_stored_with_no_sha256_reads_with_none(emulator: str) -> None:
    spec = BucketSpec("s3", client=_client(emulator), name=f"bucket-suite-{next(_names)}")
    spec.client.create_bucket(Bucket=spec.name)
    spec.client.put_bucket_versioning(
        Bucket=spec.name, VersioningConfiguration={"Status": "Enabled"}
    )
    spec.put("manifest.jsonl", b"{}\n", checksum=False)
    assert spec.open().read("manifest.jsonl").sha256 is None


def test_an_unversioned_bucket_reads_with_no_version(emulator: str) -> None:
    spec = BucketSpec("s3", client=_client(emulator), name=f"bucket-suite-{next(_names)}")
    spec.client.create_bucket(Bucket=spec.name)
    spec.put("manifest.jsonl", b"{}\n")
    assert spec.open().read("manifest.jsonl").version is None


def test_a_missing_bucket_reads_as_unavailable_not_as_a_missing_key(emulator: str) -> None:
    spec = BucketSpec("s3", client=_client(emulator), name="no-such-bucket")
    for call in (
        lambda: spec.open().read("manifest.jsonl"),
        lambda: spec.open().read("manifest.jsonl", "v1"),
        lambda: spec.open().list("journal/"),
    ):
        with pytest.raises(StoreUnavailable, match="404 NoSuchBucket") as refused:
            call()
        assert not isinstance(refused.value, BlobMissing)


def test_a_folder_that_cannot_be_read_is_unavailable_and_never_empty(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ithildinruntime.adapters import FolderBucket

    bucket = FolderBucket(tmp_path)
    (tmp_path / "journal").mkdir()

    def refuse(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("os.scandir", refuse)
    with pytest.raises(StoreUnavailable, match="journal/: PermissionError"):
        bucket.list("journal/")


def _refusing(operation: str, code: str, status: int) -> S3Bucket:
    from botocore.stub import Stubber

    client = boto3.client(
        "s3", region_name=REGION, endpoint_url="http://stubbed.invalid", **EMULATOR_KEYS
    )
    stub = Stubber(client)
    stub.add_client_error(operation, code, "said so", status)
    stub.activate()
    return S3Bucket(client, "b", "lake")


@pytest.mark.parametrize(
    ("code", "status", "kind"),
    [
        ("NoSuchVersion", 404, BlobMissing),
        ("NoSuchKey", 404, BlobMissing),
        ("NoSuchBucket", 404, StoreUnavailable),
        ("AccessDenied", 403, StoreUnavailable),
        ("InvalidArgument", 400, StoreUnavailable),
        ("SlowDown", 503, StoreUnavailable),
    ],
)
def test_a_read_s3_refuses_carries_its_answer(code: str, status: int, kind: type) -> None:
    with pytest.raises(kind, match=f"{status} {code}: said so") as refused:
        _refusing("get_object", code, status).read("k", "v1")
    assert type(refused.value) is kind


@pytest.mark.parametrize(
    ("code", "status"),
    [("AccessDenied", 403), ("NoSuchBucket", 404), ("NoSuchKey", 404), ("SlowDown", 503)],
)
def test_a_listing_s3_refuses_is_unavailable_with_its_answer(code: str, status: int) -> None:
    """A missing grant reads as its 403, and never as a prefix that holds nothing."""
    with pytest.raises(StoreUnavailable, match=f"journal/: {status} {code}: said so"):
        _refusing("list_objects_v2", code, status).list("journal/")


def test_a_bucket_that_does_not_answer_is_unavailable() -> None:
    from botocore.exceptions import EndpointConnectionError

    class Silent:
        def get_object(self, **kwargs):
            raise EndpointConnectionError(endpoint_url="http://stubbed.invalid")

        def get_paginator(self, operation):
            return self

        def paginate(self, **kwargs):
            raise EndpointConnectionError(endpoint_url="http://stubbed.invalid")

    for call in (lambda b: b.read("k"), lambda b: b.list("journal/")):
        with pytest.raises(StoreUnavailable, match="no answer"):
            call(S3Bucket(Silent(), "b", "lake"))


def test_bytes_that_fail_the_stored_checksum_are_a_mismatch() -> None:
    """botocore checks them as the body is read, against the SHA-256 S3 stored."""
    from botocore.httpchecksum import Sha256Checksum, StreamingChecksumBody

    body = StreamingChecksumBody(io.BytesIO(b"damaged"), len(b"damaged"), Sha256Checksum(), B64)

    class Answers:
        def get_object(self, **kwargs):
            return {"Body": body, "VersionId": "v1", "ChecksumSHA256": B64}

    with pytest.raises(ChecksumMismatch, match="k: .*did not match"):
        S3Bucket(Answers(), "b", "lake").read("k")


@pytest.mark.parametrize(
    "fault",
    [
        ssl.SSLError("DECRYPTION_FAILED_OR_BAD_RECORD_MAC"),
        ssl.SSLEOFError("EOF occurred in violation of protocol"),
        TimeoutError("timed out"),
        ConnectionResetError("reset by peer"),
    ],
    ids=lambda fault: type(fault).__name__,
)
def test_a_body_that_breaks_partway_is_unavailable(fault: Exception) -> None:
    """Through urllib3's own response, whose TLS failures botocore does not wrap."""
    from botocore.response import StreamingBody
    from urllib3.response import HTTPResponse

    class Breaks(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def readinto(self, into) -> int:
            raise fault

    raw = HTTPResponse(body=Breaks(), preload_content=False, headers={"content-length": "9"})

    class Answers:
        def get_object(self, **kwargs):
            return {"Body": StreamingBody(raw, 9), "VersionId": "v1", "ChecksumSHA256": B64}

    with pytest.raises(StoreUnavailable, match="k: no answer: "):
        S3Bucket(Answers(), "b", "lake").read("k")


@pytest.mark.parametrize("status", [403, 404, 503])
@pytest.mark.parametrize("fault", ["reset", "stall"])
def test_an_error_answer_that_breaks_partway_is_unavailable(status: int, fault: str) -> None:
    """botocore reads an error's body outside its own handling, over a real socket here."""
    import socket
    import threading

    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        while True:
            try:
                held, _ = listener.accept()
            except OSError:
                return
            held.recv(65536)
            held.sendall(f"HTTP/1.1 {status} Said So\r\nContent-Length: 900\r\n\r\n<Err".encode())
            if fault == "stall":
                threading.Event().wait(1.5)
            if fault == "reset":
                held.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\1\0\0\0\0\0\0\0")
            held.close()

    threading.Thread(target=serve, daemon=True).start()
    client = boto3.client(
        "s3",
        region_name=REGION,
        endpoint_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
        config=Config(read_timeout=0.5, retries={"total_max_attempts": 1}),
        **EMULATOR_KEYS,
    )
    try:
        with pytest.raises(StoreUnavailable, match="k: no answer: ") as refused:
            S3Bucket(client, "b", "lake").read("k")
        assert type(refused.value) is StoreUnavailable
    finally:
        listener.close()


def test_a_process_out_of_memory_is_no_answer_of_the_buckets() -> None:
    class Full:
        def get_object(self, **kwargs):
            raise MemoryError

    with pytest.raises(MemoryError):
        S3Bucket(Full(), "b", "lake").read("k")


def test_a_call_the_client_refuses_to_send_is_a_type_error() -> None:
    """No answer of the bucket's, so it is no refusal a reader tries again."""
    client = boto3.client("s3", region_name=REGION, **EMULATOR_KEYS)
    for call in (lambda b: b.list("journal/"), lambda b: b.read("k")):
        with pytest.raises(TypeError, match="botocore refused the call before sending it"):
            call(S3Bucket(client, "not a bucket name", "lake"))


def test_a_read_at_a_version_asks_s3_for_it() -> None:
    asked = []

    class Answers:
        def get_object(self, **kwargs):
            asked.append(kwargs)
            return {"Body": io.BytesIO(b"x"), "VersionId": "v1", "ChecksumSHA256": B64}

    S3Bucket(Answers(), "b", "lake").read("k", "v1")
    S3Bucket(Answers(), "b", "lake").read("k")
    both = {"Bucket": "b", "Key": "lake/k", "ChecksumMode": "ENABLED"}
    assert asked == [both | {"VersionId": "v1"}, both]
