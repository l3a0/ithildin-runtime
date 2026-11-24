"""The cost model, its schedule's estimate, the cost stage and its entry point.

#8's acceptance names these tests. The hand calculations are written out
beside each figure they give.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    STRATEGIES,
    Bar,
    BarStatus,
    BlobHeader,
    BlobRef,
    CostKind,
    CostSchedule,
    CostSide,
    CostTerm,
    DecisionResult,
    Halt,
    HaltRecord,
    InstrumentId,
    Mode,
    OutputFile,
    Plan,
    PlanDecision,
    Retry,
    Snapshot,
    StageKind,
    Storage,
    attempt_key,
    decode_output,
    encode,
    encode_output,
    load_strategies,
    output_key,
    parse_strategy,
    result_key,
    sha256_of,
)
from ithildinruntime.cost import CostStage, LinearCostModel
from ithildinruntime.runtime.cost_worker import cost_handlers, cost_rows
from ithildinruntime.runtime.data_worker import DATA_ROW
from ithildinruntime.runtime.loop import Item, ReceiveLoop, Stage, Table, routes
from tests.support.queues import Folder, SystemTimer, running
from tests.support.queues import storage as folder_storage
from tests.test_data_source import Timer
from tests.test_storage_contract import Alarms

ROOT = Path(__file__).parents[1]
GLD, USO = InstrumentId("GLD"), InstrumentId("USO")
AT = datetime(2026, 11, 2, 20, 40, tzinfo=UTC)
DEPLOYMENT, RUN, RELEASE = "ci", "2026-11-02", "sha256:release"

# USO carries a commission on both sides, the SEC's fee per dollar sold,
# FINRA's fee per share sold, and a placeholder gap. GLD carries a flat
# commission and no gap, written as zero.
TERMS = b"""
instruments = ["GLD", "USO"]

[[cost.USO]]
kind = "charge"
side = "both"
fixed = 1.00
per_share = 0.0035
per_dollar = 0
source = "placeholder: no broker's commission text was read"

[[cost.USO]]
kind = "charge"
side = "sell"
fixed = 0
per_share = 0
per_dollar = 0.0000206
source = "SEC fee rate advisory 2026-2, in force from 2026-04-04"

[[cost.USO]]
kind = "charge"
side = "sell"
fixed = 0.02
per_share = 0.000166
per_dollar = 0
source = "placeholder: FINRA's trading activity fee, with a flat part a test can see"

[[cost.USO]]
kind = "gap"
side = "both"
fixed = 0
per_share = 0.01
per_dollar = 0.0002
source = "placeholder: no fill has measured the gap"

[[cost.GLD]]
kind = "charge"
side = "both"
fixed = 2
per_share = 0
per_dollar = 0
source = "placeholder: a flat commission"

[[cost.GLD]]
kind = "gap"
side = "both"
fixed = 0
per_share = 0
per_dollar = 0
source = "placeholder: no gap"
"""
CHAN = parse_strategy("chan", TERMS)
OTHER = parse_strategy(
    "other",
    b"""
instruments = ["SPY"]
[[cost.SPY]]
kind = "charge"
side = "both"
fixed = 5
per_share = 0
per_dollar = 0
source = "placeholder: another strategy's commission"
[[cost.SPY]]
kind = "gap"
side = "both"
fixed = 0
per_share = 0
per_dollar = 0
source = "placeholder: another strategy's gap"
""",
)


def d(text: str) -> Decimal:
    return Decimal(text)


def snapshot(*instruments: str) -> Snapshot:
    """A snapshot of daily bars alone, with no quote, as slice 1's holds."""

    def bar(instrument: str) -> Bar:
        return Bar(
            instrument=InstrumentId(instrument),
            session=date(2026, 10, 30),
            open=d("75.00"),
            high=d("76.00"),
            low=d("74.00"),
            close=d("75.50"),
            volume=1_000_000,
            basis="as_traded",
            origin="captured",
            vintage_id="v1",
            end=datetime(2026, 10, 30, 20, 0, tzinfo=UTC),
            interval="1d",
            first_seen=AT,
            fetched=None,
            fetched_at=None,
            fetched_end=None,
            fetched_end_at=None,
            knowable_at=datetime(2026, 10, 30, 20, 0, tzinfo=UTC),
            source=None,
            source_as_of=None,
            price_kind="official close",
            venues="consolidated",
            delay="none",
            volume_counted="shares",
            excess_places=(),
        )

    bars = {InstrumentId(i): {"1d": (bar(i),)} for i in instruments}
    return Snapshot(
        decision_at=AT,
        bars=bars,
        quotes={},
        status={i: BarStatus.FRESH for i in bars},
        provisional=False,
        flags={},
        segments=(),
        ledgers={},
    )


