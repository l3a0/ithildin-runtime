"""One key builder for each row of the key table, and the functions that write a key's parts.

docs/architecture/08-crosscutting-concepts.md's `#### Where each blob is
stored` holds the table and `#### What each part of a key stands for` the
parts. No module puts a key together from strings. Every key that carries a
journal sequence number pads it to twelve digits, so a listing sorts entries
in order. Rows whose key another issue holds, such as the order list that maps
an order in doubt, have no builder here yet.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .records import Mode

__all__ = [
    "account_halt_key",
    "account_lease_key",
    "attempt_key",
    "blocked_record_key",
    "book_snapshot_key",
    "broker_read_key",
    "caps_key",
    "checkpoint_key",
    "claim_key",
    "comparison_key",
    "decision_part",
    "deploy_record_key",
    "heartbeat_key",
    "journal_entry_key",
    "kill_switch_key",
    "load_claim_key",
    "load_sent_key",
    "loss_stop_key",
    "off_switch_key",
    "output_key",
    "override_key",
    "pin_change_key",
    "plan_key",
    "plan_lease_key",
    "promotion_record_key",
    "release_key",
    "release_pin_key",
    "restart_key",
    "result_key",
    "run_halt_key",
    "run_part",
    "run_stop_key",
    "sim_account_key",
    "strategy_halt_key",
    "takeover_key",
    "timestamp_part",
    "utc_time_part",
    "valuation_key",
]

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# A part that sits beside another inside one segment, joined by `-`, holds no
# `-` of its own, so two different parts cannot write the same key.
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._]*")
# An output's name holds no `.`, so it cannot spell an attempt key, and these
# names belong to the key table's own rows.
_OUTPUT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_RESERVED = re.compile(r"result|claim|broker-read-[0-9]+")
_DIGEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(_[0-9]{4}-[0-9]{2}-[0-9]{2})?")


def _name(value: str, what: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value) or ".." in value:
        raise ValueError(f"{what} {value!r} cannot be part of a key")
    return value


def _word(value: str, what: str) -> str:
    if not isinstance(value, str) or not _WORD.fullmatch(value) or ".." in value:
        raise ValueError(f"{what} {value!r} cannot be part of a key")
    return value


def _output(value: str) -> str:
    if not isinstance(value, str) or not _OUTPUT.fullmatch(value) or _RESERVED.fullmatch(value):
        raise ValueError(f"output name {value!r} cannot be part of a key")
    return value


def _stage(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"stage {value!r} cannot be part of a key")
    for part in value.split("/"):
        _name(part, "stage")
    return value


def _sha256(value: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{value!r} is not a lowercase hex SHA-256")
    return value


def _digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"digest {value!r} cannot be part of a key")
    return value


def _run(value: str) -> str:
    if not isinstance(value, str) or not _RUN.fullmatch(value):
        raise ValueError(f"run {value!r} is not one that run_part writes")
    return value


def _padded(value: int, digits: int, what: str) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 10**digits:
        raise ValueError(f"{what} {value!r} does not fit in {digits} digits")
    return f"{value:0{digits}d}"


def _count(value: int, what: str, first: int = 1) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < first:
        raise ValueError(f"{what} {value!r} is not a count from {first}")
    return str(value)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{value!r} is not a UTC time")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{value!r} is not a UTC time")
    return value


def run_part(mode: Mode, first: date, last: date) -> str:
    """The run: the session date, or a backtest's first and last session dates."""
    if mode is Mode.BACKTEST:
        if last < first:
            raise ValueError(f"a backtest's span ends {last} before it starts {first}")
        return f"{first.isoformat()}_{last.isoformat()}"
    if first != last:
        raise ValueError(f"a {mode.value} run is one session, not {first} to {last}")
    return first.isoformat()


def decision_part(at: datetime) -> str:
    """A decision's UTC time, as `20261102T204000Z`."""
    _utc(at)
    if at.microsecond:
        raise ValueError(f"a decision time {at.isoformat()} carries a fraction of a second")
    return at.strftime("%Y%m%dT%H%M%SZ")


def timestamp_part(at: datetime) -> str:
    """A halt firing's UTC second, as `20261105T204100Z`."""
    return _utc(at).strftime("%Y%m%dT%H%M%SZ")


def utc_time_part(at: datetime) -> str:
    """A UTC time to the microsecond, as `20261102T204001.123456Z`."""
    return _utc(at).strftime("%Y%m%dT%H%M%S.%fZ")


def _run_prefix(deployment: str, run: str) -> str:
    return f"runs/{_name(deployment, 'deployment')}/{_run(run)}"


def _stage_prefix(deployment: str, run: str, decision_at: datetime, stage: str) -> str:
    return f"{_run_prefix(deployment, run)}/{decision_part(decision_at)}/{_stage(stage)}"


def plan_key(deployment: str, run: str) -> str:
    return f"{_run_prefix(deployment, run)}/plan.json"


def comparison_key(deployment: str, run: str) -> str:
    return f"{_run_prefix(deployment, run)}/comparison.json"


def run_halt_key(deployment: str, run: str) -> str:
    return f"{_run_prefix(deployment, run)}/halt.json"


def run_stop_key(deployment: str, run: str) -> str:
    return f"{_run_prefix(deployment, run)}/stop.json"


