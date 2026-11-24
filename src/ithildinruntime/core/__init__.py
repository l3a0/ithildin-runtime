"""The code every module shares: types, interfaces, the codec, the keys and the calendar.

`core` is not a module of its own. Every module may import it, and it imports
nothing else from `ithildinruntime` and nothing outside the standard library
but `rfc8785`. docs/architecture/05-building-blocks.md's `## Package layout
and the import rule` says what it holds and why.
"""

from .codec import (
    CodecError,
    Places,
    decode,
    encode,
    exceeds_places,
    from_received,
    places,
    to_decimal,
)
from .halt import Halt, Stopped
from .keys import *  # noqa: F403
from .keys import __all__ as _keys
from .names import DATA_STAGE, SNAPSHOT_OUTPUT
from .plan import PLAN_FORMAT, BuiltPlan, build_plan
from .ports import *  # noqa: F403
from .ports import __all__ as _ports
from .queue import *  # noqa: F403
from .queue import __all__ as _queue
from .records import *  # noqa: F403
from .records import __all__ as _records
from .sessions import (
    BeforeClose,
    ExchangeTime,
    ScheduleTime,
    Session,
    SessionTable,
    load_sessions,
    parse_sessions,
    to_utc,
)
from .storage import *  # noqa: F403
from .storage import __all__ as _storage
from .strategy import STRATEGIES, StrategyConfig, load_strategies, parse_strategy

__all__ = [
    "DATA_STAGE",
    "PLAN_FORMAT",
    "SNAPSHOT_OUTPUT",
    "BeforeClose",
    "BuiltPlan",
    "CodecError",
    "ExchangeTime",
    "Halt",
    "Places",
    "ScheduleTime",
    "Session",
    "STRATEGIES",
    "SessionTable",
    "Stopped",
    "StrategyConfig",
    "build_plan",
    "decode",
    "encode",
    "exceeds_places",
    "from_received",
    "load_sessions",
    "load_strategies",
    "parse_sessions",
    "parse_strategy",
    "places",
    "to_decimal",
    "to_utc",
    *_keys,
    *_ports,
    *_queue,
    *_records,
    *_storage,
]
