"""The one codec that writes and reads every blob.

docs/architecture/08-crosscutting-concepts.md's `### File formats and keys`
gives the rules. Every blob is canonical JSON after RFC 8785, written by the
`rfc8785` library, with five rules of the platform's own:

1. A decimal is a JSON string in plain notation, such as `"123.4500"`, and a
   float is refused before the library sees it.
2. An integer is a JSON integer, and one of 2**53 or more is refused.
3. A time is UTC, in RFC 3339 with a `Z` and six fractional digits. A session
   date is `YYYY-MM-DD`.
4. A field with no value is `null`, never left out.
5. A received body is kept as exact bytes, as text when it is valid UTF-8 and
   as base64 otherwise, with a field naming which.

Each field's type in `core` says how the codec writes it. `decode` parses with
Python's `json`, rebuilds the named type, and refuses bytes that are not the
canonical encoding of what it parsed.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import re
import types
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import Enum
from functools import cache
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

import rfc8785

__all__ = [
    "CodecError",
    "Places",
    "decode",
    "encode",
    "exceeds_places",
    "from_received",
    "places",
    "to_decimal",
]

_INT_LIMIT = 2**53
# A decimal the codec writes has at most this many digits, and its leading
# digit sits within this many places of the decimal point, so a hostile
# received value such as 1E+99999999 cannot make a writer allocate gigabytes.
_MAX_DIGITS = 38
_MAX_MAGNITUDE = 30
_RECEIVED = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_DECIMAL = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TEXT = "utf-8"
_BASE64 = "base64"


class CodecError(ValueError):
    """A value the codec refuses to write, or bytes it refuses to read."""


@dataclass(frozen=True)
class Places:
    """How many decimal places a decimal field declares.

    A field's places never shrink in a later release. `to_decimal` rounds a
    computed value to them, and `exceeds_places` flags a received value that
    carries more, which is kept rather than rounded.
    """

    n: int


def places(kind: type, field: str) -> int:
    """The places a decimal field of `kind` declares, or of a mapping's values."""
    hint = _hints(kind)[field]
    for candidate in [hint, *_inner(hint)]:
        for extra in getattr(candidate, "__metadata__", ()):
            if isinstance(extra, Places):
                return extra.n
    raise CodecError(f"{kind.__name__}.{field} declares no places")


def _inner(hint: Any) -> list[Any]:
    found = []
    for arg in get_args(hint):
        found.append(arg)
        found.extend(_inner(arg))
    return found


def exceeds_places(value: Decimal, n: int) -> bool:
    """Whether a decimal carries more places than `n`, which a record flags."""
    exponent = value.as_tuple().exponent
    return isinstance(exponent, int) and -exponent > n


def to_decimal(value: float | int | Decimal, n: int) -> Decimal:
    """Turn a computed value into a decimal at `n` places, rounding half to even.

    A float is taken in its shortest form first, so `0.1` becomes `0.1` rather
    than the fifty-five digits of the double behind it.
    """
    exact = _finite(value)
    try:
        return exact.quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_EVEN)
    except InvalidOperation as error:
        raise CodecError(f"{value!r} does not fit at {n} places") from error


def from_received(value: float | int | str | Decimal) -> Decimal:
    """Keep a value received from outside exactly as it arrived, never rounded.

    A string is read as the decimal it writes, trailing zeros kept, and only
    in ASCII digits with an optional minus, fraction and exponent, so a form
    Python would rewrite, such as `1_000` or `+5`, is refused rather than
    changed. A float, which is how marketlake stores a price, is read as its
    shortest round-trip text, `Decimal(repr(x))`. `to_decimal` is for computed
    values only.
    """
    if isinstance(value, str):
        if not _RECEIVED.fullmatch(value):
            raise CodecError(f"received value {value!r} is not a decimal in ASCII digits")
        try:
            parsed = Decimal(value)
        except InvalidOperation as error:
            raise CodecError(f"received value {value!r} is not a decimal") from error
        return _finite(parsed)
    return _finite(value)


def _finite(value: object) -> Decimal:
    if isinstance(value, bool):
        raise CodecError("a bool is not a decimal")
    if isinstance(value, float):
        value = Decimal(float.__repr__(value))
    elif isinstance(value, int):
        value = Decimal(value)
    if not isinstance(value, Decimal):
        raise CodecError(f"{type(value).__name__} is not a decimal")
    return _bounded(value, "the value")


def _bounded(value: Decimal, where: str) -> Decimal:
    if not value.is_finite():
        raise CodecError(f"{where} is {value}, which is not finite")
    digits = value.as_tuple().digits
    if len(digits) > _MAX_DIGITS or abs(value.adjusted()) > _MAX_MAGNITUDE:
        raise CodecError(
            f"{where} has {len(digits)} digits at magnitude {value.adjusted()}, "
            f"past the codec's {_MAX_DIGITS} digits and magnitude {_MAX_MAGNITUDE}"
        )
    return value


