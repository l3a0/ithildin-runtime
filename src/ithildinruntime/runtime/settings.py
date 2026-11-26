"""What an entry point reads from the machine, once, before it builds anything.

07-deployment.md's rule that the environment is read once, at the edge, says the
provisioning step writes the environment's name and its credentials to the
VM's memory-backed storage, and that none of it goes into a unit's settings,
which any user can read. So a unit's settings name one path, in
`ITHILDIN_SETTINGS`, and the file there holds the rest. Everything else about
the environment comes from the release's table in `runtime/deployment.py`.

The file is TOML with these keys, each a machine value no tracked file holds.

1. `environment`, the name the release's table knows it by.
2. `mirror`, the directory that caches marketlake's bucket.
3. `lake_bucket` and `lake_prefix`, where marketlake's lake is.
4. `pin`, the file the VM resolved the session's release pin into, as
   07-deployment.md's `### Rule 5` says. It holds the release's digest.
5. `release_file`, the local release file the supervisor writes, which starts
   the workers through the host's path unit.
6. `credentials`, the AWS credential file of this unit's user. Optional, and
   the default chain is read without it, as on a laptop.
7. `heartbeat`, the local file a worker touches while it is healthy, which the
   host's watchdog reads. Optional, and nothing is touched without it.

The boot id is read from `/proc/sys/kernel/random/boot_id`, which Linux writes
fresh at each boot. A key that is missing, empty or of the wrong type, or a
file that cannot be read, raises `SettingError`, which each command turns into
one line and the exit code 2.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ithildinruntime.core import is_name

from .deployment import Environment, environment

__all__ = ["BOOT_ID", "SETTINGS_VARIABLE", "SettingError", "Settings", "read_settings"]

SETTINGS_VARIABLE = "ITHILDIN_SETTINGS"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_REQUIRED = ("environment", "mirror", "lake_bucket", "lake_prefix", "pin", "release_file")
_OPTIONAL = ("credentials", "heartbeat")


class SettingError(Exception):
    """A setting that is missing or malformed, named in one line."""


@dataclass(frozen=True)
class Settings:
    environment: Environment
    mirror: Path
    lake_bucket: str
    lake_prefix: str
    pin: Path
    release_file: Path
    boot: str
    credentials: Path | None = None
    heartbeat: Path | None = None

    def release(self) -> str:
        """The session's release, as the VM resolved its pin. Read when it is needed."""
        try:
            text = self.pin.read_text().strip()
        except OSError as error:
            raise SettingError(f"the release pin file {self.pin} cannot be read: {error}") from None
        if not text or any(c.isspace() for c in text):
            raise SettingError(f"the release pin file {self.pin} holds no one release")
        return text


def read_settings(variables: Mapping[str, str] | None = None, boot_id: Path = BOOT_ID) -> Settings:
    """The settings the file `ITHILDIN_SETTINGS` names, or a `SettingError` saying what is wrong."""
    variables = os.environ if variables is None else variables
    named = variables.get(SETTINGS_VARIABLE, "")
    if not named:
        raise SettingError(f"{SETTINGS_VARIABLE} names no settings file")
    try:
        values = tomllib.loads(Path(named).read_text())
    except OSError as error:
        raise SettingError(f"the settings file {named} cannot be read: {error}") from None
    except tomllib.TOMLDecodeError as error:
        raise SettingError(f"the settings file {named} is not TOML: {error}") from None
    extra = sorted(set(values) - set(_REQUIRED) - set(_OPTIONAL))
    if extra:
        raise SettingError(f"the settings file {named} holds keys no reader knows: {extra}")
    for key in _REQUIRED:
        if key not in values:
            raise SettingError(f"the settings file {named} has no {key}")
    for key in (*_REQUIRED, *_OPTIONAL):
        if key in values and (not isinstance(values[key], str) or not values[key].strip()):
            raise SettingError(f"the settings file {named} gives {key} no text")
    try:
        place = environment(values["environment"])
    except KeyError as error:
        raise SettingError(str(error.args[0])) from None
    try:
        boot = boot_id.read_text().strip()
    except OSError as error:
        raise SettingError(f"the boot id at {boot_id} cannot be read: {error}") from None
    if not is_name(boot):
        raise SettingError(f"the boot id at {boot_id} cannot name a key: {boot!r}")
    optional = {key: Path(values[key]) for key in _OPTIONAL if key in values}
    return Settings(
        environment=place,
        mirror=Path(values["mirror"]),
        lake_bucket=values["lake_bucket"],
        lake_prefix=values["lake_prefix"],
        pin=Path(values["pin"]),
        release_file=Path(values["release_file"]),
        boot=boot,
        **optional,
    )