SNAPSHOT = snapshot("GLD", "USO")


def schedule(data: bytes = TERMS, name: str = "chan") -> CostSchedule:
    return LinearCostModel(parse_strategy(name, data)).schedule(SNAPSHOT)


# The estimate


@pytest.mark.parametrize(
    ("qty", "kind", "expected"),
    [
        # A buy pays the commission alone: 1.00 + 0.0035 × 200.
        ("200", CostKind.CHARGE, "1.70"),
        # Its gap is 0.01 × 200 + 0.0002 × 200 × 75.50.
        ("200", CostKind.GAP, "5.02"),
        ("200", None, "6.72"),
        # A sale adds 0.0000206 × 200 × 75.50 = 0.31106 and 0.02 + 0.000166 × 200 = 0.0532.
        ("-200", CostKind.CHARGE, "2.06426"),
        ("-200", CostKind.GAP, "5.02"),
        ("-200", None, "7.08426"),
        # A fractional share: 1.00 + 0.0035 × 0.5.
        ("0.5", CostKind.CHARGE, "1.00175"),
    ],
)
def test_an_estimate_sums_the_terms_of_the_trades_side_and_kind(qty, kind, expected) -> None:
    assert schedule().estimate(USO, d(qty), d("75.50"), kind) == d(expected)


def test_a_per_dollar_term_is_charged_at_the_price_the_caller_gives() -> None:
    # A million dollars sold pays the SEC's $20.60: 20,000 shares at $50.
    sec = CostTerm(CostKind.CHARGE, CostSide.SELL, d("0"), d("0"), d("0.0000206"), "SEC 2026-2")
    held = CostSchedule(terms={USO: (sec,)})
    assert held.estimate(USO, d("-20000"), d("50"), CostKind.CHARGE) == d("20.60")
    assert held.estimate(USO, d("-20000"), d("25"), CostKind.CHARGE) == d("10.30")


def test_a_sale_only_term_charges_a_buy_nothing() -> None:
    term = CostTerm(CostKind.CHARGE, CostSide.SELL, d("3"), d("0.5"), d("0.01"), "a fee on sales")
    held = CostSchedule(terms={USO: (term,)})
    assert held.estimate(USO, d("10"), d("20"), CostKind.CHARGE) == 0
    # 3 + 0.5 × 10 + 0.01 × 10 × 20.
    assert held.estimate(USO, d("-10"), d("20"), CostKind.CHARGE) == d("10")


@pytest.mark.parametrize("price", ["75.50", "0", "-1", "NaN"])
@pytest.mark.parametrize("kind", [None, *CostKind])
def test_zero_shares_cost_zero_at_any_price(kind, price) -> None:
    assert schedule().estimate(USO, d("0"), d(price), kind) == 0
    assert schedule().estimate(GLD, d("-0.0"), d(price), kind) == 0
    # A leg that does not trade costs zero even where no term was written for it.
    assert CostSchedule(terms={}).estimate(USO, d("0"), d(price), kind) == 0


@pytest.mark.parametrize(
    ("terms", "kind", "absent"),
    [
        ({}, None, "charge and no gap"),
        ({GLD: ()}, None, "charge and no gap"),
        ({USO: ()}, None, "charge and no gap"),
        ({USO: ()}, CostKind.CHARGE, "charge"),
        ("charge alone", CostKind.GAP, "gap"),
        ("charge alone", None, "gap"),
        ("gap alone", CostKind.CHARGE, "charge"),
        ("gap alone", None, "charge"),
    ],
)
@pytest.mark.parametrize("qty", ["10", "-10", "0.000000001"])
def test_an_instrument_with_no_term_asked_for_halts_rather_than_costing_nothing(
    terms, kind, absent, qty
) -> None:
    if terms == "charge alone":
        terms = {USO: schedule().terms[USO][:1]}
    elif terms == "gap alone":
        terms = {USO: schedule().terms[USO][-1:]}
    with pytest.raises(Halt) as caught:
        CostSchedule(terms=terms).estimate(USO, d(qty), d("75.50"), kind)
    assert caught.value.reason == f"USO has no {absent} term in the schedule"


def test_the_smallest_price_above_zero_is_charged() -> None:
    # 1.00 + 0.0035 × 10 + 0.01 × 10 + 0.0002 × 10 × 0.0001.
    assert schedule().estimate(USO, d("10"), d("0.0001")) == d("1.1350002")


