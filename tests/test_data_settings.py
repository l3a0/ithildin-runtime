"""Data's settings, read from each strategy's tracked file.

The number in each test's name is its acceptance test on #110. Tests 8 to 10
sit in tests/test_data_stage.py, tests/test_data_spikes.py and
tests/test_data_source.py, beside the code they read.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderBucket, FolderStore, HistoryClock
from ithildinruntime.core import (
    STRATEGIES,
    Halt,
    Mode,
    Plan,
    PlanDecision,
    Retry,
    Snapshot,
    Storage,
    decode_output,
    load_strategies,
    parse_strategy,
    result_key,
)
from ithildinruntime.data import DataSettings, QuoteLimits, data_settings, source
from ithildinruntime.data.spikes import MOVE
from ithildinruntime.runtime.data_worker import DATA_ROW, data_handler
from ithildinruntime.runtime.loop import Item, ReceiveLoop, Table
from tests.support import lake as lakes
from tests.support.queues import Folder, SystemTimer, running
from tests.support.queues import storage as loop_storage
from tests.test_cost import tracked
from tests.test_data_source import FIRST, GLD, LAST, USO, Timer, before, close, unlist
from tests.test_data_stage import DEPLOYMENT, RUN, Reports, key
from tests.test_storage_contract import Alarms

CHAN = (STRATEGIES / "chan_bollinger.toml").read_bytes()
GOOD = b"""instruments = ["GLD", "USO"]
[data]
lookback = 20
stale_lag = 1
spike_multiple = 7
[data.first_sessions]
GLD = 2006-04-26
USO = 2006-04-27
"""


def parsed(name: str, data: bytes = GOOD):
    return parse_strategy(name, data)


def edit(data: bytes, old: str, new: str) -> bytes:
    assert data.count(old.encode()) == 1, old
    return data.replace(old.encode(), new.encode())


# 1. The tracked files.


@pytest.mark.parametrize("name", tracked())
def test_1_every_tracked_strategys_data_section_builds_settings(name: str) -> None:
    (strategy,) = load_strategies(STRATEGIES, [name])
    made = data_settings([strategy])
    assert made.instruments == strategy.instruments
    # docs/architecture/06-runtime.md's `### A missing or stale bar` sets it.
    assert made.stale_lag == 0


def test_1_chans_file_holds_the_ruled_settings() -> None:
    (chan,) = load_strategies(STRATEGIES, ["chan_bollinger"])
    made = data_settings([chan])
    assert made == DataSettings(
        instruments=(GLD, USO),
        lookback=60,
        first_sessions={GLD: date(2006, 4, 26), USO: date(2006, 4, 26)},
        stale_lag=0,
        spike_multiple=Decimal(10),
        # The owner's values on #133.
        quotes={i: QuoteLimits(180, Decimal("0.001")) for i in (GLD, USO)},
    )
    assert type(made.spike_multiple) is Decimal and str(made.spike_multiple) == "10"
    assert all(str(q.max_spread_share) == "0.001" for q in made.quotes.values())


# 2. Each refusal.

KEYS = {
    "lookback": "lookback = 20\n",
    "stale_lag": "stale_lag = 1\n",
    "spike_multiple": "spike_multiple = 7\n",
    "first_sessions": "[data.first_sessions]\nGLD = 2006-04-26\nUSO = 2006-04-27\n",
}


def refused(data: bytes) -> str:
    with pytest.raises(Halt) as caught:
        data_settings([parse_strategy("s", data, source="strategies/s.toml")])
    assert caught.value.reason.startswith("strategy s's configuration strategies/s.toml has ")
    return caught.value.reason


def test_2_no_section_or_an_empty_one_halts() -> None:
    head = b'instruments = ["GLD", "USO"]\n'
    for data in (head, head + b"[data]\n", head + b"[data.first_sessions]\n"):
        assert "has no [data] section, or an empty one" in refused(data)


@pytest.mark.parametrize("name", KEYS)
def test_2_a_missing_key_halts_naming_it(name: str) -> None:
    reason = refused(edit(GOOD, KEYS[name], ""))
    assert f"a [data] section that states no {name}" in reason


def test_2_every_missing_key_is_named() -> None:
    reason = refused(b'instruments = ["GLD"]\n[data]\nlookback = 20\n')
    assert reason.endswith("states no stale_lag, spike_multiple, first_sessions")


def test_2_an_unknown_key_halts_naming_it() -> None:
    reason = refused(edit(GOOD, "lookback = 20", "lookback = 20\nlook_back = 20\nlag = 1"))
    assert "holds lag, look_back, which this form does not have" in reason


@pytest.mark.parametrize(
    ("name", "bad"),
    [("lookback", b) for b in ("0", "-1", "true", "1.5", '"60"')]
    + [("stale_lag", b) for b in ("-1", "true", "0.5", '"0"')]
    + [("spike_multiple", b) for b in ("0", "-1", "0.0", "-0.5", "true", '"10"')],
)
def test_2_a_bad_number_halts_naming_the_key(name: str, bad: str) -> None:
    old = KEYS[name].strip()
    assert f"has {name} = " in refused(edit(GOOD, old, f"{name} = {bad}"))


@pytest.mark.parametrize(
    ("old", "new", "said"),
    [
        ("GLD = 2006-04-26\nUSO = 2006-04-27\n", "", "states no first_sessions"),
        ("USO = 2006-04-27\n", "", "states no first_sessions date for USO"),
        (
            "GLD = 2006-04-26\nUSO = 2006-04-27\n",
            "SPY = 2006-01-03\n",
            "states no first_sessions date for GLD, USO",
        ),
        (
            "USO = 2006",
            "SPY = 2006-01-03\nQQQ = 2006-01-03\nUSO = 2006",
            "holds a first_sessions date for QQQ, SPY, which the strategy does not trade",
        ),
        (
            "USO = 2006",
            "SPY = 2006-01-03\nUSO = 2006",
            "holds a first_sessions date for SPY, which the strategy does not trade",
        ),
        ("2006-04-27", "2006-04-27T00:00:00Z", "has first_sessions.USO = datetime"),
        ("2006-04-26", "2006-04-26T00:00:00Z", "has first_sessions.GLD = datetime"),
        ("2006-04-27", '"2006-04-27"', "has first_sessions.USO = '2006-04-27'"),
        ("2006-04-27", "20060427", "has first_sessions.USO = 20060427"),
    ],
)
def test_2_bad_first_sessions_halt(old: str, new: str, said: str) -> None:
    assert said in refused(edit(GOOD, old, new))


@pytest.mark.parametrize("bad", ["2006-04-26", '"GLD"', "[1]"])
def test_2_first_sessions_that_are_no_table_halt(bad: str) -> None:
    data = edit(GOOD, KEYS["first_sessions"], f"first_sessions = {bad}\n")
    assert "which is not a table" in refused(data)


@pytest.mark.parametrize(
    ("name", "value", "read"),
    [("lookback", "1", 1), ("stale_lag", "0", 0), ("spike_multiple", "0.5", Decimal("0.5"))],
)
def test_2_each_least_value_passes(name: str, value: str, read: object) -> None:
    made = data_settings([parsed("s", edit(GOOD, KEYS[name].strip(), f"{name} = {value}"))])
    assert getattr(made, name) == read


def test_2_every_setting_is_a_key_a_file_writes_and_none_has_a_default() -> None:
    """A setting outside the files would move no hash."""
    fields = dataclasses.fields(DataSettings)
    assert {f.name for f in fields} == {"instruments", "quotes", *KEYS}
    assert all(f.default is f.default_factory is dataclasses.MISSING for f in fields)


def test_2_a_check_of_the_settings_own_halts_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """So a `ValueError` the reader does not foresee halts a decision, and stops no worker."""

    def stricter(self) -> None:
        raise ValueError("a check the reader does not make")

    monkeypatch.setattr(DataSettings, "__post_init__", stricter)
    with pytest.raises(Halt) as caught:
        data_settings([parsed("b"), parsed("a")])
    assert caught.value.reason == (
        "data's settings from a.toml, b.toml are refused: a check the reader does not make"
    )


# 2. The quote maxima, which are made up.

QUOTES = "[data.quotes]\nmax_age_seconds = 90\nmax_spread_share = 0.02\n"
QUOTED = GOOD + QUOTES.encode()
NINETY = QuoteLimits(90, Decimal("0.02"))


def test_2_the_quote_maxima_hold_for_every_instrument_the_file_lists() -> None:
    made = data_settings([parsed("s", QUOTED)])
    assert made.quotes == {GLD: NINETY, USO: NINETY}
    assert type(made.quotes[GLD].max_age) is int
    assert str(made.quotes[GLD].max_spread_share) == "0.02"


def test_2_a_file_with_no_quote_table_or_a_blank_one_writes_no_maximum() -> None:
    assert data_settings([parsed("s")]).quotes == {}
    assert data_settings([parsed("s", GOOD + b"[data.quotes]\n")]).quotes == {}


@pytest.mark.parametrize("name", ["max_age_seconds", "max_spread_share"])
def test_2_one_quote_maximum_without_the_other_halts_naming_it(name: str) -> None:
    (line,) = [row for row in QUOTES.splitlines(keepends=True) if row.startswith(name)]
    reason = refused(edit(QUOTED, line, ""))
    assert reason.endswith(f"states no quotes.{name} beside the other")


def test_2_a_quote_key_this_form_lacks_halts_naming_it() -> None:
    reason = refused(QUOTED + b"max_size = 1\nage = 2\n")
    assert reason.endswith("holds quotes.age, max_size, which this form does not have")


@pytest.mark.parametrize("bad", ["1", '"x"', "true", "[1]"])
def test_2_quotes_that_are_no_table_halt(bad: str) -> None:
    data = edit(GOOD, "lookback = 20\n", f"lookback = 20\nquotes = {bad}\n")
    assert "has quotes = " in refused(data) and "which is not a table" in refused(data)


@pytest.mark.parametrize("bad", ["0", "-1", "9", "true", "90.0", '"90"', str(2**53)])
def test_2_a_maximum_age_out_of_bounds_halts(bad: str) -> None:
    reason = refused(edit(QUOTED, "seconds = 90", f"seconds = {bad}"))
    assert "has under quotes a maximum age of " in reason
    assert reason.endswith(f"not a whole number of seconds from 10 to below {2**53}")


@pytest.mark.parametrize("bad", ["0", "1", "0.0", "1.0", "-0.5", "true", '"0.5"'])
def test_2_a_maximum_spread_out_of_bounds_halts(bad: str) -> None:
    reason = refused(edit(QUOTED, "share = 0.02", f"share = {bad}"))
    assert "has under quotes a maximum spread of " in reason
    assert reason.endswith("which is not a decimal above 0 and below 1")


@pytest.mark.parametrize(("age", "share"), [("10", "0.0001"), (str(2**53 - 1), "0.9999")])
def test_2_each_least_and_most_quote_maximum_passes(age: str, share: str) -> None:
    data = edit(edit(QUOTED, "seconds = 90", f"seconds = {age}"), "0.02", share)
    assert data_settings([parsed("s", data)]).quotes[GLD] == QuoteLimits(int(age), Decimal(share))


SPY_GLD = b"""instruments = ["SPY", "GLD"]
[data]
lookback = 20
stale_lag = 1
spike_multiple = 7
[data.first_sessions]
SPY = 1993-01-29
GLD = 2006-04-26
"""


def test_3_strategies_that_share_an_instrument_and_agree_on_its_maxima_build() -> None:
    made = data_settings([parsed("a", QUOTED), parsed("b", SPY_GLD + QUOTES.encode())])
    assert made.quotes == {i: NINETY for i in ("GLD", "USO", "SPY")}


@pytest.mark.parametrize(
    ("old", "new", "said"),
    [
        ("seconds = 90", "seconds = 91", "91 seconds and 0.02"),
        ("0.02", "0.03", "90 seconds and 0.03"),
        ("0.02", "0.020", "90 seconds and 0.020"),
        (QUOTES, "", "no maximum"),
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_3_strategies_that_share_an_instrument_and_differ_on_its_maxima_halt(
    old: str, new: str, said: str, reverse: bool
) -> None:
    other = edit(SPY_GLD + QUOTES.encode(), old, new)
    made = [parsed("a", QUOTED), parsed("b", other)]
    with pytest.raises(Halt) as caught:
        data_settings(reversed(made) if reverse else made)
    assert "different values of [data] quotes for GLD" in caught.value.reason
    assert caught.value.reason.endswith(f": a writes 90 seconds and 0.02, b writes {said}")


def test_3_strategies_that_share_no_instrument_keep_their_own_maxima() -> None:
    other = one_of("SPY", 27) + b"[data.quotes]\nmax_age_seconds = 45\nmax_spread_share = 0.5\n"
    made = data_settings([parsed("a", QUOTED), parsed("b", other), parsed("c", one_of("QQQ", 26))])
    assert made.quotes == {
        GLD: NINETY,
        USO: NINETY,
        "SPY": QuoteLimits(45, Decimal("0.5")),
    }


@pytest.mark.parametrize("name", tracked())
def test_1_every_tracked_strategys_quote_maxima_are_whole_or_absent(name: str) -> None:
    """The values are the owner's, on #133, and a file holds both or neither."""
    (strategy,) = load_strategies(STRATEGIES, [name])
    quotes = data_settings([strategy]).quotes
    assert set(quotes) in (set(), set(strategy.instruments))


