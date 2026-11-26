"""Execution's recover stage over the simulated broker, as #145's acceptance names the cases.

Every price and quantity is made up. Each hand calculation is a literal beside the
decision it is of. A halt stands beside the same run with nothing wrong, so it has
the one cause its case gives it. tests/test_execution_worker.py holds the cases
through the receive loop, and tests/test_reconcile.py those of the comparison.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    ACCOUNT_OUTPUT,
    BOOK_OUTPUT,
    CAPS_OUTPUT,
    CONSTRAINTS_OUTPUT,
    DATA_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    SEND_STAGE,
    SNAPSHOT_OUTPUT,
    VALUATION_OUTPUT,
    AccountRead,
    Ack,
    BlobRef,
    Book,
    BrokerOrder,
    BrokerState,
    CashRead,
    Constraints,
    DecisionResult,
    Fill,
    FillsRead,
    Halt,
    HaltRecord,
    HardCaps,
    InstrumentId,
    OrdersRead,
    OrderState,
    Outcome,
    OutputFile,
    PositionsRead,
    ReceivedPage,
    RecordsRead,
    StageKind,
    StoreUnavailable,
    Valuation,
    attempt_key,
    caps_key,
    decode,
    decode_output,
    encode,
    encode_output,
    journal_entry_key,
    output_key,
    parse_strategy,
    result_key,
    sha256_of,
    sim_account_key,
)
from ithildinruntime.execution.account import Account
from ithildinruntime.execution.brokers.faults import (
    EditCash,
    EditOrder,
    EditPosition,
    Faults,
    FillPart,
    OrderFault,
    ReadFault,
)
from ithildinruntime.execution.brokers.simulated import SimulatedBroker
from ithildinruntime.execution.recover import differences, fills_since
from ithildinruntime.journal import (
    AbandonedEntry,
    AckEntry,
    ExpiredEntry,
    InDoubtEntry,
    IntentEntry,
    Journal,
    ReadEntry,
    SendingEntry,
    record_of,
)
from ithildinruntime.risk import LimitRisk, RiskStage
from ithildinruntime.runtime.loop import Item
from tests.support import gates
from tests.support.book_oracle import flattened, oracle, row
from tests.support.gates import d as D
from tests.support.journals import Memory
from tests.support.order_world import Kill, caps_blob, planted_verdict
from tests.support.recover_world import FILES, RecoverWorld, Waits
from tests.support.sim_world import (
    ACCOUNT,
    CASH,
    DEPLOYMENT,
    GLD,
    MICRO,
    USO,
    after,
    close_of,
)
from tests.test_risk import TEXT, fitted

# Eleven sessions from Monday 2026-11-02, two of them after a weekend.
DAYS = [date(2026, 11, 2)]
while len(DAYS) < 11:
    DAYS.append(after(DAYS[-1]))
DAY = timedelta(days=1)
# A broker's key, which nothing a person reads may hold.
SECRET = "PKFAKEKEY and more"


def prices(gld: Mapping[int, object], uso: Mapping[int, object]) -> dict:
    """Each instrument's closes by the session's place in `DAYS`."""
    return {
        GLD: {DAYS[n]: price for n, price in gld.items()},
        USO: {DAYS[n]: price for n, price in uso.items()},
    }


def flat_prices(count: int = 11) -> dict:
    return prices({n: 100 for n in range(count)}, {n: 50 for n in range(count)})


# Acceptance 1 and 8: ten decisions, each file beside its hand-worked one

TEN = prices(
    dict(enumerate([100, 101, 102, 101, 103, 104, 103, 105, 106, 107, 108])),
    dict(enumerate([50, 49, 51, 52, 50, 48, 49, 50, 51, 52, 53])),
)
# Each decision is at a session's close and its orders fill there, so the next one's book
# holds them.
PLANS: dict[int, list[tuple[object, InstrumentId]]] = {
    1: [(10, GLD), (-20, USO)],
    3: [(-4, GLD)],
    4: [(5, USO), (1, GLD)],
    6: [(-7, GLD), (15, USO)],
    7: [(-3, GLD)],
    8: [(6, GLD), (-2, USO)],
}


@dataclass(frozen=True)
class Worked:
    """What one decision's book, valuation and fills come to by hand."""

    gld: int
    uso: int
    cash: int
    through: int
    value: int
    fills: tuple[int, ...]


HAND = {
    1: Worked(0, 0, 1_000_000, 2, 1_000_000, ()),
    # 1,000,000 − 10 × 101 + 20 × 49, and at those closes + 10 × 101 − 20 × 49.
    2: Worked(10, -20, 999_970, 15, 1_000_000, (1, 2)),
    # + 10 × 102 − 20 × 51. The fills are asked from a day before decision 2, which is 1's time.
    3: Worked(10, -20, 999_970, 18, 999_970, (1, 2)),
    # + 4 × 101, then + 6 × 101 − 20 × 52.
    4: Worked(6, -20, 1_000_374, 26, 999_940, (3,)),
    # − 5 × 50 − 1 × 103, then + 7 × 103 − 15 × 50. A weekend, so a day before 4 is 3's time.
    5: Worked(7, -15, 1_000_021, 39, 999_992, (3, 4, 5)),
    # + 7 × 104 − 15 × 48. A day before decision 5, a Monday, is Sunday.
    6: Worked(7, -15, 1_000_021, 42, 1_000_029, ()),
    # + 7 × 103 − 15 × 49.
    7: Worked(0, 0, 1_000_007, 55, 1_000_007, (6, 7)),
    # + 3 × 105, then − 3 × 105.
    8: Worked(-3, 0, 1_000_322, 63, 1_000_007, (6, 7, 8)),
    # − 6 × 106 + 2 × 51, then + 3 × 106 − 2 × 51.
    9: Worked(3, -2, 999_788, 76, 1_000_004, (8, 9, 10)),
    # + 3 × 107 − 2 × 52.
    10: Worked(3, -2, 999_788, 79, 1_000_005, (9, 10)),
}


def held_of(worked: Worked) -> dict[InstrumentId, Decimal]:
    return {i: D(q) for i, q in ((GLD, worked.gld), (USO, worked.uso)) if q}


def hand_book(worked: Worked) -> Book:
    held = held_of(worked)
    return Book(
        positions_by_strategy={gates.CHAN: held} if held else {},
        open_orders_by_strategy={},
        cash=D(worked.cash),
        replayed_through=worked.through,
        in_doubt=(),
        corrections=(),
    )


def hand_value(worked: Worked, n: int) -> Valuation:
    held = held_of(worked)
    return Valuation(
        session=DAYS[n - 1],
        value=D(worked.value),
        cash=D(worked.cash),
        positions=held,
        marks={i: D(TEN[i][DAYS[n - 1]]) for i in held},
        replayed_through=worked.through,
    )


CONFIG = parse_strategy(gates.CHAN, TEXT.encode())


def risk_stage(world: RecoverWorld) -> RiskStage:
    return RiskStage(
        (CONFIG,),
        world.storage(),
        records=fitted(CONFIG, world.caps),
        stage=RISK_STAGE,
        kind=StageKind.DETERMINISTIC,
        data_stage=DATA_STAGE,
        recover_stage=RECOVER_STAGE,
        deployment=DEPLOYMENT,
        run=world.run,
        release=world.release,
        config_hashes=world.hashes,
    )


def test_1_each_file_of_ten_decisions_is_the_one_worked_by_hand() -> None:
    world = RecoverWorld(TEN)
    for n, worked in HAND.items():
        at = close_of(DAYS[n])
        made = world.decide(at, world.orders(at, PLANS.get(n, [])))
        assert world.entry(BOOK_OUTPUT) == hand_book(worked), n
        assert world.entry(VALUATION_OUTPUT) == hand_value(worked, n), n
        assert world.entry(CAPS_OUTPUT) == world.caps, n
        read = world.entry(ACCOUNT_OUTPUT)
        assert dict(read.positions.positions) == held_of(worked), n
        assert (read.cash.cash, read.open_orders.orders) == (D(worked.cash), ()), n
        assert tuple(f.fill_id for f in read.fills.fills) == tuple(
            f"sim-f-{k}" for k in worked.fills
        ), n
        opened, before = close_of(DAYS[0]), close_of(DAYS[n - 1]) - DAY
        assert read.since == (opened if n == 1 else max(opened, before)), n
        assert all(part.whole for part in read.reads.values())
        # The header of every file names the same four inputs, and the first its two.
        inputs = {
            result_key(DEPLOYMENT, world.run, at, DATA_STAGE),
            output_key(DEPLOYMENT, world.run, at, DATA_STAGE, SNAPSHOT_OUTPUT),
        }
        assert set(world.file(ACCOUNT_OUTPUT).header.inputs) == inputs
        inputs |= {world.key(ACCOUNT_OUTPUT), caps_key(ACCOUNT)}
        for name in (CAPS_OUTPUT, BOOK_OUTPUT, VALUATION_OUTPUT):
            named = world.file(name).header.inputs
            assert set(named) == inputs
            assert named[caps_key(ACCOUNT)] == sha256_of(world.store.blobs[caps_key(ACCOUNT)])
        assert set(world.result.header.inputs) == inputs
        assert [ref.key for ref in world.result.outputs] == [
            world.key(name) for name in (ACCOUNT_OUTPUT, CAPS_OUTPUT, BOOK_OUTPUT, VALUATION_OUTPUT)
        ]
        assert sorted(world.stored()) == [f"{name}.json" for name in sorted(FILES)]
        # The risk stage on what recover wrote, beside the model on the hand-worked files.
        world.store.drop(output_key(DEPLOYMENT, world.run, at, RISK_STAGE, CONSTRAINTS_OUTPUT))
        limits = risk_stage(world)(Item(at, RISK_STAGE), Waits())
        assert limits.halt is None, limits.halt
        (ref,) = limits.outputs
        (written,) = decode_output(world.store.blobs[ref.key], Constraints).entries
        assert written == LimitRisk(CONFIG, fitted(CONFIG, world.caps)).constraints(
            hand_book(worked), world.snapshot(at), world.caps, hand_value(worked, n)
        )
        # Acceptance 8: the second oracle, on the stored rows the book was replayed through.
        book = world.entry(BOOK_OUTPUT)
        told = oracle(world.rows()[: book.replayed_through])
        assert told["positions"] == {(gates.CHAN, i): q for i, q in held_of(worked).items()}
        assert (told["cash"], told["through"]) == (worked.cash, worked.through)
        # Risk's file is written again by the world, under its own loose limits.
        world._file(at, RISK_STAGE, CONSTRAINTS_OUTPUT, Constraints, world.constraints)
        assert [world.send(o) for o in made] == [OrderState.ACKNOWLEDGED] * len(made)
    assert world.alarms.sent == []
    assert [f for f in world.kinds() if f == "fill"] == ["fill"] * 10
    assert flattened(world.view()) == oracle(world.rows())


# Acceptance 2: each halt, beside the run it would have been


