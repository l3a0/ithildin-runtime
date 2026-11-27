"""The gate against a second implementation that lists every case of which orders fill.

`tests/support/gate_oracle.py` shares no helper with `core` or `risk`. The
books here are seeded, and each kind of book the limits can be wrong about is
counted, so a generator that stopped making one would fail.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest

from ithildinruntime.core import (
    Book,
    Effect,
    InstrumentId,
    Limit,
    OrderIntent,
    OrderType,
    Snapshot,
    StopState,
    StressRecord,
    TimeInForce,
    Valuation,
    classify,
    exposure,
)
from tests.support import splits
from tests.support.gate_oracle import Answer, answer
from tests.support.gates import (
    CENT,
    CHAN,
    CLOSE,
    DECISION,
    GLD,
    LOOSE,
    OTHER,
    SPY,
    TABLE,
    TODAY,
    USO,
    World,
    book,
    caps,
    constraints,
    fitting,
    inputs,
    order,
    price,
    snapshot,
    valuation,
)
from tests.support.quotes import quote, read_at

DAYS = sorted(TABLE.sessions)

NAMES = (GLD, USO, SPY)
SEEDS = 4_000


def _shares(draw: random.Random, top: int) -> Decimal:
    """A quantity at nine places, or a round one, so ranges meet zero and limits exactly."""
    if draw.random() < 0.6:
        return Decimal(draw.randint(-top, top))
    return Decimal(draw.randint(-top * 10**9, top * 10**9)).scaleb(-9)


def _net(positions: dict, orders: list, marks: dict, side: int) -> Decimal:
    """Net with only the orders on one side filled."""
    held = (marks[i] * q for book in positions.values() for i, q in book.items())
    filled = (marks[o.instrument] * o.qty for o in orders if o.qty * side > 0)
    return sum((*held, *filled), Decimal(0))


def _net_limit(
    draw: random.Random, positions: dict, opens: list, intent: OrderIntent, marks: dict
) -> Decimal:
    """A net limit that is loose, or drawn, or one of the book's own nets before or after the order.

    The last lands a reducing order's net on its bound, which a drawn one rarely does.
    """
    kind = draw.random()
    if kind < 0.45:
        return LOOSE
    if kind < 0.75:
        return Decimal(draw.randint(0, 80))
    orders = [*opens, intent] if draw.random() < 0.5 else opens
    return abs(_net(positions, orders, marks, draw.choice((-1, 1))))


def _doubt(draw: random.Random, opens: list) -> tuple[InstrumentId, ...]:
    """Some of the instruments with an open order, one in ten books."""
    working = tuple(dict.fromkeys(o.instrument for o in opens))
    if draw.random() > 0.15 or not working:
        return ()
    return working if draw.random() < 0.5 else (draw.choice(working),)


def _quoted(
    draw: random.Random, marks: dict[InstrumentId, Decimal]
) -> tuple[dict[InstrumentId, Decimal], Snapshot]:
    """Each instrument's quote around its price, beside older bars at other prices.

    Some have a midpoint at five places, and some instruments have no quote at
    all and a bar of the session in its place.
    """
    made = snapshot(marks)
    quotes, bars = {}, dict(made.bars)
    priced = dict(marks)
    for k, at in marks.items():
        if draw.random() < 0.2:
            continue
        priced[k] = at + draw.choice((Decimal(0), Decimal("0.00005")))
        half = Decimal(draw.randint(0, 300)).scaleb(-4) + Decimal(priced[k] % CENT)
        quotes[k] = quote(
            k,
            priced[k] - half,
            priced[k] + half,
            DECISION,
            max_age=10**5,
            max_spread_share=Decimal("0.9"),
        )
        bars[k] = {"1d": made.bars[k]["1d"][:1]}
    read = read_at(DECISION) if quotes else {}
    return priced, replace(made, quotes=quotes, bars=bars, **read)


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
    marks = {k: Decimal(draw.randint(1, 40)) / draw.choice((1, 4, 10)) for k in instruments}
    sides = random.Random(f"quotes of {seed}")
    marked = snapshot(marks)
    if sides.random() < 0.5:
        marks, marked = _quoted(sides, marks)
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
    now = CLOSE if draw.random() < 0.3 else DECISION

    def tight(chance: float, value: object) -> object:
        return value if draw.random() < chance else LOOSE

    def limits(chance: float, limited: tuple[InstrumentId, ...]) -> dict:
        position = {k: tight(chance, Decimal(draw.randint(0, 12))) for k in limited}
        return {
            "max_position": position,
            "max_order_qty": {
                k: max(tight(0.1, Decimal(draw.randint(2, 12))), v) for k, v in position.items()
            },
            "max_gross": tight(chance, Decimal(draw.randint(5, 200))),
            "max_open_orders": draw.choice((2, 3, 100, 100)),
            "loss_stop": tight(0.2, Decimal(draw.randint(1, 40))),
            "moc_cutoff": draw.choice((0, 0, 0, 12)),
        }

    made = World(
        intent=intent,
        book=book(
            positions,
            opens,
            cash=_shares(draw, 100),
            in_doubt=_doubt(draw, opens),
            # A book at V's own entry would hold V's positions, so this one is later.
            replayed_through=8,
        ),
        snapshot=marked,
        constraints=constraints(
            # The strategy's limits need not name every instrument the account holds.
            **limits(0.3, tuple(k for k in instruments if k == i or draw.random() < 0.7)),
            max_net=_net_limit(draw, positions, opens, intent, marks),
        ),
        inputs=inputs(
            caps=caps(**limits(0.15, instruments), share=Decimal("0.1")),
            stop=draw.choice((StopState.ABSENT,) * 30 + (StopState.FIRED, StopState.UNREADABLE)),
            valuation=valuation(1),
        ),
        now=now,
    )
    # V sits near the book's own value, so a loss stop can bind either way, and
    # the net cap is half of it.
    worth = made.losing(draw.randint(-30, 30)).inputs.valuation.value
    made = made.given(valuation=_valued(draw, max(worth, Decimal(1)), instruments))
    return _split(seed, made.at(records=_records(seed, made)))


def _split(seed: int, made: World) -> World:
    """A split row on the order's instrument in some worlds, before or after the session.

    Drawn apart from the rest, so the other worlds are the ones the earlier seeds made.
    """
    draw = random.Random(f"splits of {seed}")
    if draw.random() >= 0.6:
        return made
    i = made.intent.instrument
    first = DAYS[DAYS.index(TODAY) + draw.choice((-1, 0, 1, 1, 3, 3))]
    row = splits.split(str(i), first, draw.choice((2, 1)), draw.choice((1, 8)) or 1)
    if row.new_shares == row.old_shares:
        row = replace(row, new_shares=3)
    snap = made.snapshot
    shape = draw.choice(("as built", "adjusted", "mixed", "raw"))
    if shape != "as built":
        # Adjusted after the split, which trades through it, raw, or the newest bar alone raw.
        def bar_of(b: object, n: int, count: int) -> object:
            if shape == "raw" or (shape == "mixed" and n == count - 1):
                return replace(b, basis="raw", source_as_of=None)
            return replace(b, source_as_of=first)

        snap = replace(
            snap,
            bars={
                k: {"1d": tuple(bar_of(b, n, len(by["1d"])) for n, b in enumerate(by["1d"]))}
                for k, by in snap.bars.items()
            },
        )
    covered = {}
    if draw.random() < 0.5:
        # The instrument's first covered session, on either side of the split.
        covered = {i: DAYS[DAYS.index(first) + draw.choice((-2, 0, 1))]}
    split_read = replace(snap.splits, rows={i: (row,)}, covered_from=covered)
    return made.at(snapshot=replace(snap, splits=split_read))


def _records(seed: int, made: World) -> tuple[StressRecord, ...]:
    """The record the caps name, and in some worlds a raised one, and others that do not count."""
    # A draw of its own, so the rest of each world is what it was without records.
    draw = random.Random(f"records-{seed}")
    (named,) = fitting(made.constraints, made.inputs.caps, stress_move=Decimal("0.2"))
    named = replace(named, leg=draw.choice(named.instruments))
    found = [named]

    def another(vintage: str, **changes: object) -> None:
        size = Decimal(draw.randint(20_000_001, 60_000_000)).scaleb(-8)
        found.append(replace(named, vintage=vintage * 64, stress_move=size, **changes))

    if draw.random() < 0.3:
        another("b")
    if draw.random() < 0.2:
        another("c", strategy=OTHER)
    if draw.random() < 0.2:
        another("d", withdrawn="read the wrong bars")
    if draw.random() < 0.2:
        another("e", instruments=(*named.instruments, InstrumentId("ZZZ")))
    if draw.random() < 0.2:
        zzz = InstrumentId("ZZZ")
        another("f", instruments=(*named.instruments, zzz), leg=zzz)
    draw.shuffle(found)
    return tuple(found)


def _move(world: World) -> Decimal:
    """The stress move the world's records leave, which "b" and "e" alone raise."""
    return max(r.stress_move for r in world.records if r.vintage[0] in "abe")


