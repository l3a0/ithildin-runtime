"""The receive loop every worker and the supervisor run, in every mode.

docs/design.md's `### How a component learns of a change` holds the rules this
module follows, in its list under "One receive loop hands work to a pool of
handlers". The loop reads its queue, turns each event into the stage and
decision it concerns, and hands that item to a pool of handler threads. The
lease steps and execution's kill switch handler are #10's and #11's.

How the loop runs, in order.

1. **It starts by checking every decision.** For each of its stages it reads
   the result key of every decision in the plan, in the plan's order, and
   hands each decision whose inputs are stored to a free handler. It then
   announces its newest stored result again, because the writer may have died
   between storing it and the folder's delivery. The heartbeat runs from the
   start, so a check slowed by a store outage never reads as a hung worker,
   and `stop` ends the check.
2. **It receives only for free handlers.** It asks the queue for at most as
   many events as it has handlers no event or check has taken, up to ten.
3. **An event is only a reason to wake.** The loop deletes an event from
   another bucket, outside its routes, or about a decision already stored or
   already in a handler. It reads only keys it builds from the plan and the
   table, and from an event's key it takes only which decision to check
   first. After each event and each finished handler it checks its stages
   again, in the plan's order, as far as the first decision not ready, so a
   later decision whose event was lost waits for that one, a later event or
   a restart. An event whose check met a store that did not answer is left
   unanswered, so SQS hands it out again once its visibility runs out, and on
   a named pipe it is gone. A full check that met such a store runs again
   from the ticker until one completes. A
   heard control blob's event, such as the run's stop, names no decision, so
   today it wakes a check and is deleted. #10 and #11 add the handlers that
   act on those events, and until then no component may rely on hearing one.
4. **A stage that keeps the plan's order starts decision N only once its own
   result for decision N−1 is stored**, a halt included. Every other stage
   starts any decision whose inputs are stored, side by side.
5. **The loop holds a message while its handler works.** It extends the
   message's visibility while the handler runs, deletes it once the result is
   stored, and returns at once a message it has no free handler for.
6. **A handler's failure stays in its item.** A handler that raises, other
   than one ending on a stop as item 11 says, writes a halt as its stage's
   result for that decision, and one whose error it did not handle also
   alarms, naming the worker, the stage, the decision, the error and each
   capture not yet stored. `SystemExit` and `KeyboardInterrupt` are such
   errors too, since one raised in a handler's thread ends nothing else. The
   other handlers go on. A halt that cannot be stored ends receiving, and the
   loop raises `LoopFailed` once item 9 lets it, so the host restarts the
   process. An error in a handler's thread that leaves its item with no result
   stored or held, such as one raised while its halt is built, ends the loop
   the same way, because going on would start the decision again at once, for
   ever. It alarms, naming the worker, the stage, the decision, the error and
   what the item had ended with, and the item's messages go back to the queue.
   Any error from the queue other than `QueueUnavailable` ends it with
   `LoopFailed`, and so does a queue that has refused every receive for longer
   than `deaf_after`, because a loop that can no longer receive must not go on
   beating as if it worked. Each queue failure alarms first. The refusals
   count from the first one since the queue last answered or every handler was
   last busy, so a pool busy for a while and then refused once goes on. A
   queue that has answered nothing for longer than `deaf_after` while a
   backlog keeps every handler busy alarms once, and the loop ends once a
   handler frees and the refusals go on.
7. **A hung handler stops the heartbeat.** A handler reports progress through
   the function it is handed. The loop beats only while every busy handler
   has reported within the heartbeat limit, so the host's watchdog kills a
   container with a hung handler, except while the process holds a halt or a
   capture S3 has not stored, whether or not it is still receiving. A hung
   handler stops renewing its lease all the same. Extending a
   message runs beside the beat, so a slow queue call never holds the
   heartbeat back.
8. **Ending drains the pool.** However the loop ends, by `stop`, by a
   failure or by a bug, it stops receiving and gives each handler the stop
   timeout to finish, beating by item 7's rule. A handler that waits ends at
   once, as item 11 says. A message whose handler did not finish, or whose
   item ended on the stop or with no result stored or held, goes back to the
   queue, and so does one a receive answers after the loop stopped. A
   handler still working when the stop timeout passes ends with the
   process, as in a crash, and its decision is done again after the
   restart, unless item 9 keeps the process alive.
9. **A process that holds what S3 has not stored stays alive.** After the
   drain it retries each halt it could not store and each capture the storage
   contract holds every `extend_every`, quietly, since the first refusal or
   the alarm below said so. It beats whatever its handlers do while it holds
   them, as item 7 says, so the host's watchdog does not kill it, and it
   alarms naming each blob and why receiving stopped, again until the alert
   channel takes it. A halt S3 answered and will not store, such as one whose
   key holds bytes nothing reads or another result, is dropped with an alarm
   giving its reason. A capture is never dropped, so one S3 keeps answering
   with an error holds the process until a person acts. `run` returns or
   raises only once nothing is held. The price is a process that holds a host
   slot without receiving through an outage, and one whose hung handler is not
   killed while it holds them.
10. **A stage runs only at a decision of every strategy the table declares.**
    Before a handler runs, the loop halts the stage at a decision whose
    strategies are not exactly the table's, naming the strategies left out or
    extra. A handler's result whose configuration hashes differ from the ones
    the table gives its stage is stored as a halt, with an alarm, as item 6
    says of any error a handler did not handle.
11. **A stop ends a handler's wait on the clock.** `stop` asks it, and so does
    the loop's end on a failure or a bug. A handler waiting in
    `Clock.wait_until` under the wall clock, which waits through
    `Progress.wait_for_stop`, wakes at once and ends with `Stopped`. The loop
    stores nothing for its decision and alarms nothing, because a stop is no
    fault in the decision. It returns the handler's messages to the queue
    rather than deleting them, and starts nothing once a stop is asked. A
    check, on another instance or when this one next starts, starts the
    decision again where it finds no result stored for it. A handler may wait
    this way only before it has written anything, and tests/test_stop_rule.py
    lists the modules that do. Any other handler gets the stop timeout, and
    its result is stored as usual. A handler that raises `Stopped` when no
    stop was asked has a bug, so its decision halts and alarms as item 6 says
    of any error a handler did not handle. Releasing it instead would start
    the decision again at once, for ever, while the worker beat as healthy.

Every limit is measured on the timer's monotonic reading.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from ithildinruntime.core import (
    Alerts,
    BlobHeader,
    BlobRef,
    DecisionResult,
    Event,
    EventKind,
    Halt,
    Plan,
    Progress,
    Queue,
    QueueUnavailable,
    Route,
    Stopped,
    Storage,
    StoreUnavailable,
    Timer,
    decision_part,
    result_key,
)

from .table import Stage, Table, routes

__all__ = ["Handler", "Item", "LoopFailed", "ReceiveLoop"]

# SQS hands at most ten messages to one receive.
_MOST_PER_RECEIVE = 10
# The format of the header on a halt the loop writes. A stage that moves its
# own results to a later format sets this with it, on the stage's own issue.
_RESULT_FORMAT = 1
_RESULT_KEY = re.compile(
    r"runs/[^/]+/[^/]+/(?P<decision>[0-9]{8}T[0-9]{6}Z)/(?P<stage>.+)/result\.json"
)


@dataclass(frozen=True, order=True)
class Item:
    """What a handler works on: one stage at one decision."""

    decision_at: datetime
    stage: str


class Handler(Protocol):
    """A stage's work for one decision, returning the result the loop stores.

    `report` is called at every blob write, journal append and broker call
    the handler finishes, so the loop knows it is not hung. It never raises
    for a stop, because a raise there could end a send stage between the
    broker's answer and the journal entry that records it. A handler that has
    written nothing may hand `report` to `Clock.wait_until`, and lets the
    `Stopped` a stop raises there through, since a check starts a stopped item
    again, from nothing, where it finds no result stored for it. A handler can
    run more than once for one item, after a crash or a stop or on two
    instances at once, so a stage that writes outside rests on a create-only
    record of its own, such as the send stage's claim, and on #10's lease on
    the account.
    """

    def __call__(self, item: Item, report: Progress) -> DecisionResult: ...


class LoopFailed(Exception):
    """A halt the loop could not store, an item it could not finish, or a queue
    it could not read or answer.

    The loop raises it once nothing is held, as `ReceiveLoop.run` says.
    """


@dataclass(eq=False)
class _Running:
    item: Item
    last_report: float
    extended: float
    messages: list[Event] = field(default_factory=list)
    done: bool = False
    # Whether the item ended with no result known to be stored or held, on a
    # stop or on an error in `_work` itself, so its messages go back.
    unfinished: bool = False
    # What the item ended with other than a stored result, such as a halt's
    # reason, which no blob holds until the halt is stored.
    ended: str | None = None
    extending: bool = False

    def said_ended(self) -> str:
        return "" if self.ended is None else f". The item had ended with {self.ended}"


class _Progress:
    """The `report` one handler is handed, which reads the loop's stop and cannot ask it."""

    __slots__ = ("_running", "_timer", "_wait")

    def __init__(self, running: _Running, timer: Timer, wait: Callable[[float], bool]) -> None:
        self._running = running
        self._timer = timer
        self._wait = wait

    def __call__(self) -> None:
        self._running.last_report = self._timer.monotonic()

    def wait_for_stop(self, timeout: float) -> bool:
        return self._wait(timeout)


