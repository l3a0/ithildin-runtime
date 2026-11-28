"""The push topic's pager: one page per cause, its repeats, its three lines, its cut and its load.

`adapters/ntfy.py` says what holds. A stand-in `post` records each page, so no
test reaches ntfy, apart from the last, which posts to a server on localhost.
"""

from __future__ import annotations

import http.server
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ithildinruntime.adapters.ntfy import (
    PAGE_BYTES,
    RUNBOOK,
    DayCount,
    Load,
    NtfyAlerts,
    Pager,
    PushRefused,
    Refusal,
    Repeats,
    Sent,
    anchor,
    https_post,
    page_of,
)
from ithildinruntime.core import EXPLAIN, Clears, Kind
from ithildinruntime.runtime.supervisor.deadline import (
    CAUSE_ENDS_AFTER,
    PAGE_AGAIN_FIRST,
    PAGE_AGAIN_FIRST_FOR,
    PAGE_AGAIN_LATER,
    PAGES_A_DAY,
    PAGES_WARN_AT,
)
from tests.support.markdown_links import anchors

ADDRESS = "https://ntfy.example/secret-topic-name"
START = datetime(2026, 11, 27, 14, 0, tzinfo=UTC)
OUTAGE = Kind("test.outage", "Alarms on storage and backups", "A write S3 refuses", EXPLAIN)
OTHER = Kind("test.other", "Alarms on operations", "A worker restarted")
REPEATS = Repeats(PAGE_AGAIN_FIRST, PAGE_AGAIN_FIRST_FOR, PAGE_AGAIN_LATER, CAUSE_ENDS_AFTER)


