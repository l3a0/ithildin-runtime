"""What a broker's calls answer, as `core` types them (#143's contract, items 1 to 5)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    READ_LIMIT,
    Ack,
    BrokerState,
    BrokerUnavailable,
    CashRead,
    FillsRead,
    OrdersRead,
    Outcome,
    PositionsRead,
    ReceivedPage,
    RecordsRead,
)

AT = datetime(2026, 11, 3, 20, 40, tzinfo=UTC)
NAIVE = AT.replace(tzinfo=None)
EASTERN = AT.astimezone(timezone(timedelta(hours=-5)))

READS: dict[str, Callable[[tuple[ReceivedPage, ...], bool], object]] = {
    "positions": lambda pages, whole: PositionsRead({}, pages, whole),
    "orders": lambda pages, whole: OrdersRead((), pages, whole),
    "fills": lambda pages, whole: FillsRead((), pages, whole),
    "cash": lambda pages, whole: CashRead(Decimal(1), pages, whole),
    "records": lambda pages, whole: RecordsRead(pages, whole),
}


def test_the_outcomes_and_the_states_are_closed_sets() -> None:
    assert [o.value for o in Outcome] == [
        *("accepted", "rejected", "not_sent", "unknown", "not_found", "several"),
    ]
    assert [s.value for s in BrokerState] == [
        *("working", "pending_cancel", "filled", "cancelled", "expired", "rejected", "unknown"),
    ]


@pytest.mark.parametrize("name", READS)
def test_a_read_holds_a_page_and_one_past_the_cap_is_not_whole(name: str) -> None:
    make = READS[name]
    with pytest.raises(ValueError, match="no page the broker sent"):
        make((), True)
    with pytest.raises(ValueError, match="no page the broker sent"):
        make((), False)
    half = ReceivedPage(b"x" * (READ_LIMIT // 2), AT)
    make((half, half), True)
    over = (half, half, ReceivedPage(b"x", AT))
    with pytest.raises(ValueError, match=f"a read of {READ_LIMIT + 1} bytes"):
        make(over, True)
    make(over, False)
    # A page of no bytes is one the broker sent.
    make((ReceivedPage(b"", AT),), True)


@pytest.mark.parametrize("at", [NAIVE, EASTERN, None, "2026-11-03"])
def test_an_answer_and_a_page_say_when_they_came_in_utc(at: object) -> None:
    with pytest.raises(ValueError, match="not a UTC time"):
        ReceivedPage(b"", at)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a UTC time"):
        Ack(Outcome.UNKNOWN, None, None, b"", at)  # type: ignore[arg-type]


@pytest.mark.parametrize("outcome", list(Outcome))
def test_only_an_accepted_answer_needs_a_brokers_order_id(outcome: Outcome) -> None:
    if outcome is Outcome.ACCEPTED:
        with pytest.raises(ValueError, match="no broker's order id"):
            Ack(outcome, None, None, b"", AT)
        with pytest.raises(ValueError, match="no broker's order id"):
            Ack(outcome, "", None, b"", AT)
    else:
        Ack(outcome, None, None, b"", AT)
    Ack(outcome, "b-1", None, b"", AT)


def test_an_unavailable_broker_names_a_call_and_a_type_and_no_sentence() -> None:
    assert str(BrokerUnavailable("fills", "ReadTimeout")) == (
        "the broker's fills failed with ReadTimeout"
    )
    for said in ("401: key PK1", "two words", "", "a" * 81, "9lives", "x\n", None, 7):
        error = BrokerUnavailable("fills", said)  # type: ignore[arg-type]
        assert (error.call, error.error) == ("fills", "Unnamed")
        assert BrokerUnavailable(said, "X").call == "Unnamed"  # type: ignore[arg-type]
