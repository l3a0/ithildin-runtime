"""The risk model, its `[risk]` section, the risk stage and its entry point.

#115's acceptance names these tests. Every figure is made up, and each hand
calculation is written out beside the figure it gives. The blobs the stage
reads come from `write_data` and `write_recover`, which stand in for data's
stage and for execution's recover stage, which #10 builds.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    BOOK_OUTPUT,
    CAPS_OUTPUT,
    CONSTRAINTS_OUTPUT,
    RECOVER_STAGE,
    RISK_STAGE,
    STRATEGIES,
    VALUATION_OUTPUT,
    BlobHeader,
    BlobRef,
    Book,
    Constraints,
    DecisionResult,
    Halt,
    HaltRecord,
    HardCaps,
    Mode,
    OutputFile,
    Plan,
    PlanDecision,
    Retry,
    Snapshot,
    StageDecision,
    StageKind,
    StageRun,
    Storage,
    StrategyConfig,
    StressRecord,
    StressRecords,
    Valuation,
    attempt_key,
    decode_output,
    encode,
    encode_output,
    load_strategies,
    output_key,
    parse_strategy,
    result_key,
    sha256_of,
    valuation_key,
)
from ithildinruntime.risk import SECTION as NAME
from ithildinruntime.risk import LimitRisk, RiskStage
from ithildinruntime.runtime.data_worker import DATA_ROW
from ithildinruntime.runtime.loop import Item, ReceiveLoop, Stage, Table, routes
from ithildinruntime.runtime.risk_worker import RISK_ROW, risk_handler
from tests.support.gates import (
    CLOSE,
    DECISION,
    GLD,
    OTHER,
    PRIOR,
    SPY,
    TABLE,
    USO,
    book,
    caps,
    d,
    order,
    snapshot,
    stress_record,
    valuation,
)
from tests.support.queues import Folder, SystemTimer, running
from tests.support.queues import storage as folder_storage
from tests.test_data_source import Timer
from tests.test_storage_contract import Alarms

ROOT = Path(__file__).parents[1]
DEPLOYMENT, RUN, RELEASE = "ci", "2026-11-03", "sha256:release"

SECTION = """
[risk]
max_gross = 1000.00
max_net = 900.00
max_open_orders = 4
loss_stop = 50.00
moc_cutoff = 0

[risk.max_order_qty]
GLD = 10
USO = 20

