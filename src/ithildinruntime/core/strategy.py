"""Each strategy's tracked configuration: one TOML file, its reader and its hash.

docs/design.md's `### Strategies, schedules and decisions` says what a
strategy brings. Its instruments and every tracked setting its decisions
depend on are written in one file, `<strategy>.toml`, in the `strategies`
folder beside this package's modules, so the release carries it and it is
the same in backtest, shadow, paper and live. The file holds a top-level
`instruments` list and one table per section, such as `[alpha]` or `[cost]`,
whose form each module's issue owns. It holds no secret and no cap.

The hash is the SHA-256 of the file's bytes, so a change anywhere in the
file, a comment included, names a new version, and a file left alone keeps
its hash across releases. Every header a strategy's stage writes records it.

Every refusal is a `Halt` naming the strategy and the file, because a
missing or malformed setting stops trading rather than reading as an empty
or zero one (hard limit 3). The reader refuses these.

1. An empty list of names, a name the key builder refuses, a name given
   twice, a named strategy with no file, and a file that cannot be read,
   is not UTF-8 or is not TOML.
2. A missing or empty `instruments`, one that is not a list of strings, and
   a repeated or empty instrument.
3. A top-level value other than `instruments` that is not a table.
4. A number that is infinite, not a number, or past the codec's bounds,
   because an infinite maximum age would switch a staleness rule off.
5. A local time, a local datetime and a datetime whose offset is not UTC's,
   because every time the platform holds is UTC, and a time that follows the
   close is written as minutes before it. A plain date is allowed.

An empty section reads as a missing one, so a blank `[cost]` never reads as
terms present, and `StrategyConfig.section` halts on either.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from .codec import CodecError, from_received
from .halt import Halt
from .names import is_name
from .records import InstrumentId

__all__ = ["STRATEGIES", "StrategyConfig", "load_strategies", "parse_strategy"]

# The tracked strategy files, which ship inside the package. The import rule
# refuses `importlib.resources`, so they are read through a path, as
# `sessions.py` reads its table.
STRATEGIES = Path(__file__).parents[1] / "strategies"


@dataclass(frozen=True)
class StrategyConfig:
    """One strategy's tracked configuration, as its file holds it.

    `config_hash` is the SHA-256 of the file's bytes, in lower case hex.
    `sections` maps each section's name to its settings, read-only, with
    every empty section left out.
    """

    name: str
    config_hash: str
    instruments: tuple[InstrumentId, ...]
    sections: Mapping[str, Mapping[str, object]]
    source: str

    def section(self, name: str) -> Mapping[str, object]:
        """One section's settings, halting when the file holds none or an empty one."""
        found = self.sections.get(name)
        if found is None:
            raise Halt(
                f"strategy {self.name}'s configuration {self.source} has no [{name}] section, "
                "or an empty one"
            )
        return found


def parse_strategy(name: str, data: bytes, *, source: str | None = None) -> StrategyConfig:
    """Read one strategy's configuration from the bytes of its file.

    It takes bytes rather than a path, so a research sweep can hand bytes of
    its own and their hash names them. `source` names the file in a halt, and
    defaults to `<name>.toml`.
    """
    where = source or f"{name}.toml"
    if not is_name(name):
        raise Halt(f"strategy name {name!r} in {where} cannot be part of a key")

    def refuse(what: str) -> Halt:
        return Halt(f"strategy {name}'s configuration {where} {what}")

    try:
        text = data.decode("utf-8")
        document = tomllib.loads(text, parse_float=Decimal)
    except UnicodeDecodeError as error:
        raise refuse(f"is not UTF-8: {error}") from None
    except tomllib.TOMLDecodeError as error:
        raise refuse(f"is not TOML: {error}") from None
    instruments = _instruments(document.get("instruments"), refuse)
    sections: dict[str, Mapping[str, object]] = {}
    for key, value in document.items():
        if key == "instruments":
            continue
        if not isinstance(value, dict):
            raise refuse(f"holds {key} as a {type(value).__name__}, not a section")
        settings = _frozen(value, key, refuse)
        if settings:
            sections[key] = settings
    return StrategyConfig(
        name=name,
        config_hash=hashlib.sha256(data).hexdigest(),
        instruments=instruments,
        sections=MappingProxyType(sections),
        source=where,
    )


def load_strategies(folder: Path, names: Iterable[str]) -> tuple[StrategyConfig, ...]:
    """Read the file of each named strategy in `folder`, sorted by name.

    A deployment names the strategies it runs, so a file in the folder is a
    strategy of no deployment until one names it.
    """
    if isinstance(names, str):
        raise TypeError("load_strategies takes a list of strategy names, not one name")
    names = list(names)
    if not names:
        raise Halt(f"no strategy is named to load from {folder}")
    for name in names:
        if not is_name(name):
            raise Halt(f"strategy name {name!r} cannot be part of a key")
    if repeated := sorted({n for n in names if names.count(n) > 1}):
        raise Halt(f"strategy {', '.join(repeated)} is named more than once")
    loaded = []
    for name in sorted(names):
        path = folder / f"{name}.toml"
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise Halt(f"strategy {name} has no configuration file at {path}") from None
        except OSError as error:
            raise Halt(f"strategy {name}'s configuration {path} cannot be read: {error}") from None
        loaded.append(parse_strategy(name, data, source=f"{folder.name}/{path.name}"))
    return tuple(loaded)


def _instruments(value: object, refuse) -> tuple[InstrumentId, ...]:
    if value is None:
        raise refuse("has no instruments")
    if not isinstance(value, list) or not value:
        raise refuse("holds no list of instruments")
    found: list[InstrumentId] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise refuse(f"lists the instrument {item!r}, which is not an instrument id")
        if item in found:
            raise refuse(f"lists the instrument {item} more than once")
        found.append(InstrumentId(item))
    return tuple(found)


def _frozen(value: object, where: str, refuse) -> object:
    """A section's value, read-only, after the checks on numbers and times."""
    if isinstance(value, dict):
        return MappingProxyType({k: _frozen(v, f"{where}.{k}", refuse) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(v, f"{where}[{n}]", refuse) for n, v in enumerate(value))
    if isinstance(value, bool | str):
        return value
    if isinstance(value, int | Decimal):
        try:
            from_received(value)
        except CodecError as error:
            raise refuse(f"holds {where} = {value}, which the codec refuses: {error}") from None
        return value
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if offset is None:
            raise refuse(f"holds {where} = {value.isoformat()}, a local datetime with no offset")
        if offset != timedelta(0):
            raise refuse(f"holds {where} = {value.isoformat()}, whose offset is not UTC's")
        return value.replace(tzinfo=UTC)
    if isinstance(value, time):
        raise refuse(
            f"holds {where} = {value.isoformat()}, a local time. Write a time that follows "
            "the close as minutes before it"
        )
    if isinstance(value, date):
        return value
    raise refuse(f"holds {where} as a {type(value).__name__}")
