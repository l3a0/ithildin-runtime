"""The one codec: canonical JSON through `rfc8785`, with decimals as strings.

docs/architecture/08-crosscutting-concepts.md's `### File formats and keys`
gives the rules, and #22's acceptance 4 to 6 the checks below.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime import core
from ithildinruntime.core import CodecError, InstrumentId, decode, encode
from tests.core_samples import RECORDS, sample

FIXTURES = Path(__file__).parent / "fixtures" / "codec"


@dataclasses.dataclass(frozen=True)
class Body:
    """A received body beside a field of no type, which no record in `core` holds side by side."""

    raw: bytes
    value: object


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
            outcome=core.Outcome.UNKNOWN,
            broker_order_id=None,
            order=None,
            raw=b"\xff\xfe not utf-8",
            received_at=AT,
        ),
        "fill": core.Fill(
            fill_id="f1",
            correction_id=None,
            broker_order_id="b1",
            client_order_id="c1",
            instrument=InstrumentId("USO"),
            qty=core.from_received("1.10"),
            price=core.from_received(123.45678),
            at_text="2026-11-02T20:40:00Z",
            at=AT,
            received_at=AT + timedelta(microseconds=7),
            excess_places=("price",),
            basis=None,
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
    with pytest.raises(CodecError, match="Verdict.value is a float"):
        encode(dataclasses.replace(sample(core.Verdict), value=0.5))
    assert encode(value) == b'{"entry_hash":"a","seq":1}'


def test_encode_refuses_a_value_of_the_wrong_type_for_its_field() -> None:
    with pytest.raises(CodecError):
        encode(core.JournalAck(seq="1", entry_hash="a"))  # type: ignore[arg-type]
    with pytest.raises(CodecError, match="Verdict.adding is not a bool"):
        encode(dataclasses.replace(sample(core.Verdict), adding=1))
    with pytest.raises(CodecError):
        encode(core.Heartbeat(format=1, writer="w", at=None))  # type: ignore[arg-type]


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
        '{"cash":"1","marks":{"GLD":' + price + '},"positions":{},"replayed_through":1,'
        '"session":"2026-11-02","value":"1"}'
    ).encode()
    with pytest.raises(CodecError, match="Valuation.marks.*(plain notation|wrote a string)"):
        decode(data, core.Valuation)
    sound = data.replace(b'{"GLD":' + price.encode(), b'{"GLD":"1"')
    assert decode(sound, core.Valuation).marks == {InstrumentId("GLD"): 1}


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
        decode(('{"at":' + at + ',"format":1,"writer":"w"}').encode(), core.Heartbeat)


def test_a_received_body_is_kept_as_text_or_as_base64() -> None:
    text = Body(raw=b'{"qty": "1.10"}\r\n\x00', value=None)
    binary = Body(raw=b"\xff\xfe\x00", value=None)
    assert json.loads(encode(text))["raw"] == {
        "data": '{"qty": "1.10"}\r\n\x00',
        "encoding": "utf-8",
    }
    assert json.loads(encode(binary))["raw"] == {"data": "//4A", "encoding": "base64"}
    for value in (text, binary):
        assert decode(encode(value), Body).raw == value.raw


def test_a_utf8_body_written_as_base64_is_not_canonical() -> None:
    data = b'{"raw":{"data":"YQ==","encoding":"base64"},"value":null}'
    with pytest.raises(CodecError):
        decode(data, Body)


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
    assert not core.exceeds_places(core.from_received("1.2345"), 4)
    assert not core.exceeds_places(core.from_received("1.10"), 4)


@pytest.mark.parametrize(
    "received",
    [
        float("nan"),
        "Infinity",
        "1.0 ",
        "abc",
        True,
        None,
        "1_000",
        "+5",
        "\u0663.5",
        ".5",
        "1E+99999999",
        "1e-99999999",
        "1" * 39,
        "0." + "1" * 39,
        1e300,
    ],
)
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
        flags={
            InstrumentId("USO"): (
                core.SpikeFlag(
                    session=date(2026, 11, 2),
                    field="close",
                    price=Decimal("60.123456"),
                    against=Decimal("40.5"),
                    rule="close-to-close move past the written multiple",
                    note="the source stated no volume",
                ),
            )
        },
        segments=(core.Segment("journal/x.arrows", "v1", "0" * 64),),
        ledgers={"a": "b"},
        rows={InstrumentId("USO"): ({"bid": "60.5", "ask": None},)},
        faults=("a reason",),
        quotes_read_at=AT,
    )
    back = decode(encode(value), core.Snapshot)
    assert back == value
    assert back.status[InstrumentId("USO")] is core.BarStatus.STALE
    # A flag's price keeps every place it arrived with.
    assert format(back.flags[InstrumentId("USO")][0].price, "f") == "60.123456"


class _Float(float):
    """A float whose repr is not its value, as numpy 2 prints `np.float64(1.5)`."""

    def __repr__(self) -> str:
        return f"np.float64({float.__repr__(self)})"


def test_a_float_subclass_is_read_as_its_value() -> None:
    assert core.from_received(_Float(123.45)) == Decimal("123.45")
    assert core.to_decimal(_Float(0.125), 2) == Decimal("0.12")


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1.5"),
        datetime(2026, 11, 2, tzinfo=UTC),
        b"raw",
        core.BarStatus.FRESH,
        core.JournalAck(seq=1, entry_hash="a"),
    ],
    ids=["decimal", "time", "bytes", "enum", "record"],
)
def test_a_field_typed_object_takes_only_what_reads_back_unchanged(value: object) -> None:
    entry = core.JournalEntry(
        seq=1, format=1, built_at=AT, body={"kind": "fill", "x": value}, prev_hash="0" * 64
    )
    with pytest.raises(CodecError):
        encode(entry)
    with pytest.raises(CodecError):
        encode(Body(raw=b"", value=[value]))


@pytest.mark.parametrize(
    ("received", "kept"),
    [
        ("1E+30", True),
        ("1E-30", True),
        ("0." + "1" * 38, True),
        ("1E+31", False),
        ("1E-31", False),
        ("0." + "1" * 39, False),
        ("0." + "0" * 31, True),
        ("0." + "0" * 38, True),
        ("0." + "0" * 39, False),
        ("0E-99999999", False),
        ("0E+30", True),
        ("0E+31", False),
    ],
)
def test_the_codecs_size_limits_sit_at_38_digits_and_magnitude_30(
    received: str, kept: bool
) -> None:
    """Records written under these limits must stay readable, so a release only raises them."""
    if kept:
        assert format(core.from_received(received), "f") == format(Decimal(received), "f")
    else:
        with pytest.raises(CodecError):
            core.from_received(received)


def test_a_decimal_too_large_to_write_is_refused() -> None:
    with pytest.raises(CodecError):
        encode(
            core.TargetPortfolio(
                decision_at=AT, strategy="s", shares={InstrumentId("GLD"): Decimal("1E+50")}
            )
        )
    assert (
        encode(
            core.TargetPortfolio(
                decision_at=AT, strategy="s", shares={InstrumentId("GLD"): Decimal("1E+29")}
            )
        ).count(b"1" + b"0" * 29 + b'"')
        == 1
    )


@pytest.mark.parametrize(
    ("data", "kind", "defect"),
    [
        (b"[" * 100_000 + b"]" * 100_000, core.JournalAck, "nested past the parser's depth"),
        (b'{"entry_hash":"a","seq":' + b"9" * 5000 + b"}", core.JournalAck, "a huge integer"),
        (b'{"entry_hash":"a","seq":null}', core.JournalAck, "null where a value is required"),
        (b'{"entry_hash":1,"seq":1}', core.JournalAck, "a number where a string is"),
        (b'{"entry_hash":"a","seq":"1"}', core.JournalAck, "a string where an int is"),
        (b"[]", core.JournalAck, "a list where a record is"),
        (
            b'{"bars":{},"decision_at":"2026-11-02T20:40:00.000000Z","faults":[],"flags":{},'
            b'"ledgers":{},"provisional":false,"quotes":{},"quotes_read_at":null,"rows":{},'
            b'"segments":{},"status":{}}',
            core.Snapshot,
            "an object where a list is",
        ),
        (
            b'{"bars":{},"decision_at":"2026-11-02T20:40:00.000000Z","faults":[],"flags":{},'
            b'"ledgers":{},"provisional":false,"quotes":{},"quotes_read_at":null,"rows":{},'
            b'"segments":[],"status":{"USO":"rotten"}}',
            core.Snapshot,
            "a status no BarStatus names",
        ),
        (
            b'{"bars":{},"decision_at":"2026-11-02T20:40:00.000000Z","faults":[],"flags":{},'
            b'"ledgers":[],"provisional":false,"quotes":{},"quotes_read_at":null,"rows":{},'
            b'"segments":[],"status":{}}',
            core.Snapshot,
            "a list where a mapping is",
        ),
        (
            b'{"at":"2026-19-02T20:40:00.000000Z","format":1,"writer":"w"}',
            core.Heartbeat,
            "a time no clock holds",
        ),
        (b'{"raw":{"data":"YQ"},"value":null}', Body, "a body with no encoding"),
        (b'{"raw":{"data":1,"encoding":"utf-8"},"value":null}', Body, "no text"),
        (
            b'{"raw":{"data":"*","encoding":"base64"},"value":null}',
            Body,
            "invalid base64",
        ),
        (
            b'{"raw":{"data":"a","encoding":"latin-1"},"value":null}',
            Body,
            "an encoding the codec does not know",
        ),
        (
            b'{"decision_at":"2026-11-02T20:40:00.000000Z","shares":{"GLD":"1E+5"},"strategy":"s"}',
            core.TargetPortfolio,
            "a decimal in exponent form",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_decode_refuses_dirty_bytes(data: bytes, kind: type, defect: str) -> None:
    with pytest.raises(CodecError) as raised:
        decode(data, kind)
    assert type(raised.value) is CodecError


@pytest.mark.parametrize(
    ("kind", "old", "new", "reason"),
    [
        (core.Verdict, b'"approved":false', b'"approved":"no"', "approved is not a bool"),
        (core.Verdict, b'"max_net","loss"', b'null,"loss"', "a refusal names no limit"),
        (core.Valuation, b'"2026-11-27"', b'"20261127"', "'20261127', not a session date"),
        (core.Valuation, b'"2026-11-27"', b'"2026-02-30"', "'2026-02-30', no real date"),
        (
            core.Valuation,
            b'"value":"-0.0004"',
            b'"value":"0.0004"',
            "is not its cash and positions",
        ),
    ],
)
def test_decode_names_the_defect_in_a_record_whose_fields_are_all_there(
    kind: type, old: bytes, new: bytes, reason: str
) -> None:
    """The bytes hold every field, so the defect alone is what decode refuses."""
    data = encode(sample(kind))
    assert old in data
    with pytest.raises(CodecError, match=reason):
        decode(data.replace(old, new), kind)


def test_decoding_a_snapshot_with_a_bar_under_another_key_is_a_codec_error() -> None:
    bar = dataclasses.replace(sample(core.Bar), instrument=InstrumentId("USO"), interval="1d")
    snapshot = dataclasses.replace(
        sample(core.Snapshot), bars={InstrumentId("USO"): {"1d": (bar,)}}, quotes={}
    )
    data = encode(snapshot).replace(b'"bars":{"USO"', b'"bars":{"GLD"', 1)
    assert data != encode(snapshot)
    with pytest.raises(CodecError) as raised:
        decode(data, core.Snapshot)
    assert type(raised.value) is CodecError


@pytest.mark.parametrize("escaped", ["\\ud800", "x\\udfff"])
def test_text_that_is_no_unicode_is_refused_both_ways(escaped: str) -> None:
    half = json.loads(f'"{escaped}"')
    for value in (half, {half: 1}, [half], core.JournalAck(1, half)):
        with pytest.raises(core.CodecError, match="text that is no Unicode"):
            core.encode(value)
    kind = Body
    data = b'{"raw":{"data":"%s","encoding":"utf-8"},"value":null}' % escaped.encode()
    with pytest.raises(core.CodecError, match="raw holds text that is no Unicode"):
        core.decode(data, kind)


@pytest.mark.parametrize("data", ["*", "\u00e9", "YQ", "Y Q=="])
def test_a_body_that_is_no_base64_is_refused(data: str) -> None:
    body = json.dumps({"raw": {"data": data, "encoding": "base64"}, "value": None}).encode()
    with pytest.raises(core.CodecError, match="raw is not base64"):
        core.decode(body, Body)
