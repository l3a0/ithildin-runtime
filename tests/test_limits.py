"""The hard caps blob's reader, and the tighter of the owner's and the strategy's limits."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

import pytest

from ithildinruntime import core
from ithildinruntime.core import Constraints, Halt, HardCaps, parse_caps, tighten
from tests.support.gates import GLD, LOOSE, SPY, USO, caps, constraints, d, valuation

LIMITS: dict[str, object] = {
    "max_order_qty": {"GLD": "10"},
    "max_position": {"GLD": "5"},
    "max_gross": "1000",
    "max_open_orders": 4,
    "loss_stop": "50",
    "moc_cutoff": 12,
    "share": "0.01",
    "stress_move": "0.2",
    "stress_low": "0.1",
    "stress_high": "0.6",
    "stress_vintage": "a" * 64,
    "stress_through": "2026-11-20",
    "stress_max_age_days": 365,
    "share_step": "1",
}


def blob(limits: dict[str, object] | None = None, **changes: object) -> bytes:
    fields = {"format": 1, "account": "paper", "limits": limits or LIMITS} | changes
    # Written by hand, so with spaces and in no canonical order.
    return json.dumps(fields, indent=2).encode()


def with_(**changes: object) -> bytes:
    return blob({k: v for k, v in (LIMITS | changes).items() if v is not None})


def test_the_issues_sample_blob_parses() -> None:
    assert parse_caps(blob(), "paper") == HardCaps(
        max_order_qty={GLD: d(10)},
        max_position={GLD: d(5)},
        max_gross=d(1000),
        max_open_orders=4,
        loss_stop=d(50),
        moc_cutoff=12,
        share=d("0.01"),
        stress_move=d("0.2"),
        stress_low=d("0.1"),
        stress_high=d("0.6"),
        stress_vintage="a" * 64,
        stress_through=date(2026, 11, 20),
        stress_max_age_days=365,
        share_step=d(1),
    )


def test_other_sections_belong_to_other_readers() -> None:
    read = parse_caps(blob(holds={"release_needs_blob": True, "x": 0.5}, reasons=["why"]), "paper")
    assert read == parse_caps(blob(), "paper")


def test_every_place_of_a_decimal_is_kept() -> None:
    assert parse_caps(with_(max_gross="1000.00005"), "paper").max_gross == d("1000.00005")


BAD_BLOBS: dict[str, tuple[bytes, str]] = {
    "no-bytes": (b"", "the caps blob is not JSON"),
    "not-json": (b"{", "the caps blob is not JSON"),
    "not-utf-8": (b"\xff", "the caps blob is not JSON"),
    "a-nan": (b'{"format": 1, "x": NaN}', "the caps blob is not JSON"),
    "nested-too-deep": (b"[" * 60_000, "the caps blob is not JSON"),
    "too-long": (blob(reasons="x" * 65_536), "the caps blob is 66.* bytes, past the 65536 allowed"),
    "no-blob": (None, "the caps blob is missing"),  # type: ignore[dict-item]
    "text-for-bytes": (blob().decode(), "the caps blob is missing"),  # type: ignore[dict-item]
    "a-list": (b"[]", "the caps blob is not a JSON object"),
    "a-repeated-key": (blob().replace(b'"account"', b'"format"', 1), "holds 'format' twice"),
    "a-repeated-limit": (
        blob().replace(b'"max_gross"', b'"max_gross": "9", "max_gross"', 1),
        "holds 'max_gross' twice",
    ),
    "a-repeated-instrument": (
        blob().replace(b'"GLD": "10"', b'"GLD": "10", "GLD": "11"', 1),
        "holds 'GLD' twice",
    ),
    "no-format": (blob().replace(b'"format"', b'"fromat"', 1), "format is None, not 1"),
    "another-format": (blob(format=2), "format is 2, not 1"),
    "a-true-format": (blob(format=True), "format is True, not 1"),
    "another-account": (blob(account="live"), "^the caps blob does not name the account 'paper'$"),
    "no-account": (blob(account=None), "does not name the account 'paper'"),
    "no-limits": (
        blob(limits=None, limit={}).replace(b'"limits"', b'"li"', 1),
        "limits is missing",
    ),
    "limits-as-a-list": (
        json.dumps({"format": 1, "account": "paper", "limits": []}).encode(),
        "limits is missing or no JSON object",
    ),
    "a-null-limits": (
        json.dumps({"format": 1, "account": "paper", "limits": None}).encode(),
        "limits is missing or no JSON object",
    ),
    "an-unknown-limit": (with_(max_net="5"), "holds 'max_net', which this release does not read"),
    **{f"no-{name}": (with_(**{name: None}), f"holds no {name}$") for name in LIMITS},
}


@pytest.mark.parametrize("name", BAD_BLOBS)
def test_a_blob_that_cannot_be_read_halts_naming_what(name: str) -> None:
    data, reason = BAD_BLOBS[name]
    with pytest.raises(Halt, match=reason):
        parse_caps(data, "paper")


def test_the_longest_blob_allowed_parses() -> None:
    short = blob(reasons="")
    longest = blob(reasons="x" * (65_536 - len(short)))
    assert len(longest) == 65_536
    assert parse_caps(longest, "paper") == parse_caps(blob(), "paper")


@pytest.mark.parametrize("account", [None, "", "../paper", 5])
def test_an_account_that_names_none_halts_whatever_the_blob_holds(account: object) -> None:
    with pytest.raises(Halt, match="the account whose caps are asked for is"):
        parse_caps(blob(account=account), account)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["max_order_qty", "max_position"])
@pytest.mark.parametrize("key", ["", " ", "GLD ", "GLD\n", "\ud800", "A" * 81])
def test_an_instrument_that_could_not_be_stored_is_refused(name: str, key: str) -> None:
    with pytest.raises(Halt, match=f"the caps blob's {name} names .*, no instrument") as raised:
        parse_caps(with_(**{name: {key: "5"}}), "paper")
    assert core.encode(core.HaltRecord(raised.value.reason, None, (), (), ()))


@pytest.mark.parametrize("name", ["GLD", "BRK.B", "BRK/B", "^SPX", "A" * 80])
def test_an_instrument_a_record_can_hold_is_read(name: str) -> None:
    read = parse_caps(with_(max_order_qty={name: "5"}, max_position={name: "5"}), "paper")
    assert core.decode(core.encode(read), HardCaps) == read


LONG = "9" * 5_000
DEEP = b"[" * 9_000 + b"]" * 9_000


def _nested(data: bytes) -> bytes:
    """The blob with a list nested too deep to be copied or shown where `data` marks."""
    assert data.count(b'"@"') == 1
    return data.replace(b'"@"', DEEP)


SHOWN: dict[str, bytes] = {
    **{name: with_(**{name: LONG}) for name in LIMITS},
    **{f"{name}-nested": _nested(with_(**{name: "@"})) for name in LIMITS},
    "unknown": with_(**{LONG: 5}),
    "twice": b'{"format": 1, "%s": 1, "%s": 2}' % (LONG.encode(), LONG.encode()),
    "format": blob(format=LONG),
    "format-nested": _nested(blob(format="@")),
    "account-nested": _nested(blob(account="@")),
    "limits-nested": _nested(b'{"format": 1, "account": "paper", "limits": "@"}'),
    "instrument": with_(max_order_qty={LONG: "5"}),
    "instrument-value": with_(max_order_qty={"GLD": LONG}),
    "instrument-value-nested": _nested(with_(max_order_qty={"GLD": "@"})),
}


@pytest.mark.parametrize("name", SHOWN)
def test_a_halt_shows_no_more_than_a_little_of_what_it_refused(name: str) -> None:
    assert len(SHOWN[name]) < 65_536
    with pytest.raises(Halt) as raised:
        parse_caps(SHOWN[name], "paper")
    assert len(raised.value.reason) < 250
    assert core.encode(core.HaltRecord(raised.value.reason, None, (), (), ()))


@pytest.mark.parametrize("account", [123456789, "123456789", [123456789], None, 1.5])
def test_the_account_a_blob_names_is_never_shown(account: object) -> None:
    with pytest.raises(Halt, match="^the caps blob does not name the account 'paper'$"):
        parse_caps(blob(account=account), "paper")


@pytest.mark.parametrize("name", ["max_open_orders", "moc_cutoff", "stress_max_age_days"])
def test_an_integer_a_record_cannot_hold_is_refused(name: str) -> None:
    # The stress move's age has a ceiling of its own.
    largest = 365 if name == "stress_max_age_days" else 2**53 - 1
    assert parse_caps(with_(**{name: largest}), "paper")
    for huge in (2**53, -(2**53)):
        with pytest.raises(Halt, match=f"{name} is a huge int, past what a record can hold"):
            parse_caps(with_(**{name: huge}), "paper")


DECIMALS = (
    "max_gross",
    "loss_stop",
    "share",
    "stress_move",
    "stress_low",
    "stress_high",
    "share_step",
)
WHOLES = ("max_open_orders", "moc_cutoff", "stress_max_age_days")


@pytest.mark.parametrize("name", DECIMALS)
@pytest.mark.parametrize(
    "value",
    [5, 0.5, True, "", "NaN", "Infinity", "1e3", "+1", ".5", "01", " 1", "1_0", "1" * 39, [], {}],
)
def test_a_decimal_is_a_json_string_in_plain_notation(name: str, value: object) -> None:
    with pytest.raises(Halt, match=f"the caps blob's {name} "):
        parse_caps(with_(**{name: value}), "paper")


@pytest.mark.parametrize("name", ["max_order_qty", "max_position"])
@pytest.mark.parametrize("value", [5, True, "NaN", "Infinity", "1e3", {}])
def test_a_share_cap_names_its_instrument_when_it_cannot_be_read(name: str, value: object) -> None:
    with pytest.raises(Halt, match=f"the caps blob's {name} of GLD "):
        parse_caps(with_(**{name: {"GLD": value}}), "paper")
    with pytest.raises(Halt, match=f"the caps blob's {name} is no JSON object"):
        parse_caps(with_(**{name: "5"}), "paper")


@pytest.mark.parametrize("name", WHOLES)
@pytest.mark.parametrize("value", [True, 1.5, "4", [], 4.0])
def test_a_count_is_a_json_integer(name: str, value: object) -> None:
    with pytest.raises(Halt, match=f"the caps blob's {name} is .*, not a JSON integer"):
        parse_caps(with_(**{name: value}), "paper")


@pytest.mark.parametrize("value", [20261120, "20261120", "2026-13-01", "2026-11-20T00:00", ""])
def test_a_date_is_written_as_one(value: object) -> None:
    with pytest.raises(Halt, match="the caps blob's stress_through is"):
        parse_caps(with_(stress_through=value), "paper")


# The checks both readers share. Each names its field, in caps read from a blob and in
# caps and constraints a caller built.

SHARED: dict[str, tuple[dict[str, object], str]] = {
    "no-order-cap": ({"max_order_qty": {}}, "max_order_qty limits no instrument"),
    "no-position-cap": ({"max_position": {}}, "max_position limits no instrument"),
    "order-caps-absent": ({"max_order_qty": None}, "max_order_qty limits no instrument"),
    "an-order-cap-of-no-instrument": (
        {"max_order_qty": {None: LOOSE}},
        "max_order_qty names None, no instrument",
    ),
    "a-position-cap-of-an-empty-name": (
        {"max_position": {"": LOOSE}},
        "max_position names '', no instrument",
    ),
    "maps-that-differ": (
        {"max_order_qty": {GLD: LOOSE}},
        "max_order_qty and max_position do not both limit USO",
    ),
    "an-order-cap-below-its-position": (
        {"max_order_qty": {GLD: LOOSE, USO: LOOSE - Decimal("1E-9")}},
        "max_order_qty of USO is .*, below its max_position of",
    ),
    **{
        f"{name}-{kind}": ({name: value}, f"{name} is {shown}")
        for name in ("max_gross", "loss_stop")
        for kind, value, shown in (
            ("absent", None, "None, not a finite decimal"),
            ("nan", d("NaN"), ".*not a finite decimal"),
            ("infinite", d("Infinity"), ".*not a finite decimal"),
            ("an-int", 5, "5, not a finite decimal"),
            ("a-float", 5.0, "5.0, not a finite decimal"),
            ("negative", d("-0.0001"), "-0.0001, below zero"),
        )
    },
    **{
        f"{name}-{kind}": ({name: {GLD: value, USO: LOOSE}}, f"{name} of GLD is {shown}")
        for name in ("max_order_qty", "max_position")
        for kind, value, shown in (
            ("absent", None, "None, not a finite decimal"),
            ("nan", d("NaN"), ".*not a finite decimal"),
            ("infinite", d("Infinity"), ".*not a finite decimal"),
            ("negative", d("-1E-9"), "-1E-9, below zero"),
        )
    },
    **{
        f"{name}-{kind}": ({name: value}, f"{name} is {shown}")
        for name in ("max_open_orders", "moc_cutoff")
        for kind, value, shown in (
            ("absent", None, "None, not a whole number"),
            ("a-bool", True, "True, not a whole number"),
            ("a-decimal", d(4), ".*not a whole number"),
            ("negative", -1, "-1, below zero"),
            ("past-a-record", 2**53, "a huge int, past what a record can hold"),
        )
    },
}
CAPS_ALONE: dict[str, tuple[dict[str, object], str]] = {
    **{
        f"{name}-{kind}": ({name: value}, f"{name} is .*not a finite decimal")
        for name in ("share", "stress_move", "stress_low", "stress_high")
        for kind, value in (("absent", None), ("nan", d("NaN")), ("infinite", d("Infinity")))
    },
    "share-zero": ({"share": d(0)}, "share is 0, not between 0 and 1"),
    "share-one": ({"share": d(1)}, "share is 1, not between 0 and 1"),
    "share-negative": ({"share": d("-0.01")}, "share is -0.01, not between"),
    "stress-low-zero": ({"stress_low": d(0)}, "stress_low is 0, not above zero"),
    "stress-low-negative": ({"stress_low": d("-0.1")}, "stress_low is -0.1, not above zero"),
    "stress-low-past-high": (
        {"stress_low": d("0.60000001")},
        "stress_low is 0.60000001, not above zero and at most the stress_high of 0.6",
    ),
    "stress-move-zero": ({"stress_move": d(0)}, "stress_move is 0, not from 0.1 to 0.6"),
    "stress-move-negative": ({"stress_move": d("-0.2")}, "stress_move is -0.2, not from"),
    "stress-move-below": ({"stress_move": d("0.09999999")}, "stress_move is 0.09999999, not from"),
    "stress-move-above": ({"stress_move": d("0.60000001")}, "stress_move is 0.60000001, not from"),
    "vintage-short": ({"stress_vintage": "a" * 63}, "stress_vintage is 'a+', not a SHA-256"),
    "vintage-absent": ({"stress_vintage": None}, "stress_vintage is None, not a SHA-256"),
    "through-absent": ({"stress_through": None}, "stress_through is None, not a date"),
    "through-a-time": (
        {"stress_through": datetime(2026, 11, 20)},
        "stress_through is .*, not a date",
    ),
    "age-zero": ({"stress_max_age_days": 0}, "stress_max_age_days is 0, not .* from 1 to 365"),
    "age-past-its-ceiling": (
        {"stress_max_age_days": 366},
        "stress_max_age_days is 366, not a whole number of days from 1 to 365",
    ),
    "age-far-past-its-ceiling": (
        {"stress_max_age_days": 10**20},
        "stress_max_age_days is 100000000000000000000, not .* from 1 to 365",
    ),
    "age-negative": ({"stress_max_age_days": -1}, "stress_max_age_days is -1, not a whole number"),
    "step-absent": ({"share_step": None}, "share_step is None, not a finite decimal"),
    "step-nan": ({"share_step": d("NaN")}, "share_step is .*not a finite decimal"),
    "step-infinite": ({"share_step": d("Infinity")}, "share_step is .*not a finite decimal"),
    "step-an-int": ({"share_step": 1}, "share_step is 1, not a finite decimal"),
    **{
        f"step-{step}": ({"share_step": d(step)}, f"share_step is {shown}, not written as 1, 0.1")
        for step, shown in (
            ("0", "0"),
            ("-1", "-1"),
            ("0.3", "0.3"),
            ("2", "2"),
            ("10", "10"),
            ("0.0000000001", "1E-10"),
            ("1.0", "1.0"),
            ("0.10", "0.10"),
        )
    },
    "age-a-bool": ({"stress_max_age_days": True}, "stress_max_age_days is True, not a whole"),
}
CONSTRAINTS_ALONE: dict[str, tuple[dict[str, object], str]] = {
    "no-strategy": ({"strategy": ""}, "strategy is '', which names none"),
    "strategy-absent": ({"strategy": None}, "strategy is None, which names none"),
    "net-absent": ({"max_net": None}, "max_net is None, not a finite decimal"),
    "net-nan": ({"max_net": d("NaN")}, "max_net is .*not a finite decimal"),
    "net-infinite": ({"max_net": d("Infinity")}, "max_net is .*not a finite decimal"),
    "net-negative": ({"max_net": d("-0.0001")}, "max_net is -0.0001, below zero"),
    "an-instrument-the-caps-lack": (
        {name: {GLD: LOOSE, USO: LOOSE, SPY: LOOSE} for name in ("max_order_qty", "max_position")},
        "the constraints limit SPY, which the hard caps do not",
    ),
}


@pytest.mark.parametrize("name", [*SHARED, *CAPS_ALONE])
def test_5_each_field_of_the_caps_is_checked(name: str) -> None:
    changes, reason = (SHARED | CAPS_ALONE)[name]
    with pytest.raises(Halt, match=f"the hard caps' {reason}"):
        tighten(constraints(), caps(**changes), valuation())


@pytest.mark.parametrize("name", [*SHARED, *CONSTRAINTS_ALONE])
def test_5_each_field_of_the_constraints_is_checked(name: str) -> None:
    changes, reason = (SHARED | CONSTRAINTS_ALONE)[name]
    with pytest.raises(Halt, match=reason) as raised:
        tighten(constraints(**changes), caps(), valuation())
    assert raised.value.reason.startswith("the constraints")


PARSED: dict[str, tuple[dict[str, object], str]] = {
    "share": ({"share": "1"}, "share is 1, not between 0 and 1"),
    "stress_move": ({"stress_move": "0.7"}, "stress_move is 0.7, not from 0.1 to 0.6"),
    "stress_low": ({"stress_low": "0"}, "stress_low is 0, not above zero"),
    "stress_vintage": ({"stress_vintage": "x"}, "stress_vintage is 'x', not a SHA-256"),
    "stress_vintage_number": ({"stress_vintage": 5}, "stress_vintage is 5, not a SHA-256"),
    "stress_max_age_days": ({"stress_max_age_days": 0}, "stress_max_age_days is 0, not a whole"),
    "stress_max_age_days_past": (
        {"stress_max_age_days": 366},
        "stress_max_age_days is 366, not a whole number of days from 1 to 365",
    ),
    **{
        f"share_step-{step}": (
            {"share_step": step},
            f"share_step is {shown}, not written as 1, 0.1 and so on to 0.000000001",
        )
        for step, shown in (
            ("0", "0"),
            ("-1", "-1"),
            ("0.3", "0.3"),
            ("10", "10"),
            ("0.0000000001", "1E-10"),
            ("1.0", "1.0"),
            ("0.10", "0.10"),
        )
    },
    "max_gross": ({"max_gross": "-1"}, "max_gross is -1, below zero"),
    "loss_stop": ({"loss_stop": "-1"}, "loss_stop is -1, below zero"),
    "max_open_orders": ({"max_open_orders": -1}, "max_open_orders is -1, below zero"),
    "moc_cutoff": ({"moc_cutoff": -1}, "moc_cutoff is -1, below zero"),
    "max_order_qty": ({"max_order_qty": {"GLD": "4"}}, "max_order_qty of GLD is 4, below its"),
    "max_position": ({"max_position": {"GLD": "-1"}}, "max_position of GLD is -1, below zero"),
    "an_empty_map": ({"max_position": {}}, "max_position limits no instrument"),
    "two_maps": ({"max_position": {"USO": "5"}}, "do not both limit GLD"),
}


@pytest.mark.parametrize("name", PARSED)
def test_a_blob_whose_value_is_out_of_range_halts_as_it_is_read(name: str) -> None:
    changes, reason = PARSED[name]
    with pytest.raises(Halt, match=f"the (hard caps'|caps blob's) .*{reason}"):
        parse_caps(with_(**changes), "paper")


def test_5_each_end_of_each_range_is_allowed() -> None:
    zero = {"max_gross": d(0), "loss_stop": d(0), "max_open_orders": 0, "moc_cutoff": 0}
    none = {name: {GLD: d(0), USO: d(0)} for name in ("max_order_qty", "max_position")}
    tightened = tighten(
        constraints(**zero, **none, max_net=d(0)), caps(**zero, **none), valuation()
    )
    assert tightened == constraints(**zero, **none, max_net=d(0))
    for move in ("0.1", "0.10000001", "0.59999999", "0.6"):
        assert tighten(constraints(), caps(stress_move=d(move)), valuation())
    narrow = caps(stress_move=d("0.2"), stress_low=d("0.2"), stress_high=d("0.2"))
    assert tighten(constraints(), narrow, valuation())
    assert tighten(constraints(), caps(share=d("0.00000001")), valuation())
    assert tighten(constraints(), caps(share=d("0.99999999")), valuation())
    for name in ("max_open_orders", "moc_cutoff"):
        assert tighten(constraints(**{name: 2**53 - 1}), caps(), valuation())
    for age in (1, 2, 364, 365):
        assert tighten(constraints(), caps(stress_max_age_days=age), valuation())


TIGHTER: dict[str, tuple[object, object, Callable[[Constraints], object]]] = {
    "max_gross": (d(7), d(8), lambda c: c.max_gross),
    "max_open_orders": (7, 8, lambda c: c.max_open_orders),
    "loss_stop": (d(7), d(8), lambda c: c.loss_stop),
    # More minutes before the close is the earlier cutoff.
    "moc_cutoff": (8, 7, lambda c: c.moc_cutoff),
}


@pytest.mark.parametrize("name", TIGHTER)
def test_5_the_tighter_of_each_pair_applies_from_either_side(name: str) -> None:
    tight, loose, read = TIGHTER[name]
    for mine, owners in ((tight, loose), (loose, tight), (tight, tight)):
        made = tighten(constraints(**{name: mine}), caps(**{name: owners}), valuation())
        assert read(made) == tight


@pytest.mark.parametrize("name", ["max_order_qty", "max_position"])
def test_5_the_tighter_share_cap_applies_instrument_by_instrument(name: str) -> None:
    """GLD is tighter in one source and USO in the other, so neither map is taken whole."""
    zero = {"max_position": {GLD: d(0), USO: d(0)}} if name == "max_order_qty" else {}
    mine = constraints(**zero, **{name: {GLD: d(3), USO: d(9)}})
    owners = caps(**zero, **{name: {GLD: d(8), USO: d(4)}})
    made = getattr(tighten(mine, owners, valuation()), name)
    assert made == {GLD: 3, USO: 4}


def test_5_an_instrument_only_the_caps_limit_is_left_out() -> None:
    one = {name: {GLD: d(5)} for name in ("max_order_qty", "max_position")}
    made = tighten(constraints(**one), caps(), valuation())
    assert made.max_position == {GLD: 5} and made.max_order_qty == {GLD: 5}


def test_5_the_net_cap_is_the_share_of_v_over_the_stress_move() -> None:
    owners = caps(share=d("0.01"), stress_move=d("0.2"))
    assert tighten(constraints(), owners, valuation(1000)).max_net == 50
    assert tighten(constraints(max_net=d(49)), owners, valuation(1000)).max_net == 49
    assert tighten(constraints(max_net=d(51)), owners, valuation(1000)).max_net == 50


def test_6_a_net_cap_whose_fifth_place_is_past_five_rounds_down() -> None:
    """100.0012 × 0.01 ÷ 0.347 is 2.881878…, and an even rounding would give 2.8819."""
    owners = caps(share=d("0.01"), stress_move=d("0.347"))
    made = tighten(constraints(), owners, valuation(d("100.0012")))
    assert made.max_net == d("2.8818")


@pytest.mark.parametrize("value", ["0", "-0.0001"])
def test_5_a_v_that_is_not_above_zero_halts(value: str) -> None:
    with pytest.raises(Halt, match=f"the valuation's value is {value}, not above zero"):
        tighten(constraints(), caps(), valuation(value))
    assert tighten(constraints(), caps(), valuation("0.0001")).max_net == d("0.0002")


def test_tightening_twice_changes_nothing() -> None:
    once = tighten(constraints(max_gross=d(7)), caps(loss_stop=d(3)), valuation(1000))
    assert tighten(once, caps(loss_stop=d(3)), valuation(1000)) == once


@pytest.mark.parametrize("n", range(10))
def test_each_of_the_ten_steps_is_read(n: int) -> None:
    written = "1" if n == 0 else f"0.{'0' * (n - 1)}1"
    read = parse_caps(with_(share_step=written), "paper")
    assert format(read.share_step, "f") == written
    made = caps(share_step=read.share_step)
    assert tighten(constraints(), made, valuation()) == tighten(constraints(), caps(), valuation())


def test_the_stored_caps_read_back_from_their_bytes() -> None:
    read = parse_caps(blob(), "paper")
    assert core.decode(core.encode(read), HardCaps) == read
