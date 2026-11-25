"""The refusal a broker's call ends in when it gives no answer.

It names the call and the type of error and nothing the broker or its library
said. The receive loop puts an uncaught error's text in an alarm and in a
stored halt, and a broker's text can hold a key or an account number.
"""

from __future__ import annotations

import re

__all__ = ["BrokerUnavailable"]

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")


class BrokerUnavailable(Exception):
    """A broker's call that failed or did not answer. Raise it `from None`."""

    def __init__(self, call: str, error: str) -> None:
        self.call = _word(call)
        self.error = _word(error)
        super().__init__(f"the broker's {self.call} failed with {self.error}")


def _word(value: object) -> str:
    """A name as code spells one, so no sentence a broker wrote can stand in for it."""
    return value if isinstance(value, str) and _WORD.fullmatch(value) else "Unnamed"
