"""The AWS clients the entry points hand the S3 store, marketlake's bucket and SQS.

boto3 is imported here and nowhere else, as the import rule says. Each client is
built the way the adapter that takes it refuses otherwise.

1. **S3 sends each call once.** `S3Store` refuses a client that retries on its
   own, because `core.Storage` retries every read and write under its own
   deadline.
2. **SQS reads a little past its long poll.** `SqsQueue` refuses a client whose
   read timeout is not a little over the twenty-second wait.
3. **The credential comes from the named file and nowhere else.** The VM's
   provisioning step writes a short-lived credential to a file in
   memory-backed storage that only the worker's user can read, as
   07-deployment.md's `### Rule 3: the broker key lives in a secret store`
   says, and replaces it before it expires. So the file is the one source the
   clients read: an access key in the process's environment, a profile on disk
   or the machine's own role can never stand in for it. It is read again every
   `REREAD` so a replaced credential is used, and a file that cannot be read
   refuses the start with `CredentialFileError`.
4. **No file named reads the default chain.** Every command names one, because
   `runtime/settings.py` requires it, so the default chain serves only tests
   and code a person runs outside the commands, as on a laptop.

The file holds AWS's shared-credentials form under `[default]`:
`aws_access_key_id`, `aws_secret_access_key` and, for a short-lived role,
`aws_session_token`.
"""

from __future__ import annotations

import configparser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import botocore.session
from botocore.config import Config
from botocore.credentials import CredentialProvider, CredentialResolver, RefreshableCredentials

from .sqs import LONGEST_WAIT, READ_MARGIN

__all__ = ["REREAD", "AwsClients", "CredentialFileError"]

# How often the credential file is read again. botocore refreshes a credential
# fifteen minutes before the expiry it is given, so the expiry sits this much past that.
REREAD = timedelta(minutes=5)
_ADVISORY = timedelta(minutes=15)
_METHOD = "ithildin-credential-file"


class CredentialFileError(Exception):
    """The named credential file cannot be read, or holds no credential."""


def read_credential_file(path: Path) -> dict[str, str]:
    """The credential in `path`, in botocore's metadata form, with an expiry `REREAD` ahead."""
    # No interpolation, so a `%` in a value is a character, and no error quotes a value.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open() as file:
            parser.read_file(file)
        if not parser.has_section("default"):
            raise CredentialFileError(f"the credential file {path} has no [default] section")
        section = parser["default"]
        found = {
            key: section.get(key, "").strip()
            for key in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token")
        }
    except (OSError, UnicodeDecodeError, configparser.Error) as error:
        raise CredentialFileError(
            f"the credential file {path} cannot be read: {type(error).__name__}"
        ) from None
    if not found["aws_access_key_id"] or not found["aws_secret_access_key"]:
        raise CredentialFileError(f"the credential file {path} holds no key and secret")
    expiry = datetime.now(UTC) + _ADVISORY + REREAD
    return {
        "access_key": found["aws_access_key_id"],
        "secret_key": found["aws_secret_access_key"],
        "token": found["aws_session_token"] or None,
        "expiry_time": expiry.isoformat(),
    }


class _FileProvider(CredentialProvider):
    METHOD = _METHOD

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path

    def load(self) -> RefreshableCredentials:
        return RefreshableCredentials.create_from_metadata(
            metadata=read_credential_file(self._path),
            refresh_using=lambda: read_credential_file(self._path),
            method=_METHOD,
        )


class AwsClients:
    """The clients of one region, under one credential file or the default chain."""

    def __init__(self, region: str, credentials: Path | None = None) -> None:
        core = botocore.session.Session()
        if credentials is not None:
            # Read now, so a missing file refuses the start rather than the first call.
            read_credential_file(credentials)
            core.register_component(
                "credential_provider", CredentialResolver(providers=[_FileProvider(credentials)])
            )
        self._session = boto3.Session(botocore_session=core, region_name=region)

    def method(self) -> str | None:
        """Where the clients' credential comes from, as botocore names it."""
        found = self._session.get_credentials()
        return None if found is None else found.method

    def s3(self) -> Any:
        return self._session.client("s3", config=Config(retries={"total_max_attempts": 1}))

    def sqs(self) -> Any:
        return self._session.client(
            "sqs", config=Config(read_timeout=LONGEST_WAIT + READ_MARGIN // 2)
        )

    def queue_url(self, sqs: Any, name: str) -> str:
        """The URL of the queue `name`, which carries the account's id and so is never tracked."""
        return sqs.get_queue_url(QueueName=name)["QueueUrl"]