[risk.max_position]
GLD = 5
USO = 10
"""
TEXT = f'instruments = ["GLD", "USO"]\n{SECTION}'
CHAN = parse_strategy("chan", TEXT.encode())

# V is 5,000, the share 0.02 and the stress move 0.2, so the net cap is
# 0.02 × 5,000 ÷ 0.2 = 500. Each other cap is the tighter of its pair on one
# field and the looser on the next.
CAPS = caps(
    max_order_qty={GLD: d(8), USO: d(30)},
    max_position={GLD: d(6), USO: d(8)},
    max_gross=d(1200),
    max_open_orders=3,
    loss_stop=d(60),
    moc_cutoff=12,
    share=d("0.02"),
    stress_move=d("0.2"),
)
V = valuation(5000)
EXPECTED = Constraints(
    strategy="chan",
    max_order_qty={GLD: d(8), USO: d(20)},
    max_position={GLD: d(5), USO: d(8)},
    max_gross=d("1000.00"),
    max_open_orders=3,
    max_net=d("500.0000"),
    loss_stop=d("50.00"),
    moc_cutoff=12,
)
# A decision that holds both legs, with a bar later than V's session for each.
HELD = book({"chan": {GLD: 2, USO: -3}})
MARKED = snapshot({GLD: 380, USO: 75})
# The same decision before #100, whose newest bar is the session V was valued at.
SEALED = snapshot({GLD: 380, USO: 75}, at=DECISION, day=PRIOR)


def strategy(text: str = TEXT, name: str = "chan") -> StrategyConfig:
    return parse_strategy(name, text.encode())


def fitted(config: StrategyConfig, owners: HardCaps = CAPS) -> tuple[StressRecord, ...]:
    return (
        stress_record(
            strategy=config.name,
            instruments=tuple(sorted(config.instruments)),
            vintage=owners.stress_vintage,
            through=owners.stress_through,
        ),
    )


def limits(
    config: StrategyConfig = CHAN,
    held: Book = HELD,
    marked: Snapshot = MARKED,
    owners: HardCaps = CAPS,
    v: Valuation = V,
    records: StressRecords | None = None,
) -> Constraints:
    made = fitted(config, owners) if records is None else records
    return LimitRisk(config, made).constraints(held, marked, owners, v)


# The model


def test_1_the_limits_are_the_tighter_of_the_strategys_and_the_caps() -> None:
    assert limits() == EXPECTED


@pytest.mark.parametrize(
    ("value", "share", "move", "net"),
    [
        # Each moves one of V, the share and the stress move from 0.02 × 5,000 ÷ 0.2 = 500.
        ("5000", "0.02", "0.2", "500"),
        ("2500", "0.02", "0.2", "250"),
        ("5000", "0.01", "0.2", "250"),
        ("5000", "0.02", "0.4", "250"),
        # 0.02 × 5,000 ÷ 0.3 = 333.33333…, rounded down.
        ("5000", "0.02", "0.3", "333.3333"),
        # 0.02 × 9,000 ÷ 0.2 is the strategy's own 900, and past it the strategy's is tighter.
        ("9000", "0.02", "0.2", "900"),
        ("9000.01", "0.02", "0.2", "900"),
        ("8999.99", "0.02", "0.2", "899.999"),
    ],
)
def test_1_the_net_cap_follows_v_the_share_and_the_stress_move(value, share, move, net) -> None:
    owners = replace(CAPS, share=d(share), stress_move=d(move))
    assert limits(owners=owners, v=valuation(value)) == replace(EXPECTED, max_net=d(net))


def test_1_a_book_with_nothing_held_needs_no_mark() -> None:
    flat = book({"chan": {GLD: 0}})
    assert limits(held=flat, marked=SEALED) == EXPECTED


@pytest.mark.parametrize(
    "held",
    [
        book({"chan": {USO: "0.000000001"}}),
        book({"chan": {USO: -1}}),
        # A position of a strategy the deployment does not run is the account's all the same.
        book({OTHER: {USO: 1}}),
        # Two strategies' positions that offset are each a position.
        book({"chan": {USO: 1}, OTHER: {USO: -1}}),
        book(opens=[order(1, USO, "chan", name="old-1")]),
        book(opens=[order(-1, USO, OTHER, name="old-1")]),
        # A pair whose other leg the snapshot marks.
        HELD,
    ],
    ids=[
        "long",
        "short",
        "another-strategys",
        "offset",
        "an-open-buy",
        "another-strategys-sale",
        "the-second-leg-of-a-pair",
    ],
)
def test_a_held_instrument_the_snapshot_cannot_mark_halts(held) -> None:
    late_leg = replace(MARKED, bars={**MARKED.bars, USO: SEALED.bars[USO]})
    with pytest.raises(Halt) as caught:
        limits(held=held, marked=late_leg)
    assert caught.value.reason == (
        "USO has no mark: its newest bar is for 2026-11-02, not later than 2026-11-02"
    )
    # A bar later than V's session is a mark.
    assert limits(held=held) == EXPECTED


def test_a_held_instrument_with_no_bar_halts() -> None:
    only_gld = snapshot({GLD: 380})
    with pytest.raises(Halt, match="USO missing for the session"):
        limits(held=book({"chan": {USO: 1}}), marked=only_gld)
    with pytest.raises(Halt, match="SPY missing for the session"):
        limits(held=book({OTHER: {SPY: 1}}))


@pytest.mark.parametrize("day", [PRIOR - timedelta(days=3), date(2026, 11, 3)], ids=str)
def test_a_v_of_another_session_than_the_one_before_the_decisions_halts(day) -> None:
    flat = book()
    with pytest.raises(Halt) as caught:
        limits(held=flat, v=valuation(5000, session=day))
    assert caught.value.reason == (
        f"the valuation is of {day.isoformat()}, not of 2026-11-02, the session before the "
        "decision's"
    )


@pytest.mark.parametrize(
    ("at", "prior", "days"),
    [
        # After the session's close on 2026-11-03, whose UTC date is the next day.
        (datetime(2026, 11, 4, 2, 0, tzinfo=UTC), PRIOR, 4),
        # A Saturday, whose last session is Friday's.
        (datetime(2026, 11, 7, 15, 0, tzinfo=UTC), date(2026, 11, 5), 7),
    ],
    ids=["after-the-close", "a-saturday"],
)
def test_vs_session_is_counted_from_the_session_a_decision_belongs_to(at, prior, days) -> None:
    flat = book()
    asked = replace(MARKED, decision_at=at)
    assert limits(held=flat, marked=asked, v=valuation(5000, session=prior)) == EXPECTED
    with pytest.raises(Halt, match=f"not of {prior.isoformat()}, the session before"):
        limits(held=flat, marked=asked, v=valuation(5000, session=TABLE.before(prior).day))
    # The vintage's last session is 2026-10-30, so the age counts to the session too.
    aged = {"held": flat, "marked": asked, "v": valuation(5000, session=prior)}
    assert limits(owners=replace(CAPS, stress_max_age_days=days), **aged) == EXPECTED
    with pytest.raises(Halt, match=f"{days} days before the decision"):
        limits(owners=replace(CAPS, stress_max_age_days=days - 1), **aged)


def test_a_stress_move_past_its_age_halts() -> None:
    # Its vintage ends on 2026-10-30, four days before the decision's session.
    assert limits(owners=replace(CAPS, stress_max_age_days=4)) == EXPECTED
    with pytest.raises(Halt, match="stress_through is 2026-10-30, 4 days before the decision"):
        limits(owners=replace(CAPS, stress_max_age_days=3))


def test_the_caps_limit_every_instrument_the_strategy_trades() -> None:
    one = {"max_order_qty": {GLD: d(8)}, "max_position": {GLD: d(6)}}
    with pytest.raises(Halt, match="the constraints limit USO, which the hard caps do not"):
        limits(owners=replace(CAPS, **one))
    # A cap on an instrument the strategy does not trade limits nothing here.
    more = {
        "max_order_qty": {**CAPS.max_order_qty, SPY: d(1)},
        "max_position": {**CAPS.max_position, SPY: d(1)},
    }
    assert limits(owners=replace(CAPS, **more)) == EXPECTED


def test_the_model_takes_no_looser_limit_from_its_section() -> None:
    huge = TEXT.replace("1000.00", "1e9").replace("900.00", "1e9").replace("50.00", "1e9")
    made = limits(strategy(huge.replace("= 4", "= 10000000")))
    assert (made.max_gross, made.max_net, made.loss_stop) == (1200, 500, 60)
    assert made.max_open_orders == 3


def test_the_file_order_of_a_mapping_changes_nothing() -> None:
    backward = replace(
        CAPS,
        max_order_qty=dict(reversed(CAPS.max_order_qty.items())),
        max_position=dict(reversed(CAPS.max_position.items())),
    )
    listed = strategy(TEXT.replace('["GLD", "USO"]', '["USO", "GLD"]'))
    turned = book({"chan": {USO: -3, GLD: 2}})
    assert encode(limits(listed, turned, MARKED, backward)) == encode(limits())


# The section


def test_2_a_section_is_read_exactly() -> None:
    made = limits(
        strategy(
            TEXT.replace("1000.00", "0.00000001")
            .replace("900.00", "7")
            .replace("= 4", "= 0")
            .replace("moc_cutoff = 0", "moc_cutoff = 13")
            .replace("USO = 10", "USO = 0.5")
        )
    )
    assert made.max_gross == d("0.00000001") and made.max_net == 7
    assert (made.max_open_orders, made.moc_cutoff) == (0, 13)
    assert made.max_position[USO] == d("0.5")
    # More digits than Python's default context holds, and every zero as written.
    long = "1.234567890123456789012345678901234567"
    assert str(limits(strategy(section(max_net=long))).max_net) == long
    assert str(limits().max_gross) == "1000.00"


FIELDS = (
    "max_order_qty",
    "max_position",
    "max_gross",
    "max_open_orders",
    "max_net",
    "loss_stop",
    "moc_cutoff",
)
SCALARS = ("max_gross", "max_net", "max_open_orders", "loss_stop", "moc_cutoff")


def section(**changes: str | None) -> str:
    """The sample as text with these fields written another way, or dropped for None."""
    written: dict[str, str | None] = {
        "max_gross": "1000.00",
        "max_net": "900.00",
        "max_open_orders": "4",
        "loss_stop": "50.00",
        "moc_cutoff": "0",
        "max_order_qty": "{GLD = 10, USO = 20}",
        "max_position": "{GLD = 5, USO = 10}",
    } | changes
    body = "\n".join(f"{k} = {v}" for k, v in written.items() if v is not None)
    return f'instruments = ["GLD", "USO"]\n[risk]\n{body}\n'


def test_the_form_the_refusals_below_break_is_accepted() -> None:
    assert limits(strategy(section())) == EXPECTED


def qty(gld: str, uso: str = "10") -> str:
    return f"{{GLD = {gld}, USO = {uso}}}"


BAD_SECTIONS: dict[str, tuple[str, str]] = {
    "no-section": ('instruments = ["GLD", "USO"]\n', "has no [risk] section, or an empty one"),
    "an-empty-section": (
        'instruments = ["GLD", "USO"]\n[risk]\n',
        "has no [risk] section, or an empty one",
    ),
    "blank-tables-alone": (
        'instruments = ["GLD", "USO"]\n[risk.max_position]\n[risk.max_order_qty]\n',
        "has no [risk] section, or an empty one",
    ),
    **{f"no-{f}": (section(**{f: None}), f"states no {f}") for f in FIELDS},
    "two-fields-absent": (section(max_net=None, loss_stop=None), "states no max_net, loss_stop"),
    **{
        f"an-empty-{f}": (section(**{f: "{}"}), f"states no {f}")
        for f in ("max_order_qty", "max_position")
    },
    "an-unknown-field": (section(share="0.02"), "holds share, which this form does not have"),
    "six-unknown-fields": (
        # Six, so a set's own order is seldom the sorted one.
        section(**{name: "1" for name in "fbdaec"}),
        "holds a, b, c, d, e, f, which this form does not have",
    ),
    **{
        f"a-{f}-of-six-other-instruments": (
            section(**{f: "{GLD = 10, USO = 20, F = 1, B = 1, D = 1, A = 1, E = 1, C = 1}"}),
            f"holds a {f} for A, B, C, D, E, F, which the strategy does not trade",
        )
        for f in ("max_order_qty", "max_position")
    },
    "the-strategys-name": (
        section(strategy='"chan"'),
        "holds strategy, which this form does not have",
    ),
    **{
        f"a-{f}-of-no-table": (
            section(**{f: "10"}),
            f"has {f} = 10, which is not a table of instruments",
        )
        for f in ("max_order_qty", "max_position")
    },
    **{
        f"a-{f}-missing-a-leg": (section(**{f: "{GLD = 10}"}), f"states no {f} for USO")
        for f in ("max_order_qty", "max_position")
    },
    **{
        f"a-{f}-of-another-instrument": (
            section(**{f: "{GLD = 10, USO = 20, SPY = 1}"}),
            f"holds a {f} for SPY, which the strategy does not trade",
        )
        for f in ("max_order_qty", "max_position")
    },
    **{
        f"a-bool-{f}": (section(**{f: "true"}), f"the constraints' {f} is True, not a")
        for f in SCALARS
    },
    **{
        f"text-for-{f}": (section(**{f: '"1"'}), f"the constraints' {f} is '1', not a")
        for f in SCALARS
    },
    **{
        f"a-negative-{f}": (
            section(**{f: "-1"}),
            f"the constraints' {f} is -1, {'not above' if f == 'loss_stop' else 'below'} zero",
        )
        for f in SCALARS
    },
    "a-loss_stop-of-zero": (
        section(loss_stop="0"),
        "the constraints' loss_stop is 0, not above zero",
    ),
    **{
        f"a-{f}-with-a-fraction": (
            section(**{f: "1.0"}),
            f"the constraints' {f} is Decimal('1.0'), not a whole number",
        )
        for f in ("max_open_orders", "moc_cutoff")
    },
    **{
        f"a-{f}-past-a-record": (
            section(**{f: str(2**53)}),
            f"the constraints' {f} is a huge int, past what a record can hold",
        )
        for f in ("max_open_orders", "moc_cutoff")
    },
    **{
        f"a-bool-in-{f}": (
            section(**{f: qty("true")}),
            f"the constraints' {f} of GLD is True, not a finite decimal",
        )
        for f in ("max_order_qty", "max_position")
    },
    **{
        f"text-in-{f}": (
            section(**{f: qty('"5"')}),
            f"the constraints' {f} of GLD is '5', not a finite decimal",
        )
        for f in ("max_order_qty", "max_position")
    },
    **{
        f"a-negative-in-{f}": (
            section(**{f: qty("-0.1")}),
            f"the constraints' {f} of GLD is -0.1, below zero",
        )
        for f in ("max_order_qty", "max_position")
    },
    "an-order-size-below-its-position": (
        section(max_order_qty=qty("4")),
        "the constraints' max_order_qty of GLD is 4, below its max_position of 5",
    ),
}


@pytest.mark.parametrize("case", BAD_SECTIONS)
def test_2_a_section_the_form_refuses_halts_naming_the_strategy_and_the_field(case) -> None:
    text, reason = BAD_SECTIONS[case]
    with pytest.raises(Halt) as caught:
        limits(strategy(text, name="reversal"))
    said = caught.value.reason
    assert said.startswith("strategy reversal's configuration reversal.toml has ")
    assert reason in said
    assert "[risk] section" in said


@pytest.mark.parametrize("f", ["max_open_orders", "moc_cutoff"])
def test_the_largest_count_a_record_holds_is_read(f) -> None:
    owners = replace(CAPS, max_open_orders=2**53 - 1)
    made = limits(strategy(section(**{f: str(2**53 - 1)})), owners=owners)
    assert getattr(made, f) == 2**53 - 1
    assert encode(made)


@pytest.mark.parametrize("written", ["inf", "nan", "1e400"])
def test_2_a_number_the_strategy_reader_refuses_fails_the_load(written) -> None:
    with pytest.raises(Halt, match="strategy chan's configuration chan.toml holds risk.max_net"):
        strategy(section(max_net=written))


# The tracked strategy files

# Each tracked strategy with no limits written yet, and the issue that writes them.
WITHOUT_LIMITS = {"chan_bollinger": "#129"}
# Each field of the hard caps that a strategy's limits lack, and the caps blob's own names.
CAP_NAMES = (
    {f.name for f in dataclasses.fields(HardCaps)}
    - {f.name for f in dataclasses.fields(Constraints)}
) | {"holds", "account"}
# Written out, so a name lost from the set above fails its case below.
EACH_CAP_NAME = (
    "share",
    "stress_move",
    "stress_low",
    "stress_high",
    "stress_vintage",
    "stress_through",
    "stress_max_age_days",
    "share_step",
    "holds",
    "account",
)


def tracked() -> list[str]:
    folder = STRATEGIES.relative_to(ROOT).as_posix()
    listed = subprocess.run(
        ["git", "ls-files", "--", folder], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return sorted(Path(path).stem for path in listed)


def readable(folder: Path, names: list[str]) -> list[str]:
    """Each strategy whose `[risk]` section the model reads, beside loose caps of its instruments.

    One with no section is left out, and any other refusal raises.
    """
    found = []
    for config in load_strategies(folder, names):
        if NAME not in config.sections:
            continue
        loose = {i: d(10**9) for i in config.instruments}
        owners = replace(CAPS, max_order_qty=loose, max_position=loose)
        assert limits(config, book(), owners=owners).strategy == config.name
        found.append(config.name)
    return found


def test_2_every_tracked_risk_section_is_one_the_model_reads(tmp_path) -> None:
    names = tracked()
    assert names, "no strategy file is tracked"
    read = readable(STRATEGIES, names)
    assert set(names) - set(read) == set(WITHOUT_LIMITS)
    # No tracked file holds a section until #129, so a fixture runs the same check on one.
    for name in names:
        (tmp_path / f"{name}.toml").write_bytes((STRATEGIES / f"{name}.toml").read_bytes())
    (tmp_path / "fixture.toml").write_text(TEXT)
    assert readable(tmp_path, [*names, "fixture"]) == sorted([*read, "fixture"])
    (tmp_path / "fixture.toml").write_text(section(max_net=None))
    with pytest.raises(Halt, match="strategy fixture's configuration .* states no max_net"):
        readable(tmp_path, [*names, "fixture"])


@pytest.mark.parametrize("name", WITHOUT_LIMITS)
def test_a_tracked_strategy_with_no_section_halts(name) -> None:
    (config,) = load_strategies(STRATEGIES, [name])
    with pytest.raises(Halt, match=f"{name}.toml has no \\[risk\\] section, or an empty one"):
        limits(config)


def caps_held(data: bytes) -> set[str]:
    """Each name of a hard cap, and each key of an account, anywhere in a strategy file."""

    def walk(value: object) -> Iterator[str]:
        if isinstance(value, Mapping):
            for key, inner in value.items():
                if key in CAP_NAMES:
                    yield key
                yield from walk(key)
                yield from walk(inner)
        elif isinstance(value, list):
            for inner in value:
                yield from walk(inner)
        elif isinstance(value, str) and "accounts/" in value:
            yield "account"

    found = set(walk(tomllib.loads(data.decode())))
    # A comment is no parsed value, so the text is read too.
    return found | {"account"} if b"accounts/" in data else found


def test_4_no_tracked_strategy_file_holds_a_hard_cap_or_an_accounts_key() -> None:
    names = tracked()
    assert names, "no strategy file is tracked"
    for name in names:
        assert caps_held((STRATEGIES / f"{name}.toml").read_bytes()) == set(), name


def test_4_the_names_looked_for_are_the_ones_written_out() -> None:
    assert set(EACH_CAP_NAME) == CAP_NAMES


@pytest.mark.parametrize("name", EACH_CAP_NAME)
@pytest.mark.parametrize(
    "form",
    [
        "{name} = 1\n",
        "[risk]\n{name} = 1\n",
        "[risk.{name}]\n",
        "[risk.max_position]\n{name} = 1\n",
        "[[cost.GLD]]\n{name} = 1\n",
    ],
    ids=["top-level", "in-a-section", "an-empty-table", "nested", "in-a-list"],
)
def test_4_a_file_that_holds_one_is_found(name, form) -> None:
    assert caps_held(form.format(name=name).encode()) == {name}
    assert caps_held(TEXT.encode()) == set()


@pytest.mark.parametrize(
    "text",
    [
        '[risk]\nfrom = "accounts/paper/caps.json"\n',
        '[risk]\n"accounts/paper/caps.json" = 1\n',
        '[risk]\nfrom = ["s3://bucket/accounts/paper/caps.json"]\n',
        "[risk] # accounts/paper/caps.json\n",
        '[risk]\nfrom = "\\u0061ccounts/paper/caps.json"\n',
        '[risk]\n"\\u0061ccounts/paper/caps.json" = 1\n',
    ],
    ids=["a-value", "a-name", "in-a-list", "in-a-comment", "escaped", "an-escaped-name"],
)
def test_4_an_accounts_key_is_found_wherever_it_is_written(text) -> None:
    assert caps_held(text.encode()) == {"account"}


# The stage

RECOVER_ROW = Stage(RECOVER_STAGE, StageKind.CAPTURING, inputs=("data",))


def table_of(*strategies: StrategyConfig) -> Table:
    return Table(stages=(DATA_ROW, RECOVER_ROW, RISK_ROW), strategies=strategies)


ONE = table_of(CHAN)
# A made-up record of chan on its two legs, which the caps above name.
MOVES = Path(__file__).parent / "support" / "stress_moves.toml"
DATA_RESULT = result_key(DEPLOYMENT, RUN, DECISION, "data")
SNAPSHOT_KEY = output_key(DEPLOYMENT, RUN, DECISION, "data", "snapshot")
RECOVER_RESULT = result_key(DEPLOYMENT, RUN, DECISION, RECOVER_STAGE)
FILE_KEYS = {
    name: output_key(DEPLOYMENT, RUN, DECISION, RECOVER_STAGE, name)
    for name in (BOOK_OUTPUT, CAPS_OUTPUT, VALUATION_OUTPUT)
}
RESULT = result_key(DEPLOYMENT, RUN, DECISION, RISK_STAGE)
STORED = output_key(DEPLOYMENT, RUN, DECISION, RISK_STAGE, CONSTRAINTS_OUTPUT)
EVERY_INPUT = {DATA_RESULT, SNAPSHOT_KEY, RECOVER_RESULT, *FILE_KEYS.values()}
# What recover lists: its files by name, each with its kind and its entries.
Files = Mapping[str, tuple[type, tuple]]
RECOVERED: Files = {
    BOOK_OUTPUT: (Book, (HELD,)),
    CAPS_OUTPUT: (HardCaps, (CAPS,)),
    VALUATION_OUTPUT: (Valuation, (V,)),
}


def test_the_files_names_are_the_ones_other_stages_read() -> None:
    assert FILE_KEYS[VALUATION_OUTPUT] == valuation_key(DEPLOYMENT, RUN, DECISION)
    prefix = f"runs/{DEPLOYMENT}/{RUN}/20261103T204000Z"
    assert STORED == f"{prefix}/risk/constraints.json"
    assert sorted(FILE_KEYS.values()) == [
        f"{prefix}/execution/recover/{name}.json" for name in ("book", "caps", "valuation")
    ]


def store(root: Path) -> Storage:
    return storage_over(FolderStore(root))


def storage_over(over) -> Storage:
    return Storage(
        over,
        timer=Timer(),
        pause=lambda s: None,
        retry=Retry(deadline=timedelta(seconds=1), pause=0.1, longest_pause=0.1),
        alerts=Alarms(),
        owner_step="none",
    )


def header(stage: str, table: Table, at: datetime = DECISION) -> BlobHeader:
    return BlobHeader(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        stage=stage,
        kind="capturing",
        decision_at=at,
        inputs={},
        release=RELEASE,
        config_hashes=table.hashes_for(stage),
    )


def write_stage(
    storage: Storage, stage: str, files: Files, table: Table, halt: str | None = None
) -> dict[str, str]:
    """Store a stand-in stage's files and its result, and return each blob's key and SHA-256."""
    made = header(stage, table)
    refs = []
    for name, (kind, entries) in files.items():
        key = output_key(DEPLOYMENT, RUN, DECISION, stage, name)
        data = encode_output(kind, OutputFile(made, entries, not entries))
        refs.append(storage.put(StageKind.CAPTURING, key, data).ref)
    record = HaltRecord(halt, None, (), (), ()) if halt is not None else None
    result = DecisionResult(made, tuple(refs), record)
    key = result_key(DEPLOYMENT, RUN, DECISION, stage)
    storage.put_result(key, result)
    return {key: sha256_of(encode(result)), **{ref.key: ref.sha256 for ref in refs}}


def write_data(
    storage: Storage, held: Snapshot = MARKED, table: Table = ONE, halt: str | None = None
) -> dict[str, str]:
    return write_stage(storage, "data", {"snapshot": (Snapshot, (held,))}, table, halt)


def write_recover(
    storage: Storage, files: Files = RECOVERED, table: Table = ONE, halt: str | None = None
) -> dict[str, str]:
    return write_stage(storage, RECOVER_STAGE, files, table, halt)


def write_both(storage: Storage, table: Table = ONE) -> dict[str, str]:
    return write_data(storage, table=table) | write_recover(storage, table=table)


def handler(storage: Storage, table: Table = ONE, stress_moves: Path = MOVES) -> RiskStage:
    return risk_handler(
        storage=storage,
        deployment=DEPLOYMENT,
        run=RUN,
        release=RELEASE,
        table=table,
        stress_moves=stress_moves,
    )


def run(stage: RiskStage, report: Callable[[], None] = lambda: None) -> DecisionResult:
    return stage(Item(DECISION, RISK_STAGE), report)


def blobs(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in (root / "runs").rglob("*")
        if p.is_file()
    }


def risk_header(inputs: Mapping[str, str], table: Table = ONE) -> BlobHeader:
    return BlobHeader(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        stage="risk",
        kind="deterministic",
        decision_at=DECISION,
        inputs=inputs,
        release=RELEASE,
        config_hashes=table.hashes_for(RISK_STAGE),
    )


def test_1_the_stage_stores_the_constraints_a_hand_calculation_gives(tmp_path) -> None:
    storage = store(tmp_path)
    inputs = write_both(storage)
    result = run(handler(storage))
    assert result.halt is None
    (ref,) = result.outputs
    assert ref.key == STORED
    stored = decode_output((tmp_path / STORED).read_bytes(), Constraints)
    assert stored == OutputFile(risk_header(inputs), (EXPECTED,), False)
    assert result.header == stored.header
    for key, sha in inputs.items():
        assert sha256_of((tmp_path / key).read_bytes()) == sha
    assert set(inputs) == EVERY_INPUT


def test_1_a_second_run_stores_the_same_bytes_and_the_same_result(tmp_path) -> None:
    storage = store(tmp_path)
    write_both(storage)
    first = run(handler(storage))
    held = blobs(tmp_path)
    # Storage halts a deterministic stage whose second run writes other bytes.
    assert run(handler(store(tmp_path))) == first
    assert blobs(tmp_path) == held


def test_1_mappings_built_in_another_order_store_the_same_bytes(tmp_path) -> None:
    backward = {
        BOOK_OUTPUT: (Book, (book({"chan": {USO: -3, GLD: 2}}),)),
        CAPS_OUTPUT: (
            HardCaps,
            (
                replace(
                    CAPS,
                    max_order_qty=dict(reversed(CAPS.max_order_qty.items())),
                    max_position=dict(reversed(CAPS.max_position.items())),
                ),
            ),
        ),
        VALUATION_OUTPUT: RECOVERED[VALUATION_OUTPUT],
    }
    made = []
    for name, files in (("one", RECOVERED), ("other", backward)):
        storage = store(tmp_path / name)
        write_data(storage, snapshot(dict(sorted({GLD: 380, USO: 75}.items(), reverse=bool(made)))))
        write_recover(storage, {k: files[k] for k in RECOVERED})
        assert run(handler(storage)).halt is None
        made.append((tmp_path / name / STORED).read_bytes())
    assert made[0] == made[1]


def test_the_stage_reports_after_each_read_and_after_its_write(tmp_path) -> None:
    storage = store(tmp_path)
    write_both(storage)
    seen: list[bool] = []
    run(handler(storage), lambda: seen.append((tmp_path / STORED).exists()))
    # Two results, four files, then the constraints, so no read is silent for long.
    assert seen == [False] * 6 + [True]


def test_a_second_decision_of_one_handler_names_only_its_own_inputs(tmp_path) -> None:
    storage = store(tmp_path)
    write_both(storage)
    stage = handler(storage)
    assert run(stage).halt is None
    later = DECISION + timedelta(minutes=1)
    second = stage(Item(later, RISK_STAGE), lambda: None)
    assert second.halt is not None and dict(second.header.inputs) == {}
    # It passes nothing on either, after a first decision that did.
    other = store(tmp_path / "other")
    write_data(other, halt="GLD missing")
    stage = handler(other)
    assert run(stage).halt.cause is not None
    assert stage(Item(later, RISK_STAGE), lambda: None).halt.cause is None


def test_the_stage_reads_nothing_of_the_accounts(tmp_path) -> None:
    write_both(store(tmp_path))
    read: list[str] = []

    class Watched(FolderStore):
        def get(self, key: str) -> bytes:
            read.append(key)
            return super().get(key)

    assert run(handler(storage_over(Watched(tmp_path)))).halt is None
    assert set(read) == EVERY_INPUT


def test_the_stage_reads_the_caps_recover_stored_and_not_the_owners_blob(tmp_path) -> None:
    storage = store(tmp_path)
    write_both(storage)
    first = run(handler(storage))
    # The owner rewrites the blob between two attempts.
    owners = tmp_path / "accounts" / "paper" / "caps.json"
    owners.parent.mkdir(parents=True)
    owners.write_bytes(b"{}")
    assert run(handler(store(tmp_path))) == first


@pytest.mark.parametrize("stage", ["data", RECOVER_STAGE])
def test_1_a_halt_in_either_input_is_passed_on_naming_it(tmp_path, stage) -> None:
    storage = store(tmp_path)
    wrote = {
        "data": write_data(storage, halt="GLD missing" if stage == "data" else None),
        RECOVER_STAGE: write_recover(storage, halt="book differs" if stage != "data" else None),
    }
    held = blobs(tmp_path)
    result = run(handler(storage))
    key = result_key(DEPLOYMENT, RUN, DECISION, stage)
    why = "GLD missing" if stage == "data" else "book differs"
    assert result.outputs == () and result.halt is not None
    assert result.halt.reason == f"{key} holds a halt: {why}"
    assert result.halt.cause == BlobRef(key, wrote[stage][key])
    # Each result read by then is named, and no file, because none was read.
    named = {DATA_RESULT: wrote["data"][DATA_RESULT]}
    if stage != "data":
        named[RECOVER_RESULT] = wrote[RECOVER_STAGE][RECOVER_RESULT]
    assert result.header == risk_header(named)
    assert blobs(tmp_path) == held


def test_1_a_halt_in_both_inputs_passes_on_datas(tmp_path) -> None:
    storage = store(tmp_path)
    data = write_data(storage, halt="GLD missing")
    write_recover(storage, {}, halt=f"{DATA_RESULT} holds a halt: GLD missing")
    result = run(handler(storage))
    assert result.halt.cause == BlobRef(DATA_RESULT, data[DATA_RESULT])


def test_1_recovers_halt_is_passed_on_before_any_file_or_section_is_read(tmp_path) -> None:
    bare = strategy('instruments = ["GLD", "USO"]\n')
    table = table_of(bare)
    storage = store(tmp_path)
    write_stage(storage, "data", {}, table)
    recover = write_recover(storage, {}, table, halt="book differs")
    result = run(handler(storage, table))
    assert result.halt.cause == BlobRef(RECOVER_RESULT, recover[RECOVER_RESULT])


def own_halt(tmp_path: Path, storage: Storage, table: Table = ONE) -> DecisionResult:
    """The stage's result, which holds a halt of its own and leaves no other blob behind."""
    held = blobs(tmp_path)
    result = run(handler(storage, table))
    assert result.halt is not None and result.outputs == ()
    # Nothing upstream halted, so the halt names no cause, and monitoring alarms on it.
    assert result.halt.cause is None
    assert blobs(tmp_path) == held
    return result


