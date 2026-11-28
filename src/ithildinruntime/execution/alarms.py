"""Each kind of alarm the order path, recover, send and the execution worker raise.

`core.alarms` says what a `Kind` carries. Each names the opening words of its
row in `docs/halts-and-drills.md`, which `tests/test_alarms.py` checks. A
caller scopes an alarm about one order by the order's client order id, so two
orders in doubt page apart, and `order_path.Named` scopes every alarm of an
account by its alias.
"""

from __future__ import annotations

from ithildinruntime.core import EXPLAIN, STATUS, Clears, Kind

__all__ = [
    "ANSWER_REFUSED",
    "CLAIM_TAKEN",
    "IN_DOUBT",
    "LEASE_ABSENT",
    "LEASE_RELEASE",
    "LEASE_REWRITE",
    "LEASE_TAKEN",
    "LEGS_STAND",
    "LOSS_FIRED",
    "LOSS_PAST",
    "MOVED_BY_ANOTHER",
    "NOT_SETTLED",
    "NOTHING_SENT",
    "OPEN_PAST_CLOSE",
    "OTHER_PLAN",
    "PROCESS_ENDS",
    "TAKEOVER_FAILED",
    "UNJOURNALED",
]

_ORDER = "Alarms on the order path"
_LEASE = "Alarms on leases and takeover"

IN_DOUBT = Kind(
    "execution.in-doubt", _ORDER, "An order first journaled `InDoubt`", EXPLAIN, Clears.BY_MODE
)
UNJOURNALED = Kind(
    "execution.unjournaled",
    _ORDER,
    "An answer of the broker's, or that an order was never sent, that the journal did not take",
    STATUS,
    Clears.BY_MODE,
)
ANSWER_REFUSED = Kind(
    "execution.answer-refused",
    _ORDER,
    "An answer kept as in doubt that the journal shows settled, or an answer the journal refuses",
    EXPLAIN,
    Clears.BY_MODE,
)
LOSS_FIRED = Kind(
    "execution.loss-stop", _ORDER, "The loss stop fired and latched", STATUS, Clears.BY_MODE
)
LOSS_PAST = Kind(
    "execution.loss-past", _ORDER, "The day's loss read past its stop", STATUS, Clears.BY_MODE
)
MOVED_BY_ANOTHER = Kind(
    "execution.moved-by-another",
    _ORDER,
    "A number of an order's `verdict` or `sending` that another writer took",
    EXPLAIN,
    Clears.BY_MODE,
)
OTHER_PLAN = Kind(
    "execution.other-plan",
    _ORDER,
    "A plan of the decision in the journal that is not the send stage's own",
    EXPLAIN,
    Clears.BY_MODE,
)
CLAIM_TAKEN = Kind(
    "execution.claim-taken",
    _ORDER,
    "The send stage's claim on a decision is another call's",
    EXPLAIN,
    Clears.BY_MODE,
)
NOTHING_SENT = Kind(
    "execution.nothing-sent",
    _ORDER,
    "A partner refused when the gate is asked about the whole plan, so nothing is sent",
    EXPLAIN,
    Clears.BY_MODE,
)
LEGS_STAND = Kind(
    "execution.legs-stand",
    _ORDER,
    "A send that answers anything but `Acknowledged`, or raises",
    EXPLAIN,
    Clears.BY_MODE,
)
NOT_SETTLED = Kind(
    "execution.unsettled",
    _ORDER,
    "An order of a plan a send stage left unfinished that recover cannot settle",
    EXPLAIN,
    Clears.BY_MODE,
)
OPEN_PAST_CLOSE = Kind(
    "execution.open-past-close",
    _ORDER,
    "An order that the book and the broker both hold open past its session's close",
)
PROCESS_ENDS = Kind(
    "execution.process-ends",
    _ORDER,
    "An execution process that ends while its journal lacks an answer of the broker's",
    STATUS,
    Clears.BY_MODE,
)
LEASE_TAKEN = Kind(
    "execution.lease-taken",
    _LEASE,
    "The account's or the plan's lease taken over after it expired unrenewed",
)
TAKEOVER_FAILED = Kind(
    "execution.takeover-failed",
    _LEASE,
    "A takeover's broker list read, capture write or journal write failed",
)
LEASE_REWRITE = Kind(
    "execution.lease-rewrite",
    _LEASE,
    "The store does not answer the take of the account's lease, or the rewrite that clears "
    "`owed` after a takeover",
)
LEASE_ABSENT = Kind(
    "execution.lease-absent",
    _LEASE,
    "An account's lease absent while its journal holds a `sending` or cancel entry",
    EXPLAIN,
    Clears.BY_MODE,
)
LEASE_RELEASE = Kind(
    "execution.lease-release", _LEASE, "A release of the account's lease that did not land"
)