def ready(**changes: object) -> RecoverWorld:
    """Decision 3 ready for its recover, with USO held, one order working and one new fill.

    Decision 2 reads quotes, and no bar of its session in GLD ever comes, so its order in
    GLD stays open.
    """
    closes = prices({0: 100, 1: 100, 3: 100}, {0: 50, 1: 50, 2: 51, 3: 52})
    world = RecoverWorld(closes, **changes)  # type: ignore[arg-type]
    world.day(DAYS[1], [(-20, USO)])
    world.day(DAYS[2], [(5, GLD), (2, USO)], early=20)
    world.prepare(close_of(DAYS[3]))
    return world


def faulted(faults: Faults) -> RecoverWorld:
    """The same, where the process that recovers is handed the faults."""
    world = ready()
    world.faults = faults
    world.restart()
    return world


DATA_KEYS = {"data/result.json", "data/snapshot.json"}
CAPTURED = DATA_KEYS | {"execution/recover/account.json"}


def named(world: RecoverWorld, result: DecisionResult) -> set[str]:
    """The header's inputs, each under its decision's own prefix where it has one."""
    prefix = result_key(DEPLOYMENT, world.run, world.at, "x").removesuffix("x/result.json")
    return {key.removeprefix(prefix) for key in result.header.inputs}


def halts(
    world: RecoverWorld,
    match: str,
    files: list[str],
    kinds: list[str],
    inputs: set[str],
    result: DecisionResult | None = None,
) -> str:
    """Check one halted recover: its reason, what it stored and journaled, what it names."""
    result = result or world.attempt()
    assert result.halt is not None, "it did not halt"
    assert match in result.halt.reason, result.halt.reason
    assert list(world.stored()) == files
    assert world.kinds(world.before) == kinds
    assert named(world, result) == inputs
    captures = [name for name in files if name.startswith("account")]
    assert [ref.key.rpartition("/")[2] for ref in result.outputs] == captures
    assert SECRET[:9] not in result.halt.reason + str(world.alarms.sent)
    return result.halt.reason


def test_2_the_run_each_halt_stands_beside_stores_a_result() -> None:
    world = ready()
    before = len(world.rows())
    reported: list[int] = []
    waits = Waits()
    world.store.after = lambda n, op, key: reported.append(waits.reports) if op == "create" else 0
    result = world.stage()(Item(world.at, RECOVER_STAGE), waits)
    world.store.after = lambda n, op, key: None
    # The loop is told the handler is not hung between every two writes: the simulator's
    # call, account.json, the read and fill entries, caps.json, book.json, valuation.json.
    assert len(reported) == 7
    assert all(b > a for a, b in zip(reported, reported[1:], strict=False)), reported
    assert result.halt is None
    assert world.kinds(before) == ["read", "fill"]
    assert list(world.stored()) == ["account.json", "book.json", "caps.json", "valuation.json"]
    assert named(world, result) == CAPTURED | {caps_key(ACCOUNT)}
    book = world.entry(BOOK_OUTPUT)
    assert dict(book.positions_by_strategy[gates.CHAN]) == {USO: D(-18)}
    (opened,) = book.open_orders_by_strategy[gates.CHAN]
    assert (opened.instrument, opened.qty) == (GLD, D(5))
    # The order in GLD is past its session and still open, which is said and halts nothing.
    (said,) = world.alarms.sent
    assert "is still open after its session's close" in said
    assert said.startswith(f"account {ACCOUNT}: the order {opened.client_order_id} ")


READS = ("positions", "open_orders", "account", "activities", "fills")


@pytest.mark.parametrize("read", READS)
def test_2_a_read_that_fails(read: str) -> None:
    world = faulted(Faults(reads={(read, 1): ReadFault.FAILS}))
    halts(world, f"the broker's {read} failed with TimeoutError", [], [], DATA_KEYS)


@pytest.mark.parametrize("read", READS)
def test_2_a_read_that_stops_early_is_stored_and_never_compared(read: str) -> None:
    world = faulted(Faults(reads={(read, 1): ReadFault.SHORT}))
    # It names the read that is short alone, so a missing row is never a difference.
    reason = halts(world, f"the broker's {read} did not", ["account.json"], ["read"], CAPTURED)
    assert reason == f"the broker's {read} did not come whole, as {world.key(ACCOUNT_OUTPUT)}"


def test_2_every_read_that_stops_early_is_named() -> None:
    world = faulted(Faults(reads={(read, 1): ReadFault.SHORT for read in READS}))
    halts(
        world,
        "the broker's positions, open_orders, account, activities, fills did not come whole",
        ["account.json"],
        ["read"],
        CAPTURED,
    )


# Nothing is at the broker alone but what each does, so each is the one difference.
EDITS = {
    "a position": (EditPosition(USO, D(1)), "USO is -18 in the book and -17 at the broker"),
    "a position from none": (EditPosition(GLD, D(3)), "GLD is 0 in the book and 3 at the broker"),
    "a position to none": (
        EditPosition(USO, D(18)),
        "USO is -18 in the book and none at the broker",
    ),
    "cash": (EditCash(D("0.0001")), "cash is 1000898 in the book and 1000898.0001 at the broker"),
    "an open order": (
        EditOrder("sim-o-2", D(4)),
        "the broker's sim-o-2, differs in shares (5 in the book and 4 at the broker)",
    ),
}


@pytest.mark.parametrize("edit", EDITS)
def test_2_a_difference_halts_naming_both_sides(edit: str) -> None:
    event, said = EDITS[edit]
    world = faulted(Faults(events={1: event}))
    reason = halts(world, said, ["account.json"], ["read", "fill"], CAPTURED)
    assert reason.startswith(
        f"the book and the broker differ, as {world.key(ACCOUNT_OUTPUT)} holds it: "
    )
    assert reason.count("at the broker") == 1


def test_2_every_difference_is_in_the_reason() -> None:
    events = {1: EditCash(D(1)), 2: EditPosition(GLD, D(3)), 3: EditOrder("sim-o-2", D(4))}
    world = faulted(Faults(events=events))
    # The first read is then the third call, ahead of which all three happen.
    world.log.calls = 2
    reason = halts(world, "GLD is 0", ["account.json"], ["read", "fill"], CAPTURED)
    assert [part.split(" ")[0] for part in reason.split("holds it: ")[1].split("; ")] == [
        "GLD",
        "the",
        "cash",
    ]


def test_2_a_fill_that_lands_between_two_reads_is_a_difference() -> None:
    """The positions are read before the fills, so a fill between them never matches."""
    world = ready()
    earlier = replace(world.broker().positions(), positions={USO: D(-20)})
    result = world.attempt(world.stage(brokers=doctored(world, positions=lambda: earlier)))
    said = "USO is -18 in the book and -20 at the broker"
    assert result.halt is not None and said in result.halt.reason
    assert result.halt.reason.count("at the broker") == 1


def test_2_the_reads_are_made_in_their_order_with_the_fills_last() -> None:
    world, calls = ready(), []
    result = world.attempt(world.stage(brokers=doctored(world, calls=calls)))
    assert result.halt is None
    assert calls == ["positions", "open_orders", "account", "activities", "fills"]


class Doctored:
    """A broker whose every call is the simulator's, apart from the reads a test answers."""

    def __init__(
        self,
        inner: SimulatedBroker,
        reads: Mapping[str, Callable[..., object]],
        calls: list[str] | None,
    ) -> None:
        self._inner = inner
        self._reads = reads
        self._calls = [] if calls is None else calls

    def __getattr__(self, name: str) -> object:
        if name == "alias":
            return self._inner.alias
        self._calls.append(name)
        return self._reads[name] if name in self._reads else getattr(self._inner, name)


def doctored(
    world: RecoverWorld, calls: list[str] | None = None, **reads: Callable[..., object]
) -> Callable[[datetime], object]:
    return lambda at: Doctored(world.broker(), reads, calls)


def test_2_a_missing_data_result_and_a_halt_of_datas_are_passed_on() -> None:
    world = ready()
    key = result_key(DEPLOYMENT, world.run, world.at, DATA_STAGE)
    stored = world.store.blobs[key]
    world.store.drop(key)
    halts(world, f"{key} is not stored", [], [], set())
    # A halt is passed on naming the result that holds it.
    why = HaltRecord("USO missing for the session", None, (), (), ())
    held = replace(decode(stored, DecisionResult), outputs=(), halt=why)
    world.store.put(key, encode(held))
    result = world.attempt()
    halts(world, "holds a halt: USO missing", [], [], {"data/result.json"}, result)
    assert result.halt.cause == BlobRef(key, sha256_of(encode(held)))


def test_2_datas_result_under_another_configuration_halts() -> None:
    world = ready()
    key = result_key(DEPLOYMENT, world.run, world.at, DATA_STAGE)
    result = decode(world.store.blobs[key], DecisionResult)
    other = replace(result, header=replace(result.header, config_hashes={gates.CHAN: "d" * 64}))
    world.store.put(key, encode(other))
    halts(world, "does not carry the configuration of chan", [], [], {"data/result.json"})


def test_2_a_missing_journal_halts_before_any_call_of_the_broker() -> None:
    world = RecoverWorld(flat_prices(), opened=False)
    SimulatedBroker.open(world.sim, ACCOUNT, CASH)
    world.clock.at = close_of(DAYS[0])
    world.prepare(close_of(DAYS[1]))
    calls: list[str] = []
    result = world.attempt(world.stage(brokers=doctored(world, calls=calls)))
    halts(world, f"journal {ACCOUNT}: it holds no entry", [], [], DATA_KEYS, result)
    assert calls == []


def test_2_a_journal_that_cannot_be_read_alarms_with_the_owners_step() -> None:
    world = ready()
    world.store.refuse(("list", f"journal/{ACCOUNT}/"))
    result = world.attempt()
    world.store.before = lambda n, op, key: None
    said = (
        "the journal could not be read: StoreUnavailable AccessDenied. The owner's step: the "
        "broker's site"
    )
    halts(world, said, [], [], DATA_KEYS, result)
    assert world.alarms.sent == [f"account {ACCOUNT}: {said}"]


@pytest.mark.parametrize("kind", ["read", "fill"])
def test_2_an_entry_the_store_does_not_take_alarms_with_the_owners_step(kind: str) -> None:
    world = ready()
    before = len(world.rows())
    at = before + (1 if kind == "read" else 2)
    world.store.refuse(("create", journal_entry_key(ACCOUNT, at)))
    result = world.attempt()
    world.store.before = lambda n, op, key: None
    said = f"a {kind} was not stored in the journal: StoreUnavailable AccessDenied"
    assert said in result.halt.reason and said in world.alarms.sent[0]
    assert "the store's own text" not in result.halt.reason
    assert world.kinds(before) == ([] if kind == "read" else ["read"])


@pytest.mark.parametrize(
    ("blob", "said", "read"),
    [
        (None, f"{caps_key(ACCOUNT)} is missing", False),
        ("other", f"the caps blob does not name the account '{ACCOUNT}'", True),
        (b"{", "the caps blob is not JSON", True),
        (b"[]", "the caps blob is not a JSON object", True),
    ],
)
def test_2_a_caps_blob_missing_or_refused(blob: object, said: str, read: bool) -> None:
    world = ready()
    if blob is None:
        world.store.drop(caps_key(ACCOUNT))
    else:
        # A broker's account number in place of the alias is never shown.
        wrong = caps_blob(world.caps, SECRET) if blob == "other" else blob
        world.store.put(caps_key(ACCOUNT), wrong)  # type: ignore[arg-type]
    inputs = CAPTURED | ({caps_key(ACCOUNT)} if read else set())
    halts(world, said, ["account.json"], ["read", "fill"], inputs)


