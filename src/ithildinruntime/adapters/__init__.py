"""The adapters behind `core`'s ports: the stores today, and the queues once #81 adds them.

Only `runtime` imports this package, and it imports nothing from
`ithildinruntime` but `core`, so no module builds an adapter and picks its own
mode. botocore is imported here and nowhere else, and so is boto3 once the
runtime builds its clients here.
"""

from .folder import FolderStore
from .s3 import S3Store

__all__ = ["FolderStore", "S3Store"]
