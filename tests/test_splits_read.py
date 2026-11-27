"""The record of splits through `LakeSource`, on a made-up lake of captured bars and sealed days.

#189's acceptance names each case, and a test's number is the acceptance
item's. The lake is tests/test_captured_bars.py's, 60 captured sessions of GLD
and USO, with a sealed quotes partition for each of the last 20 sessions whose
equity close states the close of the session before. Every price is made up.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderBucket
from ithildinruntime.core import Halt, InstrumentId, Snapshot
from ithildinruntime.data import DataStage, LakeSource, Mirror
from ithildinruntime.data import sealed as sealed_module
from ithildinruntime.data.reference import QUOTE_SOURCE, QuoteKindRow, QuoteKinds
from ithildinruntime.data.splits import QUOTE, SEALED, SplitRecord
from ithildinruntime.data.stage import COMPARED, differences
from ithildinruntime.runtime.loop import Item
from tests.support import lake as lakes
from tests.support import made_lake as made
from tests.support import segments
from tests.support import splits as recorded
from tests.support.sealed import close_row, gap_close_row, partition
from tests.test_alpha import DEPLOYMENT, RELEASE, RUN, store, table_of
from tests.test_captured_bars import (
    DAY,
    EARLY,
    MINUTE,
    SETTINGS,
    TRACKED,
    WALKS,
    WINDOW,
    decide,
)
from tests.test_captured_bars import KINDS as PRICE_KINDS
from tests.test_data_source import Clock, Timer
from tests.test_data_stage import Reports

GLD, USO = InstrumentId("GLD"), InstrumentId("USO")
TABLE = made.TABLE
# The covered rows end 20 sessions before the newest bar, so 20 pairs need a quote.
LAST_COVERED = WINDOW[-21]
SEALED_DAYS = WINDOW[-20:]
SHORT = recorded.record(
    recorded.covered("GLD", WINDOW[0], LAST_COVERED, line=1),
    recorded.covered("USO", WINDOW[0], LAST_COVERED, line=2),
)
KINDS = QuoteKinds(
    (
        QuoteKindRow(
            made.DAYS[0], QUOTE_SOURCE, made.DAYS[0], made.DAYS[-1], segments.KIND, "a test"
        ),
    )
)


def before(day: date) -> date:
    return TABLE.before(day).day


def seal_closes(lake: Path, ticker: str, days: list[date], walk: Mapping[date, Decimal]) -> None:
    """Seal each day's equity close, stating the close of the session before as `walk` holds it."""
    for day in days:
        row = {
            **close_row(ticker, TABLE.session(day).close),
            "close_price": float(walk[before(day)]),
        }
        partition(lake, ticker, row, day=day)


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from tests.test_captured_bars import BEFORE_CAPTURE, CAPTURED, IMPORTED

    root = made.build(
        tmp_path_factory.mktemp("splits") / "lake",
        [
            made.Made(t, IMPORTED.format(t), date(2026, 10, 31), {d: w[d] for d in BEFORE_CAPTURE})
            for t, w in WALKS.items()
        ],
        {t: {d: w[d] for d in CAPTURED} for t, w in WALKS.items()},
    )
    for ticker, walk in WALKS.items():
        seal_closes(root, ticker, SEALED_DAYS, walk)
    return root


def mine(lake: Path, tmp_path: Path) -> Path:
    return lakes.copy_lake(lake, tmp_path / "mine", ((WINDOW[0], WINDOW[-1]),))


def source(lake: Path, tmp_path: Path, splits: SplitRecord = SHORT, bucket=None) -> LakeSource:
    """`LakeSource` as `decide` builds it, with this record, reading its quotes from segments."""
    at = TABLE.session(DAY).close - EARLY
    live = tmp_path / "live"
    if not live.exists():
        for ticker, walk in WALKS.items():
            mid = walk[DAY]
            row = lakes.quote_row(
                ticker,
                at - MINUTE,
                bid=float(mid - Decimal("0.01")),
                ask=float(mid + Decimal("0.01")),
                last=float(mid),
                close_price=float(walk[before(DAY)]),
            )
            lakes.quotes_segment(live, ticker, at - MINUTE, row)
    return LakeSource(
        Mirror(bucket or FolderBucket(lake), tmp_path / "mirror"),
        SETTINGS,
        live=FolderBucket(live),
        clock=Clock(),
        timer=Timer(),
        measured=False,
        kinds=PRICE_KINDS,
        quote_kinds=KINDS,
        splits=splits,
    )


