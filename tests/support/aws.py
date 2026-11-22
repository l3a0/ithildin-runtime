"""The test environment in AWS, as the suites that run in ci.yml's `aws` job reach it.

One client guard and the names and bounds those suites share live here, so the
store suite's bucket, the queue suite's queues and the smoke check in
`tests/aws/test_test_environment.py` read the same values. infra/test applies
the bucket and the queues, and infra/test/bootstrap/roles.tf grants the test
role what these suites may do.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import boto3
from botocore.config import Config

REGION = "us-east-1"
TEST_BUCKET = "ithildin-runtime-test"
# Each reader's queue is `<prefix>-<reader>`, and its dead-letter queue adds `-dlq`.
TEST_QUEUE_PREFIX = "ithildin-test"

# How long a case waits for S3's event to reach a queue. design.md says S3's
# delivery takes "usually seconds and sometimes a minute or longer". A bound
# costs its full length only when a case fails.
DELIVERY_SECONDS = 60


def aws_client(service: str, config: Config | None = None):
    """A client for AWS itself, with the job's own credentials from the environment.

    It takes AWS's own endpoint whatever AWS_ENDPOINT_URL or a config file says,
    and refuses a host outside AWS, so a green run in the `aws` job cannot come
    from an emulator. Ten seconds to connect and thirty to read bound a call on
    a slow service well inside the job's limit, and `config` overrides either.
    """
    own = Config(ignore_configured_endpoint_urls=True, connect_timeout=10, read_timeout=30)
    made = boto3.client(service, region_name=REGION, config=own.merge(config) if config else own)
    host = urlsplit(made.meta.endpoint_url).hostname or ""
    if not host.endswith(".amazonaws.com"):
        raise RuntimeError(f"the test environment's {service} client points at {host}, not AWS")
    return made
