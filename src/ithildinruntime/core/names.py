"""The rule a name must meet to become part of a key, which the key builder and the records share.

`keys.py` builds every key from names such as a deployment, a stage or a
strategy. `records.py` checks a header's strategy names by the same rule, and
cannot import `keys.py`, which imports it. So the rule lives here, once.
"""

from __future__ import annotations

import re

__all__ = ["NAME", "is_name"]

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def is_name(value: object) -> bool:
    """Whether `value` can be part of a key.

    A name starts with a letter or a digit, goes on in letters, digits, `.`,
    `_` and `-`, and never holds `..`, so it cannot climb out of a folder.
    """
    return isinstance(value, str) and NAME.fullmatch(value) is not None and ".." not in value
