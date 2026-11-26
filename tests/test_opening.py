"""Opening an account, and the claim the send stage takes, for #146's acceptance 2 and item 12.

Every figure is made up. Each case reads back what the journal and the
simulated broker's log hold, so a half-made opening shows as one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import Halt, Mode, Retry, Storage, journal_entry_key, sim_account_key
from ithildinruntime.execution.brokers.simulated import SimulatedBroker
from ithildinruntime.execution.opening import open_account
from ithildinruntime.journal import Journal, OpeningEntry, record_of
from ithildinruntime.runtime.execution_worker import open_backtest
from tests.support.order_world import Alarms, Clock, Wired

AT = datetime(2026, 11, 2, 21, tzinfo=UTC)
ACCOUNT, CASH, WRITER = "bt-1", Decimal(1_000_000), "a" * 32


def opened(store) -> tuple[list[object], bool]:
    """The journal's entries, and whether the simulator's call 1 is stored."""
    entries = [record_of(e) for e in Journal(store, ACCOUNT).read()]
    return entries, sim_account_key(ACCOUNT, 1) in _keys(store)


def _keys(store) -> set[str]:
    return set(store.blobs) if hasattr(store, "blobs") else set(store.list("", None))


def test_an_opening_writes_both_halves_with_one_cash() -> None:
    store = Wired()
    open_account(store, SimulatedBroker.open, ACCOUNT, CASH, AT, WRITER)
    assert opened(store) == ([OpeningEntry(ACCOUNT, CASH)], True)


def test_the_same_opening_again_is_done_and_writes_nothing() -> None:
    store = Wired()
    open_account(store, SimulatedBroker.open, ACCOUNT, CASH, AT, WRITER)
    before = dict(store.blobs)
    open_account(store, SimulatedBroker.open, ACCOUNT, CASH, AT, "b" * 32)
    assert store.blobs == before


def test_another_cash_is_refused_and_appends_nothing() -> None:
    store = Wired()
    open_account(store, SimulatedBroker.open, ACCOUNT, CASH, AT, WRITER)
    before = dict(store.blobs)
    with pytest.raises(Halt, match="opens with another entry"):
        open_account(store, SimulatedBroker.open, ACCOUNT, CASH + 1, AT, WRITER)
    assert store.blobs == before


class _Killed(BaseException):
    pass


@pytest.mark.parametrize("first", ["sim", "journal"])
def test_a_kill_between_the_two_writes_is_finished_by_a_rerun(first) -> None:
    store = Wired()

    if first == "sim":
        # The simulator's half lands and the process dies before the journal's.
        def after(number: int, op: str, key: str) -> None:
            if op == "create" and key == sim_account_key(ACCOUNT, 1):
                raise _Killed

        store.after = after
        with pytest.raises(_Killed):
            open_account(store, SimulatedBroker.open, ACCOUNT, CASH, AT, WRITER)
        assert opened(store) == ([], True)
    else:
        # The journal's half stands alone, as an older order of the two writes left it.
        journal = Journal(store, ACCOUNT, WRITER)
        journal.record(journal.view(), OpeningEntry(ACCOUNT, CASH), AT)
        assert opened(store) == ([OpeningEntry(ACCOUNT, CASH)], False)
    store.after = lambda number, op, key: None
    open_account(store, SimulatedBroker.open, ACCOUNT, CASH, AT, "b" * 32)
    assert opened(store) == ([OpeningEntry(ACCOUNT, CASH)], True)


@pytest.mark.parametrize("first", ["sim", "journal"])
def test_a_half_with_another_cash_refuses_and_leaves_the_other_untouched(first) -> None:
    store = Wired()
    if first == "sim":
        SimulatedBroker.open(store, ACCOUNT, CASH + 1)
    else:
        journal = Journal(store, ACCOUNT, WRITER)
        journal.record(journal.view(), OpeningEntry(ACCOUNT, CASH + 1), AT)
    before = dict(store.blobs)
    with pytest.raises(Halt):
        open_account(store, SimulatedBroker.open, ACCOUNT, CASH, AT, "b" * 32)
    assert store.blobs == before


@pytest.mark.parametrize("mode", [Mode.SHADOW, Mode.PAPER, Mode.LIVE, "nonsense"])
def test_every_mode_but_a_backtest_opens_nothing(mode, tmp_path) -> None:
    store = FolderStore(tmp_path / "store")
    with pytest.raises(Halt, match="only for a backtest"):
        open_backtest(mode, store, ACCOUNT, CASH, AT)
    assert store.list("", None) == []


def test_a_backtest_over_another_store_than_a_folder_opens_nothing() -> None:
    store = Wired()
    with pytest.raises(Halt, match="only over a folder"):
        open_backtest(Mode.BACKTEST, store, ACCOUNT, CASH, AT)
    assert store.blobs == {}


def test_a_backtest_over_a_folder_opens_both_halves(tmp_path) -> None:
    store = FolderStore(tmp_path / "store")
    open_backtest(Mode.BACKTEST, store, ACCOUNT, CASH, AT)
    keys = set(store.list("", None))
    assert {journal_entry_key(ACCOUNT, 1), sim_account_key(ACCOUNT, 1)} <= keys


# The claim


def storage(store: Wired) -> Storage:
    clock = Clock(AT)
    return Storage(
        store,  # type: ignore[arg-type]
        timer=clock,  # type: ignore[arg-type]
        pause=clock.wait,
        retry=Retry(timedelta(seconds=10), 1.0, 4.0),
        alerts=Alarms(),
        owner_step="the broker's site",
    )


def test_a_claim_is_taken_once_and_its_own_bytes_read_as_its_own() -> None:
    store = Wired()
    made = storage(store)
    assert made.claim("runs/claim.json", b"mine")
    assert made.claim("runs/claim.json", b"mine")
    assert not made.claim("runs/claim.json", b"theirs")
    assert store.blobs["runs/claim.json"] == b"mine"
    assert made.unstored() == ()


def test_a_claim_the_store_will_not_take_halts_and_holds_nothing() -> None:
    store = Wired()
    store.refuse(("create", "runs/"))
    made = storage(store)
    with pytest.raises(Halt, match="did not store runs/claim.json"):
        made.claim("runs/claim.json", b"mine")
    assert made.unstored() == ()
