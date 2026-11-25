"""One exchange with a time server, over fixed bytes and a real socket, and the sampler."""

from __future__ import annotations

import inspect
import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from ithildinruntime.adapters import (
    TIME_SERVERS,
    AskFailed,
    Exchange,
    TimeServer,
    TimeServers,
    WallClock,
    exchange,
)
from ithildinruntime.adapters import time_servers as module
from ithildinruntime.adapters.time_servers import read_reply
from ithildinruntime.core import SYNC_BOUND, ClockReading, clock_faults
from tests.support.clocks import EPOCH, FAR, NEAR, SENT, START, TWO, Hands, micro, sampler
from tests.support.queues import SystemTimer

FROM_1900 = 2_208_988_800
# A whole second of 2012, and the four stamps a case takes around an exchange.
S = 1_333_988_000
W1, M1, M2, M4 = S * 10**6 + 200_000, 7_000_000, 7_000_003, 7_100_001
QUARTER, FIVE_SIXTEENTHS = 0x40000000, 0x50000000
RECEIVED_AT, SENT_AT = (S, QUARTER), (S, FIVE_SIXTEENTHS)


def stamp(at: tuple[int, int]) -> bytes:
    seconds, fraction = at
    return ((seconds + FROM_1900) % 2**32).to_bytes(4, "big") + fraction.to_bytes(4, "big")


def reply(
    *,
    leap: int = 0,
    version: int = 4,
    mode: int = 4,
    stratum: int = 3,
    root_delay: int = 2,
    dispersion: int = 5,
    origin: bytes = SENT,
    t2: bytes | tuple[int, int] = RECEIVED_AT,
    t3: bytes | tuple[int, int] = SENT_AT,
) -> bytes:
    return (
        bytes([leap << 6 | version << 3 | mode, stratum, 9, 0xE8])
        + root_delay.to_bytes(4, "big")
        + dispersion.to_bytes(4, "big")
        + bytes([169, 254, 1, 2])
        + stamp((S - 9, 0xABCDEF00))
        + origin
        + (t2 if isinstance(t2, bytes) else stamp(t2))
        + (t3 if isinstance(t3, bytes) else stamp(t3))
    )


def read(data: bytes, *, w1: int = W1, m1: int = M1, m2: int = M2, m4: int = M4) -> Exchange:
    return read_reply(data, SENT, m1=m1, w1=w1, m2=m2, m4=m4)


# 1. One exchange, from fixed bytes.


def test_1_a_slow_clock_reads_negative_with_every_term_of_its_error() -> None:
    # delay 100,001 - 62,500 = 37,501, half of it 18,751. The root delay of 2/65536
    # is 31 microseconds rounded up, half of it 16, and the dispersion of 5/65536 is
    # 77. The stamps took 3. (-50,000 + -12,499) // 2 = -31,250.
    assert read(reply()) == Exchange(M1, W1, -31_250, 18_751 + 16 + 77 + 3 + 2, reply())


def test_1_a_fast_clock_reads_positive() -> None:
    assert read(reply(), w1=S * 10**6 + 400_000).offset_us == 168_750


def test_1_an_asymmetric_path_is_inside_its_error() -> None:
    # The clock is exact. The request took 1,000 microseconds and the reply 90,001.
    got = read(
        reply(t2=(S, QUARTER), t3=(S, QUARTER), root_delay=0, dispersion=0),
        w1=S * 10**6 + 249_000,
        m2=M1,
        m4=M1 + 91_001,
    )
    assert got.offset_us == 44_500 and got.error_us == 45_501 + 2


def test_1_a_stall_between_the_stamps_widens_the_error_by_itself() -> None:
    still, stalled = read(reply()), read(reply(), m2=M2 + 1_000)
    assert stalled.offset_us == still.offset_us
    assert stalled.error_us == still.error_us + 1_000


@pytest.mark.parametrize("server", [RECEIVED_AT, (S, 0x40000001)])
def test_1_a_fraction_is_cut_to_the_microsecond(server: tuple[int, int]) -> None:
    assert read(reply(t2=server)).offset_us == -31_250