def encode(value: object) -> bytes:
    """Write a record, or a plain JSON value, as canonical JSON bytes."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return rfc8785.dumps(_fields_to_plain(value, type(value).__name__))
    return rfc8785.dumps(_to_plain(value, object, "value"))


def decode[T](data: bytes, kind: type[T]) -> T:
    """Read bytes of a named kind back into a value, refusing non-canonical bytes."""
    if not isinstance(data, bytes):
        raise CodecError("decode reads bytes")
    try:
        parsed = json.loads(
            data.decode("utf-8"),
            parse_float=_refuse_float,
            parse_constant=_refuse_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise CodecError(f"not JSON: {error}") from error
    value = _from_plain(parsed, kind, kind.__name__)
    if encode(value) != data:
        raise CodecError(f"not the canonical encoding of the {kind.__name__} it holds")
    return value


def _refuse_float(text: str) -> None:
    raise CodecError(f"a JSON float {text} is never canonical here")


def _refuse_constant(text: str) -> None:
    raise CodecError(f"{text} is never canonical")


@cache
def _hints(kind: type) -> dict[str, Any]:
    return get_type_hints(kind, include_extras=True)


def _strip(hint: Any) -> Any:
    """Drop `Annotated` and `NewType` wrappers, which change nothing on the wire."""
    while True:
        if get_origin(hint) is Annotated:
            hint = get_args(hint)[0]
        elif hasattr(hint, "__supertype__"):
            hint = hint.__supertype__
        else:
            return hint


def _optional(hint: Any) -> tuple[bool, Any]:
    if get_origin(hint) in (Union, types.UnionType):
        arms = [arm for arm in get_args(hint) if arm is not type(None)]
        if len(arms) != 1:
            raise CodecError(f"the codec reads one type or None, not {hint}")
        return True, arms[0]
    return False, hint


def _to_plain(value: object, hint: Any, where: str) -> object:
    hint = _strip(hint)
    nullable, hint = _optional(hint)
    hint = _strip(hint)
    if value is None:
        if nullable or hint is object or hint is Any:
            return None
        raise CodecError(f"{where} is None where a value is required")
    if isinstance(value, float):
        raise CodecError(f"{where} is a float, which no type in core holds")
    if hint is object or hint is Any:
        return _plain_by_value(value, where)
    origin = get_origin(hint)
    if origin is tuple:
        if not isinstance(value, tuple | list):
            raise CodecError(f"{where} is not a sequence")
        (item,) = [arg for arg in get_args(hint) if arg is not Ellipsis]
        return [_to_plain(x, item, f"{where}[{i}]") for i, x in enumerate(value)]
    if origin is Mapping:
        if not isinstance(value, Mapping):
            raise CodecError(f"{where} is not a mapping")
        key_hint, item = get_args(hint)
        return {
            _key(k, key_hint, where): _to_plain(v, item, f"{where}[{k!r}]")
            for k, v in value.items()
        }
    if dataclasses.is_dataclass(hint):
        if type(value) is not hint:
            raise CodecError(f"{where} is a {type(value).__name__}, not a {hint.__name__}")
        return _fields_to_plain(value, where)
    return _scalar_to_plain(value, hint, where)


def _fields_to_plain(value: Any, where: str) -> dict[str, object]:
    hints = _hints(type(value))
    return {
        f.name: _to_plain(getattr(value, f.name), hints[f.name], f"{where}.{f.name}")
        for f in dataclasses.fields(value)
    }


def _key(key: object, hint: Any, where: str) -> str:
    hint = _strip(hint)
    if isinstance(hint, type) and issubclass(hint, Enum):
        if not isinstance(key, hint):
            raise CodecError(f"{where} has a key {key!r} that is not a {hint.__name__}")
        return str(key.value)
    if not isinstance(key, str):
        raise CodecError(f"{where} has a key {key!r} that is not a string")
    return str.__str__(key)


def _scalar_to_plain(value: object, hint: Any, where: str) -> object:
    if isinstance(hint, type) and issubclass(hint, Enum):
        if not isinstance(value, hint):
            raise CodecError(f"{where} is not a {hint.__name__}")
        return value.value
    if hint is bool:
        if not isinstance(value, bool):
            raise CodecError(f"{where} is not a bool")
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CodecError(f"{where} is not an int")
        return _int(value, where)
    if hint is str:
        if not isinstance(value, str):
            raise CodecError(f"{where} is not a string")
        return str.__str__(value)
    if hint is Decimal:
        if not isinstance(value, Decimal):
            raise CodecError(f"{where} is a {type(value).__name__}, not a Decimal")
        return _decimal(value, where)
    if hint is datetime:
        if not isinstance(value, datetime):
            raise CodecError(f"{where} is not a datetime")
        return _time(value, where)
    if hint is date:
        if isinstance(value, datetime) or not isinstance(value, date):
            raise CodecError(f"{where} is not a date")
        return value.isoformat()
    if hint is bytes:
        if not isinstance(value, bytes):
            raise CodecError(f"{where} is not bytes")
        return _body(value)
    raise CodecError(f"{where} has a type the codec does not write: {hint}")


def _plain_by_value(value: object, where: str) -> object:
    """Write a field typed `object`, which holds plain JSON values only.

    `decode` hands such a field back as the JSON it parsed, so a decimal, a
    time, bytes or a record written here could not be read back as what it
    was. The codec refuses them, and the field's owner gives it a type instead.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        raise CodecError(f"{where} is an enum, which a field typed object cannot read back")
    if isinstance(value, int):
        return _int(value, where)
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, Mapping):
        return {
            _key(k, str, where): _plain_by_value(v, f"{where}[{k!r}]") for k, v in value.items()
        }
    if isinstance(value, tuple | list):
        return [_plain_by_value(x, f"{where}[{i}]") for i, x in enumerate(value)]
    raise CodecError(
        f"{where} is a {type(value).__name__}, which a field typed object cannot read back"
    )