# 3. Several strategies.

OTHER = b"""instruments = ["SPY", "GLD"]
[data]
lookback = 20
stale_lag = 1
spike_multiple = 7
[data.first_sessions]
SPY = 1993-01-29
GLD = 2006-04-26
"""


def test_3_equal_settings_build_in_the_order_of_the_strategies_names() -> None:
    one, two = parsed("a_one"), parsed("b_two", OTHER)
    made = data_settings([two, one])
    assert made == data_settings([one, two])
    assert made.instruments == ("GLD", "USO", "SPY")
    assert made.first_sessions == {
        "GLD": date(2006, 4, 26),
        "USO": date(2006, 4, 27),
        "SPY": date(1993, 1, 29),
    }
    assert (made.lookback, made.stale_lag, made.spike_multiple) == (20, 1, Decimal(7))


def test_3_a_multiple_is_compared_as_written_because_a_flags_note_prints_it() -> None:
    made = [parsed("a"), parsed("b", edit(GOOD, "multiple = 7", "multiple = 7.0"))]
    with pytest.raises(Halt, match=r"spike_multiple.*a writes 7, b writes 7\.0$"):
        data_settings(made)
    both = edit(GOOD, "multiple = 7", "multiple = 7.0")
    assert str(data_settings([parsed("a", both), parsed("b", both)]).spike_multiple) == "7.0"