AT = TABLE.session(DAY).close - EARLY


# 3. Under a covered row


def test_3_a_decision_under_a_covered_row_forecasts_and_names_what_covered_each_pair(
    lake: Path, tmp_path: Path
) -> None:
    covering = recorded.record(
        recorded.covered("GLD", WINDOW[0], DAY, line=1),
        recorded.covered("USO", WINDOW[0], DAY, line=2),
        recorded.split("USO", date(2020, 4, 29), 1, 8, line=3),
    )
    found = decide(lake, tmp_path, DAY, splits=covering)
    assert found.data.halt is None and found.alpha.halt is None and found.forecast is not None
    read = found.snapshot.splits
    assert read.sha256 == recorded.SHA
    assert read.rows == {GLD: (), USO: (covering.rows[2],)}
    by = {(c.instrument, c.earlier, c.later): c for c in read.pairs}
    for leg, row in ((GLD, covering.rows[0]), (USO, covering.rows[1])):
        for p, s in zip(WINDOW, WINDOW[1:], strict=False):
            assert by[(leg, p, s)].covered == row and by[(leg, p, s)].source is None
        own = by[(leg, WINDOW[-1], DAY)]
        assert own.covered == row and own.source == QUOTE
        assert own.close_prices == (repr(float(WALKS[str(leg)][WINDOW[-1]])),)
    assert len(read.pairs) == 2 * len(WINDOW)


# 4. Past the covered row, the sealed days


def test_4_past_the_covered_row_each_sealed_day_covers_its_pair(lake: Path, tmp_path: Path) -> None:
    found = decide(lake, tmp_path, DAY, splits=SHORT)
    assert found.data.halt is None and found.alpha.halt is None and found.forecast is not None
    by = {(c.instrument, c.later): c for c in found.snapshot.splits.pairs}
    for leg in (GLD, USO):
        for day in SEALED_DAYS:
            cover = by[(leg, day)]
            assert cover.covered is None and cover.source == SEALED
            assert cover.partition == f"quotes/ticker={leg}/date={day}.parquet"
            assert cover.partition_sha256 is not None and len(cover.partition_sha256) == 64
        assert by[(leg, LAST_COVERED)].covered is not None


def test_4_the_reading_counts_the_days_opened_and_a_second_read_opens_none(
    lake: Path, tmp_path: Path
) -> None:
    read = source(lake, tmp_path)
    first = read.read(AT)
    assert first.halts == () and first.split_days == 40
    again = read.read(AT)
    assert again.halts == () and again.split_days == 0
    assert again.snapshot.splits == first.snapshot.splits


def test_4_the_same_decision_staged_again_in_the_run_stores_no_new_attempt(
    lake: Path, tmp_path: Path
) -> None:
    table, storage = table_of(TRACKED), store(tmp_path / "store")
    stage = DataStage(
        source(lake, tmp_path),
        storage,
        Clock(),
        deployment=DEPLOYMENT,
        run=RUN,
        release=RELEASE,
        config_hashes=table.hashes_for("data"),
    )
    assert stage(Item(AT, "data"), Reports()).halt is None
    again = stage(Item(AT, "data"), Reports())
    assert again.halt is None and len(again.outputs) == 1


def test_4_one_sealed_day_with_no_data_row_halts_naming_the_pair(
    lake: Path, tmp_path: Path
) -> None:
    root = mine(lake, tmp_path)
    gap = SEALED_DAYS[5]
    partition(root, "USO", gap_close_row("USO", TABLE.session(gap).close), day=gap)
    reason = decide(root, tmp_path, DAY, splits=SHORT).data.halt.reason
    assert f"USO on the sessions {before(gap)} and {gap}" in reason
    assert f"A covered row reaching {gap} releases it" in reason
    assert "GLD" not in reason


def test_4_an_error_from_the_bucket_inside_stated_close_halts_as_a_fault(
    lake: Path, tmp_path: Path
) -> None:
    from tests.test_data_source import Counting

    bucket = Counting(FolderBucket(lake))
    bucket.refuse[f"quotes/ticker=USO/date={SEALED_DAYS[3]}.parquet"] = OSError("the bucket")
    reading = source(lake, tmp_path, bucket=bucket).read(AT)
    (reason,) = [r for r in reading.halts if "record of splits reads" in r]
    assert f"quotes/ticker=USO/date={SEALED_DAYS[3]}.parquet" in reason
    assert "could not be linked" in reason