def test_2_a_section_the_model_refuses_halts_the_stage_and_not_the_workers_start(tmp_path) -> None:
    table = table_of(strategy(section(max_net=None)))
    storage = store(tmp_path)
    inputs = write_both(storage, table)
    result = own_halt(tmp_path, storage, table)
    assert result.halt.reason == (
        "strategy chan's configuration chan.toml has a [risk] section that states no max_net"
    )
    # It names what the stage read, as every result does.
    assert result.header == risk_header(inputs, table)


def test_3_two_strategies_in_one_deployment_halt(tmp_path) -> None:
    table = table_of(CHAN, strategy(name="reversal"))
    storage = store(tmp_path)
    inputs = write_both(storage, table)
    result = own_halt(tmp_path, storage, table)
    assert result.halt.reason == (
        "the deployment runs 2 strategies (chan, reversal), and the scope each one's limits take "
        "is decided for one alone"
    )
    assert result.header == risk_header(inputs, table)
    assert set(result.header.config_hashes) == {"chan", "reversal"}


@pytest.mark.parametrize("name", RECOVERED)
def test_3_a_recover_result_that_lists_none_of_one_file_halts(tmp_path, name) -> None:
    storage = store(tmp_path)
    data = write_data(storage)
    recover = write_recover(storage, {k: v for k, v in RECOVERED.items() if k != name})
    result = own_halt(tmp_path, storage)
    assert result.halt.reason == f"{RECOVER_RESULT} lists {FILE_KEYS[name]} 0 times, not once"
    # Both results are named, and each file read before this one.
    before = (BOOK_OUTPUT, CAPS_OUTPUT, VALUATION_OUTPUT)
    read = {FILE_KEYS[k] for k in before[: before.index(name)]}
    assert dict(result.header.inputs) == {
        **data,
        RECOVER_RESULT: recover[RECOVER_RESULT],
        **{k: v for k, v in recover.items() if k in read},
    }