def _valued(draw: random.Random, value: Decimal, instruments: tuple) -> Valuation:
    """V at `value`, of holdings and cash of its own, since what was held then says nothing now."""
    positions = {i: Decimal(draw.randint(-10, 10)) for i in instruments}
    marks = {i: Decimal(draw.randint(1, 400)) / 10 for i in instruments}
    held = sum((positions[i] * marks[i] for i in instruments), Decimal(0))
    return valuation(value, cash=value - held, positions=positions, marks=marks)


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
        stress_move=f(verdict.stress_move),
        loss=f(verdict.loss.loss),
        loss_bound=f(verdict.loss.bound),
        loss_past=verdict.loss.past,
        past=tuple((str(p.limit), f(p.value), f(p.bound)) for p in verdict.past),
        account=_ends(world, world.intent, None),
        strategy=_ends(world, world.intent, world.intent.strategy),
        account_resting=_ends(world, None, None),
        strategy_resting=_ends(world, None, world.intent.strategy),
    )


def _ends(
    world: World, intent: OrderIntent | None, strategy: str | None
) -> dict[str, tuple[Fraction, Fraction]]:
    reached = {i: exposure(world.book, i, intent, strategy) for i in sorted(world.snapshot.bars)}
    return {i: (Fraction(str(r.low)), Fraction(str(r.high))) for i, r in reached.items()}


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
        world.records,
    )


