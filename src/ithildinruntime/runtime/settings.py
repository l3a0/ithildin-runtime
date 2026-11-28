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
6. `credentials`, the AWS credential file of this unit's user, which is the one
   credential the unit's clients read, as `adapters/clients.py` says. Every
   environment the release's table names runs on a VM, so it is required.
7. `heartbeat`, the local file a worker touches while it is healthy, which the
   host's watchdog reads. Optional, and nothing is touched without it.

The forwarder on the host reads a file of its own, through
`read_forwarder_settings`, whose keys that function lists. It needs none of a
worker's keys, and it holds the push topic's address, which no worker's does.

The boot id is read from `/proc/sys/kernel/random/boot_id`, which Linux writes
fresh at each boot. A key that is missing, empty or of the wrong type, or a
file that cannot be read, raises `SettingError`, which each command turns into
one line and the exit code 2.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ithildinruntime.core import is_name

from .deployment import Environment, environment

__all__ = [
    "BOOT_ID",
    "SETTINGS_VARIABLE",
    "ForwarderSettings",
    "SettingError",
    "Settings",
    "read_forwarder_settings",
    "read_settings",
]

SETTINGS_VARIABLE = "ITHILDIN_SETTINGS"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_REQUIRED = (
    "environment",
    "mirror",
    "lake_bucket",
    "lake_prefix",
    "pin",
    "release_file",
    "credentials",
)
_OPTIONAL = ("heartbeat",)


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
    credentials: Path
    heartbeat: Path | None = None

    def release(self) -> str:
        """The session's release, as the VM resolved its pin. Read when it is needed."""
        try:
            text = self.pin.read_text().strip()
        except (OSError, UnicodeDecodeError) as error:
            raise SettingError(f"the release pin file {self.pin} cannot be read: {error}") from None
        if not text or any(c.isspace() for c in text):
            raise SettingError(f"the release pin file {self.pin} holds no one release")
        return text


def _values(
    variables: Mapping[str, str] | None, required: tuple[str, ...], optional: tuple[str, ...]
) -> dict[str, str]:
    """The keys of the settings file `ITHILDIN_SETTINGS` names, each checked to hold text."""
    variables = os.environ if variables is None else variables
    named = variables.get(SETTINGS_VARIABLE, "")
    if not named:
        raise SettingError(f"{SETTINGS_VARIABLE} names no settings file")
    try:
        values = tomllib.loads(Path(named).read_text())
    except (OSError, UnicodeDecodeError) as error:
        raise SettingError(f"the settings file {named} cannot be read: {error}") from None
    except tomllib.TOMLDecodeError as error:
        raise SettingError(f"the settings file {named} is not TOML: {error}") from None
    extra = sorted(set(values) - set(required) - set(optional))
    if extra:
        raise SettingError(f"the settings file {named} holds keys no reader knows: {extra}")
    for key in required:
        if key not in values:
            raise SettingError(f"the settings file {named} has no {key}")
    for key in (*required, *optional):
        if key in values and (not isinstance(values[key], str) or not values[key].strip()):
            raise SettingError(f"the settings file {named} gives {key} no text")
    return values


def _environment(name: str) -> Environment:
    try:
        return environment(name)
    except KeyError as error:
        raise SettingError(str(error.args[0])) from None


def read_settings(variables: Mapping[str, str] | None = None, boot_id: Path = BOOT_ID) -> Settings:
    """The settings the file `ITHILDIN_SETTINGS` names, or a `SettingError` saying what is wrong."""
    values = _values(variables, _REQUIRED, _OPTIONAL)
    place = _environment(values["environment"])
    try:
        boot = boot_id.read_text().strip()
    except (OSError, UnicodeDecodeError) as error:
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
        credentials=Path(values["credentials"]),
        **optional,
    )


@dataclass(frozen=True)
class ForwarderSettings:
    """What the forwarder on the host reads: the environment, its topic's address and its files."""

    environment: Environment
    address: str = field(repr=False)
    state: Path
    journal: Path | None = None


_FORWARDER_REQUIRED = ("environment", "push_topic", "state")
_FORWARDER_OPTIONAL = ("journal",)


def read_forwarder_settings(variables: Mapping[str, str] | None = None) -> ForwarderSettings:
    """The forwarder's settings, from the file `ITHILDIN_SETTINGS` names.

    The forwarder's file holds its own keys, and none of a worker's.

    1. `environment`, the name the release's table knows it by, which each page names.
    2. `push_topic`, the file the provisioning step writes the environment's push topic's
       address to. The address is the topic's only password, so no error here shows it.
    3. `state`, the memory-backed directory that holds the forwarder's cursor, its count of
       pages a day and its marker of a refused push.
    4. `journal`, the directory of the host's journal, where the forwarder runs in a
       container that mounts it. Optional, and `journalctl` reads the default without it.
    """
    values = _values(variables, _FORWARDER_REQUIRED, _FORWARDER_OPTIONAL)
    place = _environment(values["environment"])
    named = values["push_topic"]
    try:
        address = Path(named).read_text().strip()
    except (OSError, UnicodeDecodeError) as error:
        raise SettingError(
            f"the push topic's file {named} cannot be read: {type(error).__name__}"
        ) from None
    if not address.startswith("https://") or any(c.isspace() for c in address):
        raise SettingError(f"the push topic's file {named} holds no one https address")
    state = Path(values["state"])
    if not state.is_dir():
        raise SettingError(f"the forwarder's state directory {state} is not a directory")
    journal = Path(values["journal"]) if "journal" in values else None
    return ForwarderSettings(place, address, state, journal)
