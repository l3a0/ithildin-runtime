"""An alarm as the alert channel carries it: what happened, what to run first, and its row.

A page on the owner's phone has three lines, as `docs/what-a-person-runs.md`'s
`#### What an alarm says` asks.

1. What happened.
2. The command to run first, and who may clear what the alarm reports.
3. The alarm's row in `docs/halts-and-drills.md`'s `## Every alarm and its
   drill`, which says what to check and which command follows.

So an `Alarm` carries each of those, and a `cause` beside them that stays the
same from one alarm to the next while one fault lasts. The forwarder on the
host and monitoring both page once per cause and repeat at a written interval,
so a store that times out twelve times a minute pages once, and a second cause
pages at once under its own. A reason names what this one call saw, such as
the key a store would not write, so it cannot be the key.

Each kind of alarm is one `Kind`, defined beside the code that raises it, and
`tests/test_alarms.py` checks that every `Kind` in the package names a row the
runbook holds. A table that maps a reason's text to its row was turned down,
because a reworded reason would lose its row with no test failing.

An alarm travels between processes as one line of the host's journal, which
`line` writes and `from_line` reads: `ALARM`, the component, and the alarm as
one JSON object. The worker that writes it and the forwarder that reads it ship
in one image, so a rollback moves both, and `ALARM_FORMAT` names the form.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import StrEnum

__all__ = [
    "ALARM_FORMAT",
    "EXPLAIN",
    "STATUS",
    "Alarm",
    "AlarmUnread",
    "Clears",
    "Kind",
]

ALARM_FORMAT = 1
# The two commands an alarm's second line names, which #203 builds.
STATUS = "ithildin status"
EXPLAIN = "ithildin halt explain"
_PREFIX = "ALARM "


class Clears(StrEnum):
    """Who may clear what an alarm reports.

    These are design.md's words under `### Rule 7: a halt is final for its decision`.
    """

    NOBODY = "nobody needed"
    OPERATOR = "the operator or the owner"
    OWNER = "the owner alone"
    # For a module that does not know the account's mode, such as the order path.
    BY_MODE = "the operator or the owner, and on a live account the owner alone"

    @classmethod
    def on(cls, live: bool) -> Clears:
        """The operator or the owner, and the owner alone on a live account."""
        return cls.OWNER if live else cls.OPERATOR


class AlarmUnread(ValueError):
    """A journal line that starts `ALARM` and is not an alarm of this form."""


def _one_line(text: str) -> str:
    return " ".join(str(text).splitlines())


@dataclass(frozen=True)
class Alarm:
    """One alarm: its cause, its reason, its runbook row, the first command and who clears."""

    cause: str
    reason: str
    section: str
    row: str
    first: str
    clears: Clears

    def __post_init__(self) -> None:
        for name in ("cause", "reason", "section", "row", "first"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"an alarm's {name} is text, not {type(value).__name__}")
            if not value.strip():
                raise ValueError(f"an alarm's {name} is empty")
        object.__setattr__(self, "clears", Clears(self.clears))

    def about(self, prefix: str, scope: str) -> Alarm:
        """The same alarm with `prefix: ` before its reason and its cause scoped to `scope`."""
        return replace(self, reason=f"{prefix}: {self.reason}", cause=f"{self.cause}:{scope}")

    def line(self, component: str) -> str:
        """The journal line for this alarm, with no line break in it."""
        body = {
            "format": ALARM_FORMAT,
            "cause": _one_line(self.cause),
            "reason": _one_line(self.reason),
            "section": _one_line(self.section),
            "row": _one_line(self.row),
            "first": _one_line(self.first),
            "clears": self.clears.value,
        }
        return f"{_PREFIX}{_one_line(component)} {json.dumps(body, ensure_ascii=False)}"

    @staticmethod
    def from_line(text: str) -> tuple[str, Alarm]:
        """The component and the alarm a journal line carries, or `AlarmUnread`."""
        if not text.startswith(_PREFIX):
            raise AlarmUnread("the line does not start ALARM")
        component, _, rest = text[len(_PREFIX) :].partition(" ")
        try:
            body = json.loads(rest)
        except ValueError:
            raise AlarmUnread("the line holds no JSON object after its component") from None
        if not component or not isinstance(body, dict):
            raise AlarmUnread("the line names no component or holds no JSON object")
        if body.get("format") != ALARM_FORMAT:
            raise AlarmUnread(f"the line is of format {body.get('format')!r}, not {ALARM_FORMAT}")
        fields = ("cause", "reason", "section", "row", "first", "clears")
        if set(body) != {"format", *fields}:
            raise AlarmUnread("the line holds other fields than an alarm's")
        try:
            return component, Alarm(**{name: body[name] for name in fields})
        except (TypeError, ValueError):
            raise AlarmUnread("the line's fields are not an alarm's") from None


@dataclass(frozen=True)
class Kind:
    """One kind of alarm: its cause, its runbook row, its first command and who clears it.

    Calling it with a reason makes the `Alarm`. `scope` adds to the cause what
    keeps two faults of one kind apart, such as the run or the account, and
    `clears` replaces who clears where it turns on the mode.
    """

    cause: str
    section: str
    row: str
    first: str = STATUS
    clears: Clears = Clears.NOBODY

    def __call__(self, reason: str, *, scope: str = "", clears: Clears | None = None) -> Alarm:
        cause = f"{self.cause}:{scope}" if scope else self.cause
        # A blank reason still pages, since a caller builds the alarm before the guard that
        # keeps a channel's error from stopping what follows.
        said = reason if isinstance(reason, str) and reason.strip() else "no reason given"
        if not isinstance(reason, str):
            raise TypeError(f"an alarm's reason is text, not {type(reason).__name__}")
        return Alarm(cause, said, self.section, self.row, self.first, clears or self.clears)