@pytest.mark.parametrize("name", RECOVERED)
def test_3_a_recover_result_that_lists_one_file_twice_halts(tmp_path, name) -> None:
    storage = store(tmp_path)
    data = write_data(storage)
    made = header(RECOVER_STAGE, ONE)
    refs = []
    for k, (kind, entries) in RECOVERED.items():
        file = encode_output(kind, OutputFile(made, entries, False))
        refs += [storage.put(StageKind.CAPTURING, FILE_KEYS[k], file).ref] * (2 if k == name else 1)
    storage.put_result(RECOVER_RESULT, DecisionResult(made, tuple(refs), None))
    result = own_halt(tmp_path, storage)
    assert result.halt.reason == f"{RECOVER_RESULT} lists {FILE_KEYS[name]} 2 times, not once"
    before = (BOOK_OUTPUT, CAPS_OUTPUT, VALUATION_OUTPUT)
    read = {FILE_KEYS[k] for k in before[: before.index(name)]}
    assert set(result.header.inputs) == {*data, RECOVER_RESULT, *read}


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("name", RECOVERED)
def test_3_a_file_of_other_than_one_entry_halts(tmp_path, name, count) -> None:
    kind, entries = RECOVERED[name]
    storage = store(tmp_path)
    write_data(storage)
    recover = write_recover(storage, {**RECOVERED, name: (kind, entries * count)})
    result = own_halt(tmp_path, storage)
    assert result.halt.reason == f"{FILE_KEYS[name]} holds {count} entries, not one"
    # The file was read, so the halt names it.
    assert result.header.inputs[FILE_KEYS[name]] == recover[FILE_KEYS[name]]