class _Fatal(Exception):
    pass


class ReceiveLoop:
    """One worker's receive loop over its queue, for the stages of one component.

    `worker` names the instance in halts and alarms. `beat` touches the
    worker's heartbeat file. `handlers` maps each of the component's stages to
    its handler.
    """

    def __init__(
        self,
        *,
        worker: str,
        component: str,
        table: Table,
        plan: Plan,
        queue: Queue,
        storage: Storage,
        bucket: str,
        handlers: Mapping[str, Handler],
        alerts: Alerts,
        timer: Timer,
        beat: Callable[[], None],
        pool_size: int = 4,
        heartbeat_limit: float = 30.0,
        extend_every: float = 10.0,
        tick: float = 1.0,
        stop_timeout: float = 30.0,
        deaf_after: float = 60.0,
    ) -> None:
        stages = table.stages_of(component)
        if not stages:
            raise ValueError(f"{component} runs no stage in the table")
        names = {s.name for s in stages}
        if set(handlers) != names:
            raise ValueError(f"{component} needs a handler for each of {sorted(names)}")
        if pool_size < 1:
            raise ValueError("a pool holds at least one handler")
        if not table.strategies:
            raise ValueError("the table declares no strategy, so no blob could name its hash")
        self._worker = worker
        self._component = component
        self._stages = stages
        self._table = table
        self._plan = plan
        self._queue = queue
        self._storage = storage
        self._bucket = bucket
        self._handlers = dict(handlers)
        self._alerts = alerts
        self._timer = timer
        self._beat = beat
        self._pool_size = pool_size
        self._heartbeat_limit = heartbeat_limit
        self._extend_every = extend_every
        self._tick = tick
        self._stop_timeout = stop_timeout
        self._deaf_after = deaf_after
        self._routes: tuple[Route, ...] = tuple(
            r for r in routes(table, plan.deployment) if r.reader == component
        )
        self._decisions = tuple(d.at for d in plan.decisions)
        self._deciding = {d.at: frozenset(d.strategies) for d in plan.decisions}
        self._index = {at: i for i, at in enumerate(self._decisions)}
        # The first decision of each stage not yet known to be stored, so a
        # check never reads a key it already found.
        self._first_open = {s.name: 0 for s in stages}
        self._stored: set[tuple[str, datetime]] = set()
        self._stored_lock = threading.Lock()
        self._running: dict[Item, _Running] = {}
        self._stopping = False
        self._failure: BaseException | None = None
        self._failed_because: str | None = None
        # What ended the loop, in the order it happened, for LoopFailed.
        self._causes: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inbox: asyncio.Queue | None = None
        self._ready = threading.Event()
        self._stop_asked = threading.Event()
        self._background: set[asyncio.Future] = set()
        self._retrying = False
        self._retried = float("-inf")
        self._refused_since: float | None = None
        # The first refusal since the queue last answered, which a busy pool
        # never clears, and whether its alarm has gone out.
        self._unanswered_since: float | None = None
        self._alarmed_unanswered = False
        # Set when the full check met a store that did not answer, so the
        # ticker runs it again until one completes.
        self._recheck = False
        # Each halt the loop could not store, by key, which the process holds
        # until S3 stores it.
        self._unstored_halts: dict[str, DecisionResult] = {}
        self._hold_alarmed = False
        # What each halt dropped from the hold said, for LoopFailed.
        self._dropped: list[str] = []
        self._sending: list[asyncio.Future] = []

    # Running and stopping

    def run(self) -> None:
        """Run until `stop` is called, or raise `LoopFailed` if a halt could not be stored,
        an item could not be finished, or a queue could not be read or answered.

        Either way it returns only once each handler has ended or had the stop
        timeout, and nothing is held: S3 has stored every halt and capture
        this process holds, apart from a halt S3 answered and will not store,
        which is dropped with an alarm.
        """
        try:
            asyncio.run(self._main())
        except Exception as error:
            # A halt is dropped only after a failure, so a bug alone is raised
            # as it is.
            if self._failure is None:
                raise
            # A bug and a failure both end as LoopFailed naming each in the
            # order it happened, for the host's restart record.
            reason = ". Then ".join(self._causes)
            if self._dropped:
                reason += ". Halts not stored: " + "; ".join(self._dropped)
            raise LoopFailed(reason) from error
        if self._failure is not None:
            reason = ". Then ".join(self._causes) or str(self._failure)
            if self._dropped:
                reason += ". Halts not stored: " + "; ".join(self._dropped)
            raise LoopFailed(reason) from self._failure

    def stop(self) -> None:
        """Stop receiving and drain the pool. Safe to call from any thread, at any time."""
        self._stop_asked.set()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._request_stop)
            except RuntimeError:
                pass

    def wait_started(self, timeout: float | None = None) -> bool:
        """Wait until the loop has made its first check, which a test waits on."""
        return self._ready.wait(timeout)

    def _request_stop(self) -> None:
        self._stopping = True
        assert self._inbox is not None
        self._inbox.put_nowait(("stop", None))

    def _fail(self, error: BaseException, what: str | None = None, more: str = "") -> None:
        """End the loop with `LoopFailed`, from the event loop's thread.

        `LoopFailed` says `what` and `more`, and the hold's alarm `what` alone.
        """
        if self._failure is None:
            self._failure = error
            self._causes.append((what or str(error)) + more)
            # A bug may have ended the loop first, and the first cause stands.
            if self._failed_because is None:
                self._failed_because = what
        self._stopping = True
        assert self._inbox is not None
        self._inbox.put_nowait(("stop", None))

    async def _main(self) -> None:
        self._inbox = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        if self._stop_asked.is_set():
            self._stopping = True
        self._handlers_pool = ThreadPoolExecutor(self._pool_size, f"{self._worker}-handler")
        self._io = ThreadPoolExecutor(2, f"{self._worker}-io")
        # Queue calls have threads of their own, so a slow one never holds up a check.
        self._answers = ThreadPoolExecutor(2, f"{self._worker}-answer")
        self._receiving = ThreadPoolExecutor(1, f"{self._worker}-receive")
        self._slots = asyncio.Condition()
        # One task beats from the start until the hold ends, so no wait on
        # the loop's own work, from a check to the drain, holds the beat back.
        beater = asyncio.create_task(self._beater())
        ticker = asyncio.create_task(self._ticker())
        receiver: asyncio.Task | None = None
        try:
            try:
                await self._check(full=True)
                await self._in_io(self._announce_newest)
            finally:
                self._ready.set()
            receiver = asyncio.create_task(self._receiver())
            while not self._stopping and self._failure is None:
                kind, value = await self._inbox.get()
                if kind == "events":
                    events, handled = value
                    try:
                        await self._on_events(events)
                    finally:
                        handled.set_result(None)
                    await self._check(full=False)
                elif kind == "finished":
                    await self._on_finished(value)
                    await self._check(full=False)
                elif kind == "recheck":
                    await self._check(full=True)
        except Exception as error:
            # A bug, which ends the loop as a failure does, and which the
            # hold's alarm names.
            self._stopping = True
            bug = _said(error)
            self._causes.append(bug)
            if self._failed_because is None:
                self._failed_because = bug
            raise
        finally:
            # However the loop ended, a handler that waits must not hold the
            # process to the end of its wait.
            self._stop_asked.set()
            ticker.cancel()
            if receiver is not None:
                receiver.cancel()
            # Whichever way the loop ends, each handler gets the stop timeout
            # to finish, and then the process lives until S3 has stored what
            # it holds.
            await self._drain()
            await self._hold()
            if self._sending:
                # Each alarm the loop sent on its way out goes before the exit.
                await asyncio.wait(self._sending, timeout=self._stop_timeout)
            beater.cancel()
            self._handlers_pool.shutdown(wait=False, cancel_futures=True)
            self._receiving.shutdown(wait=False, cancel_futures=True)
            self._io.shutdown(wait=False, cancel_futures=True)
            self._answers.shutdown(wait=False, cancel_futures=True)

    def _held_keys(self) -> list[str]:
        with self._stored_lock:
            halts = list(self._unstored_halts)
        return halts + [ref.key for ref in self._storage.unstored()]

    def _beat_if_reporting(self) -> None:
        """Beat only while every busy handler has reported within the heartbeat limit."""
        now = self._timer.monotonic()
        busy = [r for r in self._running.values() if not r.done]
        if all(now - r.last_report <= self._heartbeat_limit for r in busy):
            self._beat_quietly()

    async def _hold(self) -> None:
        """Retry and beat until S3 has stored every halt and capture this process holds.

        While something is held the process beats whatever its handlers do.
        The alarm and each try run on the io threads, so neither holds the
        beat back.
        """
        while self._held_keys():
            work = self._in_io(self._try_held)
            end = self._timer.monotonic() + self._extend_every
            # Beat while the try runs, then wait out the rest of the interval
            # unless the try stored everything.
            while not work.done() or (self._timer.monotonic() < end and self._held_keys()):
                if not work.done():
                    await asyncio.wait({work}, timeout=self._tick)
                else:
                    await asyncio.sleep(min(self._tick, max(0.0, end - self._timer.monotonic())))

    def _try_held(self) -> None:
        """Send the hold's alarm until the channel takes it, then try each held blob once."""
        held = self._held_keys()
        if held and not self._hold_alarmed:
            self._hold_alarmed = self._alarm_holding(held)
        self._store_held()

    def _why_stopped(self) -> str:
        """Why receiving stopped, as a clause that reads after "because"."""
        if self._failed_because is None:
            return "it was asked to stop"
        why = self._failed_because.removeprefix(f"{self._worker}: ")
        why = why.removeprefix(f"{self._worker} ")
        if why.startswith("stopped receiving: "):
            why = why.removeprefix("stopped receiving: ")
            return why if why.startswith("the queue ") else f"its queue raised {why}"
        if why.startswith("could not "):
            return f"it {why}"
        return why

    def _alarm_holding(self, held: list[str]) -> bool:
        """Say the process holds what S3 has not stored, and whether the alarm went out."""
        blobs = "blob" if len(held) == 1 else "blobs"
        try:
            self._alerts.alarm(
                f"{self._worker} holds {len(held)} {blobs} S3 has not stored: "
                f"{', '.join(held)}. It stopped receiving because {self._why_stopped()}. "
                "It keeps retrying them, and exits once they are stored, unless the host "
                "kills it first, which loses them. A capture S3 keeps answering with an "
                "error is never dropped, so it holds the process until a person acts"
            )
        except BaseException:
            # Whatever the channel raised, what is held is still tried. Sent
            # again on the next try, since this alarm alone tells a holding
            # process from a healthy one.
            return False
        return True

    def _store_held(self) -> None:
        # Neither call raises for a blob S3 does not answer, which stays held
        # and is tried again.
        self._storage.retry_held()
        with self._stored_lock:
            waiting = dict(self._unstored_halts)
        for key, halt in waiting.items():
            try:
                stored = self._storage.put_result(key, halt, once=True)
            except StoreUnavailable:
                continue
            except Exception as error:
                # S3 answered, such as with bytes no codec reads at the key,
                # and no retry changes its answer, so the halt is not held.
                # Its reason goes out in the alarm.
                self._drop_halt(key, halt, f"could not be stored: {_said(error)}")
                continue
            except BaseException:
                # No answer from S3 either, so it stays held, and the next is tried.
                continue
            if stored != halt:
                theirs = stored.halt.reason if stored.halt is not None else "an output"
                found = f"found another result at its key, which stands: {theirs}"
                self._drop_halt(key, halt, found)
                continue
            with self._stored_lock:
                self._unstored_halts.pop(key, None)

    def _drop_halt(self, key: str, halt: DecisionResult, what: str) -> None:
        reason = halt.halt.reason if halt.halt is not None else ""
        said = _escaped(f"{self._worker}'s halt for {key} {what}. Its reason: {reason}")
        with self._stored_lock:
            self._unstored_halts.pop(key, None)
            # LoopFailed names each, so the host's restart record keeps the
            # reason when the alarm cannot be sent.
            self._dropped.append(said)
        try:
            self._alerts.alarm(said)
        except Exception:
            pass

    def _beat_quietly(self) -> None:
        try:
            self._beat()
        except Exception:
            # A beat that fails leaves the file stale, which the host's
            # watchdog reads as a hung worker.
            pass

    async def _drain(self) -> None:
        pending = [r for r in self._running.values() if not r.done]
        end = self._timer.monotonic() + self._stop_timeout
        while pending and self._timer.monotonic() < end:
            await asyncio.sleep(min(0.05, self._tick))
            pending = [r for r in pending if not r.done]
        for running in self._running.values():
            if running.done:
                await self._finish_messages(running)
            else:
                for message in running.messages:
                    await self._quietly(self._queue.release, message)

    def _in_io[R](self, call: Callable[..., R], *args: object) -> asyncio.Future[R]:
        assert self._loop is not None
        return self._loop.run_in_executor(self._io, call, *args)

    async def _quietly(self, call: Callable[[Event], None], message: Event) -> None:
        # A queue that does not answer leaves the message to come back once its
        # visibility runs out. The item is then checked by its keys, and while
        # a handler still runs another instance can start the same decision,
        # which the create-only result and #10's lease on the account fence.
        # Any other error is a bug, and it ends the loop rather than leaving a
        # message whose visibility runs out unseen.
        assert self._loop is not None
        try:
            await self._loop.run_in_executor(self._answers, call, message)
        except QueueUnavailable:
            pass
        except Exception as error:
            self._give_up(f"could not answer its queue: {_said(error)}", error)

    def _give_up(self, what: str, error: BaseException, more: str = "") -> None:
        """End the loop with `LoopFailed` and alarm, from the event loop's thread."""
        self._fail(error, f"{self._worker} {what}", more)
        self._send(f"{self._worker} {what}{more}")

    def _send(self, reason: str) -> None:
        # On an io thread, so a slow alert channel never holds the beat back.
        assert self._loop is not None
        self._sending.append(self._loop.run_in_executor(self._io, self._alarm_quietly, reason))

    def _alarm_quietly(self, reason: str) -> None:
        try:
            self._alerts.alarm(reason)
        except Exception:
            # The loop ends with LoopFailed, and the host's restart shows.
            pass

    # Receiving

    def _ending(self) -> bool:
        """Whether the loop is ending, so nothing starts.

        `stop` sets `_stop_asked` at once and `_stopping` a moment later on the
        event loop's thread. What happens between them counts too, such as a
        handler that wakes on the stop and ends.
        """
        return self._stopping or self._stop_asked.is_set()

    def _free(self) -> int:
        return self._pool_size - len(self._running)

    async def _receiver(self) -> None:
        assert self._loop is not None and self._inbox is not None
        while not self._stopping:
            async with self._slots:
                await self._slots.wait_for(lambda: self._free() > 0 or self._stopping)
            if self._stopping:
                return
            # A check can take a handler while this receive waits. An event
            # that then finds no free handler goes back to the queue at once.
            limit = min(self._free(), _MOST_PER_RECEIVE)
            try:
                events = await self._loop.run_in_executor(
                    self._receiving, self._receive_or_return, limit
                )
                self._refused_since = None
                self._unanswered_since = None
                self._alarmed_unanswered = False
            except QueueUnavailable as refused:
                # Counted from the first refusal since the queue last answered
                # or the loop last waited with every handler busy, so time it
                # spent asking nothing never counts as the queue refusing.
                now = self._timer.monotonic()
                if self._refused_since is None:
                    self._refused_since = now
                if self._unanswered_since is None:
                    self._unanswered_since = now
                refusing = now - self._refused_since
                if refusing > self._deaf_after:
                    # A queue that refuses every receive for this long leaves a
                    # worker that hears nothing, which must not go on beating.
                    self._give_up(
                        f"stopped receiving: the queue refused every receive for "
                        f"{refusing:.0f} seconds, last with {_escaped(refused.answer)}",
                        refused,
                    )
                    return
                # Only when a busy pool reset the count above does this fire,
                # so one fault pages once.
                self._alarm_unanswered(now, refused)
                events = ()
                await asyncio.sleep(self._tick)
            except Exception as error:
                self._give_up(f"stopped receiving: {_said(error)}", error)
                return
            if events:
                # Wait until the events have handlers, so the next receive asks
                # only for the handlers still free.
                handled = self._loop.create_future()
                self._inbox.put_nowait(("events", (events, handled)))
                await handled

    def _alarm_unanswered(self, now: float, refused: QueueUnavailable) -> None:
        """Alarm once when the queue has answered nothing for longer than `deaf_after`.

        The count that ends the loop starts afresh each time the pool fills,
        so a backlog that keeps every handler busy could hold a deaf queue's
        halt back until the work runs out. This alarm counts from the first
        refusal since the queue last answered, which a busy pool never
        clears, so the owner hears of it on time.
        """
        assert self._unanswered_since is not None
        silent = now - self._unanswered_since
        if silent <= self._deaf_after or self._alarmed_unanswered:
            return
        self._alarmed_unanswered = True
        try:
            self._alerts.alarm(
                f"{self._worker}'s queue has answered no receive for {silent:.0f} seconds, "
                f"last with {_escaped(refused.answer)}"
            )
        except Exception:
            # The loop goes on, and the halt on the deaf queue still comes.
            pass

    def _receive_or_return(self, limit: int) -> Sequence[Event]:
        events = self._queue.receive(limit)
        if self._ending():
            # The loop stopped while this receive waited, so nothing will run them.
            for event in events:
                try:
                    self._queue.release(event)
                except QueueUnavailable:
                    pass
            return ()
        return events

    async def _on_events(self, events: Sequence[Event]) -> None:
        for event in events:
            items = self._items_for(event) if self._wanted(event) else []
            attached = False
            unplaced = False
            unknown = False
            for item in items:
                running = self._running.get(item)
                if running is not None:
                    continue
                state = await self._in_io(self._state, item)
                if state == "unknown":
                    unknown = True
                if state != "ready":
                    continue
                if self._free() > 0 and not self._ending():
                    started = self._start(item)
                    # The message travels with the first item it starts, so it
                    # is deleted once, when that item's result is stored.
                    if not attached:
                        started.messages.append(event)
                        attached = True
                else:
                    unplaced = True
            if attached:
                continue
            if unplaced:
                await self._quietly(self._queue.release, event)
            elif unknown:
                # The store did not answer, so the event stays unanswered and
                # SQS hands it out again once its visibility runs out.
                continue
            else:
                await self._quietly(self._queue.done, event)

    def _wanted(self, event: Event) -> bool:
        return (
            event.kind is not EventKind.OTHER
            and event.bucket == self._bucket
            and any(r.matches(event.kind, event.key) for r in self._routes)
        )

    def _items_for(self, event: Event) -> list[Item]:
        """The items an event names, built from the plan and the table, never from the key."""
        found = _RESULT_KEY.fullmatch(event.key)
        if found is None:
            return []
        try:
            at = datetime.strptime(found["decision"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            return []
        stage = found["stage"]
        if at not in self._index or stage not in {s.name for s in self._table.stages}:
            return []
        if self._key(Item(at, stage)) != event.key:
            return []
        items = []
        for own in self._stages:
            if stage in own.inputs:
                items.append(Item(at, own.name))
            if own.keeps_order and own.name == stage:
                following = self._index[at] + 1
                if following < len(self._decisions):
                    items.append(Item(self._decisions[following], own.name))
        return items

    # Checking keys

    def _key(self, item: Item) -> str:
        return result_key(self._plan.deployment, self._plan.run, item.decision_at, item.stage)

    def _is_stored(self, stage: str, at: datetime) -> bool:
        with self._stored_lock:
            if (stage, at) in self._stored:
                return True
        if not self._storage.exists(self._key(Item(at, stage))):
            return False
        with self._stored_lock:
            self._stored.add((stage, at))
        return True

    def _state(self, item: Item) -> str:
        """`stored`, `ready`, `waiting` or `unknown`, read from the result keys alone.

        A store that does not answer halts the read, and the item is `unknown`,
        so an outage never reads as a decision to start or one already done.
        """
        stage = self._table.stage(item.stage)
        try:
            if self._is_stored(item.stage, item.decision_at):
                return "stored"
            index = self._index[item.decision_at]
            if (
                stage.keeps_order
                and index > 0
                and not self._is_stored(item.stage, self._decisions[index - 1])
            ):
                return "waiting"
            for name in stage.inputs:
                if not self._is_stored(name, item.decision_at):
                    return "waiting"
        except Halt:
            return "unknown"
        return "ready"

    def _ready_items(self, full: bool, free: int, busy: set[Item]) -> tuple[list[Item], bool]:
        """Decisions ready to start, in the plan's order, stage by stage.

        A full check reads every decision, as the loop does when it starts.
        Otherwise a stage is read as far as its first decision not ready, and
        no further than `free` ready decisions. The second value says whether
        a store that did not answer cut the check short.
        """
        found: list[Item] = []
        cut_short = False
        for stage in self._stages:
            ready_here = 0
            for index in range(self._first_open[stage.name], len(self._decisions)):
                if self._stopping:
                    return [], cut_short
                item = Item(self._decisions[index], stage.name)
                if item in busy:
                    if stage.keeps_order:
                        break
                    continue
                state = self._state(item)
                if state == "stored":
                    if index == self._first_open[stage.name]:
                        self._first_open[stage.name] = index + 1
                    continue
                if state == "unknown":
                    # The store did not answer, so this check goes no further.
                    cut_short = True
                    break
                if state == "waiting":
                    if stage.keeps_order or not full:
                        break
                    continue
                if ready_here < free:
                    found.append(item)
                    ready_here += 1
                if ready_here >= free and not full:
                    break
        return sorted(found)[:free], cut_short

    async def _check(self, full: bool) -> None:
        free = self._free()
        if self._stopping or (free <= 0 and not full):
            return
        busy = set(self._running)
        ready, cut_short = await self._in_io(self._ready_items, full, max(free, 0), busy)
        if full:
            # A full check the store cut short runs again from the ticker, so
            # a decision with no later event still starts once S3 answers.
            self._recheck = cut_short
        for item in ready:
            if self._free() <= 0 or self._ending():
                break
            if item not in self._running:
                self._start(item)

    def _announce_newest(self) -> None:
        for stage in self._stages:
            with self._stored_lock:
                stored = [at for name, at in self._stored if name == stage.name]
            if stored:
                self._storage.announce(self._key(Item(max(stored), stage.name)))

    # Handlers

    def _start(self, item: Item) -> _Running:
        assert self._loop is not None
        now = self._timer.monotonic()
        running = _Running(item, last_report=now, extended=now)
        self._running[item] = running
        if self._free() <= 0:
            # Every handler is busy, so the loop asks the queue nothing until
            # one frees. Refusals before this do not carry across it.
            self._refused_since = None
        future = self._loop.run_in_executor(self._handlers_pool, self._work, running)
        future.add_done_callback(lambda done: self._handler_ended(running, done))
        return running

    def _handler_ended(self, running: _Running, future: asyncio.Future) -> None:
        running.done = True
        error = future.exception() if not future.cancelled() else None
        if isinstance(error, _Fatal):
            # The hold's alarm says only that the halt was not stored, so no
            # text of a handler's or a store's can keep it from going out.
            cause = error.__cause__ or error
            more = f": {_said(cause)}{running.said_ended()}"
            self._fail(cause, f"{self._worker}: {error}", more)
            self._send(f"{self._worker}: {error}{more}")
        elif error is not None:
            # An error `_work` did not turn into a stored or held result. A
            # later check would find no result and start the decision again,
            # for ever, so the loop ends and the host restarts the process.
            running.unfinished = True
            what = (
                f"could not finish stage {running.item.stage} at decision "
                f"{decision_part(running.item.decision_at)}"
            )
            self._give_up(what, error, f": {_said(error)}{running.said_ended()}")
        assert self._inbox is not None
        self._inbox.put_nowait(("finished", running))

    async def _on_finished(self, running: _Running) -> None:
        if self._failure is not None:
            return
        await self._finish_messages(running)
        self._running.pop(running.item, None)
        async with self._slots:
            self._slots.notify_all()

    async def _finish_messages(self, running: _Running) -> None:
        # An unfinished item's messages go back to the queue, for a check to
        # start the decision again where it finds no result stored for it.
        answer = self._queue.release if running.unfinished else self._queue.done
        for message in running.messages:
            await self._quietly(answer, message)
        running.messages.clear()

    def _work(self, running: _Running) -> None:
        """Run one handler and store its result, in a handler thread."""
        item = running.item
        stage = self._table.stage(item.stage)
        report = _Progress(running, self._timer, self._stop_asked.wait)
        returned: object = None
        try:
            self._check_strategies(item)
            returned = self._handlers[item.stage](item, report)
            self._check_result(item, returned)
            result = returned
        except Halt as halt:
            running.ended = _said(halt)
            result = self._halt_result(stage, item, halt)
        except Stopped as stopped:
            if self._stop_asked.is_set():
                running.unfinished = True
                return
            running.ended = f"{_said(stopped)}, and no stop was asked"
            self._store_unhandled(stage, running, returned)
            return
        except BaseException as error:
            # `SystemExit` and `KeyboardInterrupt` too. The pool keeps either in
            # its future, so one raised here would end nothing.
            running.ended = _said(error)
            self._store_unhandled(stage, running, returned)
            return
        self._store(running, result)

    def _store_unhandled(self, stage: Stage, running: _Running, returned: object) -> None:
        """Alarm on an error the handler did not handle, and store it as the item's halt."""
        item = running.item
        reason = (
            f"{self._worker} failed in stage {item.stage} at decision "
            f"{decision_part(item.decision_at)} with an error it did not handle: {running.ended}"
        )
        if unstored := self._storage.unstored():
            reason += f". Captures not yet stored: {', '.join(ref.key for ref in unstored)}"
        # The alarm goes first, so the reason is sent even when its halt
        # cannot be built or is never stored.
        try:
            self._alerts.alarm(reason)
        except BaseException:
            # Monitoring alarms on every halt it reads, so nothing the channel
            # raises may keep the halt from being stored.
            pass
        result = self._halt_result(stage, item, Halt(reason), self._own_outputs(item, returned))
        self._store(running, result)

    def _own_outputs(self, item: Item, returned: object) -> tuple:
        """The outputs a rejected result names under the item's own prefix, which a halt keeps."""
        # A result the handler built wrongly can hold anything, and naming its
        # outputs must not keep its halt from being stored.
        if not isinstance(returned, DecisionResult):
            return ()
        try:
            refs = tuple(returned.outputs)
        except Exception:
            return ()
        prefix = self._key(item).removesuffix("result.json")
        return tuple(ref for ref in refs if _is_ref(ref) and ref.key.startswith(prefix))

    def _check_strategies(self, item: Item) -> None:
        """Halt a stage at a decision whose strategies are not exactly the table's.

        The plan builder drops a clock time after an early close one strategy
        at a time, so two strategies on different schedules can meet a
        decision only one of them makes. Running every stage there would run
        the other's stages off its schedule. So the stage halts, until #11
        writes how strategies on different schedules share a deployment.
        """
        deciding = self._deciding.get(item.decision_at, frozenset())
        declared = {s.name for s in self._table.strategies}
        if deciding == declared:
            return
        parts = []
        if missing := sorted(declared - deciding):
            parts.append(f"it leaves out {', '.join(missing)}")
        if extra := sorted(deciding - declared):
            parts.append(f"it lists {', '.join(extra)}, which the table does not declare")
        raise Halt(
            f"stage {item.stage} halts at decision {decision_part(item.decision_at)}, whose "
            f"strategies differ from the table's: {', and '.join(parts)}"
        )

    def _check_result(self, item: Item, result: object) -> None:
        if not isinstance(result, DecisionResult):
            raise TypeError(f"the handler returned {type(result).__name__}, not a DecisionResult")
        header = result.header
        if header.stage != item.stage or header.decision_at != item.decision_at:
            raise ValueError(
                f"the handler returned the result of stage {header.stage} at "
                f"{decision_part(header.decision_at)}"
            )
        expected = dict(self._table.hashes_for(item.stage))
        if dict(header.config_hashes) != expected:
            raise ValueError(
                f"the handler returned a result naming the configuration hashes "
                f"{_hashes(header.config_hashes)}, and stage {item.stage} records "
                f"{_hashes(expected)}"
            )

    def _store(self, running: _Running, result: DecisionResult) -> None:
        item = running.item
        stage = self._table.stage(item.stage)
        try:
            # A key that already holds another result keeps it, such as the
            # supervisor's halt, which is final for the decision, as
            # `Storage.put_result` says. Nothing stored is overwritten.
            self._storage.put_result(self._key(item), result)
        except BaseException as error:
            # Whatever the store raised, a halt already built is held.
            if result.halt is not None:
                if running.ended is None:
                    running.ended = _escaped(f"Halt: {result.halt.reason}")
                self._keep_halt(item, result)
                raise _Fatal(f"the halt for {self._key(item)} was not stored") from error
            running.ended = f"a result that could not be stored: {_said(error)}"
            halt = Halt(
                f"{self._worker} could not store the result of stage {item.stage} at decision "
                f"{decision_part(item.decision_at)}: {_said(error)}"
            )
            fallback = self._halt_result(stage, item, halt, self._own_outputs(item, result))
            try:
                self._storage.put_result(self._key(item), fallback)
            except BaseException as second:
                self._keep_halt(item, fallback)
                raise _Fatal(f"the halt for {self._key(item)} was not stored") from second
        with self._stored_lock:
            self._stored.add((item.stage, item.decision_at))

    def _keep_halt(self, item: Item, halt: DecisionResult) -> None:
        with self._stored_lock:
            self._unstored_halts[self._key(item)] = halt

    def _halt_result(
        self, stage: Stage, item: Item, halt: Halt, found: Sequence = ()
    ) -> DecisionResult:
        header = BlobHeader(
            format=_RESULT_FORMAT,
            deployment=self._plan.deployment,
            run=self._plan.run,
            stage=item.stage,
            kind=str(stage.kind),
            decision_at=item.decision_at,
            inputs={},
            release=self._plan.release,
            config_hashes=self._table.hashes_for(item.stage),
        )
        record = self._storage.halt_record(halt, found_outputs=found)
        return DecisionResult(header=header, outputs=(), halt=record)

    # The heartbeat and the visibility of held messages

    async def _beater(self) -> None:
        """Beat each tick: whatever the handlers do while the process holds what
        S3 has not stored, and by item 7's rule otherwise."""
        while True:
            if self._held_keys():
                self._beat_quietly()
            else:
                self._beat_if_reporting()
            await asyncio.sleep(self._tick)

    async def _ticker(self) -> None:
        while not self._stopping:
            now = self._timer.monotonic()
            busy = [r for r in self._running.values() if not r.done]
            for running in busy:
                due = now - running.extended >= self._extend_every
                if running.messages and due and not running.extending:
                    running.extended = now
                    running.extending = True
                    self._spawn(self._extend(running))
            if not self._retrying and now - self._retried >= self._extend_every:
                self._retried = now
                self._spawn(self._retry_held())
                if self._recheck and self._inbox is not None:
                    self._recheck = False
                    self._inbox.put_nowait(("recheck", None))
            await asyncio.sleep(self._tick)

    def _spawn(self, work) -> None:
        # Each piece of work runs beside the beat, and a reference is kept so
        # the event loop does not drop the task before it ends.
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _extend(self, running: _Running) -> None:
        try:
            for message in list(running.messages):
                await self._quietly(self._queue.extend, message)
        finally:
            running.extending = False

    async def _retry_held(self) -> None:
        """Store each capture the storage contract still holds, once S3 answers.

        design.md's `### Rule 1: a write S3 refuses or does not answer` keeps
        each such capture in the process holding its halt until S3 stores it.
        """
        if not self._storage.unstored():
            return
        self._retrying = True
        try:
            await self._in_io(self._storage.retry_held)
        finally:
            self._retrying = False


def _said(error: BaseException) -> str:
    """The error's type and text, or its type alone where reading its text raises."""
    try:
        return _escaped(f"{type(error).__name__}: {error}")
    except BaseException:
        return f"{type(error).__name__}, whose text could not be read"


def _escaped(text: str) -> str:
    """The text as `Storage.halt_record` escapes a reason, so an alert channel can send it."""
    return text.encode("utf-8", "backslashreplace").decode()


def _is_ref(ref: object) -> bool:
    return isinstance(ref, BlobRef) and isinstance(ref.key, str) and isinstance(ref.sha256, str)


def _hashes(hashes: Mapping[str, str]) -> str:
    return "{" + ", ".join(f"{k}: {v}" for k, v in sorted(hashes.items())) + "}"
