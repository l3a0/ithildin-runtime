"""The adapters behind `core`'s ports: the folder and S3 stores, and the pipe and SQS queues.

Only `runtime` imports this package, and it imports nothing from
`ithildinruntime` but `core`, so no module builds an adapter and picks its own
mode. botocore is imported here and nowhere else, and so is boto3 once the
runtime builds its clients here.
"""

from .folder import Faults, FolderStore
from .pipe import PipeQueue
from .s3 import S3Store
from .sqs import SqsQueue

__all__ = ["Faults", "FolderStore", "PipeQueue", "S3Store", "SqsQueue"]