@pytest.mark.parametrize("name", RECOVERED)
def test_3_a_file_that_is_listed_and_missing_halts(tmp_path, name) -> None:
    storage = store(tmp_path)
    write_both(storage)
    (tmp_path / FILE_KEYS[name]).unlink()
    result = own_halt(tmp_path, storage)
    assert (
        result.halt.reason
        == f"output file {FILE_KEYS[name]}, listed by {RECOVER_RESULT}, is missing"
    )
    assert FILE_KEYS[name] not in result.header.inputs


@pytest.mark.parametrize(
    "held",
    [HELD, book(opens=[order(1, GLD, "chan", name="old-1")])],
    ids=["held", "an-open-order"],
)
def test_3_a_held_instrument_with_no_mark_halts_the_stage(tmp_path, held) -> None:
    """Before #100 a close−20 decision that holds a position halts here, ahead of the gate."""
    storage = store(tmp_path)
    inputs = write_data(storage, SEALED) | write_recover(
        storage, {**RECOVERED, BOOK_OUTPUT: (Book, (held,))}
    )
    result = own_halt(tmp_path, storage)
    assert result.halt.reason == (
        "GLD has no mark: its newest bar is for 2026-11-02, not later than 2026-11-02"
    )
    assert result.header == risk_header(inputs)


