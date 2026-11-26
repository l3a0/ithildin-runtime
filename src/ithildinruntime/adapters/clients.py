"""The AWS clients the entry points hand the S3 store, marketlake's bucket and SQS.

boto3 is imported here and nowhere else, as the import rule says. Each client is
built the way the adapter that takes it refuses otherwise.

1. **S3 sends each call once.** `S3Store` refuses a client that retries on its
   own, because `core.Storage` retries every read and write under its own
   deadline.
2. **SQS reads a little past its long poll.** `SqsQueue` refuses a client whose
   read timeout is not a little over the twenty-second wait.
3. **The credential comes from the file the VM's provisioning step writes** in
   memory-backed storage, which only the worker's user can read, as
   07-deployment.md's `### Rule 3: the broker key lives in a secret store`
   says. A file that is not named reads the default chain, as on a laptop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import boto3
import botocore.session
from botocore.config import Config

from .sqs import LONGEST_WAIT, READ_MARGIN

__all__ = ["AwsClients"]


class AwsClients:
    """The clients of one region, under one credential file or the default chain."""

    def __init__(self, region: str, credentials: Path | None = None) -> None:
        core = botocore.session.Session()
        if credentials is not None:
            core.set_config_variable("credentials_file", str(credentials))
        self._session = boto3.Session(botocore_session=core, region_name=region)

    def s3(self) -> Any:
        return self._session.client("s3", config=Config(retries={"total_max_attempts": 1}))

    def sqs(self) -> Any:
        return self._session.client(
            "sqs", config=Config(read_timeout=LONGEST_WAIT + READ_MARGIN // 2)
        )

    def queue_url(self, sqs: Any, name: str) -> str:
        """The URL of the queue `name`, which carries the account's id and so is never tracked."""
        return sqs.get_queue_url(QueueName=name)["QueueUrl"]
