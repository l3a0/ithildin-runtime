"""The read-only buckets the bucket suite runs against, and how a case puts an object in one.

The port has no call that writes, so each case puts its objects in place the
way marketlake's upload does: on S3 with the object's SHA-256 as its checksum
and the bucket's versioning on, and in a folder as a plain file.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ithildinruntime.adapters import FolderBucket, S3Bucket

# The prefix every S3 case roots its bucket at, as marketlake's target does.
ROOT = "some/lake"


@dataclass
class BucketSpec:
    kind: str
    root: Path | None = None
    client: Any = None
    name: str | None = None
    prefix: str = ""

    def open(self) -> FolderBucket | S3Bucket:
        if self.kind == "folder":
            return FolderBucket(self.root)  # type: ignore[arg-type]
        return S3Bucket(self.client, self.name, ROOT)  # type: ignore[arg-type]

    def open_beside(self) -> FolderBucket | S3Bucket:
        """The bucket beside this one, as `<prefix>-live` is beside `<prefix>`."""
        if self.kind == "folder":
            return FolderBucket(self._beside())
        return S3Bucket(self.client, self.name, f"{ROOT}-live")  # type: ignore[arg-type]

    def put_beside(self, key: str, data: bytes) -> None:
        if self.kind == "folder":
            BucketSpec("folder", root=self._beside()).put(key, data)
        else:
            self.client.put_object(Bucket=self.name, Key=f"{ROOT}-live/{key}", Body=data)

    def missing_version(self) -> str:
        """A version of the form the bucket gives, which it holds no object at."""
        return "0" * 64 if self.kind == "folder" else "00000000-0000-4000-8000-000000000000"

    def _beside(self) -> Path:
        return self.root.with_name(f"{self.root.name}-live")  # type: ignore[union-attr]

    def key(self, key: str) -> str:
        return self.prefix + key

    def put(self, key: str, data: bytes, *, checksum: bool = True) -> None:
        """Store `data` at `key`, as marketlake's upload does, with or without its SHA-256."""
        if self.kind == "folder":
            path = self.root.joinpath(*key.split("/"))  # type: ignore[union-attr]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return
        extra: dict[str, str] = {}
        if checksum:
            digest = hashlib.sha256(data).digest()
            extra["ChecksumSHA256"] = base64.b64encode(digest).decode("ascii")
        self.client.put_object(Bucket=self.name, Key=f"{ROOT}/{key}", Body=data, **extra)
