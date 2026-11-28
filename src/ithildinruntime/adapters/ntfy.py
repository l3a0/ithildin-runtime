"""The environment's push topic on ntfy, which pages once per cause and repeats while it lasts.

Every environment posts to ntfy's free public server, and a topic's name is its
only password. So the topic's address is a secret that enters no tracked file,
no page and no error text here. The host's provisioning step writes it to
memory-backed storage, and only the forwarder on the host and monitoring read
it, as `adapters/alerts.py` says.

**One page per cause.** A `Pager` keys each alarm by its component and its
`cause`, and holds, in memory, when that cause began, when it was last seen and
when it last paged. The component leaves out the instance, so one fault on
every instance of a component pages once, and the same kind of fault in two
components pages twice.

1. An alarm of a cause not seen for `Repeats.ends_after` pages at once.
2. One of a cause still going pages again once `Repeats.first` has passed
   since its last page, for the first `Repeats.first_for` of the cause, and
   once `Repeats.later` has passed after that. A cause that lasts a whole day
   then pages 27 times, 4 in its first hour and then once an hour.
3. Any other alarm is held. A held alarm counts as sent, so an `Alerts` that
   wraps the pager raises only on a push the server refused.

A repeat names how many alarms of the cause arrived since it began, and the
first page after a cause ended names how many of its alarms were held and never
paged. A second cause pages at once under its own key. The keys live in memory, because a
machine holds no state and the store may be what is down. The price is that a
restart pages each cause again at once, which is the cheaper failure than a
dropped page.

**What a page says.** Its title names the environment and the cause. Its body
has the three lines `docs/what-a-person-runs.md`'s `#### What an alarm says`
asks for: what happened, the command to run first with who may clear, and the
alarm's row in `docs/halts-and-drills.md`. A body longer than ntfy's 4,096
bytes is not shown as text, so the first line is cut to fit and the second and
third stay whole.

**The measure of load.** The public server takes about 250 messages a day from
one address, and answers 429 past it. The pager counts the pages it sends each
UTC day, and the page that brings the count to `Load.warn_at`, 75% of the
limit, is followed by one more that says the next cause may go unheard by name.

**A refused push.** A post the server answers with anything but 200, or does
not answer, is refused. The pager hands a `Refusal` to whoever built it, which
names the cause, the time and the answer, never the address, and counts the
cause as paged, so the next try waits for the cause's interval. The forwarder
writes it as a marker that #258's monitoring reads before each dead-man ping.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from ithildinruntime.core import Alarm, Kind

__all__ = [
    "PAGE_BYTES",
    "PAGES_NEAR_LIMIT",
    "RUNBOOK",
    "DayCount",
    "Load",
    "NtfyAlerts",
    "Page",
    "Pager",
    "Post",
    "PushRefused",
    "Refusal",
    "Repeats",
    "Sent",
    "https_post",
    "page_of",
]

# ntfy's largest message shown as text, from its publishing docs' limits.
PAGE_BYTES = 4096
RUNBOOK = "https://github.com/l3a0/ithildin-runtime/blob/main/docs/halts-and-drills.md"
PAGES_NEAR_LIMIT = Kind(
    "channel.pages-near-limit",
    "Alarms of the alert channel",
    "Pages a day reach 75% of the push server's daily limit",
)

# Who posts and how: the address, the body, the headers, and the status the server answered.
# An error raised means no answer.
Post = Callable[[str, bytes, Mapping[str, str]], int]


@dataclass(frozen=True)
class Repeats:
    """When a cause that lasts pages again, and how long a silent cause lasts."""

    first: timedelta
    first_for: timedelta
    later: timedelta
    ends_after: timedelta


@dataclass(frozen=True)
class Load:
    """The server's daily limit from one address, and the count that pages that it nears."""

    limit: int
    warn_at: int


@dataclass(frozen=True)
class Page:
    title: str
    body: str


@dataclass(frozen=True)
class Refusal:
    """A push the server would not take: its cause, when, and the answer, with no address."""

    cause: str
    at: datetime
    answer: str


class PushRefused(Exception):
    """A page the push server would not take."""


class Sent(StrEnum):
    SENT = "sent"
    HELD = "held"
    REFUSED = "refused"


class DayCount:
    """The pages sent each UTC day, kept in memory. The forwarder keeps its own in a file."""

    def __init__(self, day: date | None = None, pages: int = 0) -> None:
        self._day = day
        self._pages = pages

    def add(self, day: date) -> int:
        """Count one page on `day`, and return the day's count so far."""
        if day != self._day:
            self._day, self._pages = day, 0
        self._pages += 1
        return self._pages


@dataclass
class _Cause:
    began: datetime
    seen: datetime
    paged: datetime
    since_page: int = 0
    alarms: int = 1


def anchor(heading: str) -> str:
    """A heading's anchor as GitHub forms it: lower case, punctuation dropped, spaces as hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def _cut(text: str, room: int) -> str:
    """`text` cut to `room` bytes of UTF-8 with an ellipsis, on a character's edge."""
    data = text.encode()
    if len(data) <= room:
        return text
    return data[: max(room - 3, 0)].decode(errors="ignore") + "…"