def test_3_the_order_is_the_instruments_lists_and_not_the_first_sessions() -> None:
    swapped = edit(
        GOOD, "GLD = 2006-04-26\nUSO = 2006-04-27\n", "USO = 2006-04-27\nGLD = 2006-04-26\n"
    )
    assert data_settings([parsed("a", swapped)]).instruments == ("GLD", "USO")


def test_3_distinct_instruments_with_different_first_sessions_build() -> None:
    made = data_settings([parsed("a", one_of("GLD", 26)), parsed("b", one_of("SPY", 27))])
    assert made.first_sessions == {"GLD": date(2006, 4, 26), "SPY": date(2006, 4, 27)}


def one_of(instrument: str, day: int) -> bytes:
    return (
        f'instruments = ["{instrument}"]\n[data]\nlookback = 20\nstale_lag = 1\n'
        f"spike_multiple = 7\n[data.first_sessions]\n{instrument} = 2006-04-{day}\n"
    ).encode()


SCALARS = [("lookback", 20, 21), ("stale_lag", 1, 0), ("spike_multiple", 7, 8)]


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(("name", "old", "new"), SCALARS)
def test_3_each_scalar_that_differs_halts(
    count: int, reverse: bool, name: str, old: int, new: int
) -> None:
    changed = edit(OTHER, f"{name} = {old}", f"{name} = {new}")
    made = [parsed(n) for n in ("a", "b")[: count - 1]] + [parsed("z", changed)]
    with pytest.raises(Halt) as caught:
        data_settings(reversed(made) if reverse else made)
    assert f"[data] {name}" in caught.value.reason
    said = [f"{s.name} writes {old}" for s in made[:-1]] + [f"z writes {new}"]
    assert caught.value.reason.endswith(": " + ", ".join(said))


