"""The one judge of a quote, which data, the mark and execution call.

A quote is read for its midpoint, so it is as old as the older of its bid's and
its ask's stamps. `quote_faults` gives every reason a quote cannot be read,
and no answer is usable but an empty one. docs/architecture/03-context-and-scope.md's
`#### How a quote is judged` says why each check is there.

The age is measured at `now`, at each read. A stage that reads no clock passes
the decision time, and the risk gate passes its own `now`, so a quote that was
good at the decision halts the order that meets it too late. `CLOCK_BOUND`, the
bound on how far the machine's clock may be off, is added to every age. Until
#134 builds the check of that clock nothing measures the offset, so a machine
whose clock runs further behind than the bound reads an old quote as fresh.

An age is compared in whole microseconds and a spread exactly, as
`core/exact.py` says. `quote_faults` raises nothing, and the other functions
here raise nothing but `Halt`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal

from .codec import INT_LIMIT
from .exact import exact_product, exact_total, finite
from .halt import Halt
from .names import is_unstated
from .records import InstrumentId, Quote, Snapshot
from .sessions import Session, load_sessions, require_utc

__all__ = ["CLOCK_BOUND", "maximum_faults", "midpoint", "quote_faults", "require_quote"]

CLOCK_BOUND = timedelta(seconds=1)

_MICROSECOND = timedelta(microseconds=1)
_BOUND = CLOCK_BOUND // _MICROSECOND
# A written maximum is ten times the clock bound or more, the margin #50 asks for.
_LEAST_AGE = 10 * _BOUND
_SECOND = 1_000_000
_HALF = Decimal("0.5")


def maximum_faults(max_age: object, max_spread_share: object) -> tuple[str, ...]:
    """What is wrong with a written maximum age, in seconds, and a written maximum spread."""
    found = []
    if type(max_age) is not int or not (_LEAST_AGE <= max_age * _SECOND and max_age < INT_LIMIT):
        found.append(
            f"a maximum age of {max_age!r}, which is not a whole number of seconds from "
            f"{_LEAST_AGE // _SECOND} to below {INT_LIMIT}"
        )
    if (
        not isinstance(max_spread_share, Decimal)
        or not max_spread_share.is_finite()
        or not 0 < max_spread_share < 1
    ):
        found.append(
            f"a maximum spread of {max_spread_share!r}, which is not a decimal above 0 and below 1"
        )
    return tuple(found)


def quote_faults(quote: Quote, decision_at: datetime, now: datetime) -> tuple[str, ...]:
    """Every reason `quote` cannot be read for the decision at `decision_at`, as of `now`."""
    return tuple(_Judge(quote, decision_at, now).faults)


def require_quote(snapshot: Snapshot, instrument: InstrumentId, now: datetime) -> Quote:
    """The instrument's quote, which halts unless the snapshot holds one that can be read."""
    quote = snapshot.quotes.get(instrument)
    if quote is None:
        raise Halt(f"{instrument} has no quote in the snapshot")
    if faults := quote_faults(quote, snapshot.decision_at, now):
        raise Halt(f"{instrument}'s quote cannot be read: {'; '.join(faults)}")
    return quote


def midpoint(quote: Quote) -> Decimal:
    """Half the sum of the bid and the ask, exact, whatever `quote_faults` says of them."""
    who = f"{quote.instrument}'s"
    total = exact_total(
        f"{who} midpoint", (finite(f"{who} bid", quote.bid), finite(f"{who} ask", quote.ask))
    )
    return exact_product(f"{who} midpoint", total, _HALF)