def test_2_a_held_instrument_with_no_bar_of_the_session_valued_halts() -> None:
    world = RecoverWorld(flat_prices())
    world.day(DAYS[1], [(-20, USO)])
    world.day(DAYS[2], [])
    world.prepare(close_of(DAYS[3]))
    whole = world.snapshot(world.at)
    newest = {i: {"1d": whole.bars[i]["1d"][-1:]} for i in whole.bars}
    world.stored_snapshot(replace(whole, bars=newest))
    halts(
        world,
        f"USO has 0 daily bars for {DAYS[2].isoformat()}, not one",
        ["account.json", "caps.json"],
        ["read"],
        CAPTURED | {caps_key(ACCOUNT)},
    )


def test_2_a_broker_of_another_account_halts_with_nothing_of_its_name() -> None:
    world = ready()

    class Other(Doctored):
        alias = SECRET

    result = world.attempt(world.stage(brokers=lambda at: Other(world.broker(), {}, None)))
    halts(world, f"the broker trades another account than '{ACCOUNT}'", [], [], DATA_KEYS, result)


def test_2_an_account_whose_alias_keeps_no_journal_halts() -> None:
    world = ready()
    account = Account(
        store=world.store,  # type: ignore[arg-type]
        storage=world.storage(),
        account="a/b",
        writer="f" * 32,
        gate=world.account._shared.gate,
        clock=world.clock,  # type: ignore[arg-type]
        alerts=world.alarms,
        retry=world.account._shared.patience._retry,
        pause=world.clock.wait,
    )
    result = world.attempt(world.stage(account))
    halts(world, "'a/b' is no alias a journal is kept under", [], [], DATA_KEYS, result)


def test_2_a_mode_with_no_broker_halts_ahead_of_every_read() -> None:
    world = ready()
    result = world.attempt(world.stage(brokers=Halt("no broker is built on shadow")))
    halts(world, "no broker is built on shadow", [], [], set(), result)


def test_2_an_error_a_read_raises_names_its_type_alone() -> None:
    """Acceptance 10, on each read and on the lookup."""
    for read in READS:
        world = ready()

        def fails(*args: object) -> object:
            raise RuntimeError(SECRET)

        result = world.attempt(world.stage(brokers=doctored(world, **{read: fails})))
        halts(world, f"the broker's {read} failed with RuntimeError", [], [], DATA_KEYS, result)
        world = ready()
        result = world.attempt(world.stage(brokers=doctored(world, **{read: lambda *a: SECRET})))
        halts(world, f"the broker's {read} failed with str", [], [], DATA_KEYS, result)


def page(text: str = "[]") -> tuple[ReceivedPage, ...]:
    return (ReceivedPage(text.encode(), close_of(DAYS[3])),)


# A key-shaped name holds no space, so it is a name and is shown where an instrument is.
KEYLIKE = "PKA1B2C3D4E5F6G7H8"


@pytest.mark.parametrize(
    ("reads", "said"),
    [
        (
            {"positions": lambda: PositionsRead({InstrumentId(SECRET): D(1)}, page(SECRET), True)},
            "an instrument whose name is none is 0 in the book and 1 at the broker",
        ),
        (
            {
                "positions": lambda: PositionsRead(
                    {InstrumentId(SECRET): Decimal("NaN")}, page(), True
                )
            },
            "account.json was not stored: what was read is no AccountRead",
        ),
        (
            {
                "positions": lambda: PositionsRead(
                    {InstrumentId("\ud800" + SECRET): D(1)}, page(), True
                )
            },
            "account.json was not stored: what was read is no AccountRead",
        ),
        (
            {"open_orders": "beside the one working"},
            "the broker's order whose id the journal refuses, 1 of an instrument whose name is "
            "none under a client order id that is no stamp of the platform's",
        ),
        (
            {"fills": lambda since: FillsRead((odd_fill(),), page(SECRET), True)},
            "the fill whose id the journal refuses is of the broker's order whose id the "
            "journal refuses",
        ),
        (
            {"fills": lambda since: FillsRead((odd_fill(fill_id="f-9"),), page(), True)},
            "the fill f-9 is of the broker's order whose id the journal refuses",
        ),
        (
            {
                "fills": lambda since: FillsRead(
                    (odd_fill(fill_id="f-9", order="sim-o-2"),), page(), True
                )
            },
            "the fill f-9 carries a correction's id",
        ),
        (
            # A whole flag that is no bool, which the codec refuses with the rest.
            {
                "fills": lambda since: FillsRead(
                    (odd_fill(fill_id="f-9", order="sim-o-2"),), page(), 1
                )
            },
            "account.json was not stored: what was read is no AccountRead",
        ),
    ],
)
def test_10_no_word_of_the_brokers_is_in_a_halt(reads: dict, said: str) -> None:
    world = ready()
    if reads.get("open_orders") == "beside the one working":
        listed = (foreign_order(), *world.broker().open_orders().orders)
        reads = {"open_orders": lambda: OrdersRead(listed, page(SECRET), True)}
    result = world.attempt(world.stage(brokers=doctored(world, **reads)))
    assert result.halt is not None and said in result.halt.reason, result.halt
    assert SECRET[:9] not in result.halt.reason + str(world.alarms.sent)


def test_10_an_instrument_is_shown_where_it_is_a_name_whatever_its_letters() -> None:
    """What 05 says a difference shows: a name, and no other text."""
    world = ready()
    held = PositionsRead({InstrumentId(KEYLIKE): D(1), USO: D(-18)}, page(), True)
    result = world.attempt(world.stage(brokers=doctored(world, positions=lambda: held)))
    assert result.halt is not None
    assert f"{KEYLIKE} is 0 in the book and 1 at the broker" in result.halt.reason


def foreign_order() -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=SECRET,
        client_order_id=SECRET,
        instrument=InstrumentId(SECRET),
        qty=D(1),
        filled=D(0),
        type=None,
        time_in_force=None,
        state=BrokerState.WORKING,
        submitted_at=close_of(DAYS[3]),
    )


def odd_fill(fill_id: str = SECRET, order: str = SECRET, **changes: object) -> Fill:
    at = close_of(DAYS[2])
    fields: dict[str, object] = {
        "fill_id": fill_id,
        "correction_id": SECRET,
        "broker_order_id": order,
        "client_order_id": SECRET,
        "instrument": InstrumentId(SECRET),
        "qty": D(1),
        "price": D(1),
        "at_text": SECRET,
        "at": at,
        "received_at": at,
        "excess_places": (),
        "basis": None,
    }
    return Fill(**fields | changes)  # type: ignore[arg-type]


# The orders nothing has settled, which step 2 halts on


AT1 = close_of(DAYS[1]).isoformat()
ONE, TWO = (f"ith-chan-20261103T210000Z-0{n}" for n in (1, 2))


def journaled(world: RecoverWorld, *steps: tuple[str, int]) -> None:
    """Take orders of the newest plan to a state by hand, as a crash could leave them."""
    seq, entry = list(world.view().decisions.items())[-1]
    for kind, place in steps:
        order = entry.orders[place - 1]
        name = order.client_order_id
        match kind:
            case "intent":
                world._record(IntentEntry(seq, order))
            case "verdict":
                world._record(planted_verdict(name, len(world.rows())))
            case "sending":
                world._record(SendingEntry(name))
            case "in_doubt":
                world._record(InDoubtEntry(name, "no answer", None, False, world.clock.at))
            case "ack":
                world._record(AckEntry(name, f"b-{place}", b"{}", False, world.clock.at))
            case "abandoned":
                world._record(AbandonedEntry(name, "a check failed"))


SENT = [("intent", 1), ("verdict", 1), ("sending", 1)]
NONE_TWO = f"the order {TWO} has no entry"
STUCK = {
    "intended": ([("intent", 1)], f"{ONE} is Intended, its sending stored never", NONE_TWO),
    "approved": (
        [("intent", 1), ("verdict", 1)],
        f"{ONE} is Approved, its sending stored never",
        NONE_TWO,
    ),
    "sending": (SENT, f"{ONE} is Sending, its sending stored at {AT1}", NONE_TWO),
    "in doubt": (
        [*SENT, ("in_doubt", 1)],
        f"{ONE} is InDoubt, its sending stored at {AT1}",
        NONE_TWO,
    ),
    "a leg acknowledged and its partner with no entry": (
        [*SENT, ("ack", 1)],
        f"{ONE} is Acknowledged, its sending stored at {AT1}",
        NONE_TWO,
    ),
    "a leg abandoned and its partner with no entry": (
        [("intent", 1), ("abandoned", 1)],
        f"{ONE} is Abandoned, its sending stored never",
        NONE_TWO,
    ),
    "a leg abandoned and its partner intended": (
        [("intent", 1), ("abandoned", 1), ("intent", 2)],
        f"{ONE} is Abandoned, its sending stored never",
        f"{TWO} is Intended, its sending stored never",
    ),
    "a leg abandoned and its partner approved": (
        [("intent", 1), ("abandoned", 1), ("intent", 2), ("verdict", 2)],
        f"{ONE} is Abandoned, its sending stored never",
        f"{TWO} is Approved, its sending stored never",
    ),
    "a leg abandoned and its partner in doubt": (
        [("intent", 1), ("abandoned", 1), *((kind, 2) for kind, _ in SENT), ("in_doubt", 2)],
        f"{ONE} is Abandoned, its sending stored never",
        f"{TWO} is InDoubt, its sending stored at {AT1}",
    ),
    "a leg acknowledged and its partner sending": (
        [*SENT, ("ack", 1), *((kind, 2) for kind, _ in SENT)],
        f"{ONE} is Acknowledged, its sending stored at {AT1}",
        f"{TWO} is Sending, its sending stored at {AT1}",
    ),
}


def planned_pair(*steps: tuple[str, int]) -> RecoverWorld:
    world = RecoverWorld(flat_prices())
    at = close_of(DAYS[1])
    world.decide(at, world.orders(at, [(10, GLD), (-20, USO)]))
    journaled(world, *steps)
    world.prepare(close_of(DAYS[2]))
    return world


@pytest.mark.parametrize("case", STUCK)
def test_2_a_plan_a_crash_left_halts_with_each_order_and_alarms_for_each(case: str) -> None:
    steps, *said = STUCK[case]
    world = planned_pair(*steps)
    calls: list[str] = []
    stage = world.stage(brokers=doctored(world, calls=calls))
    result = world.attempt(stage)
    reason = halts(world, said[0], [], [], DATA_KEYS, result)
    when = f"the decision at {AT1}"
    assert reason == (
        f"account {ACCOUNT}: {when} is not settled: the order {'; the order '.join(bare(said))}"
    )
    assert world.alarms.sent == [
        f"account {ACCOUNT}: the order {one}, in {when}, whose orders are not settled"
        for one in bare(said)
    ]
    assert calls == []
    # A cause that stands pages once in a process, and again after a restart.
    world.prepare(close_of(DAYS[3]))
    # The worker keeps one stage for its run, and this world keys a run by its session.
    later = world.stage(brokers=doctored(world, calls=calls))
    later._said = stage._said
    again = world.attempt(later)
    assert again.halt is not None and again.halt.reason.endswith(reason.split(": ", 1)[1])
    assert len(world.alarms.sent) == len(bare(said))
    world.restart()
    world.attempt()
    assert len(world.alarms.sent) == 2 * len(bare(said))


