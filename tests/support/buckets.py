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
        return S3Bucket(self.client, self.name)  # type: ignore[arg-type]

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
        self.client.put_object(Bucket=self.name, Key=key, Body=data, **extra)
