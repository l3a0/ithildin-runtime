"""Test 2 of #4, in process: test 1's decisions with a fault on chosen days, for #147.

`tests/support/faulted.py` runs the first six decisions of test 1 with one of
four faults. Each case checks the halt on its faulted day, that no order is sent
that day, that nothing is ever sent twice, and that trading goes on afterwards,
and pins every decision's orders and the state each ended in.

1. A missing feed: data finds USO missing on day 2.
2. A stale price: data finds GLD stale on day 2.
3. An ack withheld across two decisions: the broker takes day 2's first order
   and its answer is lost, the order list that would map it fails on day 3, and
   day 4's recover finds it and journals its ack.
4. A restart in the wrong order: the execution worker dies after each journal
   append of day 2 in turn, a new one starts before day 3's data is ready, the
   stage that died runs again, and day 3's recover settles what was left.

Every figure is exploratory, as test 1's are. The orders move with any change to
G or to the rounding, so #129 re-pins them.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ithildinruntime.core import RECOVER_STAGE, SEND_STAGE, OrderState
from ithildinruntime.execution.brokers.faults import Faults, OrderFault, ReadFault
from tests.support.faulted import Day, Replay, counted

_S = OrderState
DAYS = 6
UPSTREAM = (
    "cost/chan_bollinger",
    "alpha/chan_bollinger",
    "execution/recover",
    "risk",
    "portfolio",
    "execution/send",
)
DAY_2_FIRST = "ith-chan_bollinger-20060623T200000Z-01"
DAY_2_SECOND = "ith-chan_bollinger-20060623T200000Z-02"


def run(replay: Replay, **on: dict) -> list[Day]:
    return [replay.decide(n, **on.get(str(n), {})) for n in range(DAYS)]


def pinned(replay: Replay, days: list[Day]) -> str:
    """Each decision's halted stages and orders as that day left them, and where each one ended."""
    final = sorted((name, order.state.value) for name, order in replay.view().orders.items())
    made = [(day.at.isoformat(), sorted(day.halts), day.sent) for day in days]
    return hashlib.sha256(repr((made, final)).encode()).hexdigest()


def ended(replay: Replay, *names: str) -> list[OrderState | None]:
    """Where each order ended, or `None` for one with no entry."""
    orders = replay.view().orders
    return [orders[name].state if name in orders else None for name in names]


def nothing_twice(replay: Replay) -> None:
    """Each order was asked of the broker once at most, by a send stage, and none is unsettled.

    The count is of what the platform asked, read at the simulated broker's
    submit, and an order abandoned or given up as lost was never taken.
    """
    asked = [name for _, name in replay.submits]
    assert len(asked) == len(set(asked)), asked
    assert {stage for stage, _ in replay.submits} <= {SEND_STAGE}, replay.submits
    view = replay.view()
    taken = replay.taken()
    for name, order in view.orders.items():
        assert order.state not in (_S.INTENDED, _S.APPROVED, _S.SENDING, _S.IN_DOUBT), name
        if order.state in (_S.ABANDONED, _S.LOST):
            assert name not in taken, name
    assert taken <= set(asked)


@pytest.mark.parametrize(
    ("fault", "said"),
    [
        ("USO", "USO missing in the stored snapshot"),
        ("stale:GLD", "GLD stale in the stored snapshot"),
    ],
    ids=["a missing feed", "a stale price"],
)
def test_2_a_day_data_halts_sends_no_order_and_the_next_day_trades(
    every_session, tmp_path: Path, fault: str, said: str, monkeypatch
) -> None:
    replay = Replay(every_session, tmp_path)
    counted(monkeypatch, replay)
    days = run(replay, **{"2": {"data": fault}})
    faulted = days[2]
    assert sorted(faulted.halts) == sorted(UPSTREAM)
    assert all(said in reason for reason in faulted.halts.values())
    # No plan is journaled, so nothing is sent.
    assert faulted.sent == ()
    for day in (*days[:2], *days[3:]):
        assert day.halts == {}, day.halts
        assert [state for *_, state in day.sent] == [_S.ACKNOWLEDGED, _S.ACKNOWLEDGED]
    nothing_twice(replay)
    assert pinned(replay, days) == PINS[fault]


def test_2_an_ack_withheld_across_two_decisions_is_found_and_never_sent_again(
    every_session, tmp_path: Path, monkeypatch
) -> None:
    faults = Faults(
        orders={DAY_2_FIRST: OrderFault.LOSE_ANSWER}, reads={("orders", 1): ReadFault.FAILS}
    )
    replay = Replay(every_session, tmp_path, faults)
    counted(monkeypatch, replay)
    days = run(replay)
    # Day 2: the broker took the first order and its answer was lost, so its partner is not sent.
    assert list(days[2].halts) == [SEND_STAGE]
    assert f"the order {DAY_2_FIRST} is InDoubt" in days[2].halts[SEND_STAGE]
    assert [state for *_, state in days[2].sent] == [_S.IN_DOUBT, _S.ABANDONED]
    # Day 3: the list that would map it fails, so nothing settles it and nothing is sent.
    assert list(days[3].halts) == [RECOVER_STAGE, "risk", "portfolio", SEND_STAGE]
    assert (
        f"the order list that maps {DAY_2_FIRST} could not be read: the broker's find_order "
        "failed with TimeoutError, so nothing settles it"
    ) in days[3].halts[RECOVER_STAGE]
    assert days[3].sent == ()
    # Day 4: the list is read whole, the order is looked up by the broker's id, and it filled.
    assert days[4].halts == {} and days[5].halts == {}
    assert ended(replay, DAY_2_FIRST, DAY_2_SECOND) == [_S.FILLED, _S.ABANDONED]
    nothing_twice(replay)
    assert pinned(replay, days) == PINS["withheld"]