class Topic:
    """A stand-in push server: each post it took, and the status it answers."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str, dict]] = []
        self.status = 200
        self.error: BaseException | None = None

    def __call__(self, address, body, headers) -> int:
        if self.error is not None:
            raise self.error
        self.posts.append((address, body.decode(), dict(headers)))
        return self.status

    @property
    def bodies(self) -> list[str]:
        return [body for _, body, _ in self.posts]


def pager(topic: Topic, refused: list | None = None, load: Load | None = None) -> Pager:
    return Pager(
        environment="shadow",
        address=ADDRESS,
        post=topic,
        repeats=REPEATS,
        load=load or Load(PAGES_A_DAY, PAGES_WARN_AT),
        refused=(refused.append if refused is not None else lambda r: None),
    )


def test_the_intervals_are_the_written_ones() -> None:
    assert (PAGE_AGAIN_FIRST, PAGE_AGAIN_FIRST_FOR, PAGE_AGAIN_LATER) == (
        timedelta(minutes=15),
        timedelta(hours=1),
        timedelta(hours=1),
    )
    assert (PAGES_A_DAY, PAGES_WARN_AT) == (250, 188)
    assert PAGES_WARN_AT == round(PAGES_A_DAY * 0.75)


def test_the_first_alarm_of_a_cause_pages_at_once_and_its_repeats_are_held() -> None:
    topic = Topic()
    paging = pager(topic)
    assert paging.offer("supervisor", OUTAGE("S3 did not store a"), START) is Sent.SENT
    for seconds in range(5, 15 * 60, 5):
        at = START + timedelta(seconds=seconds)
        assert paging.offer("supervisor", OUTAGE(f"S3 did not store {seconds}"), at) is Sent.HELD
    assert len(topic.posts) == 1


def test_a_cause_still_going_pages_again_at_15_minutes_saying_how_many_were_held() -> None:
    topic = Topic()
    paging = pager(topic)
    paging.offer("data", OUTAGE("first"), START)
    for minute in range(1, 15):
        paging.offer("data", OUTAGE(f"at {minute}"), START + timedelta(minutes=minute))
    assert paging.offer("data", OUTAGE("at 15"), START + timedelta(minutes=15)) is Sent.SENT
    first, again = topic.bodies
    assert first.startswith("shadow data: first\n")
    assert again.startswith(
        "shadow data: at 15. Repeated: 16 alarms of this cause since 2026-11-27T14:00Z, "
        "14 of them held\n"
    )


def test_a_cause_that_lasts_a_whole_day_pages_27_times() -> None:
    topic = Topic()
    paging = pager(topic)
    for step in range(0, 24 * 60 * 60, 5):
        paging.offer("supervisor", OUTAGE("S3 did not answer"), START + timedelta(seconds=step))
    assert len(topic.posts) == 27, "4 in the first hour and then one an hour"


def test_after_the_first_hour_a_cause_pages_once_an_hour() -> None:
    topic = Topic()
    paging = pager(topic)
    sent = []
    for minute in range(0, 4 * 60):
        at = START + timedelta(minutes=minute)
        if paging.offer("data", OUTAGE("down"), at) is Sent.SENT:
            sent.append(minute)
    assert sent == [0, 15, 30, 45, 105, 165, 225]


def test_a_second_cause_pages_at_once_while_the_first_is_held() -> None:
    topic = Topic()
    paging = pager(topic)
    paging.offer("data", OUTAGE("down"), START)
    assert paging.offer("data", OUTAGE("down"), START + timedelta(minutes=1)) is Sent.HELD
    assert paging.offer("data", OTHER("restarted"), START + timedelta(minutes=2)) is Sent.SENT
    assert (
        paging.offer("data", OUTAGE("down", scope="x"), START + timedelta(minutes=3)) is Sent.SENT
    )
    assert len(topic.posts) == 3


def test_a_cause_silent_for_an_hour_has_ended_and_its_next_alarm_begins_it_again() -> None:
    topic = Topic()
    paging = pager(topic)
    paging.offer("data", OUTAGE("down"), START)
    assert paging.offer("data", OUTAGE("down"), START + timedelta(minutes=50)) is Sent.SENT
    assert "Repeated: 2 alarms" in topic.bodies[-1], "a cause seen 50 minutes ago still goes on"
    later = START + timedelta(minutes=50) + CAUSE_ENDS_AFTER
    assert paging.offer("data", OUTAGE("down"), later) is Sent.SENT
    assert "Repeated" not in topic.bodies[-1], "a cause silent for an hour begins again"


def test_a_page_has_three_lines_what_happened_what_to_run_and_its_row() -> None:
    page = page_of("paper", "execution", OUTAGE("S3 did not store k", clears=Clears.BY_MODE))
    first, second, third = page.body.split("\n")
    assert first == "paper execution: S3 did not store k"
    assert second == (
        "Run first: ithildin halt explain. Who clears: the operator or the owner, and on a "
        "live account the owner alone."
    )
    assert third == f'Row: "A write S3 refuses" in {RUNBOOK}#alarms-on-storage-and-backups'
    assert page.title == "ithildin paper: test.outage"


def test_the_rows_link_reaches_a_heading_of_the_runbook() -> None:
    text = (Path(__file__).resolve().parents[1] / "docs" / "halts-and-drills.md").read_text()
    for section in ("Alarms on storage and backups", "Alarms of the alert channel"):
        assert anchor(section) in anchors(text)


@pytest.mark.parametrize("unit", ["a", "é", "€", "😀"])
def test_a_page_too_long_is_cut_in_its_first_line_and_keeps_its_second_and_third(unit) -> None:
    alarm = OUTAGE(unit * 9000)
    page = page_of("live", "data", alarm)
    assert len(page.body.encode()) <= PAGE_BYTES
    first, second, third = page.body.split("\n")
    assert first.endswith("…") and first.startswith("live data: ")
    assert second == page_of("live", "data", OUTAGE("x")).body.split("\n")[1]
    assert third == page_of("live", "data", OUTAGE("x")).body.split("\n")[2]
    assert len(page.body.encode()) > PAGE_BYTES - 8, "the cut keeps as much as fits"


def test_a_page_that_fits_is_not_cut() -> None:
    page = page_of("live", "data", OUTAGE("x" * 100))
    assert "…" not in page.body


def test_a_title_is_plain_ascii_for_the_header() -> None:
    page = page_of("live", "data", Kind("test.é", "s", "r")("x"))
    page.title.encode("ascii")


def test_each_page_posts_its_title_and_a_high_priority() -> None:
    topic = Topic()
    pager(topic).offer("data", OUTAGE("down"), START)
    ((address, _, headers),) = topic.posts
    assert address == ADDRESS
    assert headers == {
        "Title": "ithildin shadow: test.outage",
        "Priority": "high",
        "Tags": "warning",
    }


def test_the_page_that_reaches_75_percent_of_the_limit_is_followed_by_one_that_says_so() -> None:
    topic = Topic()
    paging = pager(topic)
    for n in range(PAGES_WARN_AT):
        paging.offer("data", OTHER("restarted", scope=str(n)), START + timedelta(seconds=n))
    assert len(topic.posts) == PAGES_WARN_AT + 1
    near = topic.bodies[-1]
    assert near.startswith("shadow channel: 188 pages went out today, of the push server's limit")
    assert "Pages a day reach 75% of the push server's daily limit" in near
    for n in range(PAGES_WARN_AT, PAGES_WARN_AT + 10):
        paging.offer("data", OTHER("restarted", scope=str(n)), START + timedelta(seconds=n))
    assert sum("pages went out today" in body for body in topic.bodies) == 1


def test_the_count_of_pages_starts_again_each_utc_day() -> None:
    count = DayCount()
    day = START.date()
    assert [count.add(day) for _ in range(3)] == [1, 2, 3]
    assert count.add(day + timedelta(days=1)) == 1


def test_a_held_alarm_is_not_counted_as_a_page() -> None:
    topic = Topic()
    paging = pager(topic, load=Load(10, 2))
    for second in range(100):
        paging.offer("data", OUTAGE("down"), START + timedelta(seconds=second))
    assert len(topic.posts) == 1, "one page, and no page about the count"


@pytest.mark.parametrize("answer", ["status", "error"])
def test_a_refused_push_is_handed_over_with_no_address_and_waits_for_its_interval(answer) -> None:
    topic, refused = Topic(), []
    if answer == "status":
        topic.status = 429
    else:
        topic.error = ConnectionError(f"could not reach {ADDRESS}")
    paging = pager(topic, refused)
    assert paging.offer("data", OUTAGE("down"), START) is Sent.REFUSED
    (refusal,) = refused
    assert refusal == Refusal(
        "test.outage", START, "HTTP 429" if answer == "status" else "no answer: ConnectionError"
    )
    assert "secret-topic-name" not in repr(refusal) and "secret-topic-name" not in repr(paging)
    topic.status, topic.error = 200, None
    assert paging.offer("data", OUTAGE("down"), START + timedelta(minutes=1)) is Sent.HELD
    assert paging.offer("data", OUTAGE("down"), START + timedelta(minutes=15)) is Sent.SENT


def test_a_refused_page_is_not_counted() -> None:
    topic = Topic()
    topic.status = 429
    paging = pager(topic, load=Load(10, 1))
    paging.offer("data", OUTAGE("down"), START)
    topic.status = 200
    paging.offer("data", OTHER("restarted"), START)
    assert len(topic.posts) == 3, "the 429 one, the one that counts as first, and the count's page"


def test_monitorings_alerts_raise_on_a_refused_push_and_take_a_held_one() -> None:
    topic = Topic()
    now = [START]
    alerts = NtfyAlerts(pager(topic), lambda: now[0])
    alerts.alarm(OUTAGE("down"))
    alerts.alarm(OUTAGE("down"))
    topic.status = 429
    with pytest.raises(PushRefused, match="test.other") as raised:
        alerts.alarm(OTHER("restarted"))
    assert "secret-topic-name" not in str(raised.value)
    assert topic.bodies[0].startswith("shadow monitoring: down")
    with pytest.raises(TypeError):
        alerts.alarm("a reason")  # type: ignore[arg-type]


class _Server(http.server.BaseHTTPRequestHandler):
    taken: list[tuple[str, bytes, dict]] = []
    status = 200

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Server.taken.append((self.path, body, dict(self.headers)))
        self.send_response(_Server.status)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


@pytest.mark.parametrize("status", [200, 429])
def test_https_post_returns_the_status_the_server_answered(status) -> None:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Server.taken.clear()
    _Server.status = status
    try:
        address = f"http://127.0.0.1:{server.server_address[1]}/topic"
        assert https_post(address, "body ü".encode(), {"Title": "t"}) == status
    finally:
        server.shutdown()
    ((path, body, headers),) = _Server.taken
    assert (path, body, headers["Title"]) == ("/topic", "body ü".encode(), "t")
