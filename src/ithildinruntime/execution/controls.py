"""The blobs of an account, a strategy and a run that stop an order.

docs/design.md's `### Rule 9, fourth item: what is checked right before a broker
call` lists them. Each is read with one `get`, and a folder with one `list`.
Only a key that holds no blob, and an empty list, read as nothing standing. Any
other answer stops the order, and its reason names the error's code and never
the store's text.

A control that halts the decision is read before the strategy's, so a strategy
that is off never hides the run's halt. The plan's lease and the account's lease
tag are read here too once #11 and #148 build them.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from ithildinruntime.core import (
    SEND_STAGE,
    BlobMissing,
    BlobStore,
    StoreError,
    account_halts_prefix,
    kill_switch_key,
    loss_stop_key,
    off_switch_key,
    result_key,
    run_halt_key,
    strategy_halts_prefix,
)

from .patience import Tries, code_of

__all__ = ["Controls", "Stopping", "Where"]


class Where(NamedTuple):
    """What an order's controls are filed under, as its `decision` entry names it."""

    strategy: str
    deployment: str
    run: str
    decision_at: datetime


class Stopping(NamedTuple):
    """A control that stops an order, and whether it halts the decision too.

    `down` says the store did not answer by the deadline, so the stop is an
    outage the order path pages once.
    """

    reason: str
    halts: bool
    down: bool = False


class _Read(NamedTuple):
    """One control: `found` where it stands, and `error` where it could not be read."""

    found: str | None
    error: str | None
    down: bool = False


class Controls:
    """The controls of one account, read afresh at each call."""

    def __init__(self, store: BlobStore, account: str) -> None:
        self._store = store
        self._account = account

    def stopping(self, where: Where, tries: Tries, *, with_stop: bool) -> Stopping | None:
        """The first control that stops an order, or `None` where every one read as absent.

        `with_stop` reads the loss stop too. The check ahead of the gate leaves
        it out, because the gate is handed the stop's state and names it.
        """
        account, (strategy, deployment, run, at) = self._account, where
        halting: list[tuple[str, str, bool]] = [
            ("kill switch", kill_switch_key(account), False),
            ("account halt", account_halts_prefix(account), True),
            ("run halt", run_halt_key(deployment, run), False),
            ("this decision's send result", result_key(deployment, run, at, SEND_STAGE), False),
        ]
        if with_stop:
            halting.append(("loss stop", loss_stop_key(account), False))
        for name, key, folder in halting:
            read = self._read(key, folder, tries)
            if read.error is not None:
                return Stopping(f"{name} unreadable: {read.error}", True, read.down)
            if read.found is not None:
                return Stopping(f"{name} stands{read.found}", True)
        return self.strategy(strategy, tries)

    def strategy(self, strategy: str, tries: Tries) -> Stopping | None:
        """The strategy's own control that stops its orders, or `None` where neither stands.

        Its off switch and its halts stop the strategy and not the decision. One
        that cannot be read halts the decision.
        """
        account = self._account
        for name, key, folder in (
            ("strategy off switch", off_switch_key(account, strategy), False),
            ("strategy halt", strategy_halts_prefix(account, strategy), True),
        ):
            read = self._read(key, folder, tries)
            if read.error is not None:
                return Stopping(f"{name} unreadable: {read.error}", True, read.down)
            if read.found is not None:
                return Stopping(f"strategy off: {name} stands{read.found}", False)
        return None

    def _read(self, key: str, folder: bool, tries: Tries) -> _Read:
        try:
            if not folder:
                tries(lambda: self._store.get(key))
                return _Read("", None)
            keys = tries(lambda: self._store.list(key, None))
            if not isinstance(keys, list | tuple):
                return _Read(None, type(keys).__name__)
            if not keys:
                return _Read(None, None)
            return _Read(f": {str(keys[0]).removeprefix(key)}", None)
        except BlobMissing as missing:
            # A folder that lists answers with an empty list, never with a missing blob.
            return _Read(None, code_of(missing) if folder else None)
        except StoreError as error:
            # Tried until the deadline, so the store did not answer.
            return _Read(None, code_of(error), True)
        except Exception as error:
            return _Read(None, code_of(error))