def test_3_a_shared_instrument_with_two_first_sessions_halts() -> None:
    changed = edit(OTHER, "GLD = 2006-04-26", "GLD = 2006-04-28")
    for made in (
        [parsed("a"), parsed("b", changed)],
        [parsed("a"), parsed("b"), parsed("c", changed)],
    ):
        with pytest.raises(Halt) as caught:
            data_settings(made)
        assert "[data] first_sessions.GLD" in caught.value.reason
        said = [f"{s.name} writes 2006-04-26" for s in made[:-1]]
        said.append(f"{made[-1].name} writes 2006-04-28")
        assert caught.value.reason.endswith(": " + ", ".join(said))


def test_3_no_strategy_halts() -> None:
    with pytest.raises(Halt, match="declares no strategy"):
        data_settings([])


# 4 to 6. Through the entry point.


def handles(lake: Path, where: Path, *strategies, store: Path | None = None):
    """The entry point's handler for these strategies, over a store in a folder."""
    return data_handler(
        bucket=FolderBucket(lake),
        live=FolderBucket(lake.with_name("no-live")),
        mirror=where / "mirror",
        storage=Storage(
            FolderStore(store or where / "store"),
            timer=Timer(),
            pause=lambda s: None,
            retry=Retry(deadline=timedelta(seconds=1), pause=0.1, longest_pause=0.1),
            alerts=Alarms(),
            owner_step="none",
        ),
        clock=HistoryClock(readings=None),
        timer=Timer(),
        deployment=DEPLOYMENT,
        run=RUN,
        release="r1",
        table=Table(stages=(DATA_ROW,), strategies=strategies),
        measured=False,
    )


