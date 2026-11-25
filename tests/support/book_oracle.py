"""What a journal replays to, worked out a second way from the stored bytes.

It shares no code with `journal` or `core.codec`. It keeps no running totals:
each answer is summed afresh from the rows, in exact fractions, so a mistake in
the fold's bookkeeping has nothing to agree with. It assumes a journal whose
entries the fold accepts, with no fill in dispute.
"""

from __future__ import annotations

import json
from datetime import datetime
from fractions import Fraction

# From #142's item 13: an order is open in these states and in no other.
OPEN = ("Approved", "Sending", "InDoubt", "Acknowledged", "PartlyFilled")


def row(data: bytes) -> dict:
    return json.loads(data)


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def oracle(rows: list[dict]) -> dict:
    """The book, the orders and what the journal holds, as plain values."""
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    bodies = [r["body"] for r in rows]
    assert bodies[0]["kind"] == "opening"
    orders = {b["order"]["client_order_id"]: b["order"] for b in bodies if b["kind"] == "intent"}
    brokers = {b["broker_order_id"]: b["client_order_id"] for b in bodies if b["kind"] == "ack"}
    fills: dict[str, dict] = {}
    owners: dict[int, str] = {}
    for r, b in zip(rows, bodies, strict=True):
        if b["kind"] == "fill":
            if b["fill"]["fill_id"] not in fills:
                fills[b["fill"]["fill_id"]] = b["fill"]
                owners[r["seq"]] = brokers[b["fill"]["broker_order_id"]]
        elif b["kind"] == "intent":
            owners[r["seq"]] = b["order"]["client_order_id"]
        elif "client_order_id" in b:
            owners[r["seq"]] = b["client_order_id"]
    filled = {
        name: sum(
            (Fraction(f["qty"]) for f in fills.values() if brokers[f["broker_order_id"]] == name),
            Fraction(0),
        )
        for name in orders
    }
    states = {name: _state(name, order, filled[name], bodies) for name, order in orders.items()}
    positions: dict[tuple[str, str], Fraction] = {}
    for f in fills.values():
        order = orders[brokers[f["broker_order_id"]]]
        key = (order["strategy"], order["instrument"])
        positions[key] = positions.get(key, Fraction(0)) + Fraction(f["qty"])
    cash = Fraction(bodies[0]["cash"]) - sum(
        (Fraction(f["qty"]) * Fraction(f["price"]) for f in fills.values()), Fraction(0)
    )
    sent = {
        b["client_order_id"]: _time(r["built_at"])
        for r, b in zip(rows, bodies, strict=True)
        if b["kind"] == "sending"
    }
    return {
        "positions": {key: qty for key, qty in positions.items() if qty},
        "cash": cash,
        "open": {
            name: Fraction(order["qty"]) - filled[name]
            for name, order in orders.items()
            if states[name] in OPEN
        },
        "in_doubt": sorted(
            {o["instrument"] for n, o in orders.items() if states[n] in ("Sending", "InDoubt")}
        ),
        "orders": {
            name: {
                "state": states[name],
                "filled": filled[name],
                "broker": next((b for b, n in brokers.items() if n == name), None),
                "entries": [
                    (r["seq"], r["body"]["kind"]) for r in rows if owners.get(r["seq"]) == name
                ],
                "sent_at": sent.get(name),
            }
            for name in orders
        },
        "fills": sorted(fills),
        "decisions": [r["seq"] for r in rows if r["body"]["kind"] == "decision"],
        "reads": [r["seq"] for r in rows if r["body"]["kind"] == "read"],
        "opened_at": _time(rows[0]["built_at"]),
        "sent_at": max(sent.values(), default=None),
        "through": rows[-1]["seq"],
    }


def _state(name: str, order: dict, filled: Fraction, bodies: list[dict]) -> str:
    kinds = [
        ("approved" if b["verdict"]["approved"] else "refused") if b["kind"] == "verdict"
        else b["kind"]
        for b in bodies
        if b.get("client_order_id") == name
    ]  # fmt: skip
    for kind, state in (
        ("abandoned", "Abandoned"),
        ("rejected", "Rejected"),
        ("expired", "Expired"),
        ("refused", "Refused"),
    ):
        if kind in kinds:
            return state
    if filled == Fraction(order["qty"]):
        return "Filled"
    if filled:
        return "PartlyFilled"
    last = {
        "approved": "Approved",
        "sending": "Sending",
        "in_doubt": "InDoubt",
        "ack": "Acknowledged",
    }
    return last[kinds[-1]] if kinds else "Intended"


def flattened(view) -> dict:
    """A `View` in the oracle's terms, read through its public fields alone."""
    book = view.book
    return {
        "positions": {
            (s, i): Fraction(q)
            for s, held in book.positions_by_strategy.items()
            for i, q in held.items()
        },
        "cash": Fraction(book.cash),
        "open": {
            o.client_order_id: Fraction(o.qty)
            for s, opens in book.open_orders_by_strategy.items()
            for o in opens
            if o.strategy == s
        },
        "in_doubt": list(book.in_doubt),
        "orders": {
            name: {
                "state": o.state.value,
                "filled": Fraction(o.filled),
                "broker": o.broker_order_id,
                "entries": [(e.seq, e.kind) for e in o.entries],
                "sent_at": o.sent_at,
            }
            for name, o in view.orders.items()
        },
        "fills": sorted(view.fills),
        "decisions": list(view.decisions),
        "reads": list(view.reads),
        "opened_at": view.opened_at,
        "sent_at": view.sent_at,
        "through": book.replayed_through,
    }