def test_3_a_held_instrument_with_no_bar_halts_the_stage(tmp_path) -> None:
    storage = store(tmp_path)
    inputs = write_data(storage, snapshot({GLD: 380})) | write_recover(storage)
    result = own_halt(tmp_path, storage)
    assert result.halt.reason == "USO missing for the session"
    assert result.header == risk_header(inputs)


def test_3_a_stress_move_past_its_age_halts_the_stage(tmp_path) -> None:
    storage = store(tmp_path)
    old = replace(CAPS, stress_max_age_days=3)
    inputs = write_data(storage) | write_recover(
        storage, {**RECOVERED, CAPS_OUTPUT: (HardCaps, (old,))}
    )
    result = own_halt(tmp_path, storage)
    assert "stress_through is 2026-10-30, 4 days before the decision" in result.halt.reason
    assert result.header == risk_header(inputs)


def test_3_a_v_not_above_zero_halts_the_stage(tmp_path) -> None:
    storage = store(tmp_path)
    inputs = write_data(storage) | write_recover(
        storage, {**RECOVERED, VALUATION_OUTPUT: (Valuation, (valuation(0),))}
    )
    result = own_halt(tmp_path, storage)
    assert result.halt.reason == "the valuation's value is 0, not above zero"
    assert result.header == risk_header(inputs)


def test_3_an_instrument_the_caps_do_not_limit_halts_the_stage(tmp_path) -> None:
    storage = store(tmp_path)
    gld = replace(CAPS, max_order_qty={GLD: d(8)}, max_position={GLD: d(6)})
    inputs = write_data(storage) | write_recover(
        storage, {**RECOVERED, CAPS_OUTPUT: (HardCaps, (gld,))}
    )
    result = own_halt(tmp_path, storage)
    assert result.halt.reason == "the constraints limit USO, which the hard caps do not"
    assert result.header == risk_header(inputs)


def test_3_a_snapshot_of_another_decision_halts_the_stage(tmp_path) -> None:
    storage = store(tmp_path)
    # The file's own header names this decision, so storage's check passes.
    older = replace(MARKED, decision_at=DECISION - timedelta(minutes=1))
    data = write_data(storage, older)
    recover = write_recover(storage, {**RECOVERED, BOOK_OUTPUT: (Book, (book(),))})
    result = own_halt(tmp_path, storage)
    assert result.halt.reason.startswith(f"{SNAPSHOT_KEY} holds a snapshot of")
    assert result.header == risk_header(data | {RECOVER_RESULT: recover[RECOVER_RESULT]})


