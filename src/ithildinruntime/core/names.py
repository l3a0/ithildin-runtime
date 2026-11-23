"""The rules a name and a SHA-256 meet to become part of a key, shared by the keys and the records.

`keys.py` builds every key from names such as a deployment, a stage or a
strategy, and from SHA-256 digests. `records.py` checks a header's strategy
names and hashes by the same rules, and cannot import `keys.py`, which
imports it. So the rules live here, once.
"""

from __future__ import annotations

import re

__all__ = ["is_name", "is_sha256"]

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def is_name(value: object) -> bool:
    """Whether `value` can be part of a key.

    A name starts with a letter or a digit, goes on in letters, digits, `.`,
    `_` and `-`, and never holds `..`, so it cannot climb out of a folder.
    """
    return isinstance(value, str) and _NAME.fullmatch(value) is not None and ".." not in value


def is_sha256(value: object) -> bool:
    """Whether `value` is a SHA-256 written as 64 lower case hex digits."""
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None