class _Judge:
    """One quote's checks, in order, each of which adds a reason and raises nothing."""

    def __init__(self, quote: Quote, decision_at: datetime, now: datetime) -> None:
        self._quote = quote
        self._decision_at = decision_at
        self._now = now
        self.faults: list[str] = []
        self._maxima()
        self._at_now()
        self._prices()
        self._said()
        self._stamps()

    def _maxima(self) -> None:
        found = maximum_faults(self._quote.max_age, self._quote.max_spread_share)
        self.faults.extend(f"{self._quote.instrument} is read under {fault}" for fault in found)

    def _at_now(self) -> None:
        now, decision = self._utc(self._now), self._utc(self._decision_at)
        if now is None or decision is None or now < decision:
            self.faults.append(
                f"now is {self._shown(self._now)}, not a UTC time from the decision at "
                f"{self._shown(self._decision_at)} on"
            )

    def _prices(self) -> None:
        quote = self._quote
        for name in ("bid", "ask"):
            price = getattr(quote, name)
            if not _positive(price):
                self.faults.append(
                    f"its {name} is {_price(price)}, received as {self._received(name)}, and not "
                    "a price above zero"
                )
        if not (_positive(quote.bid) and _positive(quote.ask)):
            return
        if quote.ask < quote.bid:  # type: ignore[operator]
            self.faults.append(f"its ask of {quote.ask} is below its bid of {quote.bid}")
        elif self._wide():
            self.faults.append(
                f"its bid of {quote.bid} and ask of {quote.ask} are further apart than the written "
                f"{quote.max_spread_share} of their midpoint"
            )

    def _wide(self) -> bool:
        """Whether the spread is past the written share of the midpoint, or cannot be compared."""
        quote = self._quote
        if maximum_faults(_LEAST_AGE // _SECOND, quote.max_spread_share):
            return False
        try:
            spread = exact_total("the spread", (quote.ask, quote.bid.copy_negate()))  # type: ignore[union-attr,arg-type]
            return spread > exact_product("the spread", quote.max_spread_share, midpoint(quote))  # type: ignore[arg-type]
        except Halt:
            return True

    def _said(self) -> None:
        quote = self._quote
        if quote.realtime is not True:
            self.faults.append(
                f"its realtime flag is received as {self._received('realtime')}, and only true "
                "says its delay is none"
            )
        unstated = is_unstated(quote.status) or is_unstated(quote.normal_status)
        if unstated or quote.status != quote.normal_status:
            self.faults.append(
                f"its status is {quote.status!r}, and the stated normal one is "
                f"{quote.normal_status!r}"
            )
        if is_unstated(quote.price_kind):
            self.faults.append(f"{quote.instrument}'s quote is of unstated price kind")

    def _stamps(self) -> None:
        quote = self._quote
        captured = self._utc(quote.captured_at)
        stamps = []
        for name in ("bid", "ask"):
            at = self._utc(getattr(quote, f"{name}_at"))
            raw = self._received(f"{name}_time")
            if at is None:
                self.faults.append(f"its {name} has the stamp {raw}, which is no instant")
                continue
            stamps.append((at, name, raw))
            if captured is not None and _microseconds(at, captured) > _BOUND:
                self.faults.append(
                    f"its {name} has the stamp {raw}, at {at.isoformat()}, which is later than its "
                    f"capture at {captured.isoformat()}"
                )
        self._captured(captured)
        now, age = self._utc(self._now), quote.max_age
        if now is None or len(stamps) < 2 or maximum_faults(age, _HALF):
            return
        older, name, raw = min(stamps)
        aged = _microseconds(now, older) + _BOUND
        if aged > age * _SECOND:  # type: ignore[operator]
            self.faults.append(
                f"its {name} has the stamp {raw}, at {older.isoformat()}, which is "
                f"{_seconds(aged - _BOUND)} seconds old at {now.isoformat()}, and with the clock "
                f"bound of {_seconds(_BOUND)} past the written maximum of {age}, and it was "
                f"captured at {self._shown(quote.captured_at)}"
            )

    def _captured(self, captured: datetime | None) -> None:
        decision = self._utc(self._decision_at)
        session = None if decision is None else _session(decision)
        if (
            captured is not None
            and decision is not None
            and session is not None
            and session.open <= captured <= decision
        ):
            return
        opened = "no session" if session is None else session.open.isoformat()
        self.faults.append(
            f"it was captured at {self._shown(self._quote.captured_at)}, not from the open at "
            f"{opened} to the decision at {self._shown(self._decision_at)}"
        )

    def _received(self, name: str) -> str:
        received = self._quote.received
        return repr(received.get(name)) if isinstance(received, Mapping) else "nothing"

    @staticmethod
    def _utc(at: object) -> datetime | None:
        try:
            return require_utc("a time", at)
        except Halt:
            return None

    @staticmethod
    def _shown(at: object) -> str:
        return at.isoformat() if isinstance(at, datetime) else repr(at)


def _session(at: datetime) -> Session | None:
    try:
        return load_sessions().opened_by(at)
    except Halt:
        return None


def _positive(price: object) -> bool:
    return isinstance(price, Decimal) and price.is_finite() and price > 0


def _price(price: object) -> str:
    return str(price) if isinstance(price, Decimal) else repr(price)


def _microseconds(later: datetime, earlier: datetime) -> int:
    return (later - earlier) // _MICROSECOND


def _seconds(microseconds: int) -> str:
    return f"{Decimal(microseconds) / _SECOND:f}"
