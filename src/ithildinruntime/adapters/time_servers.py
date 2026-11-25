"""What measures the machine's clock: one exchange with a time server, and the sampler of two.

NTP, the Network Time Protocol, is how a machine asks a server the time. chrony
sets the clock. This only measures it.

1. `exchange` sends one packet and reads one reply, or raises `AskFailed`.
2. `TimeServers` asks each server on its own schedule and its own thread, and
   keeps each one's last answer. `reading` comes from memory, so no handler
   waits on the network.

Elapsed time is measured on the raw clock, which chrony does not steer. chrony
corrects the wall clock by rate and the monotonic one with it, so a reading on
that one would not see the correction. Every figure is whole microseconds.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from ithildinruntime.core import ANSWER_BOUND, CLOCK_BOUND, DRIFT_PPM, ClockReading, TimeAnswer

__all__ = [
    "TIME_SERVERS",
    "AskFailed",
    "Exchange",
    "TimeServer",
    "TimeServers",
    "exchange",
    "fresh_nonce",
    "raw_us",
]

_SIZE = 48
# Version 4 of the protocol, sent by a client.
_FIRST_BYTE = 0x23
_MILLION = 10**6
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
# Seconds from the protocol's 1900 to 1970, and how long its 32 bits of seconds last.
_FROM_1900 = 2_208_988_800 * _MILLION
_ERA = 2**32 * _MILLION
# The wait after a first failure, and how long a stop waits for the threads.
_FIRST_RETRY = 2 * _MILLION
_STOP_SECONDS = 2.0


class AskFailed(Exception):
    """An ask that gave no answer. Its text holds no byte of the reply."""


@dataclass(frozen=True)
class TimeServer:
    """A server's name, the seconds between two asks of it, and its port."""

    name: str
    every: float
    port: int = 123


# AWS's server on the host's own link, and its public one, whose trailing dot
# keeps a search domain off it. Both smear a leap second alike.
TIME_SERVERS = (TimeServer("169.254.169.123", 16), TimeServer("time.aws.com.", 64))


@dataclass(frozen=True)
class Exchange:
    """One answer, and the raw and wall stamps it began at."""

    m1: int
    w1: int
    offset_us: int
    error_us: int
    received: bytes


def raw_us() -> int:
    """The raw clock, which no time service steers."""
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) // 1000


def fresh_nonce() -> bytes:
    """Eight bytes a reply has to return. No decision reads them."""
    return os.urandom(8)


def exchange(
    server: TimeServer,
    *,
    now: Callable[[], datetime],
    raw: Callable[[], int] = raw_us,
    nonce: Callable[[], bytes] = fresh_nonce,
    resolve: Callable[..., Any] = socket.getaddrinfo,
    timeout: float = 1.0,
) -> Exchange:
    """Ask one server the time, over IPv4 UDP, with `timeout` seconds to answer.

    A name lookup has no timeout, and a thread of its own instead.
    """
    address = _address(server, resolve)
    sent = nonce()
    packet = bytes([_FIRST_BYTE]) + bytes(39) + sent
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            # Connected, so no other address can answer.
            sock.connect((address, server.port))
            m1 = raw()
            w1 = wall_us(now())
            m2 = raw()
            sock.send(packet)
            # One byte past a reply, so a longer one reads as longer rather than cut to fit.
            reply = sock.recv(_SIZE + 1)
            m4 = raw()
    except TimeoutError:
        raise AskFailed("no reply came in time") from None
    except OSError as error:
        raise AskFailed(f"the network refused: {type(error).__name__}") from None
    return read_reply(reply, sent, m1=m1, w1=w1, m2=m2, m4=m4)


def _address(server: TimeServer, resolve: Callable[..., Any]) -> str:
    """The address to ask, resolved before any stamp, so a slow resolver adds nothing to one.

    A literal address is asked as written. A name has to resolve to a global
    one, so a search domain or a poisoned answer cannot point it inside.
    """
    try:
        return str(ipaddress.IPv4Address(server.name))
    except ValueError:
        pass
    try:
        found = resolve(server.name, server.port, socket.AF_INET, socket.SOCK_DGRAM)
    except (OSError, UnicodeError):
        # Whatever the lookup failed by, a timeout of it among them.
        raise AskFailed("the name does not resolve") from None
    address = ipaddress.IPv4Address(found[0][4][0])
    if not address.is_global or address.is_multicast:
        raise AskFailed("the name does not resolve to a global address")
    return str(address)


def read_reply(reply: bytes, sent: bytes, *, m1: int, w1: int, m2: int, m4: int) -> Exchange:
    """What a reply says of the clock, from the four stamps taken around it."""
    if len(reply) != _SIZE:
        raise AskFailed("the reply is not 48 bytes")
    if reply[0] & 7 != 4:
        raise AskFailed("the reply's mode is not a server's")
    if reply[0] >> 3 & 7 not in (3, 4):
        raise AskFailed("the reply's version is neither 3 nor 4")
    if reply[0] >> 6 == 3:
        raise AskFailed("the server says its clock is not synchronised")
    if not 1 <= reply[1] <= 15:
        raise AskFailed("the reply's stratum is not between 1 and 15")
    if reply[24:32] != sent:
        raise AskFailed("the reply does not return the bytes sent")
    if not any(reply[32:40]) or not any(reply[40:48]):
        raise AskFailed("the reply holds a time of zero")
    t2, t3 = _stamp(reply[32:40], w1), _stamp(reply[40:48], w1)
    if t3 < t2:
        raise AskFailed("the reply was sent before its request arrived")
    delay = (m4 - m1) - (t3 - t2)
    if delay < 0:
        raise AskFailed("the server held the request longer than the exchange took")
    t4 = w1 + (m4 - m1)
    offset = ((w1 - t2) + (t4 - t3)) // 2
    root_delay, root_dispersion = _short(reply[4:8]), _short(reply[8:12])
    error = _half(delay) + _half(root_delay) + root_dispersion + (m2 - m1) + 2
    # A wide answer that still proves the clock wrong is kept.
    if error > _us(ANSWER_BOUND) and not abs(offset) - error > _us(CLOCK_BOUND):
        raise AskFailed("the answer's error is too wide to judge the clock by")
    return Exchange(m1, w1, offset, error, reply)


