"""The gate against a second implementation that lists every case of which orders fill.

`tests/support/gate_oracle.py` shares no helper with `core` or `risk`. The
books here are seeded, and each kind of book the limits can be wrong about is
counted, so a generator that stopped making one would fail.
"""

from __future__ import annotations

import random
from collections import Counter
from decimal import Decimal
from fractions import Fraction

import pytest

from ithildinruntime.core import (
    Book,
    Effect,
    Limit,
    OrderIntent,
    OrderType,
    StopState,
    TimeInForce,
    classify,
    exposure,
)
from tests.support.gate_oracle import Answer, answer
from tests.support.gates import (
    CHAN,
    CLOSE,
    DECISION,
    GLD,
    LOOSE,
    OTHER,
    SPY,
    USO,
    World,
    book,
    caps,
    constraints,
    inputs,
    order,
    snapshot,
    valuation,
)

NAMES = (GLD, USO, SPY)
SEEDS = 4_000


def _shares(draw: random.Random, top: int) -> Decimal:
    """A quantity at nine places, or a round one, so ranges meet zero and limits exactly."""
    if draw.random() < 0.6:
        return Decimal(draw.randint(-top, top))
    return Decimal(draw.randint(-top * 10**9, top * 10**9)).scaleb(-9)


def _world(seed: int) -> World:
    draw = random.Random(seed)
    instruments = NAMES[: draw.randint(1, 3)]
    positions = {CHAN: {}, OTHER: {}}
    for i in instruments:
        mine = _shares(draw, 10)
        positions[CHAN][i] = mine
        if draw.random() < 0.4:
            # Another strategy, often on the other side of the same instrument.
            positions[OTHER][i] = _shares(draw, 10) if draw.random() < 0.5 else -mine / 2
    opens = [
        order(q, draw.choice(instruments), draw.choice((CHAN, CHAN, OTHER)), name=f"old-{n}")
        for n in range(draw.randint(0, 3))
        if (q := _shares(draw, 6)) != 0
    ]
    i = draw.choice(instruments)
    held = positions[CHAN][i]
    qty = {
        "out": -held,
        "part": -(held / 2).quantize(Decimal("1E-9")),
        "past": -held * 2,
        "any": _shares(draw, 8),
    }[draw.choice(("out", "part", "part", "past", "any"))]
    at_close = draw.random() < 0.5
    intent = OrderIntent(
        "new-1",
        draw.choice((CHAN,) * 19 + ("",)),
        i,
        qty or Decimal(1),
        OrderType.MARKET_ON_CLOSE if at_close else OrderType.MARKET,
        TimeInForce.CLS if at_close else TimeInForce.DAY,
    )
    marks = {k: Decimal(draw.randint(1, 40)) / draw.choice((1, 4, 10)) for k in instruments}
    now = CLOSE if draw.random() < 0.3 else DECISION

    def tight(chance: float, value: object) -> object:
        return value if draw.random() < chance else LOOSE

    def per(chance: float, low: int, high: int) -> dict:
        return {k: tight(chance, Decimal(draw.randint(low, high))) for k in instruments}

    def limits(chance: float) -> dict:
        position = per(chance, 0, 12)
        return {
            "max_position": position,
            "max_order_qty": {
                k: max(q, v)
                for (k, q), v in zip(per(0.1, 2, 12).items(), position.values(), strict=True)
            },
            "max_gross": tight(chance, Decimal(draw.randint(5, 200))),
            "max_open_orders": draw.choice((2, 3, 100, 100)),
            "loss_stop": tight(0.2, Decimal(draw.randint(0, 40))),
            "moc_cutoff": draw.choice((0, 0, 0, 12)),
        }

    made = World(
        intent=intent,
        book=book(
            positions, opens, cash=_shares(draw, 100), in_doubt=draw.choice(((),) * 9 + ((i,),))
        ),
        snapshot=snapshot(marks),
        constraints=constraints(**limits(0.3), max_net=tight(0.4, Decimal(draw.randint(0, 80)))),
        inputs=inputs(
            caps=caps(**limits(0.15), share=Decimal("0.1")),
            stop=draw.choice((StopState.ABSENT,) * 30 + (StopState.FIRED, StopState.UNREADABLE)),
            valuation=valuation(1),
        ),
        now=now,
    )
    # V sits near the book's own value, so a loss stop can bind either way, and
    # the net cap is half of it.
    worth = made.losing(draw.randint(-30, 30)).inputs.valuation.value
    return made.given(valuation=valuation(max(worth, Decimal(1))))


