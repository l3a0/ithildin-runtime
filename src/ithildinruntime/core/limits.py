"""The limits an order is held to: the owner's hard caps, the strategy's, and the tighter of each.

The hard caps blob is written by hand, so it has a reader of its own rather
than the codec, which refuses bytes that are not canonical. docs/design.md's
`## Configuration` names the blob, and
docs/architecture/05-building-blocks.md's `##### Where the limits come from`
says how the two sources combine.

Every check here raises `Halt` naming the field, because a limit that is
absent, not finite or of the wrong sign is a missing input (hard limit 3).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from decimal import ROUND_DOWN, Decimal

from .codec import INT_LIMIT, CodecError, from_received
from .exact import exact_product, finite, quotient
from .halt import Halt
from .names import is_name, is_sha256
from .records import Constraints, HardCaps, InstrumentId, Valuation

__all__ = ["parse_caps", "tighten"]

_FORMAT = 1
# A caps blob is written by hand and read before every order, so one far past
# any a person would write is refused before it is parsed.
_MAX_BYTES = 65_536
# The longest a decision may use a stress move past its vintage's last session. A
# larger age would switch that check off.
_MAX_STRESS_AGE_DAYS = 365
# How much of a refused value a halt shows, since its reason is stored and sent.
_SHOWN = 80
_NET_CAP_PLACES = 4
_PLAIN = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SHARES = ("max_order_qty", "max_position")
_MAXIMA = ("max_gross", "loss_stop")
_COUNTS = ("max_open_orders", "moc_cutoff")
_STRESS = ("share", "stress_move", "stress_low", "stress_high")
_FIELDS = frozenset(
    {*_SHARES, *_MAXIMA, *_COUNTS, *_STRESS}
    | {"stress_vintage", "stress_through", "stress_max_age_days"}
)


def parse_caps(data: bytes, account: str) -> HardCaps:
    """The hard caps of `account`, from the bytes of its caps blob.

    A decimal is a JSON string and an integer a JSON integer. Sections of the
    blob beside `format`, `account` and `limits` belong to other readers, so
    this reads none of them. A field of `limits` it does not know halts, so a
    release rolled back past a new cap stops new orders rather than trading
    without it.
    """
    if not is_name(account):
        raise Halt(f"the account whose caps are asked for is {account!r}, which names none")
    blob = _json(data)
    written = blob.get("format")
    if type(written) is not int or written != _FORMAT:
        raise Halt(f"the caps blob's format is {_shown(written)}, not {_FORMAT}")
    if blob.get("account") != account:
        # What was found there is never shown, since it could be a broker's number.
        raise Halt(f"the caps blob does not name the account {account!r}")
    limits = blob.get("limits")
    if not isinstance(limits, dict):
        raise Halt("the caps blob's limits is missing or no JSON object")
    if absent := sorted(_FIELDS - set(limits)):
        raise Halt(f"the caps blob holds no {absent[0]}")
    if unknown := sorted(set(limits) - _FIELDS):
        raise Halt(f"the caps blob holds {_shown(unknown[0])}, which this release does not read")
    vintage = limits["stress_vintage"]
    if not is_sha256(vintage):
        raise Halt(f"the caps blob's stress_vintage is {_shown(vintage)}, not a SHA-256")
    caps = HardCaps(
        max_order_qty=_shares("max_order_qty", limits),
        max_position=_shares("max_position", limits),
        max_gross=_decimal("max_gross", limits["max_gross"]),
        max_open_orders=_whole("max_open_orders", limits),
        loss_stop=_decimal("loss_stop", limits["loss_stop"]),
        moc_cutoff=_whole("moc_cutoff", limits),
        share=_decimal("share", limits["share"]),
        stress_move=_decimal("stress_move", limits["stress_move"]),
        stress_low=_decimal("stress_low", limits["stress_low"]),
        stress_high=_decimal("stress_high", limits["stress_high"]),
        stress_vintage=vintage,
        stress_through=_day("stress_through", limits),
        stress_max_age_days=_whole("stress_max_age_days", limits),
    )
    _check_caps(caps)
    return caps


def tighten(constraints: Constraints, caps: HardCaps, valuation: Valuation | None) -> Constraints:
    """The tighter of each limit of `constraints` and `caps`, with every field of both checked.

    The caps' net cap is the share of V over the stress move, rounded down.
    The tighter of two maxima or two loss stops is the smaller, and of two
    cutoffs the larger number of minutes, which is the earlier time.
    """
    if not isinstance(constraints, Constraints):
        raise Halt("the constraints are missing")
    if not isinstance(caps, HardCaps):
        raise Halt("the hard caps are missing")
    _check_constraints(constraints)
    _check_caps(caps)
    if extra := sorted(set(constraints.max_position) - set(caps.max_position)):
        raise Halt(f"the constraints limit {extra[0]}, which the hard caps do not")
    if not isinstance(valuation, Valuation):
        raise Halt("the valuation is missing")
    value = valuation.value
    if value <= 0:
        raise Halt(f"the valuation's value is {value}, not above zero")
    net = quotient(
        "the net cap",
        exact_product("the net cap", caps.share, value),
        caps.stress_move,
        _NET_CAP_PLACES,
        ROUND_DOWN,
    )
    return Constraints(
        strategy=constraints.strategy,
        max_order_qty=_smaller(constraints.max_order_qty, caps.max_order_qty),
        max_position=_smaller(constraints.max_position, caps.max_position),
        max_gross=min(constraints.max_gross, caps.max_gross),
        max_open_orders=min(constraints.max_open_orders, caps.max_open_orders),
        max_net=min(constraints.max_net, net),
        loss_stop=min(constraints.loss_stop, caps.loss_stop),
        moc_cutoff=max(constraints.moc_cutoff, caps.moc_cutoff),
    )


def _smaller(
    one: Mapping[InstrumentId, Decimal], other: Mapping[InstrumentId, Decimal]
) -> dict[InstrumentId, Decimal]:
    return {i: min(limit, other[i]) for i, limit in one.items()}


def _check_constraints(constraints: Constraints) -> None:
    where = "the constraints'"
    if not isinstance(constraints.strategy, str) or not constraints.strategy:
        raise Halt(f"{where} strategy is {constraints.strategy!r}, which names none")
    _check_shared(constraints, where)
    _not_negative(f"{where} max_net", constraints.max_net)


def _check_caps(caps: HardCaps) -> None:
    where = "the hard caps'"
    _check_shared(caps, where)
    for name in _STRESS:
        finite(f"{where} {name}", getattr(caps, name))
    if not 0 < caps.share < 1:
        raise Halt(f"{where} share is {caps.share}, not between 0 and 1")
    if not 0 < caps.stress_low <= caps.stress_high:
        raise Halt(
            f"{where} stress_low is {caps.stress_low}, not above zero and at most the "
            f"stress_high of {caps.stress_high}"
        )
    if not caps.stress_low <= caps.stress_move <= caps.stress_high:
        raise Halt(
            f"{where} stress_move is {caps.stress_move}, not from {caps.stress_low} to "
            f"{caps.stress_high}"
        )
    if not is_sha256(caps.stress_vintage):
        raise Halt(f"{where} stress_vintage is {_shown(caps.stress_vintage)}, not a SHA-256")
    if type(caps.stress_through) is not date:
        raise Halt(f"{where} stress_through is {caps.stress_through!r}, not a date")
    age = _integer(f"{where} stress_max_age_days", caps.stress_max_age_days)
    if not 1 <= age <= _MAX_STRESS_AGE_DAYS:
        raise Halt(
            f"{where} stress_max_age_days is {age}, not a whole number of days from 1 to "
            f"{_MAX_STRESS_AGE_DAYS}"
        )


def _check_shared(source: Constraints | HardCaps, where: str) -> None:
    for name in _SHARES:
        limits = getattr(source, name)
        if not isinstance(limits, Mapping) or not limits:
            raise Halt(f"{where} {name} limits no instrument")
        for instrument, limit in limits.items():
            if not isinstance(instrument, str) or not instrument:
                raise Halt(f"{where} {name} names {instrument!r}, no instrument")
            _not_negative(f"{where} {name} of {instrument}", limit)
    if odd := sorted(set(source.max_order_qty) ^ set(source.max_position)):
        raise Halt(f"{where} max_order_qty and max_position do not both limit {odd[0]}")
    for instrument, limit in source.max_position.items():
        if source.max_order_qty[instrument] < limit:
            raise Halt(
                f"{where} max_order_qty of {instrument} is {source.max_order_qty[instrument]}, "
                f"below its max_position of {limit}, so no order could close that position"
            )
    for name in _MAXIMA:
        _not_negative(f"{where} {name}", getattr(source, name))
    for name in _COUNTS:
        if _integer(f"{where} {name}", getattr(source, name)) < 0:
            raise Halt(f"{where} {name} is {getattr(source, name)}, below zero")


def _not_negative(what: str, value: object) -> None:
    if finite(what, value) < 0:
        raise Halt(f"{what} is {value}, below zero")


def _integer(what: str, value: object) -> int:
    if type(value) is not int:
        raise Halt(f"{what} is {value!r}, not a whole number")
    return value


def _json(data: bytes) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise Halt("the caps blob is missing")
    if len(data) > _MAX_BYTES:
        raise Halt(f"the caps blob is {len(data)} bytes, past the {_MAX_BYTES} allowed")
    try:
        parsed = json.loads(
            data.decode("utf-8"), object_pairs_hook=_once, parse_constant=_no_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise Halt(f"the caps blob is not JSON: {_shown(str(error))}") from error
    if not isinstance(parsed, dict):
        raise Halt("the caps blob is not a JSON object")
    return parsed


def _once(pairs: list[tuple[str, object]]) -> dict[str, object]:
    found: dict[str, object] = {}
    for key, value in pairs:
        if key in found:
            raise Halt(f"the caps blob holds {_shown(key)} twice")
        found[key] = value
    return found


def _shown(value: object) -> str:
    if isinstance(value, int) and abs(value) >= INT_LIMIT:
        return "a huge int"
    text = repr(value)
    return text if len(text) <= _SHOWN else f"{text[:_SHOWN]}…"


def _no_constant(text: str) -> None:
    raise ValueError(f"{text} is no JSON value")


def _decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, str) or not _PLAIN.fullmatch(value):
        raise Halt(f"the caps blob's {name} is {_shown(value)}, not a decimal written as a string")
    try:
        return from_received(value)
    except CodecError as error:
        raise Halt(f"the caps blob's {name} cannot be read: {_shown(str(error))}") from error


def _shares(name: str, limits: Mapping[str, object]) -> dict[InstrumentId, Decimal]:
    value = limits[name]
    if not isinstance(value, dict):
        raise Halt(f"the caps blob's {name} is no JSON object of instruments")
    for instrument in value:
        # A halt names the instrument, so it has to be one a record can hold.
        if (
            not instrument.isprintable()
            or not 0 < len(instrument.strip()) == len(instrument) <= _SHOWN
        ):
            raise Halt(f"the caps blob's {name} names {_shown(instrument)}, no instrument")
    return {InstrumentId(i): _decimal(f"{name} of {i}", v) for i, v in value.items()}


def _whole(name: str, limits: Mapping[str, object]) -> int:
    value = limits[name]
    if type(value) is not int:
        raise Halt(f"the caps blob's {name} is {_shown(value)}, not a JSON integer")
    if abs(value) >= INT_LIMIT:
        raise Halt(f"the caps blob's {name} is {_shown(value)}, past what a record can hold")
    return value


def _day(name: str, limits: Mapping[str, object]) -> date:
    value = limits[name]
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise Halt(f"the caps blob's {name} is {_shown(value)}, not a date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise Halt(f"the caps blob's {name} is {value!r}, no real date") from error
