"""The structured alarm, its journal line, and the runbook row every kind of alarm names.

`core.alarms` says what an alarm carries. These tests hold four things.

1. Every `Kind` in the package names a row that `docs/halts-and-drills.md`
   holds under the section it names, and no two kinds share a cause.
2. An alarm's journal line is one line that reads back whole, and a line of
   any other form is refused.
3. `LogAlerts` writes that line, raises where it cannot, and takes nothing
   but an `Alarm`.
4. No alarm or halt carries an S3 message or an alert channel's own words.
"""

from __future__ import annotations

import importlib
import io
import pkgutil
import re
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

import ithildinruntime
from ithildinruntime.adapters import LogAlerts
from ithildinruntime.adapters.alerts import RecordedAlerts
from ithildinruntime.adapters.s3 import _refusal
from ithildinruntime.core import (
    EXPLAIN,
    STATUS,
    Alarm,
    AlarmUnread,
    Clears,
    Kind,
    StoreUnavailable,
)

RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "halts-and-drills.md"
KIND = Kind("test.kind", "Alarms on operations", "A worker restarted")


def every_kind() -> list[Kind]:
    """Each `Kind` defined at the top of a module of the package, once each."""
    found: dict[int, Kind] = {}
    for info in pkgutil.walk_packages(ithildinruntime.__path__, "ithildinruntime."):
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            if isinstance(value, Kind):
                found[id(value)] = value
    return list(found.values())


def rows_by_section() -> dict[str, list[str]]:
    """The first cell of each table row under each `###` heading of the runbook's alarms."""
    text = RUNBOOK.read_text()
    alarms = text[
        text.index("## Every alarm and its drill") : text.index("## Drills with no alarm")
    ]
    rows: dict[str, list[str]] = {}
    section = ""
    for line in alarms.splitlines():
        if line.startswith("### "):
            section = line[4:].strip()
            rows[section] = []
        elif line.startswith("| ") and not line.startswith("| ---") and section:
            rows[section].append(line.split(" | ")[0][2:].strip())
    return rows


def test_every_kind_names_a_row_the_runbook_holds_under_its_section() -> None:
    kinds = every_kind()
    rows = rows_by_section()
    assert len(kinds) >= 40, "the walk found the package's kinds"
    missing = [
        (kind.cause, kind.section, kind.row)
        for kind in kinds
        if not any(row.startswith(kind.row) for row in rows.get(kind.section, []))
    ]
    assert missing == []


def test_a_kind_whose_row_is_reworded_is_caught() -> None:
    rows = rows_by_section()
    moved = Kind("test.moved", "Alarms on operations", "A worker restarted twice")
    assert not any(row.startswith(moved.row) for row in rows[moved.section])
    assert any(row.startswith(KIND.row) for row in rows[KIND.section])


def test_no_two_kinds_share_a_cause() -> None:
    causes = [kind.cause for kind in every_kind()]
    assert sorted(c for c in set(causes) if causes.count(c) > 1) == []


def test_a_kind_makes_an_alarm_with_its_row_and_a_scoped_cause() -> None:
    alarm = KIND("the worker restarted", scope="alpha")
    assert alarm == Alarm(
        "test.kind:alpha",
        "the worker restarted",
        "Alarms on operations",
        "A worker restarted",
        STATUS,
        Clears.NOBODY,
    )
    assert KIND("x").cause == "test.kind"
    assert KIND("x", clears=Clears.OWNER).clears is Clears.OWNER


def test_who_clears_follows_the_mode_where_the_caller_knows_it() -> None:
    assert Clears.on(True) is Clears.OWNER
    assert Clears.on(False) is Clears.OPERATOR
    assert "on a live account the owner alone" in Clears.BY_MODE.value


def test_an_alarm_about_an_account_names_it_and_is_scoped_to_it() -> None:
    alarm = KIND("in doubt").about("account paper1", "paper1")
    assert alarm.reason == "account paper1: in doubt"
    assert alarm.cause == "test.kind:paper1"


def test_the_journal_line_is_one_line_that_reads_back_whole() -> None:
    alarm = Kind("test.line", "Alarms on operations", "A worker restarted", EXPLAIN, Clears.OWNER)(
        'two\nlines and a "quote", ünïcödé'
    )
    line = alarm.line("execution")
    assert "\n" not in line and " " not in line
    assert line.startswith("ALARM execution {")
    component, back = Alarm.from_line(line)
    assert component == "execution"
    assert back.reason == 'two lines and a "quote", ünïcödé'
    assert (back.cause, back.section, back.row, back.first, back.clears) == (
        alarm.cause,
        alarm.section,
        alarm.row,
        EXPLAIN,
        Clears.OWNER,
    )


@pytest.mark.parametrize(
    "line",
    [
        "ALARM supervisor: the old form of a line",
        "ALARM supervisor",
        'ALARM supervisor ["a list"]',
        'ALARM supervisor {"format": 2}',
        'ALARM  {"format": 1}',
        "something else",
    ],
)
def test_a_line_of_any_other_form_is_refused(line: str) -> None:
    with pytest.raises(AlarmUnread):
        Alarm.from_line(line)