def test_3_a_v_of_another_session_halts_the_stage(tmp_path) -> None:
    storage = store(tmp_path)
    older = replace(V, session=TABLE.before(PRIOR).day)
    inputs = write_data(storage) | write_recover(
        storage, {**RECOVERED, VALUATION_OUTPUT: (Valuation, (older,))}
    )
    result = own_halt(tmp_path, storage)
    assert result.halt.reason.startswith("the valuation is of 2026-10-30, not of 2026-11-02")
    assert result.header == risk_header(inputs)


@pytest.mark.parametrize("stage", ["data", RECOVER_STAGE])
def test_3_an_input_with_no_result_halts(tmp_path, stage) -> None:
    storage = store(tmp_path)
    named = {} if stage == "data" else write_data(storage)
    if stage == "data":
        write_recover(storage)
    result = own_halt(tmp_path, storage)
    key = result_key(DEPLOYMENT, RUN, DECISION, stage)
    assert result.halt.reason == f"{key} is not stored, and stage risk reads it"
    assert dict(result.header.inputs) == {k: v for k, v in named.items() if k == DATA_RESULT}


@pytest.mark.parametrize("stage", ["data", RECOVER_STAGE])
def test_3_a_result_storage_refuses_halts_and_is_not_named(tmp_path, stage) -> None:
    storage = store(tmp_path)
    data = write_both(storage)
    key = result_key(DEPLOYMENT, RUN, DECISION, stage)
    (tmp_path / key).write_bytes(b"{}")
    result = own_halt(tmp_path, storage)
    assert result.halt.reason.startswith(f"{key} holds no decision result the codec reads")
    named = {} if stage == "data" else {DATA_RESULT: data[DATA_RESULT]}
    assert result.header == risk_header(named)


def test_3_constraints_already_stored_with_other_bytes_halt(tmp_path) -> None:
    storage = store(tmp_path)
    inputs = write_both(storage)
    storage.put(StageKind.DETERMINISTIC, STORED, b"{}")
    result = own_halt(tmp_path, storage)
    assert "so the step is not deterministic" in result.halt.reason
    assert result.header == risk_header(inputs)


def test_the_stage_reads_the_capture_that_counts_and_not_a_later_one(tmp_path) -> None:
    storage = store(tmp_path)
    write_data(storage)
    made = header(RECOVER_STAGE, ONE)
    later = encode_output(HardCaps, OutputFile(made, (replace(CAPS, max_gross=d(1)),), False))
    key = attempt_key(DEPLOYMENT, RUN, DECISION, RECOVER_STAGE, CAPS_OUTPUT, 2)
    # Recover lists the capture that counts first. Listing it last here checks
    # that the stage goes by its key and not by its place.
    refs = [storage.put(StageKind.CAPTURING, key, later).ref]
    for name, (kind, entries) in RECOVERED.items():
        data = encode_output(kind, OutputFile(made, entries, False))
        refs.append(storage.put(StageKind.CAPTURING, FILE_KEYS[name], data).ref)
    storage.put_result(RECOVER_RESULT, DecisionResult(made, tuple(refs), None))
    result = run(handler(storage))
    assert result.halt is None and set(result.header.inputs) == EVERY_INPUT
    (made_limits,) = decode_output((tmp_path / STORED).read_bytes(), Constraints).entries
    assert made_limits == EXPECTED


# The steps every model's stage shares


def decided(storage: Storage, **changes: object) -> StageDecision:
    made = {
        "format": 2,
        "stage": RISK_STAGE,
        "kind": StageKind.DETERMINISTIC,
        "deployment": DEPLOYMENT,
        "run": RUN,
        "release": RELEASE,
        "config_hashes": ONE.hashes_for(RISK_STAGE),
    }
    return StageRun(storage, **made | changes).decision(DECISION, lambda: None)  # type: ignore[arg-type]


def test_a_file_with_no_entries_never_says_trade_nothing_and_carries_the_stages_format(
    tmp_path,
) -> None:
    ref = decided(store(tmp_path)).put(CONSTRAINTS_OUTPUT, Constraints, ())
    file = decode_output((tmp_path / ref.key).read_bytes(), Constraints)
    assert (file.entries, file.trade_nothing, file.header.format) == ((), False, 2)


def test_a_read_of_another_blob_after_a_write_is_an_error(tmp_path) -> None:
    storage = store(tmp_path)
    write_both(storage)
    decision = decided(storage)
    decision.snapshot("data")
    decision.put(CONSTRAINTS_OUTPUT, Constraints, (EXPECTED,))
    # A blob already read is named in the file's header, so reading it again is no error.
    assert decision.snapshot("data") == MARKED
    with pytest.raises(RuntimeError, match=f"read {RECOVER_RESULT} after it had written"):
        decision.result(RECOVER_STAGE)


def test_a_halt_of_the_stages_own_after_one_it_caught_names_no_cause(tmp_path) -> None:
    storage = store(tmp_path)
    recover = write_recover(storage, {}, halt="book differs")
    decision = decided(storage)
    with pytest.raises(Halt) as caught:
        decision.result(RECOVER_STAGE)
    cause = BlobRef(RECOVER_RESULT, recover[RECOVER_RESULT])
    assert decision.halted(caught.value).halt.cause == cause
    assert decision.halted(Halt("a fault of the stage's own")).halt.cause is None
    # A second upstream halt leaves the first one's cause as it was.
    data = write_data(storage, halt="GLD missing")
    with pytest.raises(Halt) as later:
        decision.result("data")
    assert decision.halted(later.value).halt.cause == BlobRef(DATA_RESULT, data[DATA_RESULT])
    assert decision.halted(caught.value).halt.cause == cause
    # A halted result halts each time it is asked for.
    with pytest.raises(Halt) as again:
        decision.result(RECOVER_STAGE)
    assert decision.halted(again.value).halt.cause == cause


# The entry point


def test_5_the_row_is_shared_and_reads_data_and_recover() -> None:
    assert RISK_ROW == Stage(
        "risk", StageKind.DETERMINISTIC, inputs=("data", "execution/recover"), strategy=None
    )
    assert dict(ONE.hashes_for("risk")) == {"chan": CHAN.config_hash}
    heard = {r.key for r in routes(ONE, DEPLOYMENT) if r.reader == "risk"}
    assert heard == {
        f"runs/{DEPLOYMENT}/*/{stage}/result.json" for stage in ("data", "execution/recover")
    }


def test_5_the_row_refuses_to_build_without_recovers_row() -> None:
    with pytest.raises(ValueError, match="stage risk reads execution/recover, which no row"):
        Table(stages=(DATA_ROW, RISK_ROW), strategies=(CHAN,))


