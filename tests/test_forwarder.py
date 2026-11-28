"""The forwarder on the host: which journal lines page, where it resumes, and what it keeps.

`runtime/forwarder.py` says what holds. A stand-in journal hands entries after
a cursor, as `journalctl --after-cursor` does, and a stand-in post records each
page, so no test reads a real journal or reaches ntfy.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ithildinruntime.adapters import LogAlerts
from ithildinruntime.adapters.ntfy import Refusal, Sent
from ithildinruntime.core import Kind
from ithildinruntime.runtime import entry
from ithildinruntime.runtime.forwarder import (
    Entry,
    FileDayCount,
    Forwarder,
    ForwarderState,
    Journalctl,
    JournalEnded,
    entry_of,
    runtime_component,
)
from ithildinruntime.runtime.settings import SettingError, read_forwarder_settings

ADDRESS = "https://ntfy.example/secret-topic-name"
START = datetime(2026, 11, 27, 14, 0, tzinfo=UTC)
STALL = Kind("test.stall", "Alarms of the supervisor and the commands", "A stall")


def line(component: str, reason: str, kind: Kind = STALL) -> str:
    """The line `LogAlerts` writes for one alarm, as a worker would write it."""
    stream = io.StringIO()
    LogAlerts(component, stream).alarm(kind(reason))
    return stream.getvalue().rstrip("\n")


class Journal:
    """A stand-in host journal: entries in order, each read after a cursor."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.asked: list[str | None] = []

    def add(self, unit: str | None, message: str | None, seconds: int = 0) -> Entry:
        made = Entry(f"c{len(self.entries)}", unit, message, START + timedelta(seconds=seconds))
        self.entries.append(made)
        return made

    def __call__(self, after: str | None):
        self.asked.append(after)
        start = 0 if after is None else [e.cursor for e in self.entries].index(after) + 1
        yield from self.entries[start:]


class Topic:
    def __init__(self) -> None:
        self.bodies: list[str] = []
        self.status = 200

    def __call__(self, address, body, headers) -> int:
        assert address == ADDRESS
        self.bodies.append(body.decode())
        return self.status


@pytest.fixture
def settings_file(tmp_path: Path):
    topic = tmp_path / "topic"
    topic.write_text(f"{ADDRESS}\n")
    state = tmp_path / "state"
    state.mkdir()
    named = tmp_path / "forwarder.toml"
    named.write_text(f'environment = "shadow"\npush_topic = "{topic}"\nstate = "{state}"\n')
    return named


def forwarder(settings_file: Path, journal: Journal, topic: Topic) -> Forwarder:
    settings = read_forwarder_settings({"ITHILDIN_SETTINGS": str(settings_file)})
    made = entry.forwarder_for(settings, post=topic)
    made._journal = journal  # the stand-in journal in place of journalctl
    return made


@pytest.mark.parametrize(
    ("unit", "component"),
    [
        ("ithildin-supervisor.service", "supervisor"),
        ("ithildin-execution@shadow1-a.service", "execution"),
        ("ithildin-data.service", "data"),
        ("ithildin-monitoring@2.service", "monitoring"),
        ("ithildin-forwarder.service", None),
        ("ithildin-supervisor.service.d", None),
        ("xithildin-data.service", None),
        ("docker.service", None),
        ("init.scope", None),
        (None, None),
    ],
)
def test_only_the_runtimes_units_are_its_own(unit, component) -> None:
    assert runtime_component(unit) == component


def test_a_runtime_units_alarm_line_pages_naming_its_component_by_its_unit(settings_file) -> None:
    journal, topic = Journal(), Topic()
    journal.add("ithildin-execution@shadow1.service", line("supervisor", "a stall"))
    forwarder(settings_file, journal, topic).run()
    (body,) = topic.bodies
    assert body.startswith("shadow execution: a stall\n")
    assert 'Row: "A stall"' in body


@pytest.mark.parametrize(
    "unit", ["sshd.service", "ithildin-forwarder.service", "docker.service", "init.scope", None]
)
def test_an_alarm_line_from_any_other_unit_pages_nothing(settings_file, unit) -> None:
    journal, topic = Journal(), Topic()
    journal.add(unit, line("supervisor", "forged"))
    forwarder(settings_file, journal, topic).run()
    assert topic.bodies == []