# The 13 entries day 2 journals: recover's read and two fills, then the send stage's read, its
# plan, and each leg's intent, verdict, sending and ack.
APPENDS = 13
# Each leg of day 2 once day 3's recover has settled it, by the append the worker died after.
SETTLED = {
    # A plan none of whose orders has an entry sent nothing, so nothing settles it.
    5: (None, None),
    **dict.fromkeys(range(6, 8), (_S.ABANDONED, _S.ABANDONED)),
    8: (_S.LOST, _S.ABANDONED),
    9: (_S.FILLED, _S.ABANDONED),
    10: (_S.FILLED, _S.ABANDONED),
    11: (_S.FILLED, _S.ABANDONED),
    12: (_S.FILLED, _S.LOST),
    13: (_S.FILLED, _S.FILLED),
}


@pytest.mark.parametrize("after", range(1, APPENDS + 1))
def test_2_a_restart_after_any_append_sends_nothing_twice_and_the_next_decision_settles_it(
    every_session, tmp_path: Path, after: int, monkeypatch
) -> None:
    replay = Replay(every_session, tmp_path)
    counted(monkeypatch, replay)
    days = run(replay, **{"2": {"kill_after": after}})
    assert replay.starts == 2
    died = days[2].died
    if after <= 3:
        # Recover died, and its second attempt goes on from the first one's capture.
        assert died == RECOVER_STAGE and days[2].halts == {}
        assert ended(replay, DAY_2_FIRST, DAY_2_SECOND) == [_S.FILLED, _S.FILLED]
    else:
        # The send stage's item comes back, and finds the dead process's claim.
        assert died == SEND_STAGE and list(days[2].halts) == [SEND_STAGE]
        assert (
            "holds another call's claim, so this one sends nothing" in (days[2].halts[SEND_STAGE])
        )
        if after == 4:
            # Only the send stage's read had landed, so no plan was journaled.
            assert days[2].sent == ()
        else:
            assert ended(replay, DAY_2_FIRST, DAY_2_SECOND) == list(SETTLED[after])
    for day in days[3:]:
        assert day.halts == {}, day.halts
    nothing_twice(replay)
    assert pinned(replay, days) == RESTARTS[after]


def test_2_a_fill_made_while_execution_was_down_applies_once_and_a_redelivery_drops_it(
    every_session, tmp_path: Path, monkeypatch
) -> None:
    """#39: the worker dies once day 2's first ack lands, and both legs fill at the close."""
    replay = Replay(every_session, tmp_path)
    counted(monkeypatch, replay)
    for n in range(2):
        replay.decide(n)
    replay.decide(2, kill_after=APPENDS)
    replay.decide(3, twice=(RECOVER_STAGE,))
    view = replay.view()
    fills = [f for f in view.fills.values() if f.client_order_id in (DAY_2_FIRST, DAY_2_SECOND)]
    assert len(fills) == 2
    assert not view.unmatched
    assert ended(replay, DAY_2_FIRST, DAY_2_SECOND) == [_S.FILLED, _S.FILLED]
    nothing_twice(replay)


# The pins of the code that added them, at the second head of PR #214, under chan_bollinger's
# G of 10,000 and orders of fractional shares. Both data days halt every stage and send nothing,
# so they pin alike, and so do the three kills of recover.
PINS = {
    "USO": "5726e20bfd9d0be468b732066557a7ad9e71ab717eaca645cc5c0289faabe784",
    "stale:GLD": "5726e20bfd9d0be468b732066557a7ad9e71ab717eaca645cc5c0289faabe784",
    "withheld": "20010607cd56339b8a02761b1b0c23b01db75a27a78798d54c9e78c3329085fd",
}
RESTARTS = {
    1: "b909501251ed6af5c667d5aef6d4ae15bf483cde0f84c90db7bf047af34e11b3",
    2: "b909501251ed6af5c667d5aef6d4ae15bf483cde0f84c90db7bf047af34e11b3",
    3: "b909501251ed6af5c667d5aef6d4ae15bf483cde0f84c90db7bf047af34e11b3",
    4: "070a7a9ce0620d173c6a9d8115dc33d1640d90f47f8aeb21c5f3d8f6ad3b9718",
    5: "3ee24f2bd7b7463410ff0c79978ad88ca81460e3babac1876856edffe10b099f",
    6: "76240e9d3fcc3bf14923320c518a6ff4695c6e754a4c16dffa45106a38c3e46b",
    7: "9b98e584cb8062ca3ed44c3c9acaccf5b1f9171b3a8d2f8d55398f7959fc09e9",
    8: "fdf3247a09d4f8423f4832eb56144e0e587c10f553a39af43fe8e6045e3dcdc7",
    9: "32bdb045dce8d569acb4eed91afc0fb39a2b7b2453b33d8178d819da8de3f8b5",
    10: "9b4c877212b7a66037666b519b258efeedf885b48add71d77c15a0a46a38f616",
    11: "94b854229c96fc0a58d3515dc1ffe072b5373d754781ab7e79e03aa8fbe31479",
    12: "1f3d446320a4afdecadb0936b718977c508ee91ebc772f7fdfc19c34f6554ec5",
    13: "84316f8af260b05b80a6eec3bdc520453a9f4cda0379e0661714b6e0799a999d",
}