@pytest.mark.parametrize("price", ["0", "-0.0001", "NaN", "Infinity"])
def test_a_price_not_above_zero_halts(price) -> None:
    with pytest.raises(Halt, match=f"USO has a trade priced at {price}"):
        schedule().estimate(USO, d("10"), d(price))


@pytest.mark.parametrize("qty", ["NaN", "Infinity", "-Infinity"])
def test_a_quantity_that_is_not_finite_halts(qty) -> None:
    with pytest.raises(Halt, match="USO has a trade of"):
        schedule().estimate(USO, d(qty), d("75.50"))


# The model


def test_the_model_reads_the_strategys_terms_in_the_order_its_file_states() -> None:
    made = schedule()
    assert list(made.terms) == ["GLD", "USO"]
    assert made.terms[USO] == (
        CostTerm(
            CostKind.CHARGE,
            CostSide.BOTH,
            d("1.00"),
            d("0.0035"),
            d("0"),
            "placeholder: no broker's commission text was read",
        ),
        CostTerm(
            CostKind.CHARGE,
            CostSide.SELL,
            d("0"),
            d("0"),
            d("0.0000206"),
            "SEC fee rate advisory 2026-2, in force from 2026-04-04",
        ),
        CostTerm(
            CostKind.CHARGE,
            CostSide.SELL,
            d("0.02"),
            d("0.000166"),
            d("0"),
            "placeholder: FINRA's trading activity fee, with a flat part a test can see",
        ),
        CostTerm(
            CostKind.GAP,
            CostSide.BOTH,
            d("0"),
            d("0.01"),
            d("0.0002"),
            "placeholder: no fill has measured the gap",
        ),
    )


def test_the_model_reads_nothing_of_the_snapshot() -> None:
    empty = snapshot()
    assert LinearCostModel(CHAN).schedule(empty) == LinearCostModel(CHAN).schedule(SNAPSHOT)


TERM = {
    "kind": '"charge"',
    "side": '"both"',
    "fixed": "0",
    "per_share": "0",
    "per_dollar": "0",
    "source": '"placeholder: none"',
}


def uso(*terms: dict, extra: str = "") -> bytes:
    """A strategy of USO alone, with these terms and then a gap term."""
    tables = [
        "[[cost.USO]]\n" + "\n".join(f"{k} = {v}" for k, v in term.items())
        for term in (*terms, {**TERM, "kind": '"gap"'})
    ]
    return ('instruments = ["USO"]\n' + "\n".join(tables) + "\n" + extra).encode()


def test_the_form_the_refusals_below_break_is_accepted() -> None:
    assert set(schedule(uso(TERM)).terms) == {USO}


def test_a_source_is_stored_with_no_space_around_it() -> None:
    made = schedule(uso({**TERM, "source": '"  placeholder: none "'}))
    assert made.terms[USO][0].source == "placeholder: none"


def test_a_figure_at_the_places_its_field_declares_is_stored_as_written() -> None:
    finest = {**TERM, "fixed": "0.0001", "per_share": "0.00000001", "per_dollar": "0.00000001"}
    term = schedule(uso(finest)).terms[USO][0]
    assert str(term.fixed) == "0.0001"
    assert term.per_share == term.per_dollar == d("0.00000001")


BAD_TERMS = {
    **{
        f"a-negative-{f}": ({**TERM, f: step}, f"term 1 has {f} = {Decimal(step)}, which is neg")
        for f, step in (("fixed", "-0.0001"), ("per_share", "-1e-8"), ("per_dollar", "-1e-8"))
    },
    **{
        f"no-{f}": ({k: v for k, v in TERM.items() if k != f}, f"term 1 states no {f}")
        for f in TERM
    },
    "an-unknown-kind": ({**TERM, "kind": '"impact"'}, "has the kind 'impact'"),
    "a-kind-in-capitals": ({**TERM, "kind": '"CHARGE"'}, "has the kind 'CHARGE'"),
    "a-kind-that-is-a-list": ({**TERM, "kind": '["charge"]'}, "has the kind"),
    "an-unknown-side": ({**TERM, "side": '"buy"'}, "has the side 'buy'"),
    "an-empty-source": ({**TERM, "source": '""'}, "has the source ''"),
    "a-blank-source": ({**TERM, "source": '"  "'}, "has the source '  '"),
    "a-placeholder-with-no-reason": (
        {**TERM, "source": '"placeholder: "'},
        "has the source 'placeholder: '",
    ),
    **{
        f"a-placeholder-written-{n}": ({**TERM, "source": f'"{text}"'}, f"has the source '{text}'")
        for n, text in enumerate(
            (
                "placeholder",
                "placeholder:",
                "Placeholder:",
                "placeholder : x",
                "PLACEHOLDER: x",
                " placeholder",
            )
        )
    },
    "a-source-that-is-no-text": ({**TERM, "source": "7"}, "has the source 7"),
    "a-figure-that-is-text": ({**TERM, "fixed": '"0"'}, "has fixed = '0', which is not a number"),
    "a-figure-that-is-a-bool": (
        {**TERM, "per_share": "true"},
        "has per_share = True, which is not a number",
    ),
    "trailing-zeros-past-the-places": ({**TERM, "fixed": "1.00000"}, "more than the 4 places"),
    "too-many-places-for-money": ({**TERM, "fixed": "0.00001"}, "more than the 4 places"),
    "too-many-places-for-a-rate": (
        {**TERM, "per_share": "0.000000001"},
        "more than the 8 places",
    ),
    "too-many-places-for-a-dollar-rate": (
        {**TERM, "per_dollar": "0.000000001"},
        "more than the 8 places",
    ),
    "a-field-no-term-has": ({**TERM, "cap": "8.30"}, "holds cap, which no term has"),
}