def test_4_a_quarantine_or_an_unreadable_day_is_a_fault_and_a_gap_row_reads_as_no_row(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lake.loader import NoSpotClose, PartitionQuarantined

    for error, said in (
        (PartitionQuarantined("q", [{"reason": "a test"}]), "is quarantined"),
        (ValueError("the path /x"), "could not be read: ValueError"),
        (NoSpotClose("USO", "2027-01-01", 1), None),
    ):

        def raising(*args: object, error: Exception = error, **kwargs: object) -> object:
            raise error

        monkeypatch.setattr(sealed_module, "load_quotes", raising)
        root = tmp_path / type(error).__name__
        root.mkdir()
        reading = source(lake, root).read(AT)
        if said is None:
            assert any("does not reach" in r for r in reading.halts)
            assert not any("record of splits reads" in r for r in reading.halts)
        else:
            assert any(said in r and "/x" not in r for r in reading.halts)


# 5. Which quote each pair reads


def test_5_the_decisions_own_pair_reads_its_quote_and_never_a_sealed_day(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a backtest the decision's own day is sealed, holding a close the cut never saw."""
    root = mine(lake, tmp_path)
    days: list[date] = []
    real = sealed_module.stated_close

    def counted(mirror, root_, ticker, day):  # type: ignore[no-untyped-def]
        days.append(day)
        return real(mirror, root_, ticker, day)

    import ithildinruntime.data.source as source_module

    monkeypatch.setattr(source_module, "stated_close", counted)
    for ticker, walk in WALKS.items():
        mid, prior = walk[DAY], float(walk[before(DAY)])
        cut = lakes.quote_row(
            ticker,
            AT - MINUTE,
            bid=float(mid - Decimal("0.01")),
            ask=float(mid + Decimal("0.01")),
            last=float(mid),
            close_price=prior,
        )
        late = {**close_row(ticker, TABLE.session(DAY).close), "close_price": prior * 3}
        partition(root, ticker, cut, late, day=DAY)
    found = decide(root, tmp_path, DAY, splits=SHORT)
    assert found.data.halt is None and not found.snapshot.provisional
    assert DAY not in days and sorted(set(days)) == SEALED_DAYS
    own = [c for c in found.snapshot.splits.pairs if c.later == DAY]
    assert {c.source for c in own} == {QUOTE}


def test_5_a_sealed_day_whose_rows_state_two_values_halts_and_so_does_its_redelivery(
    lake: Path, tmp_path: Path
) -> None:
    root = mine(lake, tmp_path)
    day = SEALED_DAYS[7]
    close = TABLE.session(day).close
    one = float(WALKS["GLD"][before(day)])
    rows = [
        {**close_row("GLD", close), "close_price": one},
        {**close_row("GLD", close), "close_price": one + 1},
    ]
    partition(root, "GLD", *rows, day=day)
    found = decide(root, tmp_path, DAY, splits=SHORT, redeliver=True)
    assert "states 2 values of the close" in found.data.halt.reason
    assert "states 2 values of the close" in found.redelivered.halt.reason


# 6. A split with no row, through the read


@pytest.mark.parametrize("ratio", [Decimal(2), Decimal("0.5"), Decimal("0.125")])
@pytest.mark.parametrize("restated", [True, False])
def test_6_a_split_with_no_row_halts_through_the_read(
    lake: Path, tmp_path: Path, ratio: Decimal, restated: bool
) -> None:
    root = mine(lake, tmp_path)
    effective = SEALED_DAYS[10]
    template = root / lakes.partition_key("USO", WINDOW[0])
    walk = dict(WALKS["USO"])
    for day in [d for d in WINDOW if d >= effective] + [DAY]:
        walk[day] = (WALKS["USO"][day] / ratio).quantize(Decimal("0.01"))
        if day != DAY:
            made._capture(root, "USO", day, walk[day], template)
    stating = dict(walk)
    if restated:
        # Schwab states the session before on the new scale, on the split's own session.
        stating[before(effective)] = (WALKS["USO"][before(effective)] / ratio).quantize(
            Decimal("0.01")
        )
    seal_closes(root, "USO", [d for d in SEALED_DAYS if d >= effective], stating)
    found = decide(
        root, tmp_path, DAY, splits=SHORT, mids={"USO": walk[DAY]}, walks={**WALKS, "USO": walk}
    )
    reason = found.data.halt.reason
    said = "outside 0.95 to 1.05" if restated else "only a quote covers"
    assert said in reason and f"{effective}" in reason
    assert found.alpha.outputs == ()
    # Once its row is known, the decision halts on the row instead.
    known = recorded.record(*SHORT.rows, recorded.split("USO", effective, 2, 1, line=3))
    row_reason = decide(
        root,
        tmp_path / "known",
        DAY,
        splits=known,
        mids={"USO": walk[DAY]},
        walks={**WALKS, "USO": walk},
    ).data.halt.reason
    assert "a recorded split of 2 new shares for 1 old" in row_reason
    assert said not in row_reason


# 8. A recorded split, through the stage


def test_8_a_split_row_halts_every_decision_on_its_instrument_and_keeps_its_orders(
    lake: Path, tmp_path: Path
) -> None:
    row = recorded.split("USO", DAY, 1, 8, line=4)
    found = decide(lake, tmp_path, DAY, splits=recorded.record(*SHORT.rows, row), redeliver=True)
    assert "USO has a recorded split of 1 new shares for 8 old" in found.data.halt.reason
    assert "line 4" in found.redelivered.halt.reason
    # A halt is data's result, which stops alpha, and no stage sends a cancel for it.
    assert found.alpha.outputs == ()


# 11. A redelivery that differs only in its read of the record


def test_11_differences_names_splits_and_compares_every_field() -> None:
    import dataclasses

    names = {f.name for f in dataclasses.fields(Snapshot)}
    assert set(COMPARED) == names - {"decision_at", "bars", "quotes_read_at", "clock"}


def test_11_a_redelivery_whose_read_differs_only_in_splits_names_them(
    lake: Path, tmp_path: Path
) -> None:
    from ithildinruntime.core import BlobHeader, OutputFile, encode_output

    found = decide(lake, tmp_path, DAY, splits=SHORT)
    header = BlobHeader(
        5, DEPLOYMENT, RUN, "data", "capturing", AT, {}, RELEASE, {"chan": "c" * 64}
    )
    one = encode_output(Snapshot, OutputFile(header, (found.snapshot,), False))
    other = replace(found.snapshot, splits=replace(found.snapshot.splits, sha256="b" * 64))
    two = encode_output(Snapshot, OutputFile(header, (other,), False))
    assert differences(one, two, measured=False) == "These differ: splits"


# 12. A replay after the file gains a row


def test_12_a_stored_decision_judged_after_the_file_gains_a_row_decides_as_it_did(
    lake: Path, tmp_path: Path
) -> None:
    from ithildinruntime.data.source import judge

    found = decide(lake, tmp_path, DAY, splits=SHORT)
    assert found.data.halt is None
    # The replay judges the stored snapshot, whose rows are those the decision read.
    assert judge(found.snapshot, SETTINGS.instruments, measured=False) == ()


# 15. The injected faults


def test_15_a_missing_record_halts_every_decision_through_the_stage(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ithildinruntime.data import splits as splits_module

    monkeypatch.setattr(splits_module, "SPLITS", tmp_path / "gone.csv")
    monkeypatch.setattr(splits_module.load_splits, "__defaults__", (tmp_path / "gone.csv",))
    made_source = LakeSource(
        Mirror(FolderBucket(lake), tmp_path / "mirror"),
        SETTINGS,
        live=FolderBucket(tmp_path),
        clock=Clock(),
        timer=Timer(),
        measured=False,
    )
    with pytest.raises(Halt, match="splits.csv cannot be read"):
        made_source.read(AT)


def test_15_a_bad_row_halts_every_decision(lake: Path, tmp_path: Path, monkeypatch) -> None:
    from ithildinruntime.data import splits as splits_module

    bad = tmp_path / "splits.csv"
    bad.write_text(",".join(splits_module.HEADER) + "\n2026-11-27,splat,USO,2020-04-29,,1,8,x,y\n")
    monkeypatch.setattr(splits_module.load_splits, "__defaults__", (bad,))
    made_source = LakeSource(
        Mirror(FolderBucket(lake), tmp_path / "mirror"),
        SETTINGS,
        live=FolderBucket(tmp_path),
        clock=Clock(),
        timer=Timer(),
        measured=False,
    )
    with pytest.raises(Halt, match="splits.csv line 2 is of kind 'splat'"):
        made_source.read(AT)