def bare(said: list[str]) -> list[str]:
    return [one.removeprefix("the order ") for one in said]


def test_2_an_alarm_about_a_plan_that_could_not_be_sent_is_in_the_reason() -> None:
    world = planned_pair(*SENT)
    world.alarms.error = RuntimeError(SECRET)
    halts(world, "The alarm could not be sent, and is tried again", [], [], DATA_KEYS)
    # Both alarms were tried, and each is owed until the channel takes it.
    assert len(world.alarms.sent) == 2 and len(world.account.unsent()) == 2
    world.alarms.error = None
    world.account.settle(lambda: None)
    assert len(world.alarms.sent) == 4 and world.account.unsent() == ()


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [("intent", 1), ("abandoned", 1), ("intent", 2), ("abandoned", 2)],
    ],
    ids=["no order has an entry", "each order ended"],
)
def test_2_a_plan_that_stands_halts_nothing(steps: list) -> None:
    """A crash right after the `decision` entry sent nothing, so the next decision goes on."""
    world = planned_pair(*steps)
    assert world.attempt().halt is None
    assert world.alarms.sent == []


def test_2_every_unfinished_plan_is_named_not_the_newest_alone() -> None:
    world = planned_pair(*SENT)
    world.store.put(
        result_key(DEPLOYMENT, world.run, world.at, RECOVER_STAGE), b"a halted recover's result"
    )
    world._file(world.at, RECOVER_STAGE, VALUATION_OUTPUT, Valuation, hand_value(HAND[1], 1))
    world.plan(())
    world.prepare(close_of(DAYS[3]))
    result = world.attempt()
    assert result.halt is not None and f"{ONE} is Sending" in result.halt.reason


def test_2_an_answer_this_process_owes_is_settled_before_the_view() -> None:
    """An order whose `ack` the store refused reads as in doubt to another process alone."""
    world = RecoverWorld(flat_prices())
    at = close_of(DAYS[1])
    (made,) = world.decide(at, world.orders(at, [(10, GLD)]))
    ack = journal_entry_key(ACCOUNT, len(world.rows()) + 4)
    world.store.refuse(("create", ack))
    with pytest.raises(Halt, match="the journal did not take the ack"):
        world.send(made)
    assert world.account.unstored()
    world.prepare(close_of(DAYS[2]))
    # While the store refuses it, the decision halts naming it and reads nothing more.
    result = world.attempt()
    halts(world, "the journal still does not take the ack", [], [], DATA_KEYS, result)
    world.store.before = lambda n, op, key: None
    assert world.attempt().halt is None
    assert world.kinds()[-3:] == ["ack", "read", "fill"]
    assert world.account.unstored() == ()


# The fills: what is new, what is held, what differs and what is no order's of the platform's


def broker_fill(world: RecoverWorld, **changes: object) -> Fill:
    """The one new fill of `ready`, as the broker lists it, changed."""
    fill = world.broker().fills(close_of(DAYS[0])).fills[-1]
    return replace(fill, **changes)


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"broker_order_id": SECRET}, "whose id the journal refuses, which no order of the"),
        ({"fill_id": SECRET}, "the fill's id is not a broker's id"),
    ],
)
def test_2_a_fill_whose_id_the_journal_refuses_halts_and_is_not_journaled(changes, said) -> None:
    world = ready()
    odd = broker_fill(world, **changes)
    read = FillsRead((odd,), page(), True)
    result = world.attempt(world.stage(brokers=doctored(world, fills=lambda since: read)))
    halts(world, said, ["account.json"], ["read"], CAPTURED, result)


UNOWNED = {
    "no order": (
        {"broker_order_id": "b-unknown"},
        "the fill sim-f-2 is of the broker's order b-unknown, which no order was acknowledged "
        "under",
    ),
    "no order, and no shares": (
        {"broker_order_id": "b-unknown", "qty": D(0)},
        "the fill sim-f-2 is of the broker's order b-unknown, which no order was acknowledged "
        "under",
    ),
    "client order id": (
        {"client_order_id": SECRET},
        "fill sim-f-2 names another client order id than",
    ),
    "instrument": ({"instrument": GLD}, "fill sim-f-2 is in 'GLD', and "),
    "unnamed instrument": (
        {"instrument": InstrumentId(SECRET)},
        "is in an instrument whose name is none, and",
    ),
    "side": ({"qty": D(-2)}, "fill sim-f-2 of -2 is not on "),
    "past its quantity": ({"qty": D(3)}, "to 3 filled of 2"),
    "zero shares": ({"qty": D(0)}, "the fill sim-f-2 is for zero shares"),
    "no price": ({"price": D(0)}, "the fill sim-f-2 has a price of 0, not above zero"),
    "a correction": (
        {"correction_id": SECRET},
        "the fill sim-f-2 carries a correction's id",
    ),
}


@pytest.mark.parametrize("case", UNOWNED)
def test_2_a_fill_no_order_of_the_platforms_takes_is_kept_as_unmatched_and_holds(
    case: str,
) -> None:
    changes, said = UNOWNED[case]
    world = ready()
    odd = broker_fill(world, **changes)
    read = FillsRead((odd,), page(), True)
    brokers = doctored(world, fills=lambda since: read)
    result = world.attempt(world.stage(brokers=brokers))
    reason = halts(world, said, ["account.json"], ["read", "unmatched_fill"], CAPTURED, result)
    assert (
        "keeps an unmatched fill, which holds the account until a person releases it, and "
        "nothing releases one yet" in reason
    )
    assert SECRET not in reason
    (row,) = [r for r in world.rows() if r["body"]["kind"] == "unmatched_fill"]
    # Kept as it came.
    assert decode(encode(row["body"]["fill"]), Fill) == odd
    # The next decision reads it again, keeps nothing new, and halts the same way.
    world.prepare(world.at + DAY)
    again = world.attempt(world.stage(brokers=brokers))
    assert again.halt is not None and said in again.halt.reason
    assert world.kinds(world.before) == ["read"]


@pytest.mark.parametrize(
    "changes",
    [
        {"broker_order_id": "b-other"},
        {"instrument": GLD},
        {"qty": D(-19)},
        {"price": D("50.0001")},
        {"at": close_of(DAYS[1]) + MICRO},
    ],
    ids=["order", "instrument", "shares", "price", "time"],
)
def test_2_a_repeat_whose_content_differs_is_a_dispute_once_and_halts_every_decision(
    changes: dict,
) -> None:
    world = ready()
    (first, second) = world.broker().fills(close_of(DAYS[0])).fills
    read = FillsRead((replace(first, **changes), second), page(), True)
    brokers = doctored(world, fills=lambda since: read)
    result = world.attempt(world.stage(brokers=brokers))
    halts(
        world,
        "the fill sim-f-1 is in dispute",
        ["account.json"],
        ["read", "disputed_fill", "fill"],
        CAPTURED,
        result,
    )
    # The next decision reads the same two contents and records neither again.
    world.prepare(close_of(DAYS[4]) if DAYS[4] in world.closes[USO] else world.at + DAY)
    result = world.attempt(world.stage(brokers=brokers))
    assert result.halt is not None and "the fill sim-f-1 is in dispute" in result.halt.reason
    assert world.kinds()[-3:] == ["disputed_fill", "fill", "read"]


@pytest.mark.parametrize(
    "changes",
    [
        {"received_at": close_of(DAYS[3]) + DAY},
        {"client_order_id": None},
        {"at_text": "another form"},
        {"basis": None},
        {"price": D("50.00")},
    ],
)
def test_2_a_repeat_whose_content_is_the_same_is_skipped(changes: dict) -> None:
    world = ready()
    (first, second) = world.broker().fills(close_of(DAYS[0])).fills
    read = FillsRead((replace(first, **changes), second, second), page(), True)
    result = world.attempt(world.stage(brokers=doctored(world, fills=lambda since: read)))
    assert result.halt is None
    assert world.kinds()[-2:] == ["read", "fill"]


# The orders the broker no longer lists


def unlisted(
    answer: Callable[[SimulatedBroker, str], object], faults: Faults | None = None
) -> tuple[RecoverWorld, DecisionResult, list[str]]:
    """`ready`, where the broker lists no open order and a lookup of the one in GLD answers so."""
    world = ready()
    calls: list[str] = []
    none = OrdersRead((), page(), True)
    result = world.attempt(
        world.stage(
            brokers=doctored(
                world,
                calls,
                open_orders=lambda: none,
                lookup=lambda broker: answer(world.broker(), broker),
            )
        )
    )
    return world, result, calls


def says(**changes: object) -> Callable[[SimulatedBroker, str], Ack]:
    def answer(broker: SimulatedBroker, name: str) -> Ack:
        found = broker.lookup(name)
        assert found.order is not None
        return replace(found, order=replace(found.order, **changes), raw=b"what it said")

    return answer


@pytest.mark.parametrize("state", [BrokerState.EXPIRED, BrokerState.REJECTED])
def test_2_an_order_the_broker_no_longer_lists_is_looked_up_and_its_end_journaled(state) -> None:
    world, result, calls = unlisted(says(state=state))
    assert result.halt is None, result.halt.reason
    assert world.kinds()[-3:] == ["read", "fill", state.value]
    assert calls == ["positions", "open_orders", "account", "activities", "fills", "lookup"]
    last = record_of(list(Journal(Memory(world.store.blobs), ACCOUNT).read())[-1])
    assert (last.raw, last.cut) == (b"what it said", False)
    assert world.entry(BOOK_OUTPUT).open_orders_by_strategy == {}
    assert world.alarms.sent == []


def test_2_an_end_whose_bytes_are_too_long_is_journaled_cut() -> None:
    def long(broker: SimulatedBroker, name: str) -> Ack:
        return replace(says(state=BrokerState.EXPIRED)(broker, name), raw=b"x" * 65_537)

    world, result, _ = unlisted(long)
    assert result.halt is None
    last = record_of(list(Journal(Memory(world.store.blobs), ACCOUNT).read())[-1])
    assert (len(last.raw), last.cut) == (65_536, True)


def test_2_an_end_whose_bytes_are_exactly_the_most_kept_is_not_cut() -> None:
    def whole(broker: SimulatedBroker, name: str) -> Ack:
        return replace(says(state=BrokerState.EXPIRED)(broker, name), raw=b"x" * 65_536)

    world, result, _ = unlisted(whole)
    assert result.halt is None
    last = record_of(list(Journal(Memory(world.store.blobs), ACCOUNT).read())[-1])
    assert (len(last.raw), last.cut) == (65_536, False)


def not_found(broker: SimulatedBroker, name: str) -> Ack:
    return Ack(Outcome.NOT_FOUND, None, None, b"{}", close_of(DAYS[3]))


