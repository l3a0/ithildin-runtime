"""Where an alarm goes from a process that is not monitoring: one line in the host's journal.

Only monitoring and the host hold the address of the environment's push topic,
as `docs/halts-and-drills.md`'s `### What each environment drills as it
arrives` says. ntfy.sh takes a post to any topic name, so a container that can
reach it can post anywhere, and execution holds the broker key on paper and
live. So every other process writes its alarm to standard error, which the
host's journal keeps under its unit, and `runtime.forwarder` on the host reads
those lines and pages each.

1. **An alarm is one line.** `core.Alarm.line` writes it: `ALARM`, the
   component, and the alarm as one JSON object, with each line break in it
   turned into a space.
2. **A line that cannot be written raises.** The caller then knows the alarm
   went nowhere, as it would from a channel that refused it.
3. **`heartbeat` sends nothing.** Monitoring's dead-man ping is #258's.

`RecordedAlerts` is the backtest's channel, which keeps each alarm and sends
nothing, as the `Alerts` column of 05-building-blocks.md's table of modes says.
"""

from __future__ import annotations

import sys
from typing import TextIO

from ithildinruntime.core import Alarm

__all__ = ["LogAlerts", "RecordedAlerts"]


def _checked(alarm: object) -> Alarm:
    if not isinstance(alarm, Alarm):
        raise TypeError(f"an alarm is a core.Alarm, not {type(alarm).__name__}")
    return alarm


class LogAlerts:
    """An `Alerts` that writes each alarm as one journal line, to standard error by default."""

    def __init__(self, component: str, stream: TextIO | None = None) -> None:
        self._component = component
        self._stream = stream

    def alarm(self, alarm: Alarm) -> None:
        stream = self._stream if self._stream is not None else sys.stderr
        stream.write(f"{_checked(alarm).line(self._component)}\n")
        stream.flush()

    def heartbeat(self) -> None:
        pass


class RecordedAlerts:
    """An `Alerts` that keeps each alarm in `alarms` and sends nothing."""

    def __init__(self) -> None:
        self.alarms: list[Alarm] = []

    def alarm(self, alarm: Alarm) -> None:
        self.alarms.append(_checked(alarm))

    def heartbeat(self) -> None:
        pass