def _net_end(world: World, with_order: bool) -> Decimal:
    """The size of net at the end the order moves, with it counted or not."""
    opens = [o for orders in world.book.open_orders_by_strategy.values() for o in orders]
    marks = {i: price(world.snapshot, i) for i in world.snapshot.bars}
    orders = [*opens, world.intent] if with_order else opens
    return abs(_net(world.book.positions_by_strategy, orders, marks, _side(world)))


def _side(world: World) -> int:
    return -1 if world.intent.qty < 0 else 1


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
        seen["nine places"] += intent.qty * 10**8 % 1 != 0
        limited = world.constraints.max_position
        seen["held outside the constraints"] += any(
            i not in limited for held in (mine, theirs) for i, q in held.items() if q
        )
        seen["in doubt elsewhere"] += bool(b.in_doubt) and intent.instrument not in b.in_doubt
        seen["two in doubt"] += len(b.in_doubt) > 1
        if not verdict.adding and verdict.approved:
            for entry in verdict.past:
                seen[f"approved past {entry.limit}"] += 1
        seen["reducing refused on net"] += not verdict.adding and verdict.limit is Limit.MAX_NET
        if not verdict.adding:
            owners = world.inputs.valuation.value / 10 / _move(world)
            bound = min(world.constraints.max_net, owners)
            before, after = (_net_end(world, with_order) for with_order in (False, True))
            seen["reducing to net's bound"] += after == bound < before
        seen["adding"] += verdict.adding
        raised = _move(world) > world.inputs.caps.stress_move
        seen["raised"] += raised
        seen["raised and refused on net"] += raised and verdict.limit is Limit.MAX_NET
        seen["another strategy's"] += any(r.strategy == OTHER for r in world.records)
        seen["withdrawn"] += any(r.withdrawn is not None for r in world.records)
        seen["raised by a wider record"] += any(r.vintage[0] == "e" for r in world.records)
        seen["another leg's"] += any(r.leg == "ZZZ" for r in world.records)
        quotes = world.snapshot.quotes
        seen["marked at a quote"] += bool(quotes)
        seen["a quote beside a close"] += 0 < len(quotes) < len(world.snapshot.bars)
        seen["a midpoint at five places"] += any(
            price(world.snapshot, i) * 10**4 % 1 != 0 for i in quotes
        )
        for i, q in quotes.items():
            close = world.snapshot.bars[i]["1d"][-1].close
            middle = price(world.snapshot, i)
            seen["four prices apart"] += len({q.bid, q.ask, q.last, close, middle}) == 5
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
        "reducing to net's bound",
        "held outside the constraints",
        "in doubt elsewhere",
        "two in doubt",
        "adding",
        "raised",
        "raised and refused on net",
        "another strategy's",
        "withdrawn",
        "raised by a wider record",
        "another leg's",
        "approved",
        "marked at a quote",
        "a quote beside a close",
        "a midpoint at five places",
        "four prices apart",
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
        b.replayed_through,
        b.in_doubt,
        b.corrections,
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
