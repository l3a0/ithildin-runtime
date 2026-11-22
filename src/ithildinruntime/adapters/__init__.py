"""The adapters behind `core`'s ports: stores, read-only buckets, queues and clocks.

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

__all__ = [
    "Faults",
    "FolderBucket",
    "FolderStore",
    "HistoryClock",
    "PipeQueue",
    "S3Bucket",
    "S3Store",
    "SqsQueue",
    "WallClock",
]