def test_the_handler_takes_its_kind_and_its_inputs_from_the_table(tmp_path) -> None:
    feed = Stage("feed", StageKind.CAPTURING)
    caught_up = Stage("caught-up", StageKind.CAPTURING)
    row = Stage("risk", StageKind.CAPTURING, inputs=("feed", "caught-up"))
    table = Table(stages=(feed, caught_up, row), strategies=(CHAN,))
    storage = store(tmp_path)
    result = run(handler(storage, table))
    assert "/feed/result.json is not stored" in result.halt.reason
    assert result.header.kind == "capturing"
    write_stage(storage, "feed", {"snapshot": (Snapshot, (MARKED,))}, table)
    assert "/caught-up/result.json is not stored" in run(handler(storage, table)).halt.reason
    write_stage(storage, "caught-up", RECOVERED, table)
    assert run(handler(storage, table)).halt is None


def test_the_risk_worker_loads_neither_marketlake_nor_pandas() -> None:
    code = (
        "import sys, ithildinruntime.runtime.risk_worker; "
        "print(sorted({'lake', 'pandas', 'pyarrow', 'numpy'} & set(sys.modules)))"
    )
    ran = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert ran.stdout.strip() == "[]"


def looped(
    tmp_path: Path,
    write: Callable[[Storage], object],
    table: Table = ONE,
    stress_moves: Path = MOVES,
) -> tuple:
    """The risk stage's result from the receive loop, once `write` has stored its inputs."""
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    alarms = Alarms()
    storage = folder_storage(folder.store(), alarms)
    # The loop finds these in its check as it starts.
    wrote = write(storage)
    plan = Plan(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        mode=Mode.PAPER,
        release=RELEASE,
        decisions=(PlanDecision(DECISION, tuple(sorted(s.name for s in table.strategies))),),
    )
    loop = ReceiveLoop(
        worker="risk-1",
        component="risk",
        table=table,
        plan=plan,
        queue=folder.queue("risk"),
        storage=storage,
        bucket=folder.bucket,
        handlers={RISK_STAGE: handler(storage, table, stress_moves)},
        alerts=alarms,
        timer=SystemTimer(),
        beat=lambda: None,
        tick=0.05,
        stop_timeout=5.0,
    )
    with running({"risk": loop}) as started:
        assert started.wait_for(lambda: (folder.root / RESULT).exists(), 20)
    result = storage.read_result(RESULT, DECISION)
    assert result is not None
    return storage, result, wrote


def test_1_the_loop_stores_the_constraints_beside_stand_in_rows(tmp_path) -> None:
    storage, result, inputs = looped(tmp_path, write_both)
    assert result.halt is None and result.header == risk_header(inputs)
    (ref,) = result.outputs
    file = storage.read_output(RESULT, result, ref, DECISION, Constraints)
    assert file.entries == (EXPECTED,) and file.header == result.header


@pytest.mark.parametrize("stage", ["data", RECOVER_STAGE])
def test_1_the_loop_stores_each_halt_passed_on(tmp_path, stage) -> None:
    def write(storage: Storage) -> dict[str, str]:
        if stage == "data":
            return write_data(storage, halt="why") | write_recover(storage, {}, halt="why")
        return write_data(storage) | write_recover(storage, {}, halt="why")

    _, result, wrote = looped(tmp_path, write)
    key = result_key(DEPLOYMENT, RUN, DECISION, stage)
    assert result.halt.cause == BlobRef(key, wrote[key])
    assert result.halt.reason == f"{key} holds a halt: why"


def test_3_the_loop_stores_the_stages_own_halt_naming_every_input(tmp_path) -> None:
    table = table_of(CHAN, strategy(name="reversal"))
    _, result, inputs = looped(tmp_path, lambda storage: write_both(storage, table), table)
    assert result.halt.cause is None and "runs 2 strategies" in result.halt.reason
    assert result.header == risk_header(inputs, table)


def test_a_decision_at_the_close_is_bounded_as_one_before_it(tmp_path) -> None:
    """Test 1's account decides at the close itself, whose session is the one that closed."""
    at_close = snapshot({GLD: 380, USO: 75}, at=CLOSE)
    assert limits(marked=at_close) == EXPECTED


def test_every_field_of_the_constraints_but_the_name_is_in_the_form() -> None:
    assert set(FIELDS) == {f for f in Constraints.__dataclass_fields__ if f != "strategy"}
    assert isinstance(EXPECTED.max_gross, Decimal)


# The hold to the stress records


def moves(tmp_path: Path, *sizes: str, **changes: str) -> Path:
    """The made-up record, and one more of another vintage for each size."""
    text = MOVES.read_text(encoding="utf-8")
    for old, new in changes.items():
        assert old in text
        text = text.replace(old, new)
    made = text
    for n, size in enumerate(sizes):
        more = text.replace("a" * 64, "cdef"[n] * 64)
        made += more.replace('stress_move = "0.00000001"', f'stress_move = "{size}"')
    path = tmp_path / "moves.toml"
    path.write_text(made, encoding="utf-8")
    return path


def test_a_raised_record_lowers_the_models_net_cap() -> None:
    own, more = stress_record(), stress_record(vintage="c" * 64, stress_move=d("0.4"))
    # 0.02 × 5,000 ÷ 0.4 = 250, where the caps' own 0.2 gives 500.
    assert limits(records=(own, more)) == replace(EXPECTED, max_net=d("250.0000"))
    assert limits(records=(more, own)).max_net == 250
    for other in (replace(more, strategy=OTHER), replace(more, withdrawn="x")):
        assert limits(records=(own, other)) == EXPECTED


@pytest.mark.parametrize(
    ("records", "why"),
    [
        ((), "there is no stress record"),
        (Halt("moves.toml is missing"), "^moves.toml is missing$"),
        ((stress_record(vintage="c" * 64),), "of no record of chan"),
        ((stress_record(withdrawn="x"),), "withdrawn: x"),
        ((stress_record(instruments=(GLD,)),), "do not both name USO"),
        ((stress_record(through=date(2026, 10, 29)),), "stress_through"),
        ((stress_record(stress_move=d("0.3")),), "stress_move is 0.2, below"),
        (
            (stress_record(), stress_record(vintage="c" * 64, stress_move=d("0.7"))),
            "stress_high is 0.6",
        ),
    ],
)
def test_the_model_halts_on_caps_no_record_holds(records, why) -> None:
    with pytest.raises(Halt, match=why):
        limits(records=records)


def test_the_loop_stores_the_limits_a_raised_record_leaves(tmp_path) -> None:
    raised = moves(tmp_path, "0.40000000")
    storage, result, inputs = looped(tmp_path, write_both, stress_moves=raised)
    assert result.halt is None and result.header == risk_header(inputs)
    (ref,) = result.outputs
    file = storage.read_output(RESULT, result, ref, DECISION, Constraints)
    assert file.entries == (replace(EXPECTED, max_net=d("250.0000")),)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (None, "moves.toml is missing"),
        ("x = 1\n", "moves.toml holds x, which this release does not read"),
        ("", "there is no stress record, so none holds the hard caps' stress_vintage"),
    ],
    ids=["absent", "bad", "empty"],
)
def test_a_file_of_no_use_lets_the_worker_start_and_halts_its_decision(tmp_path, text, why) -> None:
    path = tmp_path / "moves.toml"
    if text is not None:
        path.write_text(text)
    _, result, inputs = looped(tmp_path, write_both, stress_moves=path)
    assert result.halt.reason == why and result.halt.cause is None
    assert result.header == risk_header(inputs)
