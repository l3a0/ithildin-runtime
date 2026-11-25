"""An order's moves from state to state, in one table.

docs/architecture/06-runtime.md's `## The life of an order` draws it. A move
the table does not hold halts, and no branch treats an unnamed case as the
default, as docs/design.md's `### The code holds its own rules, and every check
runs in production` asks.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from ithildinruntime.core import Halt, OrderState

__all__ = ["DOUBTFUL", "ENDED", "MOVES", "OPEN", "Move", "moved"]


class Move(StrEnum):
    """What an entry does to an order: its kind, or one of a kind's two outcomes."""

    INTENT = "an intent"
    APPROVED = "an approved verdict"
    REFUSED = "a refused verdict"
    ABANDONED = "an abandoned"
    SENDING = "a sending"
    ACK = "an ack"
    REJECTED = "a rejected"
    IN_DOUBT = "an in_doubt"
    PART_FILL = "a fill"
    LAST_FILL = "a fill of the rest"
    EXPIRED = "an expired"


_S = OrderState
_TABLE: tuple[tuple[Move, tuple[OrderState | None, ...], OrderState], ...] = (
    (Move.INTENT, (None,), _S.INTENDED),
    (Move.APPROVED, (_S.INTENDED,), _S.APPROVED),
    (Move.REFUSED, (_S.INTENDED,), _S.REFUSED),
    (Move.ABANDONED, (_S.INTENDED, _S.APPROVED, _S.SENDING), _S.ABANDONED),
    (Move.SENDING, (_S.APPROVED,), _S.SENDING),
    (Move.ACK, (_S.SENDING, _S.IN_DOUBT), _S.ACKNOWLEDGED),
    (Move.REJECTED, (_S.SENDING, _S.ACKNOWLEDGED), _S.REJECTED),
    (Move.IN_DOUBT, (_S.SENDING,), _S.IN_DOUBT),
    (Move.PART_FILL, (_S.ACKNOWLEDGED, _S.PARTLY_FILLED), _S.PARTLY_FILLED),
    (Move.LAST_FILL, (_S.ACKNOWLEDGED, _S.PARTLY_FILLED), _S.FILLED),
    (Move.EXPIRED, (_S.ACKNOWLEDGED, _S.PARTLY_FILLED), _S.EXPIRED),
)
MOVES: Mapping[tuple[Move, OrderState | None], OrderState] = MappingProxyType(
    {(move, start): end for move, starts, end in _TABLE for start in starts}
)

# An order in one of these states is in the book, at its unfilled remainder.
OPEN = frozenset({_S.APPROVED, _S.SENDING, _S.IN_DOUBT, _S.ACKNOWLEDGED, _S.PARTLY_FILLED})
# An order in one of these may or may not be with the broker.
DOUBTFUL = frozenset({_S.SENDING, _S.IN_DOUBT})
# No entry moves an order out of one of these.
ENDED = frozenset({_S.REFUSED, _S.ABANDONED, _S.FILLED, _S.REJECTED, _S.EXPIRED})


def moved(seq: int, name: str, move: Move, state: OrderState | None) -> OrderState:
    """The state entry `seq` leaves an order in, or a halt where the table holds no such move."""
    end = MOVES.get((move, state))
    if end is None:
        stands = "has no entry" if state is None else f"is {state.value}"
        raise Halt(f"entry {seq} is {move.value} of the order {name}, which {stands}")
    return end
