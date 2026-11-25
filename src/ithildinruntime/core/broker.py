"""The refusal a broker's call ends in when it gives no answer.

It names the call and the type of error and nothing the broker or its library
said. The receive loop puts an uncaught error's text in an alarm and in a
stored halt, and a broker's text can hold a key or an account number. So it
takes the error itself, or its type, and never a word an adapter writes.
"""

from __future__ import annotations

import re

__all__ = ["BrokerUnavailable", "error_name"]

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")


def error_name(error: object) -> str:
    """The name of an error's type, as code spells one, and never a sentence.

    It never raises, whatever an adapter's own kind of error does as it is read.
    """
    try:
        name = error.__name__ if issubclass(type(error), type) else type(error).__name__
    except BaseException:
        name = None
    return _word(name)


class BrokerUnavailable(Exception):
    """A broker's call that failed or did not answer. Raise it `from None`."""

    def __init__(self, call: str, error: BaseException | type) -> None:
        self.call = _word(call)
        # A subclass is an adapter's own, whose word could be a key.
        own = type(error) is BrokerUnavailable
        self.error = _word(getattr(error, "error", None)) if own else error_name(error)
        super().__init__(f"the broker's {self.call} failed with {self.error}")


def _word(value: object) -> str:
    return value if type(value) is str and _WORD.fullmatch(value) else "Unnamed"
