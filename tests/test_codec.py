"""The one codec: canonical JSON through `rfc8785`, with decimals as strings.

docs/architecture/08-crosscutting-concepts.md's `### File formats and keys`
gives the rules, and #22's acceptance 4 to 6 the checks below.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime import core
from ithildinruntime.core import CodecError, InstrumentId, decode, encode
from tests.core_samples import RECORDS, sample

FIXTURES = Path(__file__).parent / "fixtures" / "codec"
AT = datetime(2026, 11, 2, 20, 40, tzinfo=UTC)


@pytest.mark.parametrize("empty", [False, True], ids=["filled", "empty"])
@pytest.mark.parametrize("kind", RECORDS, ids=lambda k: k.__name__)
def test_every_type_reads_back_unchanged(kind: type, empty: bool) -> None:
    value = sample(kind, empty=empty)
    data = encode(value)
    assert decode(data, kind) == value
    assert encode(decode(data, kind)) == data


def _fixture_values() -> dict[str, object]:
    """The committed values whose bytes are pinned under tests/fixtures/codec."""
    return {
        "journal-entry": core.JournalEntry(
            seq=1000,
            format=1,
            built_at=datetime(2026, 11, 2, 20, 40, 1, 123456, tzinfo=UTC),
            body={
                "kind": "fill",
                "\U0001f600": "above U+FFFF",
                "\ue000": "private use",
                "z": None,
                "qty": "0.00004",
            },
            prev_hash="0" * 64,
        ),
        "target-portfolio": core.TargetPortfolio(
            decision_at=AT,
            strategy="bollinger",
            shares={
                InstrumentId("USO"): Decimal("12.000000000"),
                InstrumentId("GLD"): Decimal("-3.2500"),
            },
        ),
        "ack": core.Ack(
            client_order_id="bollinger-20261102T204000Z-1",
            broker_order_id=None,
            order=None,
            state="unknown",
            outcome="unknown",
            raw=b"\xff\xfe not utf-8",
        ),
        "fill": core.Fill(
            fill_id="f1",
            correction_id=None,
            broker_order_id="b1",
            client_order_id="c1",
            qty=core.from_received("1.10"),
            price=core.from_received(123.45678),
            at=AT,
            received_at=AT + timedelta(microseconds=7),
        ),
    }


@pytest.mark.parametrize("name", sorted(_fixture_values()))
def test_committed_values_encode_to_committed_bytes(name: str) -> None:
    value = _fixture_values()[name]
    expected = (FIXTURES / f"{name}.json").read_bytes()
    assert encode(value) == expected, encode(value).decode("utf-8")
    assert decode(expected, type(value)) == value


def test_keys_sort_by_utf16_units_not_by_code_point() -> None:
    data = (FIXTURES / "journal-entry.json").read_bytes().decode("utf-8")
    body = data[data.index('"body"') :]
    assert body.index("\U0001f600") < body.index("\ue000")
    plain = json.dumps({"\U0001f600": 1, "\ue000": 2}, sort_keys=True, ensure_ascii=False)
    assert plain.index("\U0001f600") > plain.index("\ue000")


@pytest.mark.parametrize(
    "value",
    [
        0.1,
        float("nan"),
        float("inf"),
        {"price": 1.5},
        [Decimal("1"), 2.0],
        2**53,
        -(2**53),
        Decimal("NaN"),
        Decimal("Infinity"),
        datetime(2026, 11, 2, 20, 40),
        datetime(2026, 11, 2, 15, 40, tzinfo=timezone(timedelta(hours=-5))),
        {1: "a key that is not a string"},
        object(),
    ],
)
def test_encode_refuses_what_has_no_canonical_form(value: object) -> None:
    with pytest.raises(CodecError):
        encode(value)


def test_encode_refuses_a_float_inside_a_decimal_field() -> None:
    value = core.JournalAck(seq=1, entry_hash="a")
    with pytest.raises(CodecError):
        encode(core.Verdict(approved=True, limit=None, value=0.5, missing_term=None))  # type: ignore[arg-type]
    assert encode(value) == b'{"entry_hash":"a","seq":1}'


def test_encode_refuses_a_value_of_the_wrong_type_for_its_field() -> None:
    with pytest.raises(CodecError):
        encode(core.JournalAck(seq="1", entry_hash="a"))  # type: ignore[arg-type]
    with pytest.raises(CodecError):
        encode(core.Verdict(approved=1, limit=None, value=None, missing_term=None))  # type: ignore[arg-type]
    with pytest.raises(CodecError):
        encode(core.Heartbeat(writer="w", at=None))  # type: ignore[arg-type]


def test_an_integer_just_below_two_to_the_fifty_third_is_written() -> None:
    assert encode(2**53 - 1) == b"9007199254740991"


@pytest.mark.parametrize(
    "data",
    [
        b'{"seq":1, "entry_hash":"a"}',
        b'{"seq":1,"entry_hash":"a"}',
        b'{"entry_hash":"a","seq":1.0}',
        b'{"entry_hash":"a","seq":1,"seq":1}',
        b'{"entry_hash":"a"}',
        b'{"entry_hash":"a","seq":1,"extra":null}',
        b'{"entry_hash":"\\u0061","seq":1}',
        b'{"entry_hash":"a","seq":true}',
        b"\xff",
        b"not json",
    ],
)
def test_decode_refuses_bytes_that_are_not_canonical(data: bytes) -> None:
    with pytest.raises(CodecError):
        decode(data, core.JournalAck)


@pytest.mark.parametrize(
    "price",
    ['"0123.45"', '"+1"', '"1e5"', '"1."', '".5"', '" 1"', "1", '"NaN"'],
)
def test_decode_refuses_a_decimal_not_in_plain_notation(price: str) -> None:
    data = (
        '{"cash":"1","marks":{"GLD":' + price + '},"replayed_through":1,'
        '"session":"2026-11-02","value":"1"}'
    ).encode()
    with pytest.raises(CodecError):
        decode(data, core.Valuation)


@pytest.mark.parametrize(
    "at",
    [
        '"2026-11-02T20:40:00Z"',
        '"2026-11-02T20:40:00.000000+00:00"',
        '"2026-11-02 20:40:00.000000Z"',
    ],
)
def test_decode_refuses_a_time_in_another_form(at: str) -> None:
    with pytest.raises(CodecError):
        decode(('{"at":' + at + ',"writer":"w"}').encode(), core.Heartbeat)


def test_a_received_body_is_kept_as_text_or_as_base64() -> None:
    text = core.BrokerRead(raw=b'{"qty": "1.10"}\r\n\x00', value=None)
    binary = core.BrokerRead(raw=b"\xff\xfe\x00", value=None)
    assert json.loads(encode(text))["raw"] == {
        "data": '{"qty": "1.10"}\r\n\x00',
        "encoding": "utf-8",
    }
    assert json.loads(encode(binary))["raw"] == {"data": "//4A", "encoding": "base64"}
    for value in (text, binary):
        assert decode(encode(value), core.BrokerRead).raw == value.raw


def test_a_utf8_body_written_as_base64_is_not_canonical() -> None:
    data = b'{"raw":{"data":"YQ==","encoding":"base64"},"value":null}'
    with pytest.raises(CodecError):
        decode(data, core.BrokerRead)


@pytest.mark.parametrize(
    ("received", "expected"),
    [
        (123.45678, "123.45678"),
        (0.00004, "0.00004"),
        ("123.45678", "123.45678"),
        ("0.00004", "0.00004"),
        ("1.10", "1.10"),
        (7, "7"),
    ],
)
def test_a_received_value_survives_the_converter_unchanged(received: object, expected: str) -> None:
    value = core.from_received(received)  # type: ignore[arg-type]
    assert format(value, "f") == expected
    assert core.exceeds_places(core.from_received(0.00004), 4)
    assert not core.exceeds_places(core.from_received("1.10"), 4)


@pytest.mark.parametrize("received", [float("nan"), "Infinity", "1.0 ", "abc", True, None])
def test_the_converter_refuses_what_is_not_a_finite_decimal(received: object) -> None:
    with pytest.raises(CodecError):
        core.from_received(received)  # type: ignore[arg-type]


def test_a_computed_value_is_rounded_once_half_to_even() -> None:
    assert core.to_decimal(0.125, 2) == Decimal("0.12")
    assert core.to_decimal(0.135, 2) == Decimal("0.14")
    assert format(core.to_decimal(0.1, 4), "f") == "0.1000"
    assert format(core.to_decimal(1 / 3, 8), "f") == "0.33333333"
    with pytest.raises(CodecError):
        core.to_decimal(float("nan"), 2)


def test_decode_reads_every_kind_of_value_a_field_can_hold() -> None:
    value = core.Snapshot(
        decision_at=AT,
        bars={},
        quotes={},
        status={InstrumentId("USO"): core.BarStatus.STALE},
        provisional=True,
        flags={"USO": {"spike": True}},
        segments=("s3://x",),
        ledgers={"a": "b"},
    )
    back = decode(encode(value), core.Snapshot)
    assert back == value
    assert back.status[InstrumentId("USO")] is core.BarStatus.STALE