def wall_us(at: datetime) -> int:
    return (at - _EPOCH) // _MICROSECOND


def _us(span: timedelta) -> int:
    return span // _MICROSECOND


def _stamp(data: bytes, near: int) -> int:
    """A 64-bit server time as microseconds since 1970, in the era nearest `near`."""
    seconds, fraction = int.from_bytes(data[:4], "big"), int.from_bytes(data[4:], "big")
    at = seconds * _MILLION + (fraction * _MILLION >> 32) - _FROM_1900
    return at + (near - at + _ERA // 2) // _ERA * _ERA


def _short(data: bytes) -> int:
    """An unsigned 16.16 number of seconds as microseconds, rounded up."""
    return -(-int.from_bytes(data, "big") * _MILLION >> 16)


def _half(us: int) -> int:
    return -(-us // 2)


@dataclass(frozen=True)
class _State:
    """What one server last said, how its newest ask failed, and when it is next due."""

    due: int | None = None
    pause: int = 0
    last: Exchange | None = None
    failed: str | None = None


class TimeServers:
    """Asks two or more servers, each on its own thread, and keeps each one's last answer."""

    def __init__(
        self,
        servers: Sequence[TimeServer],
        *,
        now: Callable[[], datetime],
        raw: Callable[[], int] = raw_us,
        ask: Callable[..., Exchange] = exchange,
        pause: float = 1.0,
    ) -> None:
        names = [server.name for server in servers]
        if len(names) < 2 or len(set(names)) != len(names):
            raise ValueError("a clock is judged by two or more servers of distinct names")
        if any(server.every <= 0 for server in servers):
            raise ValueError("a time server is asked on a positive interval")
        self._servers = tuple(servers)
        self._now = now
        self._raw = raw
        self._ask = ask
        self._pause = pause
        # One immutable value per server, replaced whole, so a reading on
        # another thread never sees half of one. The lock spans no ask.
        self._states = {server.name: _State() for server in servers}
        self._changed = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def poll(self, server: TimeServer) -> None:
        """Ask the server if it is due. A failed ask keeps the last answer."""
        state = self._states[server.name]
        at = self._raw()
        if state.due is not None and at < state.due:
            return
        every = round(server.every * _MILLION)
        try:
            last = self._ask(server, now=self._now, raw=self._raw)
        except AskFailed as error:
            said = _failed(state, at, every, str(error) or "the ask gave no reason")
        except Exception as error:
            said = _failed(state, at, every, f"the ask raised {type(error).__name__}")
        else:
            said = _State(at + every, 0, last, None)
        with self._changed:
            self._states[server.name] = said
            self._changed.notify_all()

    def start(self) -> None:
        """Start one daemon thread per server, and return at once."""
        if self._threads:
            raise RuntimeError("the time servers are already started")
        self._threads = [
            threading.Thread(
                target=self._run, args=(server,), name=f"time-server {server.name}", daemon=True
            )
            for server in self._servers
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Ask each thread to end, and wait at most two seconds in all for them."""
        self._stop.set()
        until = time.monotonic() + _STOP_SECONDS
        for thread in self._threads:
            thread.join(max(0.0, until - time.monotonic()))

    def wait(self, timeout: float) -> bool:
        """Whether every server has an answer, waiting up to `timeout` seconds for it."""

        def answered() -> bool:
            return all(state.last is not None for state in self._states.values())

        with self._changed:
            return self._changed.wait_for(answered, timeout)

    def _run(self, server: TimeServer) -> None:
        while True:
            self.poll(server)
            if self._stop.wait(self._pause):
                return

    def reading(self) -> ClockReading:
        """What the last answers say of the clock now, from memory.

        The wall clock's move against the raw one since an answer is added to
        its offset, so a step or a slew shows here before the next ask.
        """
        # The answers come first: one that came after `m` would have an age below 0.
        states = [(server.name, self._states[server.name]) for server in self._servers]
        m = self._raw()
        read_at = self._now()
        after = self._raw()
        w = wall_us(read_at)
        return ClockReading(
            read_at, tuple(_answer(name, state, m, w, after) for name, state in states)
        )


def _failed(state: _State, at: int, every: int, text: str) -> _State:
    pause = min(every, state.pause * 2 or _FIRST_RETRY)
    return replace(state, due=at + pause, pause=pause, failed=text)


def _answer(name: str, state: _State, m: int, w: int, after: int) -> TimeAnswer:
    last = state.last
    if last is None:
        return TimeAnswer(name, None, None, None, None, state.failed)
    age = m - last.m1
    drift = -(-age * DRIFT_PPM // _MILLION)
    return TimeAnswer(
        server=name,
        offset_us=last.offset_us + (w - m) - (last.w1 - last.m1),
        error_us=last.error_us + (after - m) + drift,
        age_us=age,
        received=last.received.hex(),
        failed=state.failed,
    )