def run(lake: Path, where: Path, *strategies, at: date = LAST, store: Path | None = None):
    """One decision through `data_handler`: its result, its snapshot and its header."""
    root = store or where / "store"
    handler = handles(lake, where, *strategies, store=root)
    result = handler(Item(close(at), "data"), Reports())
    stored = decode_output((root / result.outputs[-1].key).read_bytes(), Snapshot)
    assert dict(stored.header.config_hashes) == dict(result.header.config_hashes)
    assert dict(stored.header.config_hashes) == {s.name: s.config_hash for s in strategies}
    return result, stored.entries[0], dict(stored.header.config_hashes)


def counts(snapshot: Snapshot) -> dict[str, int]:
    return {str(i): len(b["1d"]) for i, b in snapshot.bars.items()}


def moves(snapshot: Snapshot) -> set:
    return {(i, f.session) for i, fs in snapshot.flags.items() for f in fs if f.rule == MOVE}


def changed(old: str, new: str):
    return parse_strategy("chan_bollinger", edit(CHAN, old, new))


def chan():
    return parse_strategy("chan_bollinger", CHAN)


def test_4_a_lookback_moves_the_hash_and_the_bars(lake: Path, tmp_path: Path) -> None:
    _, first, old = run(lake, tmp_path / "a", chan())
    _, second, new = run(lake, tmp_path / "b", changed("lookback = 60", "lookback = 59"))
    assert old["chan_bollinger"] != new["chan_bollinger"]
    assert counts(first) == {"GLD": 60, "USO": 60}
    assert counts(second) == {"GLD": 59, "USO": 59}


@pytest.mark.parametrize(("leg", "other"), [("GLD", "USO"), ("USO", "GLD")])
def test_4_a_first_session_moves_the_hash_and_one_legs_bars(
    lake: Path, tmp_path: Path, leg: str, other: str
) -> None:
    _, first, old = run(lake, tmp_path / "a", chan(), at=date(2006, 6, 21))
    # A lake whose series of that leg starts there, so the later date cuts nothing it lists.
    for day in (date(2006, 4, 26), date(2006, 4, 27), date(2006, 4, 28)):
        unlist(lake, lakes.partition_key(leg, day))
    later = changed(f"{leg} = 2006-04-26", f"{leg} = 2006-05-01")
    result, second, new = run(lake, tmp_path / "b", later, at=date(2006, 6, 21))
    assert old["chan_bollinger"] != new["chan_bollinger"]
    assert counts(first) == {"GLD": 40, "USO": 40}
    assert counts(second) == {leg: 37, other: 40}
    assert result.halt is None


