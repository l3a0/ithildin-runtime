"""The stores the store suite runs against, described so a second process can open one.

A test that races two processes hands each a `StoreSpec`, which pickles,
rather than a store, which holds a client or a folder handle.

The S3 store runs against moto's S3 server, started in the test process.
moto honours `If-None-Match` and `If-Match`, and a probe on 2026-11-21 saw one
of 40 racing creates win. It does not check the SHA-256 a write sends, so the
suite marks that case as failing on moto, strictly, and botocore's stubbed
answers in tests/test_s3_answers.py show the adapter's handling of S3's own
refusal. The run against the test environment's bucket, which #88 holds,
closes both gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.config import Config

from ithildinruntime.adapters import FolderStore, S3Store

# Credentials for the emulator only. They reach no AWS endpoint, because
# every client here names the emulator's URL.
EMULATOR_KEYS = {"aws_access_key_id": "emulator", "aws_secret_access_key": "emulator"}
REGION = "us-east-1"


@dataclass(frozen=True)
class StoreSpec:
    kind: str
    root: str | None = None
    endpoint: str | None = None
    bucket: str | None = None

    @property
    def checks_sha256(self) -> bool:
        """Whether the store itself refuses bytes whose SHA-256 differs. moto does not."""
        return self.kind == "folder"

    def open(self) -> FolderStore | S3Store:
        if self.kind == "folder":
            return FolderStore(self.root)
        return S3Store(self.client(), self.bucket)

    def client(self):
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            region_name=REGION,
            config=Config(retries={"mode": "standard", "total_max_attempts": 1}),
            **EMULATOR_KEYS,
        )

    def remove(self, key: str) -> None:
        """Delete a blob by hand, which no worker can do through the port."""
        if self.kind == "folder":
            Path(self.root, key).unlink()
        else:
            self.client().delete_object(Bucket=self.bucket, Key=key)

    def overwrite(self, key: str, data: bytes) -> None:
        """Change a stored blob by hand, around every rule the store keeps."""
        if self.kind == "folder":
            Path(self.root, key).write_bytes(data)
        else:
            self.client().put_object(Bucket=self.bucket, Key=key, Body=data)