def test_a_line_with_a_field_missing_extra_or_empty_is_refused() -> None:
    good = KIND("why").line("data")
    with pytest.raises(AlarmUnread):
        Alarm.from_line(good.replace('"format": 1', '"format": 1, "more": 1'))
    with pytest.raises(AlarmUnread):
        Alarm.from_line(re.sub(r', "row": "[^"]*"', "", good))
    with pytest.raises(AlarmUnread):
        Alarm.from_line(good.replace('"reason": "why"', '"reason": " "'))
    with pytest.raises(AlarmUnread):
        Alarm.from_line(good.replace('"clears": "nobody needed"', '"clears": "anyone"'))
    with pytest.raises(AlarmUnread, match="format 2"):
        Alarm.from_line(good.replace('"format": 1', '"format": 2'))


def test_a_blank_reason_still_makes_an_alarm_and_an_untyped_one_is_refused() -> None:
    # Built before the guard that keeps a channel's error from stopping what follows, so a
    # blank reason pages rather than raising there.
    assert KIND("").reason == KIND("  \n").reason == "no reason given"
    with pytest.raises(TypeError):
        KIND(3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Alarm("c", "", "s", "r", STATUS, Clears.NOBODY)


def test_log_alerts_writes_one_line_per_alarm() -> None:
    stream = io.StringIO()
    LogAlerts("supervisor", stream).alarm(KIND("a\nb"))
    (line,) = stream.getvalue().splitlines()
    assert Alarm.from_line(line)[1].reason == "a b"


def test_log_alerts_refuses_a_bare_reason() -> None:
    with pytest.raises(TypeError, match="core.Alarm"):
        LogAlerts("supervisor", io.StringIO()).alarm("a reason")  # type: ignore[arg-type]


def test_log_alerts_raises_where_the_line_cannot_be_written() -> None:
    stream = io.StringIO()
    stream.close()
    with pytest.raises(ValueError):
        LogAlerts("supervisor", stream).alarm(KIND("why"))


def test_the_backtests_channel_records_and_sends_nothing() -> None:
    alerts = RecordedAlerts()
    alerts.alarm(KIND("why"))
    alerts.heartbeat()
    assert [a.reason for a in alerts.alarms] == ["why"]
    with pytest.raises(TypeError):
        alerts.alarm("why")  # type: ignore[arg-type]


def test_an_s3_refusal_keeps_its_status_and_code_and_drops_its_message() -> None:
    arn = "arn:aws:sts::123456789012:assumed-role/ithildin-shadow-data/i-0abc"
    error = ClientError(
        {
            "Error": {"Code": "AccessDenied", "Message": f"User: {arn} is not authorized"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "PutObject",
    )
    refusal = _refusal("k", error, None, StoreUnavailable)
    assert refusal.answer == "403 AccessDenied"
    assert "arn:" not in str(refusal) and "123456789012" not in str(refusal)


def test_an_sqs_refusal_keeps_its_status_and_code_and_drops_its_message() -> None:
    from ithildinruntime.adapters.sqs import SqsQueue
    from ithildinruntime.core import QueueUnavailable

    arn = "arn:aws:sts::123456789012:assumed-role/ithildin-shadow-data/i-0abc"

    def refused():
        raise ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": f"User: {arn} is not authorized"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "ReceiveMessage",
        )

    queue = SqsQueue.__new__(SqsQueue)
    with pytest.raises(QueueUnavailable) as raised:
        queue._call(refused)
    assert raised.value.answer == "403 AccessDenied"
    assert "arn:" not in str(raised.value) and "123456789012" not in str(raised.value)


# Each kind that goes with a halt or a hold, whose page must never say that nobody needs to
# clear it, since on a live account only the owner does.
HALTS = [
    "execution.in-doubt",
    "execution.unjournaled",
    "execution.answer-refused",
    "execution.loss-stop",
    "execution.loss-past",
    "execution.moved-by-another",
    "execution.other-plan",
    "execution.claim-taken",
    "execution.nothing-sent",
    "execution.legs-stand",
    "execution.unsettled",
    "execution.process-ending",
    "execution.process-ends",
    "execution.takeover-failed",
    "execution.lease-rewrite",
    "execution.lease-absent",
    "storage.outage",
    "loop.unhandled",
    "loop.could-not-finish",
    "loop.holding",
    "loop.halt-unstored",
    "loop.halt-dropped",
    "supervisor.hold-unfinished",
    "supervisor.unclean-hold",
    "supervisor.halt-stands",
    "supervisor.stall",
    "supervisor.stop-asked",
    "supervisor.run-halted",
    "supervisor.run-halt-unstored",
    "supervisor.stop-asked-before-halt",
    "supervisor.run-halt-stored",
    "supervisor.run-halt-unread",
]


@pytest.mark.parametrize("cause", HALTS)
def test_a_kind_that_goes_with_a_halt_never_says_nobody_needs_to_clear_it(cause: str) -> None:
    (kind,) = [k for k in every_kind() if k.cause == cause]
    assert kind.clears is not Clears.NOBODY