def _int(value: int, where: str) -> int:
    if not -_INT_LIMIT < value < _INT_LIMIT:
        raise CodecError(f"{where} is {value}, whose magnitude reaches 2**53")
    return int(value)


def _decimal(value: Decimal, where: str) -> str:
    return format(_bounded(value, where), "f")


def _time(value: datetime, where: str) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CodecError(f"{where} is not a UTC time")
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}T"
        f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}.{value.microsecond:06d}Z"
    )


def _body(value: bytes) -> dict[str, str]:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return {"data": base64.b64encode(value).decode("ascii"), "encoding": _BASE64}
    return {"data": text, "encoding": _TEXT}


def _from_plain(value: object, hint: Any, where: str) -> Any:
    hint = _strip(hint)
    nullable, hint = _optional(hint)
    hint = _strip(hint)
    if value is None:
        if nullable or hint is object or hint is Any:
            return None
        raise CodecError(f"{where} is null where a value is required")
    if hint is object or hint is Any:
        return value
    origin = get_origin(hint)
    if origin is tuple:
        if not isinstance(value, list):
            raise CodecError(f"{where} is not a list")
        (item,) = [arg for arg in get_args(hint) if arg is not Ellipsis]
        return tuple(_from_plain(x, item, f"{where}[{i}]") for i, x in enumerate(value))
    if origin is Mapping:
        if not isinstance(value, dict):
            raise CodecError(f"{where} is not an object")
        key_hint, item = get_args(hint)
        key_hint = _strip(key_hint)
        return {
            _read_key(k, key_hint, where): _from_plain(v, item, f"{where}[{k!r}]")
            for k, v in value.items()
        }
    if dataclasses.is_dataclass(hint):
        if not isinstance(value, dict):
            raise CodecError(f"{where} is not an object")
        hints = _hints(hint)
        names = [f.name for f in dataclasses.fields(hint)]
        if sorted(value) != sorted(names):
            raise CodecError(f"{where} holds fields {sorted(value)}, not {sorted(names)}")
        return hint(**{n: _from_plain(value[n], hints[n], f"{where}.{n}") for n in names})
    return _scalar_from_plain(value, hint, where)


def _read_key(key: str, hint: Any, where: str) -> object:
    if isinstance(hint, type) and issubclass(hint, Enum):
        try:
            return hint(key)
        except ValueError as error:
            raise CodecError(f"{where} has a key {key!r} that is no {hint.__name__}") from error
    return key


def _scalar_from_plain(value: object, hint: Any, where: str) -> object:
    if isinstance(hint, type) and issubclass(hint, Enum):
        try:
            return hint(value)
        except ValueError as error:
            raise CodecError(f"{where} is {value!r}, no {hint.__name__}") from error
    if hint is bool:
        if not isinstance(value, bool):
            raise CodecError(f"{where} is not a bool")
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CodecError(f"{where} is not an int")
        return value
    if not isinstance(value, str | dict):
        raise CodecError(f"{where} is a {type(value).__name__} where the codec wrote a string")
    if hint is str:
        if not isinstance(value, str):
            raise CodecError(f"{where} is not a string")
        return value
    if hint is Decimal:
        if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
            raise CodecError(f"{where} is {value!r}, not a decimal in plain notation")
        return Decimal(value)
    if hint is datetime:
        if not isinstance(value, str) or not _TIME.fullmatch(value):
            raise CodecError(f"{where} is {value!r}, not a UTC time")
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        except ValueError as error:
            raise CodecError(f"{where} is {value!r}, no real time") from error
    if hint is date:
        if not isinstance(value, str) or not _DATE.fullmatch(value):
            raise CodecError(f"{where} is {value!r}, not a session date")
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise CodecError(f"{where} is {value!r}, no real date") from error
    if hint is bytes:
        return _read_body(value, where)
    raise CodecError(f"{where} has a type the codec does not read: {hint}")


def _read_body(value: object, where: str) -> bytes:
    if not isinstance(value, dict) or sorted(value) != ["data", "encoding"]:
        raise CodecError(f"{where} is not a body")
    data, form = value["data"], value["encoding"]
    if not isinstance(data, str):
        raise CodecError(f"{where} holds no text")
    if form == _TEXT:
        return data.encode("utf-8")
    if form == _BASE64:
        try:
            return base64.b64decode(data, validate=True)
        except binascii.Error as error:
            raise CodecError(f"{where} is not base64") from error
    raise CodecError(f"{where} names an encoding {form!r} the codec does not know")