def test_1_the_root_values_are_unsigned_and_round_up() -> None:
    tight = {"t2": RECEIVED_AT, "t3": RECEIVED_AT}
    base = read(reply(**tight, root_delay=0, dispersion=0)).error_us
    assert read(reply(**tight, root_delay=65536 // 8, dispersion=0)).error_us == base + 62_500
    assert read(reply(**tight, root_delay=0, dispersion=1)).error_us == base + 16
    with pytest.raises(AskFailed, match="too wide"):
        read(reply(**tight, root_delay=0xFFFFFFFF, dispersion=0))


# The 32 bits of seconds run out at 2036-02-07T06:28:16Z.
TURN = 2_085_978_496


@pytest.mark.parametrize(
    ("t2", "t3"),
    [
        ((TURN - 1, QUARTER), (TURN - 1, FIVE_SIXTEENTHS)),
        ((TURN - 1, 0xF0000000), (TURN, 0x00000001)),
        ((TURN, QUARTER), (TURN, FIVE_SIXTEENTHS)),
        ((TURN, 0x00000100), (TURN, QUARTER)),
        ((TURN + 10**9, QUARTER), (TURN + 10**9, FIVE_SIXTEENTHS)),
    ],
)
def test_1_a_time_is_read_in_the_era_nearest_the_clock(t2, t3) -> None:
    def us(at):
        return at[0] * 10**6 + (at[1] * 10**6 >> 32)

    w1 = us(t2) - 10_000
    held = us(t3) - us(t2)
    got = read(reply(t2=t2, t3=t3), w1=w1, m4=M1 + held + 20_000)
    assert got.offset_us == 0
    # A clock on the other side of the turn, either way, reads as a second off.
    for wrong in (-(10**6), 10**6):
        got = read(reply(t2=t2, t3=t3), w1=w1 + wrong, m4=M1 + held + 20_000)
        assert got.offset_us == wrong
    assert datetime.fromtimestamp(TURN, UTC) == datetime(2036, 2, 7, 6, 28, 16, tzinfo=UTC)


SIZE = "the reply is not 48 bytes"
MODE = "the reply's mode is not a server's"
VERSION = "the reply's version is neither 3 nor 4"
LEAP = "the server says its clock is not synchronised"
STRATUM = "the reply's stratum is not between 1 and 15"
ORIGIN = "the reply does not return the bytes sent"
ZERO = "the reply holds a time of zero"
BACK = "the reply was sent before its request arrived"
HELD = "the server held the request longer than the exchange took"
WIDE = "the answer's error is too wide to judge the clock by"
LATE = (S, QUARTER - 0x10000)


@pytest.mark.parametrize(
    ("stamps", "data", "said"),
    [
        ({}, reply()[:47], SIZE),
        ({}, reply() + b"\0", SIZE),
        ({}, b"", SIZE),
        ({}, reply(mode=3), MODE),
        ({}, reply(mode=5), MODE),
        ({}, reply(version=2), VERSION),
        ({}, reply(version=5), VERSION),
        ({}, reply(leap=3), LEAP),
        ({}, reply(stratum=0), STRATUM),
        ({}, reply(stratum=16), STRATUM),
        ({}, reply(origin=SENT[::-1]), ORIGIN),
        ({}, reply(origin=SENT[:7] + b"\0"), ORIGIN),
        ({}, reply(t2=bytes(8)), ZERO),
        ({}, reply(t3=bytes(8)), ZERO),
        ({}, reply(t3=LATE), BACK),
        ({"m4": M1 + 62_499}, reply(), HELD),
        ({"m2": M1 + 231_155}, reply(), WIDE),
        # The first of several.
        ({}, reply(mode=3, version=2)[:47], SIZE),
        ({}, reply(mode=3, version=2), MODE),
        ({}, reply(version=2, leap=3), VERSION),
        ({}, reply(leap=3, stratum=0), LEAP),
        ({}, reply(stratum=0, origin=bytes(8)), STRATUM),
        ({}, reply(origin=bytes(8), t2=bytes(8)), ORIGIN),
        ({}, reply(t2=(S + 9, 0), t3=bytes(8)), ZERO),
        ({"m4": M1 - 10**6}, reply(t3=LATE), BACK),
        ({"m4": M1 + 62_499, "m2": M1 + 10**6}, reply(), HELD),
    ],
)
def test_1_each_refused_reply_has_its_own_text(stamps, data: bytes, said: str) -> None:
    with pytest.raises(AskFailed) as raised:
        read(data, **stamps)
    assert str(raised.value) == said


@pytest.mark.parametrize(
    ("stamps", "data"),
    [
        ({}, reply(version=3)),
        ({}, reply(stratum=1)),
        ({}, reply(stratum=15)),
        ({}, reply(leap=1)),
        ({}, reply(leap=2)),
        ({}, reply(t3=RECEIVED_AT)),
        ({"m4": M1 + 62_500}, reply()),
        ({"m2": M1 + 231_154}, reply()),
        ({"m4": M1 + 400_000}, reply(t2=(S, 0))),
        ({"m4": M1 + 400_000}, reply(t3=(S, 1 << 30), t2=(S, 1))),
    ],
)
def test_1_a_reply_at_each_boundary_is_an_answer(stamps, data: bytes) -> None:
    assert read(data, **stamps).received == data


@pytest.mark.parametrize("side", [-1, 1])
def test_1_a_wide_answer_is_kept_only_where_it_proves_a_fault(side: int) -> None:
    # An error of 300,000 and an offset of 1,200,000 either way proves nothing.
    zero = {"t2": (S, 0), "t3": (S, 0), "root_delay": 0, "dispersion": 0}
    stamps = {"m2": M1 + 299_998, "m4": M1}
    for offset in (1_200_000, 1_300_000):
        with pytest.raises(AskFailed, match="too wide"):
            read(reply(**zero), w1=S * 10**6 + side * offset, **stamps)
    got = read(reply(**zero), w1=S * 10**6 + side * 1_300_001, **stamps)
    assert (got.offset_us, got.error_us) == (side * 1_300_001, 300_000)


# 2. Over a real socket on loopback, on scripted stamps.


class Server:
    """A server on loopback that answers one packet as `behaves` says."""

    def __init__(self, behaves: Callable) -> None:  # type: ignore[type-arg]
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(5)
        self.at = TimeServer("127.0.0.1", 16, self.sock.getsockname()[1])
        self.packet = b""
        self.arrived = threading.Event()
        self.behaves = behaves
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> Server:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.thread.join(5)
        self.sock.close()

    def _run(self) -> None:
        self.packet, client = self.sock.recvfrom(1024)
        self.arrived.set()
        self.behaves(self.sock, self.packet, client)


# The stamps are scripted, so `T2` equals `T3`, half way through them.
HALF = {"t2": (S, QUARTER), "t3": (S, QUARTER), "root_delay": 0, "dispersion": 0}


def answers(sock, packet: bytes, client, *, more: bytes = b"", origin: bytes | None = None) -> None:
    sock.sendto(reply(origin=packet[40:] if origin is None else origin, **HALF) + more, client)


def ask(server: TimeServer, hands: Hands | None = None, sent: bytes = SENT, **more) -> Exchange:
    hands = hands or Hands(wall=EPOCH + micro(S * 10**6 + 250_000), step_us=10)
    return exchange(server, now=hands.now, raw=hands.raw, nonce=lambda: sent, **more)


def test_2_an_answer_and_the_request_that_asked() -> None:
    hands = Hands(wall=EPOCH + micro(S * 10**6 + 250_000), raw_us=70, step_us=10)
    with Server(answers) as server:
        got = ask(server.at, hands)
    assert hands.calls == ["raw", "now", "raw", "raw"]
    # m1 70, w1 10 past the server's time, m2 90, m4 100.
    assert (got.m1, got.w1, got.error_us) == (70, S * 10**6 + 250_010, 15 + 20 + 2)
    assert got.offset_us == (10 + 40) // 2
    assert server.packet == b"\x23" + bytes(39) + SENT
    assert got.received == reply(origin=SENT, **HALF)


def test_2_two_asks_carry_different_bytes() -> None:
    seen = []
    for _ in range(2):
        hands = Hands(wall=EPOCH + micro(S * 10**6 + 250_000))
        with Server(answers) as server:
            exchange(server.at, now=hands.now, raw=hands.raw, nonce=module.fresh_nonce)
        seen.append(server.packet[40:])
    assert seen[0] != seen[1] and {len(s) for s in seen} == {8}


def test_2_the_bytes_come_from_the_systems_randomness(monkeypatch) -> None:
    monkeypatch.setattr(os, "urandom", lambda n: bytes([n]) * n)
    assert module.fresh_nonce() == bytes([8]) * 8


def test_2_the_raw_clock_is_the_one_no_time_service_steers(monkeypatch) -> None:
    asked = []

    def read_ns(clock: int) -> int:
        asked.append(clock)
        return 1_234_567_899

    monkeypatch.setattr(module.time, "clock_gettime_ns", read_ns)
    assert module.raw_us() == 1_234_567
    assert asked == [time.CLOCK_MONOTONIC_RAW]


def test_2_every_stamp_before_the_send_is_read_before_the_packet_arrives() -> None:
    hands = Hands(wall=EPOCH + micro(S * 10**6 + 250_000))
    early: list[bool] = []

    with Server(answers) as server:

        def held(read):  # type: ignore[no-untyped-def]
            def stamp():  # type: ignore[no-untyped-def]
                if len(hands.calls) < 3:
                    # A packet already sent would have arrived by now.
                    early.append(server.arrived.wait(0.2))
                return read()

            return stamp

        exchange(server.at, now=held(hands.now), raw=held(hands.raw), nonce=lambda: SENT)
    assert early == [False, False, False]


def test_2_a_silent_server_is_a_timeout() -> None:
    began = time.monotonic()
    with Server(lambda s, p, c: None) as server:
        with pytest.raises(AskFailed) as raised:
            ask(server.at, timeout=0.2)
        assert str(raised.value) == "no reply came in time"
    assert time.monotonic() - began < 2


def test_2_a_reply_from_another_port_is_not_read() -> None:
    def elsewhere(sock, packet, client) -> None:  # type: ignore[no-untyped-def]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
            other.bind(("127.0.0.1", 0))
            answers(other, packet, client)

    with Server(elsewhere) as server:
        with pytest.raises(AskFailed, match="no reply came in time"):
            ask(server.at, timeout=0.3)


def test_2_a_reply_with_another_origin_fails() -> None:
    with Server(lambda s, p, c: answers(s, p, c, origin=bytes(8))) as server:
        with pytest.raises(AskFailed) as raised:
            ask(server.at)
    assert str(raised.value) == ORIGIN


@pytest.mark.parametrize("more", [1, 12])
def test_2_a_longer_reply_is_not_cut_to_fit(more: int) -> None:
    with Server(lambda s, p, c: answers(s, p, c, more=bytes(more))) as server:
        with pytest.raises(AskFailed) as raised:
            ask(server.at)
    assert str(raised.value) == SIZE


def test_2_a_port_nothing_listens_on_is_named_by_its_class() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(AskFailed) as raised:
        ask(TimeServer("127.0.0.1", 16, port))
    assert str(raised.value) == "the network refused: ConnectionRefusedError"


def resolving(*addresses: str, hands: Hands | None = None, error: Exception | None = None):
    """A fake `resolve`, which a case hands in."""

    def resolve(name, port, family, kind):  # type: ignore[no-untyped-def]
        assert (family, kind) == (socket.AF_INET, socket.SOCK_DGRAM)
        assert name == "time.example."
        if error:
            raise error
        if hands:
            hands.pass_us(9_000_000)
            hands.calls.append("resolved")
        return [(family, kind, 17, "", (a, port)) for a in addresses]

    return resolve


def redirected(monkeypatch) -> list[str]:
    """Every connect goes to loopback instead, because no address a case names is there."""
    to: list[str] = []

    class Redirected(socket.socket):
        def connect(self, address) -> None:  # type: ignore[no-untyped-def]
            to.append(address[0])
            super().connect(("127.0.0.1", address[1]))

    monkeypatch.setattr(module.socket, "socket", Redirected)
    return to


def test_2_a_resolver_that_takes_long_adds_nothing_to_the_delay(monkeypatch) -> None:
    hands = Hands(wall=EPOCH + micro(S * 10**6 - 8_750_000), step_us=10)
    with Server(answers) as server:
        to = redirected(monkeypatch)
        at = TimeServer("time.example.", 16, server.at.port)
        got = ask(at, hands, resolve=resolving("8.8.8.8", "9.9.9.9", hands=hands))
    assert hands.calls == ["resolved", "raw", "now", "raw", "raw"]
    assert got.error_us == 15 + 20 + 2 and to == ["8.8.8.8"]


def test_2_an_address_written_out_is_asked_as_it_is(monkeypatch) -> None:
    refuses = resolving(error=AssertionError("a written address was looked up"))
    with Server(answers) as server:
        to = redirected(monkeypatch)
        ask(TimeServer("169.254.169.123", 16, server.at.port), resolve=refuses)
    assert to == ["169.254.169.123"]


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.9", "100.64.0.1", "224.0.1.1", "169.254.169.123"]
)
def test_2_a_name_that_resolves_to_no_global_address_fails(address: str) -> None:
    with pytest.raises(AskFailed) as raised:
        ask(TimeServer("time.example.", 16), resolve=resolving(address, "8.8.8.8"))
    assert str(raised.value) == "the name does not resolve to a global address"


@pytest.mark.parametrize(
    "error",
    [
        socket.gaierror(8, "not known"),
        UnicodeError("label empty or too long"),
        TimeoutError(),
        PermissionError(),
    ],
)
def test_2_every_error_of_the_lookup_is_a_name_that_does_not_resolve(error: Exception) -> None:
    with pytest.raises(AskFailed) as raised:
        ask(TimeServer("time.example.", 16), resolve=resolving(error=error))
    assert str(raised.value) == "the name does not resolve"
    with pytest.raises(LookupError):
        ask(TimeServer("time.example.", 16), resolve=resolving(error=LookupError()))


# One reply from time.aws.com, captured on 2026-11-25, with the stamps around it.
CAPTURED = bytes.fromhex(
    "240400ee000000180000000da9fea97beeb16ad4d2b5db6e17a2ce27868bb486eeb16ad7002d2ce7eeb16ad7002d5074"
)


def test_2_the_captured_reply_parses() -> None:
    w1 = 1_795_615_830_924_028
    sent = bytes.fromhex("17a2ce27868bb486")
    got = read_reply(CAPTURED, sent, m1=0, w1=w1, m2=2, m4=27_000)
    # T2 is 2026-11-25T14:10:31.000689Z, and T3 two microseconds later.
    assert got.offset_us == (2 * (w1 - 1_795_615_831_000_690) + 27_000) // 2 == -63_162
    # 24/65536 and 13/65536 round up to 367 and 199.
    assert got.error_us == 13_499 + 184 + 199 + 2 + 2


# 3. The sampler.


class Asks:
    """A fake `ask`, whose next answer or failure a case sets per server."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.offsets = {"near": -40, "far": 60}
        self.fails: dict[str, Exception] = {}

    def __call__(self, server: TimeServer, *, now, raw) -> Exchange:
        self.asked.append(server.name)
        if server.name in self.fails:
            raise self.fails[server.name]
        error = {"near": 100, "far": 200}[server.name]
        wall = (now() - EPOCH) // micro(1)
        return Exchange(raw(), wall, self.offsets[server.name], error, bytes([7]) * 48)


def made() -> tuple[TimeServers, Hands, Asks]:
    hands, asks = Hands(), Asks()
    return sampler(hands, asks), hands, asks


def poll(servers: TimeServers) -> None:
    for server in TWO:
        servers.poll(server)


def test_3_a_server_is_asked_only_once_it_is_due() -> None:
    servers, hands, asks = made()
    poll(servers)
    assert asks.asked == ["near", "far"]
    for us, want in [
        (0, []),
        (15_999_999, []),
        (1, ["near"]),
        (1, []),
        (15_999_999, ["near"]),
        (16_000_000, ["near"]),
        (16_000_000, ["near", "far"]),
    ]:
        asks.asked.clear()
        hands.pass_us(us)
        poll(servers)
        assert asks.asked == want, us


def test_3_an_ask_is_due_from_when_the_last_one_began() -> None:
    hands, asks = Hands(), Asks()

    def takes_long(server: TimeServer, **clocks) -> Exchange:
        got = asks(server, **clocks)
        hands.pass_us(5_000_000)
        return got

    servers = sampler(hands, takes_long)
    servers.poll(NEAR)
    hands.pass_us(10_999_999)
    servers.poll(NEAR)
    assert len(asks.asked) == 1
    hands.pass_us(1)
    servers.poll(NEAR)
    assert len(asks.asked) == 2


@pytest.mark.parametrize(
    ("server", "pauses"),
    [(NEAR, [2, 4, 8, 16, 16]), (FAR, [2, 4, 8, 16, 32, 64, 64])],
)
def test_3_a_failed_ask_is_asked_again_sooner_and_each_time_later(server, pauses) -> None:
    servers, hands, asks = made()
    asks.fails[server.name] = AskFailed("no reply came in time")
    servers.poll(server)
    for pause in pauses:
        asks.asked.clear()
        hands.pass_us(pause * 10**6 - 1)
        servers.poll(server)
        assert asks.asked == [], pause
        hands.pass_us(1)
        servers.poll(server)
        assert asks.asked == [server.name], pause
    # An answer starts the doubling again.
    asks.fails.clear()
    hands.pass_us(pauses[-1] * 10**6)
    servers.poll(server)
    asks.fails[server.name] = AskFailed("no reply came in time")
    hands.pass_us(round(server.every * 10**6))
    servers.poll(server)
    asks.asked.clear()
    hands.pass_us(2 * 10**6)
    servers.poll(server)
    assert asks.asked == [server.name]


def test_3_the_retry_never_waits_past_the_interval() -> None:
    hands, asks = Hands(), Asks()
    quick = TimeServer("near", 0.5)
    servers = TimeServers((quick, FAR), now=hands.now, raw=hands.raw, ask=asks)
    asks.fails["near"] = AskFailed("no reply came in time")
    servers.poll(quick)
    hands.pass_us(500_000)
    servers.poll(quick)
    assert asks.asked == ["near", "near"]


def test_3_a_reading_before_any_ask_has_no_answers() -> None:
    servers, _, _ = made()
    got = servers.reading()
    assert got.read_at == START
    assert [(a.server, a.offset_us, a.failed) for a in got.answers] == [
        ("near", None, None),
        ("far", None, None),
    ]
    assert len(clock_faults(got, measured=True)) == 2


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (AskFailed("no reply came in time"), "no reply came in time"),
        (ValueError("reply b'secret'"), "the ask raised ValueError"),
        (AskFailed(""), "the ask gave no reason"),
    ],
)
def test_3_a_failure_keeps_the_last_answer_and_a_later_answer_clears_it(error, said) -> None:
    servers, hands, asks = made()
    poll(servers)
    asks.fails["near"] = error
    hands.pass_us(16_000_000)
    poll(servers)
    near, far = servers.reading().answers
    assert (near.offset_us, near.age_us, near.failed) == (-40, 16_000_000, said)
    assert far.failed is None
    asks.fails.clear()
    hands.pass_us(16_000_000)
    poll(servers)
    near, _ = servers.reading().answers
    assert (near.age_us, near.failed) == (0, None)


def test_3_the_newest_failure_is_the_one_kept() -> None:
    servers, hands, asks = made()
    for text in ("first", "second"):
        asks.fails["near"] = AskFailed(text)
        poll(servers)
        hands.pass_us(16_000_000)
    assert servers.reading().answers[0].failed == "second"


@pytest.mark.parametrize("step", [-3_000_000, 3_000_000])
def test_3_a_step_shows_at_the_next_reading_and_the_next_answer_confirms_it(step: int) -> None:
    servers, hands, asks = made()
    poll(servers)
    hands.pass_us(1_000_000)
    hands.wall += micro(step)
    near, far = servers.reading().answers
    assert (near.offset_us, far.offset_us) == (-40 + step, 60 + step)
    assert near.age_us == 1_000_000
    asks.offsets = {"near": -40 + step, "far": 60 + step}
    hands.pass_us(63_000_000)
    poll(servers)
    near, far = servers.reading().answers
    assert (near.offset_us, far.offset_us, near.age_us) == (-40 + step, 60 + step, 0)


@pytest.mark.parametrize("rate", [-83_333, 83_333])
def test_3_a_slew_shows_as_it_goes(rate: int) -> None:
    servers, hands, _ = made()
    poll(servers)
    for second in range(1, 15):
        hands.raw_us += 10**6
        hands.wall += micro(10**6 + rate)
        assert servers.reading().answers[0].offset_us == -40 + rate * second
    assert len(clock_faults(servers.reading(), measured=True)) == 2


@pytest.mark.parametrize(("age", "drift"), [(0, 0), (1, 1), (2_000, 1), (2_001, 2), (10**7, 5_000)])
def test_3_drift_is_added_to_the_error(age: int, drift: int) -> None:
    servers, hands, _ = made()
    poll(servers)
    hands.pass_us(age)
    near, far = servers.reading().answers
    assert (near.error_us, far.error_us) == (100 + drift, 200 + drift)
    assert (near.offset_us, near.age_us, near.received) == (-40, age, "07" * 48)


def test_3_a_pause_between_the_stamps_widens_the_error() -> None:
    servers, hands, _ = made()
    poll(servers)
    hands.calls.clear()
    hands.step_us = 7
    got = servers.reading()
    near = got.answers[0]
    assert hands.calls == ["raw", "now", "raw"]
    assert (near.offset_us, near.error_us, near.age_us) == (-40 + 7, 100 + 14, 0)
    assert got.read_at == START + micro(7)


@pytest.mark.parametrize("between", [0, 1, 2])
def test_3_an_answer_between_the_stamps_never_makes_a_reading_raise(between: int) -> None:
    servers, hands, _ = made()
    poll(servers)
    stamps = [hands.raw, hands.now, hands.raw]
    taken: list[int] = []

    def then_polls(read):  # type: ignore[no-untyped-def]
        def stamp():  # type: ignore[no-untyped-def]
            at = read()
            taken.append(1)
            if len(taken) == between + 1:
                hands.pass_us(64_000_000)
                poll(servers)
            return at

        return stamp

    servers._raw, servers._now = then_polls(stamps[0]), then_polls(stamps[1])
    near = servers.reading().answers[0]
    assert near.age_us == 0 and near.failed is None


@pytest.mark.parametrize(
    ("servers", "said"),
    [
        ((), "two or more"),
        ((TWO[0],), "two or more"),
        ((TWO[0], TWO[0]), "distinct names"),
        ((TWO[0], TimeServer("near", 64, 124)), "distinct names"),
        ((TWO[0], TimeServer("far", 0)), "positive interval"),
        ((TimeServer("near", -1), TWO[1]), "positive interval"),
    ],
)
def test_3_each_refusal_at_construction(servers, said: str) -> None:
    hands = Hands()
    with pytest.raises(ValueError, match=said):
        TimeServers(servers, now=hands.now, raw=hands.raw, ask=Asks())


def test_3_the_servers_the_port_the_timeout_and_the_ask() -> None:
    assert TIME_SERVERS == (
        TimeServer("169.254.169.123", 16, 123),
        TimeServer("time.aws.com.", 64, 123),
    )
    assert TimeServer("a", 1).port == 123
    assert inspect.signature(exchange).parameters["timeout"].default == 1.0
    asked = {k: p.default for k, p in inspect.signature(exchange).parameters.items()}
    assert asked["raw"] is module.raw_us and asked["nonce"] is module.fresh_nonce
    assert asked["resolve"] is socket.getaddrinfo
    built = {k: p.default for k, p in inspect.signature(TimeServers).parameters.items()}
    assert built["ask"] is exchange and built["raw"] is module.raw_us and built["pause"] == 1.0


def test_3_the_wall_clock_hands_the_reading_through() -> None:
    servers, _, _ = made()
    poll(servers)
    assert WallClock(SystemTimer(), servers=servers).reading() == servers.reading()


def test_3_a_wall_clock_with_no_servers_reads_no_answers() -> None:
    hands = Hands()

    class OnHands(SystemTimer):
        def now(self) -> datetime:
            return hands.now()

    assert WallClock(OnHands(), servers=None).reading() == ClockReading(START, None)


# 4. The threads.


def test_4_each_server_is_polled_again_and_none_after_a_stop() -> None:
    hands = Hands()
    lock = threading.Lock()
    counts = {"near": 0, "far": 0}
    sixth = {name: threading.Event() for name in counts}

    def ask_again(server: TimeServer, *, now, raw) -> Exchange:
        with lock:
            counts[server.name] += 1
            if counts[server.name] == 6:
                sixth[server.name].set()
            got = Asks()(server, now=now, raw=raw)
            hands.pass_us(64_000_000)
        return got

    servers = sampler(hands, ask_again, pause=0.005)
    began = time.monotonic()
    servers.start()
    assert all(t.daemon for t in servers._threads) and len(servers._threads) == 2
    assert all(event.wait(10) for event in sixth.values())
    # The wait is the one handed in. The second a run takes would be five of them.
    assert time.monotonic() - began < 4
    servers.stop()
    assert not any(t.is_alive() for t in servers._threads)
    ended = dict(counts)
    time.sleep(0.05)
    assert counts == ended
    # A thread that stopped shows as an age.
    hands.pass_us(SYNC_BOUND // micro(1) + 1)
    faults = clock_faults(servers.reading(), measured=True)
    assert len(faults) == 2 and all("past the bound" in fault for fault in faults)


@pytest.mark.parametrize("hung", [{"far"}, {"near", "far"}], ids=["one", "two"])
def test_4_a_hung_ask_holds_neither_the_other_server_nor_start_nor_stop(hung: set[str]) -> None:
    hands, asks = Hands(), Asks()
    release = threading.Event()
    hanging = {name: threading.Event() for name in hung}

    def hangs(server: TimeServer, **clocks) -> Exchange:
        if server.name in hung:
            hanging[server.name].set()
            release.wait(10)
        return asks(server, **clocks)

    servers = sampler(hands, hangs, pause=0.005)
    try:
        began = time.monotonic()
        servers.start()
        assert time.monotonic() - began < 1.5
        assert all(event.wait(5) for event in hanging.values())
        end = time.monotonic() + 5
        while "near" not in hung and servers.reading().answers[0].age_us is None:
            assert time.monotonic() < end
            time.sleep(0.005)
        # A reading comes back while an ask blocks.
        answered = [a.server for a in servers.reading().answers if a.age_us is not None]
        assert answered == [name for name in ("near", "far") if name not in hung]
        with pytest.raises(RuntimeError, match="already started"):
            servers.start()
        began = time.monotonic()
        servers.stop()
        # Two seconds in all, however many hang.
        assert 1.9 < time.monotonic() - began < 3.9
    finally:
        release.set()
    for thread in servers._threads:
        thread.join(5)


def test_4_wait_returns_at_once_where_every_server_has_answered() -> None:
    servers, _, _ = made()
    poll(servers)
    began = time.monotonic()
    assert servers.wait(5) is True and time.monotonic() - began < 1


def test_4_wait_ends_at_its_time_where_one_has_not() -> None:
    servers, _, asks = made()
    asks.fails["far"] = AskFailed("no reply came in time")
    poll(servers)
    began = time.monotonic()
    assert servers.wait(0.2) is False
    assert 0.15 < time.monotonic() - began < 2


def test_4_wait_ends_on_the_answer_that_arrives_while_it_waits() -> None:
    servers, _, _ = made()
    servers.poll(NEAR)
    waiting = threading.Event()
    results: list[tuple[bool, float]] = []

    def waits() -> None:
        began = time.monotonic()
        waiting.set()
        results.append((servers.wait(10), time.monotonic() - began))

    waiter = threading.Thread(target=waits)
    waiter.start()
    assert waiting.wait(5)
    time.sleep(0.1)
    servers.poll(FAR)
    waiter.join(15)
    ((answered, took),) = results
    assert answered is True and took < 5


def test_4_a_stop_before_a_start_returns() -> None:
    servers, _, _ = made()
    servers.stop()