@pytest.mark.parametrize("case", BAD_TERMS)
def test_a_term_the_form_refuses_halts_naming_the_strategy_and_the_instrument(case) -> None:
    term, reason = BAD_TERMS[case]
    with pytest.raises(Halt) as caught:
        schedule(uso(term), name="reversal")
    assert "strategy reversal's configuration reversal.toml" in caught.value.reason
    assert "for USO" in caught.value.reason
    assert reason in caught.value.reason


NO_SECTION = "has no [cost] section, or an empty one, so it states no cost for {}"
BAD_SECTIONS = {
    "no-section": (b'instruments = ["USO"]\n', NO_SECTION.format("USO")),
    "no-section-for-two": (b'instruments = ["USO", "GLD"]\n', NO_SECTION.format("USO, GLD")),
    "an-empty-section": (b'instruments = ["USO"]\n[cost]\n', NO_SECTION.format("USO")),
    "a-blank-instrument": (b'instruments = ["USO"]\n[cost.USO]\n', NO_SECTION.format("USO")),
    "an-empty-list": (b'instruments = ["USO"]\n[cost]\nUSO = []\n', "for USO, which holds no list"),
    "a-table-in-place-of-a-list": (
        b'instruments = ["USO"]\n[cost.USO]\nkind = "charge"\n',
        'for USO, which holds no list of terms. Each term starts with [[cost."USO"]]',
    ),
    "a-term-that-is-no-table": (
        b'instruments = ["USO"]\n[cost]\nUSO = ["charge"]\n',
        "for USO, whose term 1 is not a table",
    ),
    "an-instrument-with-no-terms": (
        uso(TERM).replace(b'["USO"]', b'["GLD", "USO"]'),
        "no [cost] terms for GLD",
    ),
    "an-instrument-the-strategy-does-not-trade": (
        uso(TERM, extra="[[cost.GLD]]\nfixed = 0\n"),
        "for GLD, which the strategy does not trade",
    ),
    "no-charge": (uso(), "for USO, which states no charge term for both sides"),
    "no-gap": (
        uso(TERM).replace(b'"gap"', b'"charge"'),
        "for USO, which states no gap term for both sides",
    ),
    "a-charge-on-sales-alone": (
        uso({**TERM, "side": '"sell"'}),
        "for USO, which states no charge term for both sides",
    ),
    "both-kinds-on-sales-alone": (
        uso({**TERM, "side": '"sell"'}).replace(b'"both"', b'"sell"'),
        "for USO, which states no charge and no gap term for both sides",
    ),
}


@pytest.mark.parametrize("case", BAD_SECTIONS)
def test_a_missing_figure_halts_naming_the_strategy_and_the_instrument(case) -> None:
    data, reason = BAD_SECTIONS[case]
    with pytest.raises(Halt) as caught:
        schedule(data, name="reversal")
    assert "strategy reversal's configuration reversal.toml" in caught.value.reason
    assert reason in caught.value.reason


def test_a_bad_term_is_named_by_its_place_in_the_list() -> None:
    with pytest.raises(Halt, match="whose term 2 has fixed = -1, which is negative"):
        schedule(uso(TERM, {**TERM, "fixed": "-1"}))


# The tracked strategy files