@pytest.mark.parametrize("legs", [("GLD",), ("USO",), ("GLD", "USO")])
def test_4_a_stale_lag_moves_the_hash_and_the_halt(
    lake: Path, tmp_path: Path, legs: tuple[str, ...]
) -> None:
    for leg in legs:
        unlist(lake, lakes.partition_key(leg, LAST))
    halted, first, old = run(lake, tmp_path / "a", chan())
    passed, second, new = run(lake, tmp_path / "b", changed("stale_lag = 0", "stale_lag = 1"))
    assert old["chan_bollinger"] != new["chan_bollinger"]
    assert halted.halt is not None and "past the written lag of 0" in halted.halt.reason
    assert passed.halt is None
    assert {i for i, s in first.status.items() if s == "stale"} == set(legs)
    assert set(second.status.values()) == {"fresh"}


def test_4_a_spike_multiple_moves_the_hash_and_the_flags(lake: Path, tmp_path: Path) -> None:
    results = {}
    for m in ("5", "6"):
        result, snap, hashes = run(
            lake, tmp_path / m, changed("spike_multiple = 10", f"spike_multiple = {m}")
        )
        assert result.halt is None
        results[m] = (moves(snap), hashes["chan_bollinger"])
    assert results["5"][0] == {(GLD, date(2012, 2, 29))} and results["6"][0] == set()
    assert results["5"][1] != results["6"][1]


SPY = b"""instruments = ["SPY"]
[data]
lookback = 60
stale_lag = 0
spike_multiple = 10
[data.first_sessions]
SPY = 1993-01-29
"""


def test_4_a_second_strategys_first_session_moves_its_hash_alone(
    lake: Path, tmp_path: Path
) -> None:
    one = parse_strategy("second", SPY)
    two = parse_strategy("second", edit(SPY, "1993-01-29", "2030-01-02"))
    a, first, old = run(lake, tmp_path / "a", chan(), one)
    b, second, new = run(lake, tmp_path / "b", chan(), two)
    assert old["chan_bollinger"] == new["chan_bollinger"] and old["second"] != new["second"]
    assert counts(first) == counts(second) == {"GLD": 60, "USO": 60}
    assert "SPY missing for the decision: no bar from 2012-01-12" in a.halt.reason
    assert "SPY missing for the decision: no lookback session" in b.halt.reason


# 5.


def test_5_chans_tracked_file_reads_60_bars_and_40(lake: Path, tmp_path: Path) -> None:
    (tracked_chan,) = load_strategies(STRATEGIES, ["chan_bollinger"])
    for at, n in (
        (LAST, 60),
        (date(2006, 6, 21), 40),
        (date(2006, 7, 19), 59),
        (date(2006, 7, 20), 60),
    ):
        result, snap, _ = run(lake, tmp_path / at.isoformat(), tracked_chan, at=at)
        assert result.halt is None
        assert counts(snap) == {"GLD": n, "USO": n}
        assert min(b.session for b in snap.bars[GLD]["1d"]) >= FIRST
    assert USO in snap.bars


# 6.


@pytest.mark.parametrize(("one", "two"), [("1", "0"), ("0", "1")])
def test_6_a_redelivery_under_another_lag_halts_naming_both_hashes(
    lake: Path, tmp_path: Path, one: str, two: str
) -> None:
    unlist(lake, lakes.partition_key("USO", LAST))
    wrote = changed("stale_lag = 0", f"stale_lag = {one}\n# wrote")
    reads = changed("stale_lag = 0", f"stale_lag = {two}\n# reads")
    store = tmp_path / "store"
    first, _, _ = run(lake, tmp_path / "a", wrote, store=store)
    assert (first.halt is None) is (one == "1")
    again, _, _ = run(lake, tmp_path / "b", reads, store=store)
    assert [o.key for o in again.outputs] == [key(), key(2)]
    assert again.halt is not None and again.halt.cause == again.outputs[0]
    assert again.halt.reason == (
        f"{key()} counts and was written under configuration hashes "
        f"{{'chan_bollinger': '{wrote.config_hash}'}}, not this stage's "
        f"{{'chan_bollinger': '{reads.config_hash}'}}, so it cannot be judged"
    )