def _asked(world: World) -> Answer:
    verdict = world.check()

    def f(value: Decimal | None) -> Fraction | None:
        return None if value is None else Fraction(str(value))

    return Answer(
        approved=verdict.approved,
        limit=None if verdict.limit is None else str(verdict.limit),
        value=f(verdict.value),
        bound=f(verdict.bound),
        missing_term=verdict.missing_term,
        adding=verdict.adding,
        worst_loss=f(verdict.worst_loss),
        loss=f(verdict.loss.loss),
        loss_bound=f(verdict.loss.bound),
        loss_past=verdict.loss.past,
        past=tuple((str(p.limit), f(p.value), f(p.bound)) for p in verdict.past),
    )


def _expected(world: World) -> Answer:
    b = world.book
    return answer(
        world.intent,
        b.positions_by_strategy,
        [o for orders in b.open_orders_by_strategy.values() for o in orders],
        b.cash,
        b.in_doubt,
        world.snapshot,
        world.constraints,
        world.inputs,
        world.now,
        CLOSE,
    )


@pytest.fixture(scope="module")
def worlds() -> list[World]:
    return [_world(seed) for seed in range(SEEDS)]


def test_4_every_field_of_every_verdict_agrees(worlds: list[World]) -> None:
    for seed, world in enumerate(worlds):
        assert _asked(world) == _expected(world), seed


def test_4_the_books_hold_each_kind_the_limits_can_be_wrong_about(worlds: list[World]) -> None:
    seen: Counter[str] = Counter()
    for world in worlds:
        verdict = world.check()
        b, intent = world.book, world.intent
        mine, theirs = (b.positions_by_strategy[s] for s in (CHAN, OTHER))
        seen["opposite strategies"] += any(mine[i] * q < 0 for i, q in theirs.items())
        signs = {q > 0 for q in mine.values() if q}
        seen["legs on one side"] += len(signs) == 1 and len([q for q in mine.values() if q]) > 1
        held = mine[intent.instrument]
        seen["crosses zero"] += held * (held + intent.qty) < 0
        seen["nine places"] += intent.qty.as_tuple().exponent == -9
        if not verdict.adding and verdict.approved:
            for entry in verdict.past:
                seen[f"approved past {entry.limit}"] += 1
        seen["reducing refused on net"] += not verdict.adding and verdict.limit is Limit.MAX_NET
        seen["adding"] += verdict.adding
        seen["approved"] += verdict.approved
        if verdict.limit is not None:
            seen[f"refused on {verdict.limit}"] += 1
    for kind in (
        "opposite strategies",
        "legs on one side",
        "crosses zero",
        "nine places",
        "approved past max_position",
        "approved past max_gross",
        "approved past max_net",
        "reducing refused on net",
        "adding",
        "approved",
        *(f"refused on {limit}" for limit in Limit if limit is not Limit.CLOSE_ENTRY),
    ):
        assert seen[kind] > 10, (kind, seen)


def test_4_a_reducing_order_meets_one_of_the_three_only_on_net(worlds: list[World]) -> None:
    for world in worlds:
        verdict = world.check()
        if not verdict.adding:
            assert verdict.limit not in (Limit.MAX_POSITION, Limit.MAX_GROSS)


def _replace_opens(b: Book, opens: list[OrderIntent]) -> Book:
    made = book(opens=opens)
    return Book(
        b.positions_by_strategy,
        made.open_orders_by_strategy,
        b.cash,
        b.platform_realised_pnl,
        b.replayed_through,
        b.in_doubt,
        b.corrected_through,
        b.repositioned_at,
    )


def test_3_a_working_order_reads_the_same_before_and_after_it_joins_the_book(
    worlds: list[World],
) -> None:
    """The sort of #10 classifies an order already in the book, which never counts it twice."""
    effects: Counter[Effect] = Counter()
    for world in worlds[:1000]:
        if not world.intent.strategy:
            continue
        before = classify(world.book, world.intent)
        opens = [o for orders in world.book.open_orders_by_strategy.values() for o in orders]
        joined = _replace_opens(world.book, [*opens, world.intent])
        assert classify(joined, world.intent) is before
        i = world.intent.instrument
        for strategy in (None, CHAN):
            assert exposure(joined, i, world.intent, strategy) == exposure(
                world.book, i, world.intent, strategy
            )
        effects[before] += 1
    assert min(effects[e] for e in Effect) > 50