def test_a_runtime_units_other_lines_page_nothing(settings_file) -> None:
    journal, topic = Journal(), Topic()
    journal.add("ithildin-data.service", "a traceback line")
    journal.add("ithildin-data.service", None)
    journal.add("ithildin-data.service", " ALARM data {}")
    forwarder(settings_file, journal, topic).run()
    assert topic.bodies == []


def test_a_line_that_starts_alarm_and_is_not_one_pages_naming_the_unit_alone(settings_file) -> None:
    journal, topic = Journal(), Topic()
    journal.add("ithildin-alpha.service", "ALARM alpha: the account arn:aws:iam::1:role/x said no")
    forwarder(settings_file, journal, topic).run()
    (body,) = topic.bodies
    assert "a line of ithildin-alpha.service starts ALARM and is not an alarm" in body
    assert "arn:aws" not in body and "said no" not in body
    assert "2026-11-27T14:00:00Z" in body


def test_one_cause_from_two_instances_of_a_host_pages_once(settings_file) -> None:
    journal, topic = Journal(), Topic()
    journal.add("ithildin-data@a.service", line("data", "S3 down at k1"))
    journal.add("ithildin-data@b.service", line("data", "S3 down at k2"), seconds=5)
    forwarder(settings_file, journal, topic).run()
    assert len(topic.bodies) == 1


def test_the_cursor_is_saved_after_each_entry(settings_file, tmp_path) -> None:
    journal, topic = Journal(), Topic()
    journal.add("sshd.service", "noise")
    journal.add("ithildin-data.service", line("data", "a stall"))
    forwarder(settings_file, journal, topic).run()
    assert (tmp_path / "state" / "cursor").read_text() == "c1"


def test_a_forwarder_started_again_reads_on_from_its_cursor(settings_file) -> None:
    journal, topic = Journal(), Topic()
    journal.add(
        "ithildin-data.service", line("data", "first", Kind("t.a", STALL.section, "A stall"))
    )
    forwarder(settings_file, journal, topic).run()
    # Written while the forwarder was down, and read once it starts again.
    journal.add(
        "ithildin-risk.service", line("risk", "second", Kind("t.b", STALL.section, "A stall"))
    )
    forwarder(settings_file, journal, topic).run()
    assert journal.asked == [None, "c0"]
    assert [b.split("\n")[0] for b in topic.bodies] == ["shadow data: first", "shadow risk: second"]


def test_a_refused_push_writes_its_marker_with_no_address(settings_file, tmp_path) -> None:
    journal, topic = Journal(), Topic()
    topic.status = 429
    journal.add("ithildin-data.service", line("data", "a stall"))
    forwarder(settings_file, journal, topic).run()
    marker = (tmp_path / "state" / "refused.json").read_text()
    assert json.loads(marker) == {
        "cause": "test.stall",
        "at": "2026-11-27T14:00:00+00:00",
        "answer": "HTTP 429",
    }
    assert "secret-topic-name" not in marker
    assert (tmp_path / "state" / "cursor").read_text() == "c0", "the line counts as handled"


def test_the_days_count_of_pages_outlives_a_restart(tmp_path) -> None:
    path = tmp_path / "pages.json"
    count = FileDayCount(path)
    day = START.date()
    assert [count.add(day), count.add(day)] == [1, 2]
    assert FileDayCount(path).add(day) == 3
    assert FileDayCount(path).add(day + timedelta(days=1)) == 1


@pytest.mark.parametrize("held", ["not json", '{"day": "x", "pages": 3}', "[]"])
def test_a_count_that_cannot_be_read_starts_at_zero(tmp_path, held) -> None:
    path = tmp_path / "pages.json"
    path.write_text(held)
    assert FileDayCount(path).add(START.date()) == 1


def test_the_state_files_are_the_owners_alone(tmp_path) -> None:
    state = ForwarderState(tmp_path)
    state.save_cursor("c9")
    state.mark_refused(Refusal("x", START, "HTTP 429"))
    for name in ("cursor", "refused.json"):
        assert (tmp_path / name).stat().st_mode & 0o077 == 0
    assert ForwarderState(tmp_path).cursor() == "c9"
    (tmp_path / "none").mkdir()
    assert ForwarderState(tmp_path / "none").cursor() is None


