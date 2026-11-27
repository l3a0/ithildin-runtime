"""The stand-in alert channel: each alarm as one line on standard error, reaching no person.

#201 builds the channel that reaches the owner and the operator, the
environment's push topic and the dead-man check. Until it lands, every entry
point hands its modules this one, so an alarm is at least written where the
host's journal keeps it, and an operator-role session attends each shadow
decision, as the MVP plan on #4 says.

1. **An alarm is one line.** It starts `ALARM`, names the component, and
   carries the reason with each line break turned into a space, so a search of
   the host's journal finds the whole of it.
2. **A line that cannot be written raises.** The caller then knows the alarm
   went nowhere, as it would from a channel that refused it.
3. **`heartbeat` sends nothing**, because no dead-man check is built.
"""

from __future__ import annotations

import sys
from typing import TextIO

__all__ = ["LogAlerts"]


class LogAlerts:
    """An `Alerts` that writes each alarm to a stream, standard error unless told otherwise."""

    def __init__(self, component: str, stream: TextIO | None = None) -> None:
        self._component = component
        self._stream = stream

    def alarm(self, reason: str) -> None:
        stream = self._stream if self._stream is not None else sys.stderr
        line = " ".join(str(reason).splitlines())
        stream.write(f"ALARM {self._component}: {line}\n")
        stream.flush()

    def heartbeat(self) -> None:
        pass