def fails(broker: SimulatedBroker, name: str) -> Ack:
    raise RuntimeError(SECRET)


OTHER = "its lookup found another order"
LOOKUPS = {
    "still working": (says(), "its lookup says working with 0 filled"),
    "cancelled": (says(state=BrokerState.CANCELLED), "its lookup says cancelled with 0 filled"),
    "being cancelled": (says(state=BrokerState.PENDING_CANCEL), "says pending_cancel with 0"),
    "filled": (says(state=BrokerState.FILLED, filled=D(5)), "says filled with 5 filled"),
    "in no state it reads": (says(state=BrokerState.UNKNOWN), "its lookup says unknown with 0"),
    "rejected with shares filled": (
        says(state=BrokerState.REJECTED, filled=D(1)),
        "its lookup says rejected with 1 filled",
    ),
    "expired with a fill the journal lacks": (
        says(state=BrokerState.EXPIRED, filled=D(1)),
        "expired with 1 filled, and its fills come to 0",
    ),
    "not found": (not_found, "its lookup found no such order"),
    "another id": (says(broker_order_id="sim-o-1"), OTHER),
    "another client order id": (says(client_order_id=SECRET), OTHER),
    "another instrument": (says(instrument=USO), OTHER),
    "other shares": (says(qty=D(4)), OTHER),
    "another id on the answer": (
        lambda b, n: replace(says(state=BrokerState.EXPIRED)(b, n), broker_order_id="sim-o-1"),
        OTHER,
    ),
    "unknown with a copy that expired": (
        lambda b, n: replace(says(state=BrokerState.EXPIRED)(b, n), outcome=Outcome.UNKNOWN),
        "its lookup found no such order",
    ),
    "a lookup that fails": (fails, "the broker's lookup failed with RuntimeError"),
}


@pytest.mark.parametrize("case", LOOKUPS)
def test_2_any_other_answer_to_a_lookup_halts(case: str) -> None:
    answer, said = LOOKUPS[case]
    world, result, _ = unlisted(answer)
    halts(world, said, ["account.json"], ["read", "fill"], CAPTURED, result)


def test_2_an_order_whose_fills_explain_it_is_not_looked_up() -> None:
    world = RecoverWorld(flat_prices())
    world.day(DAYS[1], [(-20, USO)])
    world.prepare(close_of(DAYS[2]))
    calls: list[str] = []
    assert world.attempt(world.stage(brokers=doctored(world, calls=calls))).halt is None
    assert calls == list(READS)


def test_2_a_part_filled_order_that_expired_is_journaled() -> None:
    """Through the simulator's own fault, with the fill and the end in one recover."""
    world = RecoverWorld(flat_prices())
    at = close_of(DAYS[1])
    made = world.decide(at, world.orders(at, [(10, GLD), (-20, USO)]))
    faults = {
        made[0].client_order_id: FillPart(D(3)),
        made[1].client_order_id: OrderFault.REJECT_LATER,
    }
    world.faults = Faults(orders=faults)
    for order in made:
        world.send(order)
    # The second was rejected as the first's answer came, or at the next call.
    world.prepare(close_of(DAYS[2]))
    result = world.attempt()
    assert result.halt is None, result.halt.reason
    assert world.kinds()[-4:] == ["read", "fill", "expired", "rejected"]
    book = world.entry(BOOK_OUTPUT)
    assert (book.positions_by_strategy, book.open_orders_by_strategy) == (
        {gates.CHAN: {GLD: D(3)}},
        {},
    )


# Acceptance 3: a kill after each write, and then another attempt


def busy() -> RecoverWorld:
    """Decision 2 ready, with two fills and an order that expired in part to journal."""
    world = RecoverWorld(flat_prices())
    at = close_of(DAYS[1])
    made = world.decide(at, world.orders(at, [(10, GLD), (-20, USO)]))
    world.faults = Faults(orders={made[0].client_order_id: FillPart(D(3))})
    for order in made:
        world.send(order)
    world.prepare(close_of(DAYS[2]))
    return world


BUSY_KINDS = ["read", "fill", "fill", "expired"]
BUSY_FILES = ["account.json", "book.json", "caps.json", "valuation.json"]


def creates(world: RecoverWorld) -> list[str]:
    """The keys one attempt creates, in order."""
    world.store.calls.clear()
    assert world.attempt().halt is None
    return [key for op, key in world.store.calls if op == "create"]


def killed(world: RecoverWorld, at: int, landed: bool) -> None:
    """One attempt that ends at its `at`th create, before it or after it, and a new process."""
    count = 0

    def hook(number: int, op: str, key: str) -> None:
        nonlocal count
        if op == "create":
            count += 1
            if count == at:
                raise Kill

    if landed:
        world.store.after = hook
    else:
        world.store.before = hook
    try:
        world.attempt()
    except Kill:
        assert at > 1
    else:
        # The path turns whatever a broker's call raises into a halt, which dies with the process.
        assert at == 1
    world.store.before = world.store.after = lambda n, op, key: None
    world.restart()


WRITES = 9  # the simulator's call, four files and four entries


def test_3_a_recover_writes_in_its_order() -> None:
    world = busy()
    made = [key.rpartition("/")[2] for key in creates(world)]
    entries = [f"{n:012d}.json" for n in range(13, 17)]
    assert made == [
        "000000000004.json",
        "account.json",
        *entries,
        "caps.json",
        "book.json",
        "valuation.json",
    ]
    assert len(made) == WRITES


@pytest.mark.parametrize("landed", [False, True])
@pytest.mark.parametrize("at", range(1, WRITES + 1))
def test_3_a_kill_at_a_write_then_a_rerun_gives_the_same_files(at: int, landed: bool) -> None:
    whole = busy()
    result = whole.attempt()
    world = busy()
    killed(world, at, landed)
    again = world.attempt()
    assert again == result
    assert world.stored() == whole.stored()
    assert world.kinds(12) == BUSY_KINDS
    assert world.alarms.sent == []


@pytest.mark.parametrize("gone", [False, True])
@pytest.mark.parametrize("at", [7, 8, 9])
def test_3_with_the_caps_rewritten_between_attempts_the_stored_ones_stand(
    at: int, gone: bool
) -> None:
    whole = busy()
    result = whole.attempt()
    world = busy()
    killed(world, at, True)
    if gone:
        world.store.drop(caps_key(ACCOUNT))
    else:
        world.rewrite_caps(max_gross=D(5))
    assert world.attempt() == result
    assert world.stored() == whole.stored()


def test_3_caps_rewritten_before_they_were_stored_are_the_new_ones() -> None:
    world = busy()
    killed(world, 7, False)
    world.rewrite_caps(max_gross=D(5))
    result = world.attempt()
    assert world.entry(CAPS_OUTPUT).max_gross == 5
    new = sha256_of(world.store.blobs[caps_key(ACCOUNT)])
    for name in (CAPS_OUTPUT, BOOK_OUTPUT, VALUATION_OUTPUT):
        assert world.file(name).header.inputs[caps_key(ACCOUNT)] == new
    assert result.header.inputs[caps_key(ACCOUNT)] == new


def test_3_a_caps_file_that_names_no_blob_halts() -> None:
    world = busy()
    world.attempt()
    held = world.file(CAPS_OUTPUT)
    inputs = {k: v for k, v in held.header.inputs.items() if k != caps_key(ACCOUNT)}
    bare = OutputFile(replace(held.header, inputs=inputs), held.entries, False)
    world.store.put(world.key(CAPS_OUTPUT), encode_output(HardCaps, bare))
    result = world.attempt()
    assert result.halt is not None
    assert f"does not name {caps_key(ACCOUNT)}" in result.halt.reason


def test_3_with_an_entry_appended_between_attempts_the_stored_book_stands() -> None:
    whole = busy()
    result = whole.attempt()
    for at in (8, 9):
        world = busy()
        killed(world, at, True)
        # An entry that moves nothing of the book, so the comparison still agrees.
        world._record(ReadEntry(SEND_STAGE, world.at, BlobRef("r-2", "b" * 64), world.at))
        assert world.attempt() == result
        assert world.stored() == whole.stored()
    world = busy()
    killed(world, 8, False)
    world._record(ReadEntry(SEND_STAGE, world.at, BlobRef("r-2", "b" * 64), world.at))
    assert world.attempt().halt is None
    # A book not yet stored is the one the journal now replays to.
    assert world.entry(BOOK_OUTPUT).replayed_through == len(world.rows())
    assert world.entry(VALUATION_OUTPUT).replayed_through == len(world.rows())


def test_3_a_second_read_in_which_the_account_moved_is_stored_and_halts() -> None:
    """The first capture counts, and files made from it now would be stale."""
    world = busy()
    killed(world, 3, False)
    first = world.stored()["account.json"]
    # Someone moves cash by hand between the two attempts.
    world.faults = replace(world.faults, events={1: EditCash(D(7))})
    world.restart()
    result = world.attempt()
    assert result.halt is not None
    assert result.halt.reason == (
        f"{world.key(ACCOUNT_OUTPUT)} and this attempt's read of the broker differ in cash, so "
        "the account may have moved between two attempts at one decision"
    )
    stored = world.stored()
    attempt = attempt_key(DEPLOYMENT, world.run, world.at, RECOVER_STAGE, ACCOUNT_OUTPUT, 2)
    assert stored["account.json"] == first
    assert list(stored) == ["account.attempt-2.json", "account.json"]
    assert [ref.key for ref in result.outputs] == [world.key(ACCOUNT_OUTPUT), attempt]
    assert world.key(ACCOUNT_OUTPUT) in result.header.inputs
    assert attempt not in result.header.inputs
    (said,) = world.alarms.sent
    assert said.endswith("These differ: cash, the pages' bytes")


def test_3_a_second_read_that_lost_a_fill_under_the_same_bound_halts() -> None:
    """Only a bound that moved past it may leave out a fill the first read held."""
    world = ready()
    kept = world.broker()
    first = {"fills": lambda since: kept.fills(since)}

    def hook(number: int, op: str, key: str) -> None:
        if op == "create" and key.startswith("journal/"):
            raise Kill

    world.store.before = hook
    with pytest.raises(Kill):
        world.attempt(world.stage(brokers=doctored(world, **first)))
    world.store.before = lambda n, op, key: None
    world.restart()
    since = world.entry(ACCOUNT_OUTPUT).since
    none = {"fills": lambda asked: FillsRead((), page(), True)}
    result = world.attempt(world.stage(brokers=doctored(world, **none)))
    assert world.entry(ACCOUNT_OUTPUT).since == since
    assert (
        result.halt is not None
        and "differ in fills, so the account may have moved" in result.halt.reason
    )


def test_3_a_second_read_that_lists_the_open_orders_in_another_order_goes_on() -> None:
    world = RecoverWorld(flat_prices())
    world.day(DAYS[1], [(-20, USO)])
    world.day(DAYS[2], [(5, GLD), (2, USO)], early=20)
    world.prepare(close_of(DAYS[3]))
    listed = world.broker().open_orders()
    assert len(listed.orders) == 2
    assert recover_moved(world, listed) == []


