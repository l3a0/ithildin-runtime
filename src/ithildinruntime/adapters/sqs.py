"""The SQS queue, which shadow, paper and live read.

S3 announces each blob it stores to EventBridge, and each rule the routes
generate copies a matching event to one reader's queue. design.md's `### How
a component learns of a change` holds the reasons.

1. **A receive long polls.** SQS holds the call open for up to twenty
   seconds and answers as soon as a message arrives. The client's read
   timeout sits a little over that wait, rather than botocore's default of
   sixty, so a connection that died without a clean close shows itself
   within one wait. The queue refuses a client whose read timeout does not.
2. **`extend` resets the visibility timeout from now.** SQS counts a new
   visibility timeout from the moment it receives the change, so the loop
   calls it while the handler works and the message stays hidden.
3. **`release` sets the visibility to 0**, so another instance can take the
   message at once.
4. **Every refusal and every missing answer is `QueueUnavailable`.** A
   delete or an extend of a message whose receipt has expired is one, and
   costs nothing, because the item is checked by its keys whoever receives
   the message next.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from ithildinruntime.core import Event, QueueUnavailable, parse_event

__all__ = ["SqsQueue"]

R = TypeVar("R")

# SQS's own limits: ten messages a receive, twenty seconds of long polling,
# and twelve hours of visibility.
MOST_MESSAGES = 10
LONGEST_WAIT = 20
LONGEST_VISIBILITY = 12 * 60 * 60
# How far past the wait a client's read timeout may sit.
READ_MARGIN = 10


class SqsQueue:
    """A `Queue` on one SQS queue. `client` is a boto3 SQS client."""

    def __init__(
        self,
        client: Any,
        url: str,
        *,
        visibility_timeout: int,
        wait_seconds: int = LONGEST_WAIT,
    ) -> None:
        if not 1 <= wait_seconds <= LONGEST_WAIT:
            raise ValueError(f"a long poll waits 1 to {LONGEST_WAIT} seconds")
        if not 1 <= visibility_timeout <= LONGEST_VISIBILITY:
            raise ValueError(f"a visibility timeout is 1 to {LONGEST_VISIBILITY} seconds")
        read_timeout = client.meta.config.read_timeout
        if not wait_seconds < read_timeout <= wait_seconds + READ_MARGIN:
            raise ValueError(
                f"an SQS client's read timeout sits a little over the {wait_seconds}-second "
                f"wait, at most {READ_MARGIN} seconds over it, and this one is {read_timeout}"
            )
        self._client = client
        self._url = url
        self._visibility = visibility_timeout
        self._wait = wait_seconds

    def receive(self, limit: int) -> Sequence[Event]:
        if limit < 1:
            raise ValueError("a receive asks for at least one event")
        reply = self._call(
            lambda: self._client.receive_message(
                QueueUrl=self._url,
                MaxNumberOfMessages=min(limit, MOST_MESSAGES),
                WaitTimeSeconds=self._wait,
                VisibilityTimeout=self._visibility,
            )
        )
        return tuple(
            parse_event(message.get("Body", ""), message["ReceiptHandle"])
            for message in reply.get("Messages", ())
        )

    def done(self, event: Event) -> None:
        self._call(
            lambda: self._client.delete_message(QueueUrl=self._url, ReceiptHandle=event.handle)
        )

    def extend(self, event: Event) -> None:
        self._visible_in(event, self._visibility)

    def release(self, event: Event) -> None:
        self._visible_in(event, 0)

    def _visible_in(self, event: Event, seconds: int) -> None:
        self._call(
            lambda: self._client.change_message_visibility(
                QueueUrl=self._url, ReceiptHandle=event.handle, VisibilityTimeout=seconds
            )
        )

    def _call(self, call: Callable[[], R]) -> R:
        try:
            return call()
        except ParamValidationError:
            # A call botocore refuses before sending is a bug here, never an outage.
            raise
        except ClientError as error:
            detail = error.response.get("Error", {})
            raise QueueUnavailable(
                f"{detail.get('Code', '')}: {detail.get('Message', '')}".strip()
            ) from None
        except BotoCoreError as error:
            raise QueueUnavailable(f"no answer: {error}") from None