def test_an_entry_reads_its_cursor_unit_message_and_time() -> None:
    made = entry_of(
        {
            "__CURSOR": "s=1;i=2",
            "_SYSTEMD_UNIT": "ithildin-data.service",
            "MESSAGE": [65, 76, 65, 82, 77, 32, 255],
            "__REALTIME_TIMESTAMP": "1764252000000000",
        }
    )
    assert made == Entry(
        "s=1;i=2", "ithildin-data.service", "ALARM �", datetime(2025, 11, 27, 14, 0, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    "record",
    [{}, [], {"__CURSOR": 3}, {"__CURSOR": "c"}, {"__CURSOR": "c", "__REALTIME_TIMESTAMP": "x"}],
)
def test_an_entry_with_no_cursor_or_time_ends_the_reading(record) -> None:
    with pytest.raises(JournalEnded):
        entry_of(record)


def test_journalctl_is_asked_to_follow_this_boot_from_the_cursor() -> None:
    reader = Journalctl(directory=Path("/var/log/journal"))
    assert reader.arguments("s=1") == [
        "journalctl",
        "--output=json",
        "--follow",
        "--no-tail",
        "--no-pager",
        "--boot",
        "--unit=ithildin-*",
        "--directory=/var/log/journal",
        "--after-cursor=s=1",
    ]
    assert "--after-cursor" not in " ".join(Journalctl().arguments(None))


def test_a_journal_that_ends_ends_the_forwarder(tmp_path) -> None:
    script = tmp_path / "journal.py"
    record = {
        "__CURSOR": "c0",
        "_SYSTEMD_UNIT": "ithildin-data.service",
        "MESSAGE": "hello",
        "__REALTIME_TIMESTAMP": "1764252000000000",
    }
    script.write_text(f"import sys\nprint({json.dumps(json.dumps(record))})\nsys.exit(3)\n")
    reader = Journalctl(command=(sys.executable, str(script)))
    entries = reader.entries(None)
    assert next(entries).message == "hello"
    with pytest.raises(JournalEnded, match="exit code 3"):
        next(entries)


def test_a_journal_line_that_is_not_json_ends_the_reading(tmp_path) -> None:
    script = tmp_path / "journal.py"
    script.write_text("print('not json')\n")
    with pytest.raises(JournalEnded, match="not JSON"):
        next(Journalctl(command=(sys.executable, str(script))).entries(None))


def test_the_settings_read_the_address_and_never_show_it(tmp_path, settings_file) -> None:
    settings = read_forwarder_settings({"ITHILDIN_SETTINGS": str(settings_file)})
    assert settings.address == ADDRESS and settings.environment.name == "shadow"
    assert "secret-topic-name" not in repr(settings)
    (tmp_path / "topic").write_text("http://ntfy.example/secret-topic-name")
    with pytest.raises(SettingError) as refused:
        read_forwarder_settings({"ITHILDIN_SETTINGS": str(settings_file)})
    assert "no one https address" in str(refused.value)
    assert "secret-topic-name" not in str(refused.value)


def test_settings_with_no_state_directory_are_refused(tmp_path, settings_file) -> None:
    (tmp_path / "state").rmdir()
    with pytest.raises(SettingError, match="is not a directory"):
        read_forwarder_settings({"ITHILDIN_SETTINGS": str(settings_file)})


def test_settings_with_a_workers_key_are_refused(tmp_path, settings_file) -> None:
    settings_file.write_text(settings_file.read_text() + 'mirror = "/m"\n')
    with pytest.raises(SettingError, match="keys no reader knows"):
        read_forwarder_settings({"ITHILDIN_SETTINGS": str(settings_file)})


def test_the_forwarder_is_a_command_of_the_image() -> None:
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert 'ithildin-forwarder = "ithildinruntime.runtime.entry:forwarder_main"' in text


def test_a_held_repeat_from_the_journal_is_not_paged(settings_file) -> None:
    journal, topic = Journal(), Topic()
    for second in range(0, 600, 5):
        journal.add("ithildin-supervisor.service", line("supervisor", "a stall"), second)
    made = forwarder(settings_file, journal, topic)
    made.run()
    assert len(topic.bodies) == 1
    repeat = journal.add("ithildin-supervisor.service", line("supervisor", "x"), 601)
    assert made.handle(repeat) is Sent.HELD


# Round 1 of the review


def test_an_address_written_where_its_file_belongs_is_never_printed(
    tmp_path, settings_file, monkeypatch, capsys
) -> None:
    text = settings_file.read_text().replace(str(tmp_path / "topic"), ADDRESS)
    settings_file.write_text(text)
    monkeypatch.setenv("ITHILDIN_SETTINGS", str(settings_file))
    with pytest.raises(SystemExit) as ended:
        entry.forwarder_main()
    assert ended.value.code == entry.EXIT_SETTING
    err = capsys.readouterr().err
    assert "the file the key push_topic names cannot be read" in err
    assert "secret-topic-name" not in err and "ntfy.example" not in err


@pytest.mark.parametrize("held", [f"{ADDRESS}\nhttps://other/x", f"{ADDRESS} x", "ftp://a/b"])
def test_a_topic_file_that_holds_no_one_https_address_is_refused(
    tmp_path, settings_file, held
) -> None:
    (tmp_path / "topic").write_text(held)
    with pytest.raises(SettingError, match="holds no one https address") as refused:
        read_forwarder_settings({"ITHILDIN_SETTINGS": str(settings_file)})
    assert "secret-topic-name" not in str(refused.value)


def test_the_journal_and_heartbeat_keys_reach_the_forwarder(tmp_path, settings_file) -> None:
    beat = tmp_path / "beat"
    settings_file.write_text(
        settings_file.read_text() + f'journal = "/var/log/journal"\nheartbeat = "{beat}"\n'
    )
    settings = read_forwarder_settings({"ITHILDIN_SETTINGS": str(settings_file)})
    assert (settings.journal, settings.heartbeat) == (Path("/var/log/journal"), beat)
    made = entry.forwarder_for(settings, post=Topic())
    assert "--directory=/var/log/journal" in made._journal.__self__.arguments(None)
    made._journal = Journal()
    made.run()
    assert beat.exists(), "the forwarder touched its heartbeat as it started"


def test_the_forwarder_beats_while_it_runs(settings_file) -> None:
    import time

    beats: list[float] = []

    def slow(after):
        for n in range(5):
            time.sleep(0.05)
            yield Entry(f"c{n}", "sshd.service", "noise", START)

    settings = read_forwarder_settings({"ITHILDIN_SETTINGS": str(settings_file)})
    made = entry.forwarder_for(settings, post=Topic())
    made = Forwarder(
        journal=slow,
        pager=made._pager,
        state=made._state,
        beat=lambda: beats.append(time.monotonic()),
        beat_every=0.02,
    )
    made.run()
    assert len(beats) >= 3, beats


def test_a_state_write_that_fails_leaves_the_cursor_before_the_line(
    tmp_path, settings_file
) -> None:
    journal, topic = Journal(), Topic()
    journal.add("sshd.service", "noise")
    journal.add("ithildin-data.service", line("data", "a stall"), seconds=1)
    topic.status = 429
    made = forwarder(settings_file, journal, topic)

    def broken(refusal) -> None:
        raise OSError("the state directory is full")

    made._pager._refused = broken
    with pytest.raises(OSError):
        made.run()
    assert (tmp_path / "state" / "cursor").read_text() == "c0", "a restart reads the line again"


def test_two_units_unreadable_lines_page_apart(settings_file) -> None:
    journal, topic = Journal(), Topic()
    journal.add("ithildin-alpha.service", "ALARM alpha: old form")
    journal.add("ithildin-alpha@b.service", "ALARM alpha: old form", seconds=1)
    journal.add("ithildin-risk.service", "ALARM risk: old form", seconds=2)
    forwarder(settings_file, journal, topic).run()
    assert len(topic.bodies) == 3, "each unit's unreadable line is its own cause"


def test_a_link_planted_at_a_part_file_is_not_followed(tmp_path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    (tmp_path / ".cursor.part").symlink_to(target)
    ForwarderState(tmp_path).save_cursor("c1")
    assert target.read_text() == "keep"
    assert (tmp_path / "cursor").read_text() == "c1"