def test_3_a_second_read_that_lists_an_order_twice_is_a_move() -> None:
    from ithildinruntime.execution.recover import _moved

    read = AccountRead(
        positions=PositionsRead({}, page(), True),
        open_orders=OrdersRead((foreign_order(),), page(), True),
        cash=CashRead(D(1), page(), True),
        since=close_of(DAYS[0]),
        records=RecordsRead((), page(), True),
        fills=FillsRead((), page(), True),
    )
    twice = replace(read.open_orders, orders=(foreign_order(), foreign_order()))
    assert _moved(read, replace(read, open_orders=twice)) == ["open orders"]


def recover_moved(world: RecoverWorld, orders: OrdersRead) -> list[str]:
    from ithildinruntime.execution.recover import _moved

    read = AccountRead(
        positions=PositionsRead({}, page(), True),
        open_orders=orders,
        cash=CashRead(D(1), page(), True),
        since=close_of(DAYS[0]),
        records=RecordsRead((), page(), True),
        fills=FillsRead((), page(), True),
    )
    flipped = replace(read, open_orders=replace(orders, orders=tuple(reversed(orders.orders))))
    return _moved(read, flipped)


@pytest.mark.parametrize("what", ["positions", "open orders", "a new fill"])
def test_3_a_second_read_in_which_the_account_moved_halts_on_each_part(what: str) -> None:
    world = ready()
    fills = world.broker().fills(close_of(DAYS[0]))
    first = {
        "positions": {"positions": lambda: PositionsRead({USO: D(-19)}, page(), True)},
        "open orders": {"open_orders": lambda: OrdersRead((), page(), True)},
        "a new fill": {"fills": lambda since: replace(fills, fills=fills.fills[:1])},
    }[what]

    def hook(number: int, op: str, key: str) -> None:
        if op == "create" and key.startswith("journal/"):
            raise Kill

    world.store.before = hook
    with pytest.raises(Kill):
        world.attempt(world.stage(brokers=doctored(world, **first)))
    world.store.before = lambda n, op, key: None
    world.restart()
    result = world.attempt()
    word = "fills" if what == "a new fill" else what
    assert (
        result.halt is not None
        and f"differ in {word}, so the account may have moved" in result.halt.reason
    )
    assert world.kinds(world.before) == []


def test_3_a_second_read_whose_pages_alone_came_later_goes_on_from_the_first() -> None:
    whole = busy()
    result = whole.attempt()
    for at in range(2, WRITES + 1):
        world = busy()
        killed(world, at, False)
        world.clock.at += timedelta(seconds=1)
        again = world.attempt()
        assert again.halt is None, (at, again.halt)
        assert world.kinds(12) == BUSY_KINDS
        if at > 2:
            # The first capture landed, so every file comes from it as in a run with no kill.
            held = {k: v for k, v in world.stored().items() if "attempt" not in k}
            assert held == whole.stored()
            assert again.header == result.header


def test_3_a_fill_the_first_capture_lacks_halts_a_second_attempt_and_is_not_journaled() -> None:
    """The first capture counts, so the fill it lacks and the order it ended wait."""
    world = ready()
    broker = world.broker()
    fills = broker.fills(close_of(DAYS[0]))
    # As the broker stood before the second order in USO filled, which agrees with the book.
    working = [
        replace(o, filled=D(0), state=BrokerState.WORKING)
        for o in broker.orders(close_of(DAYS[0])).orders[1:]
    ]
    first = {
        "positions": lambda: PositionsRead({USO: D(-20)}, page(), True),
        "open_orders": lambda: OrdersRead(tuple(working), page(), True),
        "account": lambda: CashRead(D(1_001_000), page(), True),
        "fills": lambda since: replace(fills, fills=fills.fills[:1]),
    }

    def hook(number: int, op: str, key: str) -> None:
        if op == "create" and key.startswith("journal/"):
            raise Kill

    world.store.before = hook
    with pytest.raises(Kill):
        world.attempt(world.stage(brokers=doctored(world, **first)))
    world.store.before = lambda n, op, key: None
    world.restart()
    calls: list[str] = []
    result = world.attempt(world.stage(brokers=doctored(world, calls)))
    assert result.halt is not None
    assert (
        "differ in positions, open orders, cash, fills, so the account may have moved"
        in result.halt.reason
    )
    # Nothing of either read is journaled, and nothing is looked up.
    assert world.kinds(world.before) == []
    assert calls == list(READS)


def test_2_an_entry_another_writer_adds_ahead_of_the_second_view_is_compared() -> None:
    world = busy()
    lists = 0

    def hook(number: int, op: str, key: str) -> None:
        nonlocal lists
        if op == "list" and key == f"journal/{ACCOUNT}/":
            lists += 1
            if lists == 2:
                world.store.quiet = True
                world.plan(world.orders(world.at, [(1, GLD)]))
                journaled(world, ("intent", 1), ("verdict", 1))
                world.store.quiet = False

    world.store.before = hook
    world.store.put(world.key(VALUATION_OUTPUT), b"a file the plan names")
    result = world.attempt()
    assert lists >= 2 and result.halt is not None
    assert "is open in the book alone, Approved" in result.halt.reason


def test_2_a_decision_at_the_close_of_the_session_an_order_rests_in_alarms_nothing() -> None:
    world = RecoverWorld(flat_prices())
    world.day(DAYS[1], [(10, GLD)], early=60)
    world.prepare(close_of(DAYS[1]))
    result = world.attempt()
    assert result.halt is not None and "a second decision in the session of" in result.halt.reason
    assert world.alarms.sent == []


def test_3_a_first_capture_that_differs_halts_a_second_attempt_that_would_agree() -> None:
    world = busy()
    world.faults = replace(world.faults, events={1: EditCash(D(7))})
    world.restart()
    killed(world, 4, False)
    world.faults = replace(world.faults, events={1: EditCash(D(-7))})
    world.restart()
    result = world.attempt()
    assert result.halt is not None
    assert "cash is 1000700 in the book and 1000707 at the broker" in result.halt.reason


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"release": "r2"}, "was stored under another release"),
        ({"hashes": {gates.CHAN: "d" * 64}}, "was stored under another configuration"),
    ],
)
@pytest.mark.parametrize("at", [3, 8])
def test_3_a_file_of_another_release_or_configuration_halts(changes, said, at: int) -> None:
    world = busy()
    killed(world, at, False)
    for name, value in changes.items():
        setattr(world, name, value)
    result = world.attempt()
    assert result.halt is not None and said in result.halt.reason
    assert world.key(ACCOUNT_OUTPUT) in {ref.key for ref in result.outputs}


@pytest.mark.parametrize("name", list(FILES))
def test_3_a_stored_file_this_release_cannot_read_halts(name: str) -> None:
    world = busy()
    world.attempt()
    world.store.put(world.key(name), SECRET.encode())
    result = world.attempt()
    assert result.halt is not None
    assert f"{world.key(name)} is no {FILES[name].__name__} file" in result.halt.reason
    assert SECRET[:9] not in result.halt.reason


def test_3_a_release_with_no_name_is_refused() -> None:
    world = busy()
    world.release = ""
    with pytest.raises(ValueError, match="recover names its release"):
        world.stage()


def test_3_what_two_captures_differ_in_is_named() -> None:
    world = busy()
    world.attempt()
    read = world.entry(ACCOUNT_OUTPUT)
    later = replace(
        read,
        positions=replace(read.positions, received=page()),
        fills=replace(
            read.fills,
            fills=tuple(replace(f, received_at=f.received_at + DAY) for f in read.fills.fills),
        ),
    )
    moved = [replace(p, at=p.at + DAY) for p in read.positions.received]
    same = replace(later, positions=replace(read.positions, received=tuple(moved)))
    assert differences(read, same) == (
        "Only the times the pages came differ, as on a second attempt after a crash"
    )
    assert differences(read, later) == "These differ: the pages' bytes"
    assert differences(read, replace(read, since=read.since + MICRO)) == (
        "These differ: the time the records and fills were asked from"
    )
    short = replace(read, cash=replace(read.cash, whole=False))
    assert differences(read, short) == "These differ: whether each came whole"
    gone = replace(read, fills=replace(read.fills, fills=()))
    assert differences(read, gone) == "These differ: fills"
    placed = replace(read.open_orders, orders=(foreign_order(),))
    assert differences(read, replace(read, open_orders=placed)) == "These differ: open orders"
    moved_position = replace(read.positions, positions={GLD: D(9)})
    assert differences(read, replace(read, positions=moved_position)) == "These differ: positions"


# Acceptance 4: the bound on fills


THANKS = [date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)]
# Thursday the 26th has no session, and Friday the 27th closes early.
THANKS += [date(2026, 11, 27), date(2026, 11, 30)]


def test_4_a_fill_a_halted_recover_did_not_journal_is_applied_once() -> None:
    """Across a weekend, an early close and two more halted sessions, and never again."""
    closes = {GLD: {d: 100 for d in THANKS}, USO: {d: 50 for d in THANKS}}
    closes[GLD][date(2026, 11, 19)] = closes[USO][date(2026, 11, 19)] = 100
    world = RecoverWorld(closes)
    world.day(THANKS[0], [(10, GLD)])
    assert close_of(THANKS[4]).hour == 18
    for n in (1, 2, 3):
        world.prepare(close_of(THANKS[n]))
        # The `read` entry lands and the `fill` does not.
        refused = journal_entry_key(ACCOUNT, len(world.rows()) + 2)
        world.store.refuse(("create", refused))
        halted = world.recover()
        world.store.before = lambda n, op, key: None
        assert halted.halt is not None and "a fill was not stored" in halted.halt.reason
        assert world.kinds()[-1] == "read"
    for n in (4, 5):
        before = len(world.rows())
        world.decide(close_of(THANKS[n]))
        assert world.kinds(before) == ["read", *(["fill"] if n == 4 else []), "read", "decision"]
        assert world.entry(BOOK_OUTPUT).positions_by_strategy == {gates.CHAN: {GLD: D(10)}}
    assert world.kinds().count("fill") == 1


def test_4_an_order_kept_working_across_decisions_is_filled_once() -> None:
    """No snapshot holds a bar of its session until the last, which then gives its close."""
    closes = prices({0: 100, 2: 100, 3: 100, 4: 100, 5: 100}, {n: 50 for n in range(6)})
    world = RecoverWorld(closes)
    world.day(DAYS[1], [(10, GLD)], early=20)
    for n in (2, 3, 4):
        before = len(world.rows())
        world.decide(close_of(DAYS[n]))
        assert world.kinds(before) == ["read", "read", "decision"]
        (opened,) = world.entry(BOOK_OUTPUT).open_orders_by_strategy[gates.CHAN]
        assert opened.qty == 10
    assert len(world.alarms.sent) == 3
    at = close_of(DAYS[5])
    late = world.snapshot(at)
    bars = {**late.bars, GLD: {"1d": (gates.stated(GLD, DAYS[1], D(99)), *late.bars[GLD]["1d"])}}
    world.decide(at, snapshot=replace(late, bars=bars))
    read = world.entry(ACCOUNT_OUTPUT)
    (fill,) = read.fills.fills
    assert (fill.at, fill.price) == (close_of(DAYS[1]), 99)
    assert read.since == max(close_of(DAYS[0]), close_of(DAYS[1]) - timedelta(minutes=20) - DAY)
    assert world.entry(BOOK_OUTPUT).positions_by_strategy == {gates.CHAN: {GLD: D(10)}}
    world.closes[GLD][DAYS[6]] = world.closes[USO][DAYS[6]] = 100
    world.decide(close_of(DAYS[6]))
    assert world.kinds().count("fill") == 1
    assert len(world.alarms.sent) == 3


