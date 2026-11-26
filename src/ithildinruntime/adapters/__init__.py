"""The adapters behind `core`'s ports: stores, read-only buckets, queues, clocks and time servers.

Only `runtime` imports this package, and it imports nothing from
`ithildinruntime` but `core`, so no module builds an adapter and picks its own
mode. botocore and boto3 are imported here and nowhere else, and `AwsClients` builds
the runtime's clients.
"""

from .alerts import LogAlerts
from .bucket import FolderBucket, S3Bucket
from .clients import AwsClients
from .clocks import HistoryClock, SystemTimer, WallClock
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
)

__all__ = [
    "TIME_SERVERS",
    "AskFailed",
    "AwsClients",
    "Exchange",
    "Faults",
    "FolderBucket",
    "FolderStore",
    "HistoryClock",
    "LogAlerts",
    "PipeQueue",
    "S3Bucket",
    "S3Store",
    "SqsQueue",
    "SystemTimer",
    "TimeServer",
    "TimeServers",
    "WallClock",
    "exchange",
]
