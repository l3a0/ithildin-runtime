"""What a capturing stage asks of `core.StageDecision`: a capture, and its own stored files.

tests/test_recover.py holds the same through the recover stage. These are the cases
no run of that stage reaches.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from ithildinruntime.core import (
    BlobRef,
    Halt,
    Instrument,
    InstrumentId,
    Retry,
    StageDecision,
    StageKind,
    StageRun,
    StoredBlob,
    attempt_key,
    encode_output,
    output_key,
)
from ithildinruntime.core import OutputFile as File
from tests.support.order_world import AT, Alarms, Clock, Wired
from tests.support.order_world import World as Stores

RUN = "2026-11-03"
ONE, TWO = (Instrument(InstrumentId(name), name) for name in ("GLD", "USO"))
KEY = output_key("d-7", RUN, AT, "execution/recover", "account")


class World:
    def __init__(self) -> None:
        self.store = Wired()
        self.alarms = Alarms()
        self.clock = Clock(AT)
        self.retry = Retry(timedelta(seconds=10), 1.0, 4.0)
        self.storage = Stores.storage(self)  # type: ignore[arg-type]

    def decision(self, **changes: object) -> StageDecision:
        given: dict[str, object] = {
            "format": 1,
            "stage": "execution/recover",
            "kind": StageKind.CAPTURING,
            "deployment": "d-7",
            "run": RUN,
            "release": "r1",
            "config_hashes": {"chan": "c" * 64},
        }
        made = StageRun(self.storage, **given | changes)  # type: ignore[arg-type]
        return made.decision(AT, lambda: None)


def said(first: Instrument, later: Instrument) -> str:
    return f"{first.symbol} and {later.symbol}"


def test_the_first_capture_counts_and_a_later_one_is_kept_at_its_attempt() -> None:
    world = World()
    first = world.decision().capture("account", Instrument, ONE, said)
    assert first.entry == ONE and first.ref.key == KEY
    later = world.decision()
    counts = later.capture("account", Instrument, TWO, said)
    # The first is given back, under the header it was stored with.
    assert (counts.ref, counts.entry, dict(counts.header.inputs)) == (first.ref, ONE, {})
    attempt = attempt_key("d-7", RUN, AT, "execution/recover", "account", 2)
    assert [r.key for r in later.done().outputs] == [KEY, attempt]
    assert [r.key for r in later.halted(Halt("no")).outputs] == [KEY, attempt]
    assert world.alarms.sent[0].endswith(". GLD and USO")
    # The same bytes again are the first, and say nothing.
    again = world.decision()
    assert again.capture("account", Instrument, ONE, said).ref == first.ref
    assert [r.key for r in again.done().outputs] == [KEY]
    assert len(world.alarms.sent) == 1


def test_an_alarm_says_where_what_the_stage_had_read_differs() -> None:
    world = World()
    world.decision().capture("account", Instrument, ONE, said)
    later = world.decision()
    later.names(BlobRef("runs/x", "a" * 64))
    later.capture("account", Instrument, ONE, said)
    assert world.alarms.sent[0].endswith(". What the stage had read differs. GLD and GLD")


@pytest.mark.parametrize(
    ("changes", "odd"),
    [
        ({"format": 2}, "format"),
        ({"release": "r2"}, "release"),
        ({"config_hashes": {"chan": "d" * 64}}, "configuration"),
        ({"format": 2, "release": "r2"}, "format, release"),
    ],
)
def test_a_file_this_stage_did_not_write_halts(changes: dict, odd: str) -> None:
    world = World()
    stored = world.decision().keep("account", Instrument, ONE)
    reason = f"{KEY} was stored under another {odd}, and nothing goes on from it"
    with pytest.raises(Halt) as raised:
        world.decision(**changes).own("account", Instrument)
    assert raised.value.reason == reason
    with pytest.raises(Halt) as raised:
        world.decision(**changes).keep("account", Instrument, ONE)
    assert raised.value.reason == reason
    with pytest.raises(Halt) as raised:
        world.decision(**changes).capture("account", Instrument, ONE, said)
    assert raised.value.reason == reason
    # Its alarm says the same of it.
    assert world.alarms.sent[0].endswith(f". {reason}")
    assert world.decision().own("account", Instrument) == stored


@pytest.mark.parametrize("entries", [(), (ONE, TWO)], ids=["none", "two"])
def test_a_file_of_other_than_one_entry_halts(entries: tuple) -> None:
    world = World()
    header = world.decision().keep("other", Instrument, ONE).header
    file = File(header, entries, not entries)
    world.store.put(KEY, encode_output(Instrument, file))
    with pytest.raises(Halt, match=f"{KEY} holds {len(entries)} entries, not one"):
        world.decision().own("account", Instrument)


def test_a_file_read_back_as_other_bytes_than_it_was_stored_with_halts() -> None:
    world = World()
    world.decision().keep("account", Instrument, ONE)

    class Lies(Wired):
        def head(self, key: str) -> StoredBlob:
            found = super().head(key)
            return replace(found, ref=replace(found.ref, sha256="e" * 64))

    world.store = Lies(world.store.blobs)
    world.storage = Stores.storage(world)  # type: ignore[arg-type]
    with pytest.raises(Halt, match=f"{KEY} was read back with SHA-256 [0-9a-f]+, not the stored e"):
        world.decision().keep("account", Instrument, TWO)


def test_a_file_not_stored_is_none_and_a_read_of_it_is_no_input() -> None:
    world = World()
    decision = world.decision()
    assert decision.own("account", Instrument) is None
    decision.keep("account", Instrument, ONE)
    # Reading its own file after it wrote is no read of an input.
    assert decision.own("account", Instrument) is not None
    assert dict(decision.done().header.inputs) == {}
    with pytest.raises(RuntimeError, match="read runs/x after it had written"):
        decision.names(BlobRef("runs/x", "a" * 64))


def test_a_capture_the_codec_refuses_halts_with_nothing_of_what_it_held() -> None:
    world = World()
    odd = Instrument(InstrumentId("\ud800PKFAKEKEY"), "PKFAKEKEY")
    with pytest.raises(Halt) as raised:
        world.decision().capture("account", Instrument, odd, said)
    assert raised.value.reason == f"{KEY} was not stored: what was read is no Instrument"
    assert world.store.blobs == {}
