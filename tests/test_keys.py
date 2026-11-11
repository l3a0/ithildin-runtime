"""The key builders, one for each row of 08's key table, and the parts they write.

docs/architecture/08-crosscutting-concepts.md's `#### Where each blob is
stored` holds the table. Every key that carries a journal sequence number
pads it to twelve digits.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from ithildinruntime import core
from ithildinruntime.core import Mode

AT = datetime(2026, 11, 2, 20, 40, tzinfo=UTC)
FIRED = datetime(2026, 11, 5, 20, 41, 0, 987654, tzinfo=UTC)
HELD = datetime(2026, 11, 2, 20, 40, 1, 123456, tzinfo=UTC)
SHA = "ab" * 32
RUN = "2026-11-02"

ROWS = [
    (core.plan_key("paper", RUN), "runs/paper/2026-11-02/plan.json"),
    (core.comparison_key("paper", RUN), "runs/paper/2026-11-02/comparison.json"),
    (core.run_halt_key("paper", RUN), "runs/paper/2026-11-02/halt.json"),
    (core.run_stop_key("paper", RUN), "runs/paper/2026-11-02/stop.json"),
    (
        core.result_key("paper", RUN, AT, "data"),
        "runs/paper/2026-11-02/20261102T204000Z/data/result.json",
    ),
    (
        core.output_key("paper", RUN, AT, "data", "snapshot"),
        "runs/paper/2026-11-02/20261102T204000Z/data/snapshot.json",
    ),
    (
        core.valuation_key("paper", RUN, AT),
        "runs/paper/2026-11-02/20261102T204000Z/execution/recover/valuation.json",
    ),
    (
        core.broker_read_key("paper", RUN, AT, 1),
        "runs/paper/2026-11-02/20261102T204000Z/execution/send/broker-read-1.json",
    ),
    (
        core.attempt_key("paper", RUN, AT, "data", "snapshot", 2),
        "runs/paper/2026-11-02/20261102T204000Z/data/snapshot.attempt-2.json",
    ),
    (
        core.takeover_key("paper", RUN, HELD),
        "runs/paper/2026-11-02/execution/takeovers/20261102T204001.123456Z.json",
    ),
    (
        core.claim_key("paper", RUN, AT),
        "runs/paper/2026-11-02/20261102T204000Z/execution/send/claim.json",
    ),
    (core.journal_entry_key("paper1", 42), "journal/paper1/000000000042.json"),
    (core.checkpoint_key("paper1", 42), "journal/paper1/checkpoints/000000000042.json"),
    (core.book_snapshot_key("paper1", 42), "accounts/paper1/books/000000000042.json"),
    (core.sim_account_key("sim1", AT, 3), "sim/sim1/20261102T204000Z/0003.json"),
    (core.plan_lease_key("paper", RUN), "leases/plans/paper/2026-11-02.json"),
    (core.account_lease_key("paper1"), "leases/accounts/paper1.json"),
    (core.kill_switch_key("paper1"), "accounts/paper1/switch.json"),
    (core.loss_stop_key("paper1"), "accounts/paper1/stop.json"),
    (
        core.account_halt_key("paper1", "execution", "cancel", FIRED, 1),
        "accounts/paper1/halts/execution-cancel-20261105T204100Z-1.json",
    ),
    (core.off_switch_key("paper1", "bollinger"), "accounts/paper1/strategies/bollinger/off.json"),
    (
        core.strategy_halt_key("paper1", "bollinger", "risk", "loss", FIRED, 2),
        "accounts/paper1/strategies/bollinger/halts/risk-loss-20261105T204100Z-2.json",
    ),
    (core.caps_key("paper1"), "accounts/paper1/caps.json"),
    (core.override_key("paper1", SHA), f"accounts/paper1/overrides/{SHA}.json"),
    (core.release_key("paper1", SHA), f"accounts/paper1/releases/{SHA}.json"),
    (core.load_claim_key("journal", "1x"), "load/journal-1x/claim.json"),
    (core.load_sent_key("journal", "1x"), "load/journal-1x/sent.json"),
    (
        core.heartbeat_key("paper", "data", HELD),
        "heartbeats/paper/data/20261102T204001.123456Z.json",
    ),
    (
        core.restart_key("paper", RUN, "data", "boot7", 12),
        "restarts/paper/2026-11-02/data/boot7/000012.json",
    ),
    (core.release_pin_key(), "release/pin.json"),
    (core.deploy_record_key(f"sha256:{SHA}"), f"release/deploys/sha256:{SHA}.json"),
    (core.blocked_record_key(f"sha256:{SHA}"), f"release/blocked/sha256:{SHA}.json"),
    (core.promotion_record_key(f"sha256:{SHA}"), f"release/promotions/sha256:{SHA}.json"),
    (core.pin_change_key(HELD), "release/changes/20261102T204001.123456Z.json"),
]


@pytest.mark.parametrize(("built", "expected"), ROWS, ids=[e for _, e in ROWS])
def test_each_builder_writes_its_rows_key(built: str, expected: str) -> None:
    assert built == expected


@pytest.mark.parametrize(
    "builder", [core.journal_entry_key, core.checkpoint_key, core.book_snapshot_key]
)
def test_every_sequence_number_is_padded_to_twelve_digits_and_sorts(builder) -> None:
    keys = [builder("paper1", n) for n in (1000, 999, 1, 10**12 - 1)]
    assert all(len(k.rsplit("/", 1)[1]) == len("000000000000.json") for k in keys)
    assert sorted(keys) == [builder("paper1", n) for n in (1, 999, 1000, 10**12 - 1)]
    with pytest.raises(ValueError):
        builder("paper1", 10**12)
    with pytest.raises(ValueError):
        builder("paper1", -1)


def test_the_run_part_names_a_session_or_a_backtests_span() -> None:
    assert core.run_part(Mode.PAPER, date(2026, 11, 2), date(2026, 11, 2)) == "2026-11-02"
    span = core.run_part(Mode.BACKTEST, date(2006, 4, 26), date(2012, 4, 9))
    assert span == "2006-04-26_2012-04-09"
    with pytest.raises(ValueError):
        core.run_part(Mode.LIVE, date(2026, 11, 2), date(2026, 11, 3))
    with pytest.raises(ValueError):
        core.run_part(Mode.BACKTEST, date(2026, 11, 3), date(2026, 11, 2))


def test_the_time_parts_are_utc_and_sort_in_order() -> None:
    assert core.decision_part(AT) == "20261102T204000Z"
    assert core.timestamp_part(FIRED) == "20261105T204100Z"
    assert core.utc_time_part(HELD) == "20261102T204001.123456Z"
    later = HELD + timedelta(microseconds=1)
    assert core.utc_time_part(HELD) < core.utc_time_part(later)
    with pytest.raises(ValueError):
        core.decision_part(HELD)
    with pytest.raises(ValueError):
        core.decision_part(datetime(2026, 11, 2, 20, 40))
    with pytest.raises(ValueError):
        core.utc_time_part(datetime(2026, 11, 2, 15, 40, tzinfo=timezone(timedelta(hours=-5))))


@pytest.mark.parametrize(
    "build",
    [
        lambda: core.plan_key("../paper", RUN),
        lambda: core.plan_key("paper/x", RUN),
        lambda: core.plan_key("", RUN),
        lambda: core.plan_key("paper", "20261102"),
        lambda: core.result_key("paper", RUN, AT, "execution//send"),
        lambda: core.output_key("paper", RUN, AT, "data", "a b"),
        lambda: core.override_key("paper1", "AB" * 32),
        lambda: core.attempt_key("paper", RUN, AT, "data", "snapshot", 1),
        lambda: core.broker_read_key("paper", RUN, AT, 0),
        lambda: core.sim_account_key("sim1", AT, 10_000),
        lambda: core.restart_key("paper", RUN, "data", "boot7", 1_000_000),
        lambda: core.account_halt_key("paper1", "execution", "cancel", FIRED, 0),
    ],
)
def test_a_part_that_would_break_a_key_is_refused(build) -> None:
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize(
    "build",
    [
        lambda: core.output_key("paper", RUN, AT, "data", "snapshot.attempt-2"),
        lambda: core.output_key("paper", RUN, AT, "data", "result"),
        lambda: core.output_key("paper", RUN, AT, "execution/send", "claim"),
        lambda: core.output_key("paper", RUN, AT, "execution/send", "broker-read-1"),
        lambda: core.attempt_key("paper", RUN, AT, "data", "snap.x", 2),
        lambda: core.account_halt_key("paper1", "data-worker", "stale", FIRED, 1),
        lambda: core.account_halt_key("paper1", "data", "worker-stale", FIRED, 1),
        lambda: core.strategy_halt_key("paper1", "s", "risk", "loss-stop", FIRED, 1),
        lambda: core.load_claim_key("a-b", "c"),
        lambda: core.load_sent_key("a", "b-c"),
    ],
)
def test_a_name_that_would_spell_another_rows_key_is_refused(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_an_output_name_may_hold_a_dash() -> None:
    key = core.output_key("paper", RUN, AT, "alpha/bollinger", "signal-records")
    assert key == "runs/paper/2026-11-02/20261102T204000Z/alpha/bollinger/signal-records.json"