def result_key(deployment: str, run: str, decision_at: datetime, stage: str) -> str:
    return f"{_stage_prefix(deployment, run, decision_at, stage)}/result.json"


def output_key(deployment: str, run: str, decision_at: datetime, stage: str, name: str) -> str:
    stem = _output(name)
    return f"{_stage_prefix(deployment, run, decision_at, stage)}/{stem}.json"


def valuation_key(deployment: str, run: str, decision_at: datetime) -> str:
    return output_key(deployment, run, decision_at, "execution/recover", "valuation")


def broker_read_key(deployment: str, run: str, decision_at: datetime, attempt: int) -> str:
    n = _count(attempt, "attempt")
    return f"{_stage_prefix(deployment, run, decision_at, 'execution/send')}/broker-read-{n}.json"


def attempt_key(
    deployment: str, run: str, decision_at: datetime, stage: str, name: str, attempt: int
) -> str:
    """A capturing stage's later capture that differs from its first, counting from 2."""
    stem = _output(name)
    n = _count(attempt, "attempt", first=2)
    return f"{_stage_prefix(deployment, run, decision_at, stage)}/{stem}.attempt-{n}.json"


def takeover_key(deployment: str, run: str, at: datetime) -> str:
    return f"{_run_prefix(deployment, run)}/execution/takeovers/{utc_time_part(at)}.json"


def claim_key(deployment: str, run: str, decision_at: datetime) -> str:
    return f"{_stage_prefix(deployment, run, decision_at, 'execution/send')}/claim.json"


def journal_entry_key(account: str, seq: int) -> str:
    return f"journal/{_name(account, 'account')}/{_padded(seq, 12, 'seq')}.json"


def checkpoint_key(account: str, seq: int) -> str:
    return f"journal/{_name(account, 'account')}/checkpoints/{_padded(seq, 12, 'seq')}.json"


def book_snapshot_key(account: str, seq: int) -> str:
    return f"accounts/{_name(account, 'account')}/books/{_padded(seq, 12, 'seq')}.json"


def sim_account_key(account: str, decision_at: datetime, call: int) -> str:
    call_part = _padded(call, 4, "call")
    return f"sim/{_name(account, 'account')}/{decision_part(decision_at)}/{call_part}.json"


def plan_lease_key(deployment: str, run: str) -> str:
    return f"leases/plans/{_name(deployment, 'deployment')}/{_run(run)}.json"


def account_lease_key(account: str) -> str:
    return f"leases/accounts/{_name(account, 'account')}.json"


def kill_switch_key(account: str) -> str:
    return f"accounts/{_name(account, 'account')}/switch.json"


def loss_stop_key(account: str) -> str:
    return f"accounts/{_name(account, 'account')}/stop.json"


def _halt_name(component: str, cause: str, at: datetime, count: int) -> str:
    return (
        f"{_word(component, 'component')}-{_word(cause, 'cause')}-"
        f"{timestamp_part(at)}-{_count(count, 'count')}.json"
    )


def account_halt_key(account: str, component: str, cause: str, at: datetime, count: int) -> str:
    return f"accounts/{_name(account, 'account')}/halts/{_halt_name(component, cause, at, count)}"


def off_switch_key(account: str, strategy: str) -> str:
    return f"accounts/{_name(account, 'account')}/strategies/{_name(strategy, 'strategy')}/off.json"


def strategy_halt_key(
    account: str, strategy: str, component: str, cause: str, at: datetime, count: int
) -> str:
    prefix = f"accounts/{_name(account, 'account')}/strategies/{_name(strategy, 'strategy')}"
    return f"{prefix}/halts/{_halt_name(component, cause, at, count)}"


def caps_key(account: str) -> str:
    return f"accounts/{_name(account, 'account')}/caps.json"


def override_key(account: str, sha256: str) -> str:
    return f"accounts/{_name(account, 'account')}/overrides/{_sha256(sha256)}.json"


def release_key(account: str, sha256: str) -> str:
    return f"accounts/{_name(account, 'account')}/releases/{_sha256(sha256)}.json"


def load_claim_key(parameter: str, level: str) -> str:
    return f"load/{_word(parameter, 'parameter')}-{_word(level, 'level')}/claim.json"


def load_sent_key(parameter: str, level: str) -> str:
    return f"load/{_word(parameter, 'parameter')}-{_word(level, 'level')}/sent.json"


def heartbeat_key(deployment: str, writer: str, at: datetime) -> str:
    return (
        f"heartbeats/{_name(deployment, 'deployment')}/{_name(writer, 'writer')}/"
        f"{utc_time_part(at)}.json"
    )


def restart_key(deployment: str, run: str, worker: str, boot: str, count: int) -> str:
    return (
        f"restarts/{_name(deployment, 'deployment')}/{_run(run)}/"
        f"{_name(worker, 'worker')}/{_name(boot, 'boot')}/{_padded(count, 6, 'count')}.json"
    )


def release_pin_key() -> str:
    return "release/pin.json"


def deploy_record_key(digest: str) -> str:
    return f"release/deploys/{_digest(digest)}.json"


def blocked_record_key(digest: str) -> str:
    return f"release/blocked/{_digest(digest)}.json"


def promotion_record_key(digest: str) -> str:
    return f"release/promotions/{_digest(digest)}.json"


def pin_change_key(at: datetime) -> str:
    return f"release/changes/{utc_time_part(at)}.json"
