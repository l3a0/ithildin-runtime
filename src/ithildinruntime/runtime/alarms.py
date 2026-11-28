"""Each kind of alarm the commands and the forwarder raise.

`core.alarms` says what a `Kind` carries. Each names the opening words of its
row in `docs/halts-and-drills.md`, which `tests/test_alarms.py` checks. The
receive loop's kinds are in `runtime/loop/alarms.py`, and the supervisor's in
`runtime/supervisor/alarms.py`, so neither package imports `runtime`.
"""

from __future__ import annotations

from ithildinruntime.core import Kind

__all__ = [
    "FORWARDER_UNREAD",
    "PLAN_UNRUN",
    "SETTING",
]

_SUPERVISOR = "Alarms of the supervisor and the commands"
_CHANNEL = "Alarms of the alert channel"

# The commands'
PLAN_UNRUN = Kind("command.plan-unrun", _SUPERVISOR, "A worker's plan it cannot run")
SETTING = Kind(
    "command.setting",
    _SUPERVISOR,
    "A setting, a credential file or a pin file a command cannot use",
)

# The forwarder's own. `adapters.ntfy` holds the alarm on the channel's load
FORWARDER_UNREAD = Kind(
    "forwarder.unread", _CHANNEL, "A line of a runtime unit that starts `ALARM` and is not an alarm"
)