def test_4_an_order_open_past_its_session_pages_once_in_a_process() -> None:
    closes = prices({0: 100, 2: 100, 3: 100, 4: 100}, {n: 50 for n in range(5)})
    world = RecoverWorld(closes)
    world.day(DAYS[1], [(10, GLD)], early=20)
    said: set[str] = set()
    for n in (2, 3, 4):
        world.prepare(close_of(DAYS[n]))
        stage = world.stage()
        stage._said = said
        result = world.attempt(stage)
        assert result.halt is None
        world.store.put(result_key(DEPLOYMENT, world.run, world.at, RECOVER_STAGE), encode(result))
        world.plan(())
    assert len(world.alarms.sent) == 1
    assert "is still open after its session's close" in world.alarms.sent[0]


def test_4_a_second_attempt_whose_bound_moved_past_a_fill_it_journaled_goes_on() -> None:
    """The fill ended the oldest order, so the later read asks from later and lacks it."""
    closes = prices({0: 100, 2: 100, 3: 100, 4: 100}, {n: 50 for n in range(5)})
    world = RecoverWorld(closes)
    world.day(DAYS[1], [(10, GLD)], early=20)
    world.decide(close_of(DAYS[2]))
    world.decide(close_of(DAYS[3]))
    at = close_of(DAYS[4])
    late = world.snapshot(at)
    bars = {**late.bars, GLD: {"1d": (gates.stated(GLD, DAYS[1], D(99)), *late.bars[GLD]["1d"])}}
    world.prepare(at, replace(late, bars=bars))

    def hook(number: int, op: str, key: str) -> None:
        if op == "create" and world.kinds()[-1:] == ["fill"]:
            raise Kill

    world.store.before = hook
    with pytest.raises(Kill):
        world.attempt()
    world.store.before = lambda n, op, key: None
    world.restart()
    result = world.attempt()
    assert result.halt is None, result.halt.reason
    assert world.kinds().count("fill") == 1
    first, later = (
        decode_output(world.store.blobs[k], AccountRead).entries[0]
        for k in (
            world.key(ACCOUNT_OUTPUT),
            attempt_key(DEPLOYMENT, world.run, world.at, RECOVER_STAGE, ACCOUNT_OUTPUT, 2),
        )
    )
    assert len(first.fills.fills) == 1 and later.fills.fills == ()


@pytest.mark.parametrize("session", [1, 2])
@pytest.mark.parametrize(
    ("by", "asked"), [(-MICRO, False), (timedelta(0), True), (MICRO, True)], ids=str
)
def test_4_a_fill_around_the_opening(session: int, by: timedelta, asked: bool) -> None:
    """A suite's fills before the account was opened are in its opening cash."""
    world = RecoverWorld(flat_prices())
    # An earlier time of day than the opening's, so a day before it is before the opening too.
    if session == 2:
        world.decide(close_of(DAYS[1]) - timedelta(minutes=20))
    world.prepare(close_of(DAYS[session]) - timedelta(minutes=20))
    made = odd_fill("f-1", "b-1", at=close_of(DAYS[0]) + by)

    def fills(since: datetime) -> FillsRead:
        return FillsRead(tuple(f for f in (made,) if f.at >= since), page(), True)

    result = world.attempt(world.stage(brokers=doctored(world, fills=fills)))
    assert world.entry(ACCOUNT_OUTPUT).since == close_of(DAYS[0])
    if asked:
        assert result.halt is not None
        assert "the fill f-1 is of the broker's order b-1" in result.halt.reason
    else:
        assert result.halt is None


def test_4_the_bound_is_the_earlier_of_its_two_times_and_never_before_the_opening() -> None:
    world = RecoverWorld(flat_prices())
    opened = close_of(DAYS[0])
    assert fills_since(world.view()) == opened
    world.decide(close_of(DAYS[2]))
    assert fills_since(world.view()) == close_of(DAYS[2]) - DAY
    world.decide(close_of(DAYS[4]))
    assert fills_since(world.view()) == close_of(DAYS[4]) - DAY
    # A read with no decision after it moves nothing.
    world.prepare(close_of(DAYS[5]))
    world.recover()
    assert fills_since(world.view()) == close_of(DAYS[4]) - DAY
    # Neither does a read of the send stage's, which is later and has a decision after it too.
    late = world.at + timedelta(hours=1)
    world._record(ReadEntry(SEND_STAGE, world.at, BlobRef("r-9", "b" * 64), late))
    world.plan(())
    assert fills_since(world.view()) == close_of(DAYS[5]) - DAY


def test_4_the_bound_goes_by_the_oldest_order_that_has_not_ended() -> None:
    closes = prices({0: 100, 4: 100, 5: 100}, {0: 50, 4: 50, 5: 50})
    world = RecoverWorld(closes)
    world.day(DAYS[1], [(1, GLD)], early=20)
    first = close_of(DAYS[1]) - timedelta(minutes=20)
    world.day(DAYS[2], [(1, USO)], early=20)
    assert len(world.view().live) == 2
    world.decide(close_of(DAYS[4]))
    assert fills_since(world.view()) == max(close_of(DAYS[0]), first - DAY)
    # Two of them, and the read of the last decision is later than both.
    sent = sorted(o.sent_at for o in world.view().live.values())
    assert sent == [first, close_of(DAYS[2]) - timedelta(minutes=20)]


@pytest.mark.parametrize(("by", "dated"), [(-DAY, -DAY), (timedelta(0), timedelta(0)), (DAY, None)])
def test_4_a_read_is_dated_by_its_earliest_page_and_never_later_than_now(by, dated) -> None:
    """The next recover asks for fills from this time, so a fast clock must not move it on."""
    world = ready()
    read = world.broker().fills(close_of(DAYS[0]))
    moved = replace(read, received=tuple(replace(p, at=p.at + by) for p in read.received))
    if dated is None:
        # Every page is dated late.
        reads = {name: getattr(world.broker(), name)() for name in READS[:3]}
        later = {
            name: replace(r, received=tuple(replace(p, at=p.at + by) for p in r.received))
            for name, r in reads.items()
        }
        given = {name: lambda r=r: r for name, r in later.items()}
    else:
        given = {}
    brokers = doctored(world, fills=lambda since: moved, **given)
    assert world.attempt(world.stage(brokers=brokers)).halt is None
    entry = list(world.view().reads.values())[-1]
    assert entry.at == world.clock.at + (dated or timedelta(0))
    assert entry.ref == BlobRef(
        world.key(ACCOUNT_OUTPUT), sha256_of(world.store.blobs[world.key(ACCOUNT_OUTPUT)])
    )


def test_4_a_journal_with_no_opening_gives_no_bound() -> None:
    with pytest.raises(Halt, match="holds no opening, so no fill can be dated"):
        fills_since(Journal(Memory(), ACCOUNT).view())


# Acceptance 6: the valuation at each edge


def test_6_the_first_session_of_an_account_values_its_opening_cash() -> None:
    world = RecoverWorld(flat_prices())
    world.decide(close_of(DAYS[1]))
    assert world.entry(VALUATION_OUTPUT) == hand_value(HAND[1], 1)


@pytest.mark.parametrize("early", [False, True], ids=["a-full-day", "an-early-close"])
@pytest.mark.parametrize(("late", "first"), [(timedelta(0), True), (MICRO, False)])
def test_6_an_entry_stamped_at_the_prior_close_and_one_microsecond_after_it(
    late: timedelta, first: bool, early: bool
) -> None:
    days = [date(2026, 11, 27), date(2026, 11, 30)] if early else DAYS[1:3]
    closes = {i: {DAYS[0]: 100, **dict.fromkeys(days, 100)} for i in (GLD, USO)}
    world = RecoverWorld(closes)
    at = close_of(days[0])
    world.decide(at, world.orders(at, [(10, GLD)]))
    journaled(world, ("intent", 1), ("verdict", 1))
    world.clock.at = at + late
    journaled(world, ("sending", 1), ("in_doubt", 1), ("ack", 1))
    # Only the stamp is tested, so the order ends with nothing filled.
    (planned,) = list(world.view().decisions.values())[-1].orders
    world._record(ExpiredEntry(planned.client_order_id, D(0), b"{}", False, at))
    assert world.view().orders[planned.client_order_id].sent_at == at + late
    world.clock.at = at
    world.prepare(close_of(days[1]))
    result = world.attempt()
    if first:
        assert result.halt is None
        assert world.entry(VALUATION_OUTPUT).session == days[0]
        return
    assert result.halt is not None
    sent = (at + MICRO).isoformat()
    assert result.halt.reason == (
        f"an order was sent at {sent}, after the close of {days[0].isoformat()}, so this is a "
        f"second decision in the session of {days[1].isoformat()}, whose valuation #183 copies "
        "from the first"
    )
    assert list(world.stored()) == ["account.json", "caps.json"]


def test_6_a_second_decision_in_one_session_halts() -> None:
    world = RecoverWorld(flat_prices())
    world.day(DAYS[1], [(10, GLD)], early=60)
    world.prepare(close_of(DAYS[1]) - timedelta(minutes=20))
    result = world.attempt()
    assert result.halt is not None and "a second decision in the session of" in result.halt.reason


@pytest.mark.parametrize(
    ("day", "prior"),
    [
        (date(2026, 11, 23), date(2026, 11, 20)),  # a Monday
        (date(2026, 11, 27), date(2026, 11, 25)),  # an early close, after a holiday
        (date(2026, 11, 30), date(2026, 11, 27)),  # the session after it
    ],
)
def test_6_v_is_of_the_session_before_the_decisions(day: date, prior: date) -> None:
    closes = {GLD: {d: 100 for d in THANKS}, USO: {d: 50 for d in THANKS}}
    world = RecoverWorld(closes)
    world.day(prior, [])
    world.decide(close_of(day))
    assert world.entry(VALUATION_OUTPUT).session == prior


def test_6_a_resumed_recover_past_an_append_values_the_stored_book() -> None:
    world = busy()
    killed(world, 9, False)
    stored = world.entry(BOOK_OUTPUT)
    world._record(ReadEntry(SEND_STAGE, world.at, BlobRef("r-2", "b" * 64), world.at))
    assert world.attempt().halt is None
    value = world.entry(VALUATION_OUTPUT)
    assert value.replayed_through == stored.replayed_through == len(world.rows()) - 1


# Acceptance 7: two instances


class Other:
    """A second process over one world's store, with an `Account` and a simulator of its own."""

    def __init__(self, world: RecoverWorld) -> None:
        self.world = world
        self.account, self.log = world.account, world.log

    def attempt(self) -> DecisionResult:
        world, mine = self.world, (self.world.account, self.world.log)
        world.account, world.log = self.account, self.log
        try:
            return world.attempt()
        finally:
            world.account, world.log = mine


