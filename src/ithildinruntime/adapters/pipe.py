"""The named-pipe queue, which a backtest and CI read in place of SQS.

The memory-backed folder plays S3's part. After each write it sends the event
down the named pipe of each reader its routes name, and the reader's receive
loop reads it here. A named pipe, or FIFO, is a file the kernel passes bytes
through from a writer to a reader without storing them.

Three choices keep it close to SQS where it can be, and say where it is not.

1. **Every event is one record of `RECORD` bytes.** The kernel writes a
   record of at most `PIPE_BUF` bytes whole, even with several writers on one
   pipe, and macOS's `PIPE_BUF` is 512. So a body is padded with spaces to
   512 bytes, a read asks for whole records, and two instances sharing one
   pipe each take whole events, never more than they ask for.
2. **A reader that is down misses its events.** A writer opens the pipe
   without waiting, and the kernel refuses that open when no reader holds the
   pipe, so the event is dropped. A full pipe drops it too. On macOS a pipe
   held 16 records in the queue suite's case, and Linux's default of 64 KiB
   holds 128. design.md's `### How a component learns of a change` says a
   pipe keeps nothing while its reader is down, and a worker recovers by
   checking its keys.
3. **`done`, `extend` and `release` do nothing.** A pipe hands each record to
   one reader once and keeps nothing to hide or return, so the queue suite
   holds the pipe only to what both queues guarantee.
"""

from __future__ import annotations

import errno
import itertools
import os
import select
import stat
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from ithildinruntime.core import Event, QueueUnavailable, parse_event

__all__ = ["RECORD", "PipeQueue", "pipe_path", "send"]

RECORD = 512


def pipe_path(pipes: Path | str, reader: str) -> Path:
    """Where a reader's named pipe lives in the queue folder."""
    return Path(pipes) / f"{reader}.pipe"


def record(body: bytes) -> bytes:
    """One event as the pipe carries it, refusing a body too long to arrive whole."""
    if b"\n" in body or len(body) >= RECORD:
        raise ValueError(f"an event of {len(body)} bytes does not fit one {RECORD}-byte record")
    return body.ljust(RECORD - 1) + b"\n"


def send(pipes: Path | str, reader: str, body: bytes) -> bool:
    """Send one event to a reader, and say whether its pipe took it."""
    data = record(body)
    try:
        fd = os.open(pipe_path(pipes, reader), os.O_WRONLY | os.O_NONBLOCK)
    except OSError as error:
        # No pipe yet, or no reader holding it: the reader is down.
        if error.errno in (errno.ENOENT, errno.ENXIO):
            return False
        raise
    try:
        return os.write(fd, data) == len(data)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


class PipeQueue:
    """A `Queue` on one reader's named pipe.

    The reader creates its pipe and holds it open from the moment this is
    built, so a writer's event reaches it from then on. It also holds the pipe
    open for writing itself, so a read never meets the end of the file while
    no writer has the pipe open.

    `close` wakes a receive that is waiting and waits for it to return before
    it closes anything. Closing a descriptor under a waiting receive would let
    the kernel give its number to the next file another thread opens, and the
    receive would then read that file's bytes, as CI on Linux showed.
    """

    def __init__(self, pipes: Path | str, reader: str, *, wait_seconds: float = 20.0) -> None:
        if wait_seconds <= 0:
            raise ValueError("a receive waits a positive time")
        path = pipe_path(pipes, reader)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkfifo(path)
        except FileExistsError:
            if not stat.S_ISFIFO(path.stat().st_mode):
                raise ValueError(f"{path} exists and is not a named pipe") from None
        self._wait = wait_seconds
        self._read = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self._keep = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        self._wake, self._waker = os.pipe()
        self._serial = itertools.count(1)
        self._lock = threading.Lock()
        # Held for the whole of a receive, so `close` waits for it.
        self._reading = threading.Lock()
        self._closed = False

    def receive(self, limit: int) -> Sequence[Event]:
        if limit < 1:
            raise ValueError("a receive asks for at least one event")
        with self._reading:
            if self._closed:
                raise QueueUnavailable("the pipe is closed")
            return self._receive(limit)

    def _receive(self, limit: int) -> Sequence[Event]:
        end = time.monotonic() + self._wait
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return ()
            try:
                ready, _, _ = select.select([self._read, self._wake], [], [], left)
            except (OSError, ValueError) as error:
                raise QueueUnavailable(f"the pipe cannot be read: {error}") from None
            if self._wake in ready or not ready:
                return ()
            try:
                data = os.read(self._read, RECORD * limit)
            except BlockingIOError:
                # Another instance on the same pipe took the records first.
                continue
            except OSError as error:
                raise QueueUnavailable(f"the pipe cannot be read: {error}") from None
            if data:
                return self._events(data)

    def _events(self, data: bytes) -> tuple[Event, ...]:
        events = []
        with self._lock:
            for start in range(0, len(data), RECORD):
                body = data[start : start + RECORD].rstrip()
                events.append(parse_event(body, f"pipe-{next(self._serial)}"))
        return tuple(events)

    def done(self, event: Event) -> None:
        """A pipe keeps nothing to delete."""

    def extend(self, event: Event) -> None:
        """A pipe hides nothing from another reader."""

    def release(self, event: Event) -> None:
        """A pipe keeps nothing to return, so a released event is gone."""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.write(self._waker, b"x")
        with self._reading:
            for fd in (self._read, self._keep, self._wake, self._waker):
                try:
                    os.close(fd)
                except OSError:
                    pass