def tracked() -> list[str]:
    folder = STRATEGIES.relative_to(ROOT).as_posix()
    listed = subprocess.run(
        ["git", "ls-files", "--", folder], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return sorted(Path(path).stem for path in listed)


@pytest.mark.parametrize("name", tracked())
def test_every_tracked_strategys_cost_section_is_one_the_model_reads(name) -> None:
    (strategy,) = load_strategies(STRATEGIES, [name])
    made = LinearCostModel(strategy).schedule(SNAPSHOT)
    assert set(made.terms) == set(strategy.instruments)


def test_the_tracked_files_hold_chans_band() -> None:
    assert "chan_bollinger" in tracked()


def test_chans_replication_charges_nothing_in_any_term() -> None:
    (chan,) = load_strategies(STRATEGIES, ["chan_bollinger"])
    made = LinearCostModel(chan).schedule(SNAPSHOT)
    for instrument in (GLD, USO):
        for term in made.terms[instrument]:
            assert (term.fixed, term.per_share, term.per_dollar) == (0, 0, 0)
        for kind in (None, *CostKind):
            for qty in ("100", "-100"):
                assert made.estimate(instrument, d(qty), d("75.50"), kind) == 0


# The stage

TABLE = Table(stages=(DATA_ROW, *cost_rows([CHAN, OTHER])), strategies=(CHAN, OTHER))
HASHES = {"chan": CHAN.config_hash, "other": OTHER.config_hash}


def store(root: Path) -> Storage:
    return Storage(
        FolderStore(root),
        timer=Timer(),
        pause=lambda s: None,
        retry=Retry(deadline=timedelta(seconds=1), pause=0.1, longest_pause=0.1),
        alerts=Alarms(),
        owner_step="none",
    )


def data_header(at: datetime = AT) -> BlobHeader:
    return BlobHeader(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        stage="data",
        kind="capturing",
        decision_at=at,
        inputs={},
        release=RELEASE,
        config_hashes=HASHES,
    )


DATA_RESULT = result_key(DEPLOYMENT, RUN, AT, "data")
SNAPSHOT_KEY = output_key(DEPLOYMENT, RUN, AT, "data", "snapshot")


def write_data(
    storage: Storage,
    *,
    halt: str | None = None,
    entries: tuple[Snapshot, ...] = (SNAPSHOT,),
    trade_nothing: bool = False,
    later: bool = False,
) -> BlobRef:
    """Store data's snapshot and its result, as data's stage leaves them."""
    file = encode_output(Snapshot, OutputFile(data_header(), entries, trade_nothing))
    refs = [storage.put(StageKind.CAPTURING, SNAPSHOT_KEY, file).ref]
    if later:
        key = attempt_key(DEPLOYMENT, RUN, AT, "data", "snapshot", 2)
        other = encode_output(Snapshot, OutputFile(data_header(), (snapshot("GLD"),), False))
        # Data lists the capture that counts first. Listing it last here checks
        # that the stage goes by its key and not by its place.
        refs.insert(0, storage.put(StageKind.CAPTURING, key, other).ref)
    record = HaltRecord(halt, refs[0], (), (), ()) if halt is not None else None
    result = DecisionResult(data_header(), tuple(refs), record)
    storage.put_result(DATA_RESULT, result)
    return BlobRef(DATA_RESULT, sha256_of(encode(result)))


def handler(storage: Storage, strategy: str = "chan", table: Table = TABLE) -> CostStage:
    made = cost_handlers(
        storage=storage, deployment=DEPLOYMENT, run=RUN, release=RELEASE, table=table
    )
    return made[f"cost/{strategy}"]


def run(stage: CostStage, strategy: str = "chan") -> DecisionResult:
    return stage(Item(AT, f"cost/{strategy}"), lambda: None)


def blobs(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in (root / "runs").rglob("*") if p.is_file()}


def test_the_stage_stores_the_schedule_a_hand_calculation_gives(tmp_path) -> None:
    storage = store(tmp_path)
    data = write_data(storage)
    result = run(handler(storage))
    assert result.halt is None
    (ref,) = result.outputs
    assert ref.key == output_key(DEPLOYMENT, RUN, AT, "cost/chan", "schedule")
    stored = decode_output((tmp_path / ref.key).read_bytes(), CostSchedule)
    assert not stored.trade_nothing
    (made,) = stored.entries
    assert made == schedule()
    # The figures test_an_estimate_sums_the_terms_of_the_trades_side_and_kind works out.
    assert made.estimate(USO, d("-200"), d("75.50")) == d("7.08426")
    assert made.estimate(USO, d("200"), d("75.50")) == d("6.72")
    # The flat commission alone: 2.
    assert made.estimate(GLD, d("-30"), d("380.00")) == d("2")

    snapshot_sha = sha256_of((tmp_path / SNAPSHOT_KEY).read_bytes())
    assert result.header == stored.header
    assert stored.header == BlobHeader(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        stage="cost/chan",
        kind="deterministic",
        decision_at=AT,
        inputs={data.key: data.sha256, SNAPSHOT_KEY: snapshot_sha},
        release=RELEASE,
        config_hashes={"chan": CHAN.config_hash},
    )
    assert sha256_of((tmp_path / data.key).read_bytes()) == data.sha256


def test_each_strategys_stage_stores_its_own_terms_under_its_own_hash(tmp_path) -> None:
    storage = store(tmp_path)
    write_data(storage)
    for strategy in (CHAN, OTHER):
        (ref,) = run(handler(storage, strategy.name), strategy.name).outputs
        stored = decode_output((tmp_path / ref.key).read_bytes(), CostSchedule)
        assert dict(stored.header.config_hashes) == {strategy.name: strategy.config_hash}
        assert set(stored.entries[0].terms) == set(strategy.instruments)


def test_a_second_run_stores_the_same_bytes_and_the_same_result(tmp_path) -> None:
    storage = store(tmp_path)
    write_data(storage)
    first = run(handler(storage))
    held = blobs(tmp_path)
    # Storage halts a deterministic stage whose second run writes other bytes.
    assert run(handler(storage)) == first
    assert blobs(tmp_path) == held


def test_a_second_run_that_writes_other_bytes_halts_as_not_deterministic(tmp_path) -> None:
    storage = store(tmp_path)
    write_data(storage)
    key = output_key(DEPLOYMENT, RUN, AT, "cost/chan", "schedule")
    storage.put(StageKind.DETERMINISTIC, key, b"{}")
    halted = run(handler(storage))
    assert halted.halt is not None and halted.halt.cause is None
    assert "so the step is not deterministic" in halted.halt.reason
    assert set(halted.header.inputs) == {DATA_RESULT, SNAPSHOT_KEY}


def test_the_stage_reports_after_each_read_and_after_its_write(tmp_path) -> None:
    storage = store(tmp_path)
    write_data(storage)
    stored: list[bool] = []
    schedule_key = output_key(DEPLOYMENT, RUN, AT, "cost/chan", "schedule")
    handler(storage)(
        Item(AT, "cost/chan"), lambda: stored.append((tmp_path / schedule_key).exists())
    )
    # Data's result, the snapshot, then the schedule.
    assert stored == [False, False, True]


def test_a_halt_in_datas_result_is_passed_on_naming_it(tmp_path) -> None:
    storage = store(tmp_path)
    data = write_data(storage, halt="GLD missing for the session")
    held = blobs(tmp_path)
    result = run(handler(storage))
    assert result.outputs == ()
    assert result.halt is not None and result.halt.cause == data
    assert result.halt.reason == f"{DATA_RESULT} holds a halt: GLD missing for the session"
    assert dict(result.header.inputs) == {data.key: data.sha256}
    assert dict(result.header.config_hashes) == {"chan": CHAN.config_hash}
    assert blobs(tmp_path) == held


def test_a_halt_in_datas_result_is_passed_on_before_the_section_is_read(tmp_path) -> None:
    bare = parse_strategy("chan", b'instruments = ["GLD", "USO"]\n')
    table = Table(stages=(DATA_ROW, *cost_rows([bare])), strategies=(bare,))
    storage = store(tmp_path)
    data = write_data(storage, halt="data not ready")
    result = run(handler(storage, table=table))
    assert result.halt is not None and result.halt.cause == data


def test_a_section_the_model_refuses_halts_in_the_stage_and_stores_no_schedule(tmp_path) -> None:
    bare = parse_strategy("chan", uso(TERM).replace(b'["USO"]', b'["GLD", "USO"]'))
    table = Table(stages=(DATA_ROW, *cost_rows([bare])), strategies=(bare,))
    storage = store(tmp_path)
    data = write_data(storage)
    held = blobs(tmp_path)
    # Building the handler refuses nothing, so the halt is the stage's own result.
    stage = handler(storage, table=table)
    result = run(stage)
    assert result.outputs == ()
    assert result.halt is not None
    assert result.halt.reason == (
        "strategy chan's configuration chan.toml has no [cost] terms for GLD"
    )
    # Nothing upstream halted, so the halt names no cause, and monitoring alarms on it.
    assert result.halt.cause is None
    # It names what the stage read, as every result does.
    snapshot_sha = sha256_of((tmp_path / SNAPSHOT_KEY).read_bytes())
    assert dict(result.header.inputs) == {data.key: data.sha256, SNAPSHOT_KEY: snapshot_sha}
    assert dict(result.header.config_hashes) == {"chan": bare.config_hash}
    assert blobs(tmp_path) == held


def test_a_decision_with_no_data_result_halts(tmp_path) -> None:
    halted = run(handler(store(tmp_path)))
    assert halted.halt is not None and halted.halt.cause is None
    assert halted.halt.reason == f"{DATA_RESULT} is not stored, and stage cost/chan reads it"
    assert dict(halted.header.inputs) == {}


def test_the_stage_reads_the_capture_that_counts_and_not_a_later_one(tmp_path) -> None:
    storage = store(tmp_path)
    write_data(storage, later=True)
    result = run(handler(storage))
    assert result.halt is None
    assert set(result.header.inputs) == {DATA_RESULT, SNAPSHOT_KEY}


@pytest.mark.parametrize("times", [0, 2])
def test_a_data_result_that_lists_the_snapshot_other_than_once_halts(tmp_path, times) -> None:
    storage = store(tmp_path)
    file = encode_output(Snapshot, OutputFile(data_header(), (SNAPSHOT,), False))
    ref = storage.put(StageKind.CAPTURING, SNAPSHOT_KEY, file).ref
    data = DecisionResult(data_header(), (ref,) * times, None)
    storage.put_result(DATA_RESULT, data)
    halted = run(handler(storage))
    assert halted.halt is not None and halted.halt.cause is None
    assert halted.halt.reason == f"{DATA_RESULT} lists {SNAPSHOT_KEY} {times} times, not once"
    assert dict(halted.header.inputs) == {DATA_RESULT: sha256_of(encode(data))}


@pytest.mark.parametrize(
    "apart",
    [timedelta(days=-1), timedelta(days=1), timedelta(minutes=-20), timedelta(microseconds=1)],
    ids=str,
)
def test_a_snapshot_of_another_decision_halts(tmp_path, apart) -> None:
    storage = store(tmp_path)
    other = AT + apart
    # The file's own header names this decision, so storage's check passes.
    write_data(storage, entries=(replace(SNAPSHOT, decision_at=other),))
    halted = run(handler(storage))
    assert halted.halt is not None and halted.halt.cause is None
    assert halted.halt.reason == (
        f"{SNAPSHOT_KEY} holds a snapshot of {other.isoformat()}, not of {AT.isoformat()}"
    )
    assert set(halted.header.inputs) == {DATA_RESULT, SNAPSHOT_KEY}


@pytest.mark.parametrize(
    ("entries", "trade_nothing"), [((SNAPSHOT, SNAPSHOT), False), ((), True)], ids=["two", "none"]
)
def test_a_snapshot_file_that_holds_other_than_one_snapshot_halts(
    tmp_path, entries, trade_nothing
) -> None:
    storage = store(tmp_path)
    write_data(storage, entries=entries, trade_nothing=trade_nothing)
    halted = run(handler(storage))
    assert halted.halt is not None and halted.halt.cause is None
    assert halted.halt.reason == f"{SNAPSHOT_KEY} holds {len(entries)} entries, not one"
    assert set(halted.header.inputs) == {DATA_RESULT, SNAPSHOT_KEY}


def test_a_snapshot_storage_refuses_halts_naming_datas_result_alone(tmp_path) -> None:
    storage = store(tmp_path)
    data = write_data(storage)
    (tmp_path / SNAPSHOT_KEY).write_bytes(b"{}")
    halted = run(handler(storage))
    assert halted.halt is not None and halted.halt.cause is None
    assert f"output file {SNAPSHOT_KEY} has SHA-256" in halted.halt.reason
    # The stage read no snapshot, so it names none.
    assert dict(halted.header.inputs) == {data.key: data.sha256}


# The entry point


def test_the_entry_point_gives_each_strategy_a_deterministic_row_that_reads_data() -> None:
    assert cost_rows([CHAN, OTHER]) == (
        Stage("cost/chan", StageKind.DETERMINISTIC, inputs=("data",), strategy="chan"),
        Stage("cost/other", StageKind.DETERMINISTIC, inputs=("data",), strategy="other"),
    )
    assert [s.name for s in TABLE.stages_of("cost")] == ["cost/chan", "cost/other"]
    (heard,) = [r for r in routes(TABLE, DEPLOYMENT) if r.reader == "cost"]
    assert heard.key == f"runs/{DEPLOYMENT}/*/data/result.json"


def test_the_handler_takes_its_kind_and_its_input_from_the_table(tmp_path) -> None:
    feed = Stage("feed", StageKind.CAPTURING)
    row = Stage("cost/chan", StageKind.CAPTURING, inputs=("feed",), strategy="chan")
    stage = handler(store(tmp_path), table=Table(stages=(feed, row), strategies=(CHAN,)))
    assert "/feed/result.json is not stored" in run(stage).halt.reason
    header = BlobHeader(
        **{f: getattr(data_header(), f) for f in BlobHeader.__dataclass_fields__}
        | {"stage": "feed", "config_hashes": {"chan": CHAN.config_hash}}
    )
    storage = store(tmp_path)
    halted = DecisionResult(header, (), HaltRecord("no feed", None, (), (), ()))
    storage.put_result(result_key(DEPLOYMENT, RUN, AT, "feed"), halted)
    assert run(stage).header.kind == "capturing"

    # A capturing stage with no attempt key halts in its own words on other bytes.
    storage = store(tmp_path / "second")
    key = output_key(DEPLOYMENT, RUN, AT, "feed", "snapshot")
    file = encode_output(Snapshot, OutputFile(header, (SNAPSHOT,), False))
    ref = storage.put(StageKind.CAPTURING, key, file).ref
    listed = DecisionResult(header, (ref,), None)
    storage.put_result(result_key(DEPLOYMENT, RUN, AT, "feed"), listed)
    storage.put(
        StageKind.CAPTURING, output_key(DEPLOYMENT, RUN, AT, "cost/chan", "schedule"), b"{}"
    )
    halted = run(handler(storage, table=Table(stages=(feed, row), strategies=(CHAN,))))
    assert "has no other key to go to" in halted.halt.reason
    # A capture storage has not stored is named by the halt.
    assert [ref.key for ref in halted.halt.unstored] == [
        output_key(DEPLOYMENT, RUN, AT, "cost/chan", "schedule")
    ]


def test_the_cost_worker_loads_neither_marketlake_nor_pandas() -> None:
    code = (
        "import sys, ithildinruntime.runtime.cost_worker; "
        "print(sorted({'lake', 'pandas', 'pyarrow', 'numpy'} & set(sys.modules)))"
    )
    ran = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert ran.stdout.strip() == "[]"


def test_the_loop_stores_each_strategys_schedule_and_one_bad_files_halt(tmp_path) -> None:
    bad = parse_strategy("other", b'instruments = ["SPY"]\n')
    table = Table(stages=(DATA_ROW, *cost_rows([CHAN, bad])), strategies=(CHAN, bad))
    global_hashes = {"chan": CHAN.config_hash, "other": bad.config_hash}
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    alarms = Alarms()
    storage = folder_storage(folder.store(), alarms)
    header = BlobHeader(
        **{f: getattr(data_header(), f) for f in BlobHeader.__dataclass_fields__}
        | {"config_hashes": global_hashes}
    )
    ref = storage.put(
        StageKind.CAPTURING,
        SNAPSHOT_KEY,
        encode_output(Snapshot, OutputFile(header, (SNAPSHOT,), False)),
    ).ref
    storage.put_result(DATA_RESULT, DecisionResult(header, (ref,), None))
    plan = Plan(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        mode=Mode.PAPER,
        release=RELEASE,
        decisions=(PlanDecision(AT, ("chan", "other")),),
    )
    made = ReceiveLoop(
        worker="cost-1",
        component="cost",
        table=table,
        plan=plan,
        queue=folder.queue("cost"),
        storage=storage,
        bucket=folder.bucket,
        handlers=cost_handlers(
            storage=storage, deployment=DEPLOYMENT, run=RUN, release=RELEASE, table=table
        ),
        alerts=alarms,
        timer=SystemTimer(),
        beat=lambda: None,
        tick=0.05,
        stop_timeout=5.0,
    )
    keys = {s: result_key(DEPLOYMENT, RUN, AT, f"cost/{s}") for s in ("chan", "other")}
    with running({"cost": made}) as started:
        assert started.wait_for(lambda: all((folder.root / k).exists() for k in keys.values()), 20)
    chan = storage.read_result(keys["chan"], AT)
    other = storage.read_result(keys["other"], AT)
    assert chan.halt is None and len(chan.outputs) == 1
    assert other.halt is not None and other.outputs == ()
    assert "strategy other's configuration other.toml has no [cost] section" in other.halt.reason
    assert other.halt.reason.endswith("so it states no cost for SPY")
    assert other.halt.cause is None and set(other.header.inputs) == {DATA_RESULT, SNAPSHOT_KEY}
    assert dict(other.header.config_hashes) == {"other": bad.config_hash}
    read = storage.read_output(keys["chan"], chan, chan.outputs[0], AT, CostSchedule)
    assert read.entries == (schedule(),)
