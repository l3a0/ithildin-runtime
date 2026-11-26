"""The AWS clients read the named credential file and nothing else, and read it again.

Every key here is made up, and no call leaves the machine: a credential is read
from botocore's session, which asks no service for it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ithildinruntime.adapters import AwsClients, CredentialFileError

FILE_KEY, ENV_KEY, NEW_KEY = "AKIAFILEFAKE0000000", "AKIAENVFAKE00000000", "AKIANEWFAKE00000000"


def _file(path: Path, key: str, token: str = "") -> Path:
    more = f"aws_session_token = {token}\n" if token else ""
    path.write_text(f"[default]\naws_access_key_id = {key}\naws_secret_access_key = s\n{more}")
    return path


@pytest.fixture
def no_metadata(monkeypatch) -> None:
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


def test_the_named_file_wins_over_a_key_in_the_environment(
    tmp_path: Path, monkeypatch, no_metadata
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ENV_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "e")
    clients = AwsClients("us-east-1", _file(tmp_path / "c", FILE_KEY, "t0k"))
    found = clients._session.get_credentials()
    assert clients.method() == "ithildin-credential-file"
    frozen = found.get_frozen_credentials()
    assert (frozen.access_key, frozen.token) == (FILE_KEY, "t0k")


def test_a_replaced_file_is_read_again(tmp_path: Path, no_metadata) -> None:
    path = _file(tmp_path / "c", FILE_KEY)
    found = AwsClients("us-east-1", path)._session.get_credentials()
    assert found.get_frozen_credentials().access_key == FILE_KEY
    _file(path, NEW_KEY)
    # Its expiry is what botocore reads to refresh, so it is brought to now.
    found._expiry_time = datetime.now(UTC)
    assert found.get_frozen_credentials().access_key == NEW_KEY


@pytest.mark.parametrize(
    "text", [None, "", "[other]\naws_access_key_id = k\n", "[default]\naws_access_key_id = k\n"]
)
def test_a_file_that_holds_no_credential_refuses_the_start(
    tmp_path: Path, monkeypatch, no_metadata, text: str | None
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ENV_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "e")
    path = tmp_path / "c"
    if text is not None:
        path.write_text(text)
    with pytest.raises(CredentialFileError):
        AwsClients("us-east-1", path)


def test_no_file_reads_the_default_chain(monkeypatch, no_metadata) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ENV_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "e")
    assert AwsClients("us-east-1").method() == "env"


def test_the_credential_is_read_again_twenty_minutes_ahead(tmp_path: Path) -> None:
    from datetime import timedelta

    from ithildinruntime.adapters.clients import read_credential_file

    before = datetime.now(UTC)
    read = read_credential_file(_file(tmp_path / "c", FILE_KEY))
    expiry = datetime.fromisoformat(read["expiry_time"])
    # botocore refreshes fifteen minutes before an expiry, so five minutes after the read.
    assert before + timedelta(minutes=20) <= expiry <= datetime.now(UTC) + timedelta(minutes=20)


@pytest.mark.parametrize(
    "text",
    [
        "aws_secret_access_key = SECRETVALUE\n",
        "[default]\nSECRETVALUE\n",
        # A `%` is a character of the value, and no error quotes what follows it.
        "[default]\naws_access_key_id = %SECRETVALUE\naws_secret_access_key = %(SECRETVALUE\n",
    ],
)
def test_a_file_that_does_not_parse_names_none_of_its_text(
    tmp_path: Path, no_metadata, text: str
) -> None:
    path = tmp_path / "c"
    path.write_text(text)
    try:
        clients = AwsClients("us-east-1", path)
    except CredentialFileError as refused:
        assert "SECRETVALUE" not in str(refused)
    else:
        # A file that parses with `%` in its values gives them back as they are written.
        frozen = clients._session.get_credentials().get_frozen_credentials()
        assert frozen.access_key == "%SECRETVALUE"


def test_a_bad_file_swapped_in_mid_run_raises_and_falls_back_to_nothing(
    tmp_path: Path, monkeypatch, no_metadata
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ENV_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "e")
    path = _file(tmp_path / "c", FILE_KEY)
    found = AwsClients("us-east-1", path)._session.get_credentials()
    path.write_text("[default]\n")
    found._expiry_time = datetime.now(UTC)
    with pytest.raises(CredentialFileError):
        found.get_frozen_credentials()


def test_a_file_that_is_not_utf_8_refuses_the_start(tmp_path: Path, no_metadata) -> None:
    path = tmp_path / "c"
    path.write_bytes(b"[default]\naws_access_key_id = k\naws_secret_access_key = \xff\xfe\n")
    with pytest.raises(CredentialFileError, match="UnicodeDecodeError"):
        AwsClients("us-east-1", path)
