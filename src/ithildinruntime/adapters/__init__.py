"""The adapters behind `core`'s ports: stores, read-only buckets, queues, clocks and time servers.

Only `runtime` imports this package, and it imports nothing from
`ithildinruntime` but `core`, so no module builds an adapter and picks its own
mode. botocore is imported here and nowhere else, and so is boto3 once the
runtime builds its clients here.
"""

from .bucket import FolderBucket, S3Bucket
from .clocks import HistoryClock, WallClock
from .folder import Faults, FolderStore
from .pipe import PipeQueue
from .s3 import S3Store
from .sqs import SqsQueue
from .time_servers import (
    TIME_SERVERS,
    AskFailed,
    Exchange,
    TimeServer,
    TimeServers,
    exchange,
    fresh_nonce,
    raw_us,
)

__all__ = [
    "TIME_SERVERS",
    "AskFailed",
    "Exchange",
    "Faults",
    "FolderBucket",
    "FolderStore",
    "HistoryClock",
    "PipeQueue",
    "S3Bucket",
    "S3Store",
    "SqsQueue",
    "TimeServer",
    "TimeServers",
    "WallClock",
    "exchange",
    "fresh_nonce",
    "raw_us",
]