def test_6_a_redelivery_under_a_refused_section_halts_and_leaves_the_first_capture(
    lake: Path, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    run(lake, tmp_path / "a", chan(), store=store)
    held = {p: p.read_bytes() for p in store.rglob("*") if p.is_file()}
    assert store / key() in held
    refused = handles(lake, tmp_path / "b", changed("stale_lag = 0", "stale_lag = -1"), store=store)
    with pytest.raises(Halt, match="has stale_lag = -1, which is not a whole number of 0 or more"):
        refused(Item(close(LAST), "data"), Reports())
    assert {p: p.read_bytes() for p in store.rglob("*") if p.is_file()} == held


# 7.


def test_7_one_source_reads_each_session_once_for_every_decision_of_a_run(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two lookbacks share 59 sessions of each leg, so 61 are read of each."""
    reads: list[tuple[str, date]] = []
    real = source.load_bars

    def counted(ticker, *args, **kwargs):
        reads.append((ticker, kwargs["start"]))
        return real(ticker, *args, **kwargs)

    monkeypatch.setattr(source, "load_bars", counted)
    handler = handles(lake, tmp_path, chan())
    for at in (before(LAST), LAST):
        assert handler(Item(close(at), "data"), Reports()).halt is None
    assert len(reads) == len(set(reads)) == 2 * 61


class Waited(HistoryClock):
    def __init__(self) -> None:
        super().__init__(readings=None)
        self.waits: list = []

    def wait_until(self, at, report) -> None:
        self.waits.append((at, report))
        super().wait_until(at, report)


@pytest.mark.parametrize(
    ("strategies", "said"),
    [
        ((parse_strategy("chan", b'instruments = ["GLD"]\n'),), "has no [data] section"),
        (
            (parsed("a"), parsed("b", edit(GOOD, "stale_lag = 1", "stale_lag = 2"))),
            "write different values of [data] stale_lag",
        ),
    ],
)
def test_7_a_refused_section_halts_each_decision_through_the_loop(
    lake: Path, tmp_path: Path, strategies, said: str
) -> None:
    table = Table(stages=(DATA_ROW,), strategies=strategies)
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    alarms = Alarms()
    store = loop_storage(folder.store(), alarms)
    clock = Waited()
    handler = data_handler(
        bucket=FolderBucket(lake),
        live=FolderBucket(lake.with_name("no-live")),
        mirror=tmp_path / "mirror",
        storage=store,
        clock=clock,
        timer=SystemTimer(),
        deployment=DEPLOYMENT,
        run=RUN,
        release="r1",
        table=table,
        measured=False,
    )
    names = tuple(s.name for s in strategies)
    plan = Plan(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        mode=Mode.PAPER,
        release="r1",
        decisions=tuple(PlanDecision(close(LAST) + timedelta(minutes=n), names) for n in range(2)),
    )
    raised: list[Halt] = []

    def spy(item, report):
        try:
            return handler(item, report)
        except Halt as halt:
            raised.append(halt)
            raise

    made = ReceiveLoop(
        worker="data-1",
        component="data",
        table=table,
        plan=plan,
        queue=folder.queue("data"),
        storage=store,
        bucket=folder.bucket,
        handlers={"data": spy},
        alerts=alarms,
        timer=SystemTimer(),
        beat=lambda: None,
        tick=0.05,
        stop_timeout=5.0,
    )
    keys = [result_key(DEPLOYMENT, RUN, d.at, "data") for d in plan.decisions]
    with running({"data": made}) as started:
        assert started.wait_for(lambda: all((folder.root / k).exists() for k in keys), 20)
    for k, d in zip(keys, plan.decisions, strict=True):
        result = store.read_result(k, d.at)
        assert result is not None and result.halt is not None
        assert said in result.halt.reason and result.outputs == ()
        assert dict(result.header.config_hashes) == dict(table.hashes_for("data"))
    assert sorted(at for at, _ in clock.waits) == [d.at for d in plan.decisions]
    assert list(folder.root.rglob("snapshot*")) == []
    assert len(raised) == 2 and raised[0] is not raised[1]