def page_of(environment: str, component: str, alarm: Alarm, again: str = "") -> Page:
    """The page for one alarm, cut to `PAGE_BYTES` with its second and third lines whole."""
    second = f"Run first: {alarm.first}. Who clears: {alarm.clears.value}."
    third = f'Row: "{alarm.row}" in {RUNBOOK}#{anchor(alarm.section)}'
    tail = f"\n{second}\n{third}"
    first = _cut(
        f"{environment} {component}: {alarm.reason}{again}", PAGE_BYTES - len(tail.encode())
    )
    title = f"ithildin {environment}: {alarm.cause}".encode("ascii", "replace").decode()
    return Page(title[:200], f"{first}{tail}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follows no redirect, which would turn the post into a GET and read as sent."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def https_post(address: str, body: bytes, headers: Mapping[str, str], timeout: float = 10.0) -> int:
    """Post `body` to `address`, and return the status the server answered, a redirect's too."""
    request = urllib.request.Request(address, data=body, headers=dict(headers), method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as answer:
            return int(answer.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


class Pager:
    """Pages each alarm's cause once and again at its interval, through `post` to `address`."""

    def __init__(
        self,
        *,
        environment: str,
        address: str,
        post: Post,
        repeats: Repeats,
        load: Load,
        count: DayCount | None = None,
        refused: Callable[[Refusal], None] = lambda refusal: None,
    ) -> None:
        self._environment = environment
        self._address = address
        self._post = post
        self._repeats = repeats
        self._load = load
        self._count = count if count is not None else DayCount()
        self._refused = refused
        self._causes: dict[str, _Cause] = {}

    def __repr__(self) -> str:
        # The address is the topic's only password, so no repr or log line shows it.
        return f"Pager({self._environment!r})"

    def offer(self, component: str, alarm: Alarm, at: datetime) -> Sent:
        """Page `alarm` now, or hold it as a repeat of a cause that paged within its interval."""
        key = f"{component}:{alarm.cause}"
        cause = self._causes.get(key)
        if cause is None or at - cause.seen >= self._repeats.ends_after:
            self._forget(at)
            self._causes[key] = _Cause(began=at, seen=at, paged=at)
            held = (
                f". Before this, {cause.since_page} alarms of this cause from "
                f"{cause.paged:%Y-%m-%dT%H:%M}Z to {cause.seen:%Y-%m-%dT%H:%M}Z were held and "
                "not paged"
                if cause is not None and cause.since_page
                else ""
            )
            return self._send(component, alarm, at, held)
        cause.seen, cause.alarms = at, cause.alarms + 1
        young = at - cause.began < self._repeats.first_for
        interval = self._repeats.first if young else self._repeats.later
        if at - cause.paged < interval:
            cause.since_page += 1
            return Sent.HELD
        again = (
            f". Repeated: {cause.alarms} alarms of this cause since "
            f"{cause.began:%Y-%m-%dT%H:%M}Z, {cause.since_page} of them held"
        )
        cause.paged, cause.since_page = at, 0
        return self._send(component, alarm, at, again)

    def _forget(self, at: datetime) -> None:
        """Drop each cause that has ended, so a fault that keeps naming new causes holds no memory.

        A cause that ended with alarms held keeps its count until its own next alarm, which
        names them, or until twice `Repeats.ends_after` has passed.
        """
        ended = 2 * self._repeats.ends_after
        for key in [k for k, c in self._causes.items() if at - c.seen >= ended]:
            del self._causes[key]

    def _send(self, component: str, alarm: Alarm, at: datetime, again: str) -> Sent:
        sent = self._push(alarm.cause, page_of(self._environment, component, alarm, again), at)
        if sent is not Sent.SENT:
            return sent
        if self._count.add(at.date()) == self._load.warn_at:
            near = PAGES_NEAR_LIMIT(
                f"{self._load.warn_at} pages went out today, of the push server's limit of about "
                f"{self._load.limit} a day from one address. Past the limit the server refuses "
                "each page, and each refusal leaves only the marker of a refused push"
            )
            if self._push(near.cause, page_of(self._environment, "channel", near), at) is Sent.SENT:
                self._count.add(at.date())
        return Sent.SENT

    def _push(self, cause: str, page: Page, at: datetime) -> Sent:
        headers = {"Title": page.title, "Priority": "high", "Tags": "warning"}
        try:
            status = self._post(self._address, page.body.encode(), headers)
        except Exception as error:
            # The type alone, since an error's words can carry the address.
            answer = f"no answer: {type(error).__name__}"
        else:
            if status == 200:
                return Sent.SENT
            answer = f"HTTP {status}"
        self._refused(Refusal(cause, at, answer))
        return Sent.REFUSED


class NtfyAlerts:
    """Monitoring's `Alerts`: each alarm offered to a `Pager`, raising on a push refused."""

    def __init__(self, pager: Pager, now: Callable[[], datetime], component: str = "monitoring"):
        self._pager = pager
        self._now = now
        self._component = component

    def alarm(self, alarm: Alarm) -> None:
        if not isinstance(alarm, Alarm):
            raise TypeError(f"an alarm is a core.Alarm, not {type(alarm).__name__}")
        if self._pager.offer(self._component, alarm, self._now()) is Sent.REFUSED:
            raise PushRefused(f"the push server refused the page of {alarm.cause}")

    def heartbeat(self) -> None:
        pass