def calls_of(world: RecoverWorld) -> int:
    world.store.calls.clear()
    assert world.attempt().halt is None
    return len(world.store.calls)


# A process that has just started, which reads its journal and its simulator's log whole.
CALLS = 41


def test_7_the_calls_of_one_recover_are_counted() -> None:
    world = busy()
    world.restart(sim_writer="d" * 32)
    assert calls_of(world) == CALLS


@pytest.mark.parametrize("at", range(1, CALLS + 1))
def test_7_a_second_instance_between_any_two_store_calls(at: int) -> None:
    """The whole of the other's recover runs ahead of one store call of this one's."""
    whole = busy()
    result = whole.attempt()
    world = busy()
    other = Other(world)
    world.restart(sim_writer="d" * 32)
    found: list[DecisionResult] = []

    def hook(number: int, op: str, key: str) -> None:
        if number == at and not found:
            world.store.quiet = True
            found.append(other.attempt())
            world.store.quiet = False

    world.store.calls.clear()
    world.store.before = hook
    mine = world.attempt()
    world.store.before = lambda n, op, key: None
    assert found and found[0] == mine == result
    assert world.stored() == whole.stored()
    assert world.kinds(12) == BUSY_KINDS
    assert world.alarms.sent == []


def test_7_two_instances_that_both_looked_an_order_up_journal_its_end_once() -> None:
    """The other records the end between this one's lookup and its own append."""
    world = busy()
    other = Other(world)
    world.restart(sim_writer="d" * 32)
    found: list[DecisionResult] = []

    def brokers(at: datetime) -> Doctored:
        made = world.broker()

        def lookup(name: str) -> Ack:
            answer = made.lookup(name)
            found.append(other.attempt())
            return answer

        return Doctored(made, {"lookup": lookup}, None)

    mine = world.attempt(world.stage(brokers=brokers))
    assert [r.halt for r in (mine, *found)] == [None, None]
    assert world.kinds(12) == BUSY_KINDS


@pytest.mark.parametrize(("lost", "halts"), [(99, False), (100, True)])
def test_7_an_entry_that_loses_its_number_a_hundred_times_halts(lost: int, halts: bool) -> None:
    world = busy()
    taken: list[str] = []

    def hook(number: int, op: str, key: str) -> None:
        if op == "create" and key.startswith(f"journal/{ACCOUNT}/") and len(taken) < lost:
            world.store.quiet = True
            ref = BlobRef(f"r-{len(taken)}", "b" * 64)
            world._record(ReadEntry(SEND_STAGE, world.at, ref, world.at))
            taken.append(key)
            world.store.quiet = False

    world.store.before = hook
    result = world.attempt()
    assert len(taken) == lost
    if halts:
        assert result.halt is not None
        assert "other writers took 100 numbers of the journal in a row" in result.halt.reason
        assert world.kinds(world.before + lost) == []
    else:
        assert result.halt is None
        assert world.kinds(world.before + lost) == BUSY_KINDS


def test_8_the_second_oracle_agrees_wherever_a_recover_ends() -> None:
    """Acceptance 8, on the runs with a part fill, an expiry and an order kept working."""
    for world in (busy(), ready()):
        assert world.attempt().halt is None
        book = world.entry(BOOK_OUTPUT)
        told = oracle([row(world.store.blobs[k]) for k in journal_keys(world)])
        assert told == flattened(world.view())
        assert told["through"] == book.replayed_through
        assert {
            (s, i): q for s, held in book.positions_by_strategy.items() for i, q in held.items()
        } == told["positions"]
        assert book.cash == told["cash"]


def journal_keys(world: RecoverWorld) -> list[str]:
    return sorted(key for key in world.store.blobs if key.startswith(f"journal/{ACCOUNT}/"))


# The simulated account's own halts, as recover meets them


def test_2_a_lost_newest_call_of_the_simulator_is_a_difference() -> None:
    """The log reads as the account before it, so only the comparison finds it."""
    world = RecoverWorld(flat_prices())
    world.day(DAYS[1], [(10, GLD)])
    world.prepare(close_of(DAYS[2]))
    newest = max(world.blobs())
    assert newest == sim_account_key(ACCOUNT, 2)
    world.store.drop(newest)
    world.restart()
    result = world.attempt()
    assert result.halt is not None
    assert "its lookup found no such order" in result.halt.reason
    # A lost fill comes again at the next call, so a recover that has not journaled it is whole.
    world = RecoverWorld(flat_prices())
    world.day(DAYS[1], [(10, GLD)])
    world.day(DAYS[2], [])
    world.store.drop(max(world.blobs()))
    world.restart()
    world.prepare(close_of(DAYS[3]))
    assert world.attempt().halt is None


@pytest.mark.parametrize(
    ("broken", "said"),
    [
        (lambda w: w.store.drop(sim_account_key(ACCOUNT, 1)), "call 1 is missing"),
        (
            lambda w: [w.store.drop(k) for k in list(w.blobs())],
            "it was never opened, or its log is lost",
        ),
        (lambda w: w.store.put(sim_account_key(ACCOUNT, 2), b"{}"), "call 2"),
    ],
)
def test_2_a_simulated_log_that_cannot_be_read_halts_recover(broken, said: str) -> None:
    world = busy()
    broken(world)
    world.restart()
    halts(world, said, [], [], DATA_KEYS)


def test_2_a_simulator_whose_clock_reads_before_its_newest_call_halts_recover() -> None:
    world = busy()
    world.clock.at = close_of(DAYS[2]) + DAY
    world.broker().positions()
    world.restart()
    world.clock.at = close_of(DAYS[2])
    halts(world, "before the simulated account sim-1's newest call", [], [], DATA_KEYS)


def test_2_a_store_that_does_not_answer_the_simulator_halts_recover() -> None:
    world = busy()
    world.store.refuse(("*", "sim/"))
    result = world.attempt()
    world.store.before = lambda n, op, key: None
    halts(world, "the broker's positions failed with", [], [], DATA_KEYS, result)


# `Account`'s part


def test_the_account_records_only_what_a_stage_may() -> None:
    world = busy()
    view = world.account.view(lambda: None)
    name = next(iter(view.live))
    with pytest.raises(TypeError, match="a stage does not record a sending"):
        world.account.record(view, SendingEntry(name), lambda: None)  # type: ignore[arg-type]
    assert world.account.alias == ACCOUNT


def test_an_append_the_store_refuses_once_is_resent_unchanged() -> None:
    world = busy()
    refused: list[str] = []

    def hook(number: int, op: str, key: str) -> None:
        if op == "create" and key.startswith("journal/") and key not in refused:
            refused.append(key)
            raise StoreUnavailable(key, "503 SlowDown: again")

    world.store.before = hook
    world.store.calls.clear()
    assert world.attempt().halt is None
    assert len(refused) == 4 and world.kinds(12) == BUSY_KINDS
    # Each entry went again as the bytes it was first built as, and landed on that try.
    creates = [k for op, k in world.store.calls if op == "create" and k.startswith("journal/")]
    assert creates == [k for key in refused for k in (key, key)]


def test_a_listing_the_store_refuses_once_is_asked_again() -> None:
    world = busy()
    refused: list[str] = []

    def hook(number: int, op: str, key: str) -> None:
        if op == "list" and key == f"journal/{ACCOUNT}/" and len(refused) < 1:
            refused.append(key)
            raise StoreUnavailable(key, "503 SlowDown: again")

    world.store.before = hook
    assert world.attempt().halt is None
    assert refused and world.alarms.sent == []


def test_the_groups_of_states_recover_reads_cover_every_state_once() -> None:
    from ithildinruntime.execution.recover import _AT_THE_BROKER, _UNSETTLED
    from ithildinruntime.journal import ENDED

    groups = (ENDED, _UNSETTLED, _AT_THE_BROKER)
    assert sorted(s for g in groups for s in g) == sorted(OrderState)
    assert _AT_THE_BROKER == {OrderState.ACKNOWLEDGED, OrderState.PARTLY_FILLED}


def test_2_a_fill_in_dispute_is_named_ahead_of_any_lookup() -> None:
    world = ready()
    (first, second) = world.broker().fills(close_of(DAYS[0])).fills
    read = FillsRead((replace(first, price=D("50.0001")), second), page(), True)
    none = OrdersRead((), page(), True)
    calls: list[str] = []
    brokers = doctored(world, calls, fills=lambda since: read, open_orders=lambda: none)
    result = world.attempt(world.stage(brokers=brokers))
    assert result.halt is not None and "the fill sim-f-1 is in dispute" in result.halt.reason
    assert "lookup" not in calls


def test_3_a_stored_book_other_than_the_journals_halts() -> None:
    world = busy()
    killed(world, 9, False)
    held = world.file(BOOK_OUTPUT)
    moved = replace(held.entries[0], cash=held.entries[0].cash + 500)
    world.store.put(
        world.key(BOOK_OUTPUT), encode_output(Book, OutputFile(held.header, (moved,), False))
    )
    result = world.attempt()
    assert result.halt is not None
    assert result.halt.reason == f"{world.key(BOOK_OUTPUT)} holds another book than the journal's"


def test_3_a_stored_valuation_other_than_its_books_halts() -> None:
    world = busy()
    world.attempt()
    held = world.file(VALUATION_OUTPUT)
    value = held.entries[0]
    other = replace(value, value=value.value + 1, cash=value.cash + 1)
    world.store.put(
        world.key(VALUATION_OUTPUT),
        encode_output(Valuation, OutputFile(held.header, (other,), False)),
    )
    result = world.attempt()
    assert result.halt is not None
    assert result.halt.reason == (
        f"{world.key(VALUATION_OUTPUT)} holds another valuation than its book's"
    )


def test_3_a_second_read_that_did_not_come_whole_halts() -> None:
    """Its missing page could hold a fill the first read lacks."""
    from ithildinruntime.execution.recover import _moved

    world = ready()
    read = world.broker().fills(close_of(DAYS[0]))
    first = AccountRead(
        positions=PositionsRead({}, page(), True),
        open_orders=OrdersRead((), page(), True),
        cash=CashRead(D(1), page(), True),
        since=close_of(DAYS[0]),
        records=RecordsRead((), page(), True),
        fills=read,
    )
    assert _moved(first, replace(first, fills=replace(read, whole=False))) == [
        "whether each came whole"
    ]


def test_3_a_fill_gone_at_the_later_bound_itself_counts_as_a_move() -> None:
    """A read since a time holds a fill at that time, so its absence is the broker's doing."""
    from ithildinruntime.execution.recover import _moved

    world = ready()
    read = world.broker().fills(close_of(DAYS[0]))
    first = AccountRead(
        positions=PositionsRead({}, page(), True),
        open_orders=OrdersRead((), page(), True),
        cash=CashRead(D(1), page(), True),
        since=close_of(DAYS[0]),
        records=RecordsRead((), page(), True),
        fills=replace(read, fills=read.fills[:1]),
    )
    at = read.fills[0].at
    gone = replace(first, fills=replace(read, fills=()))
    assert _moved(first, replace(gone, since=at)) == ["fills"]
    assert _moved(first, replace(gone, since=at + MICRO)) == []
