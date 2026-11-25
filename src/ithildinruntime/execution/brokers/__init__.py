"""The brokers an order can reach, each an adapter of `core.Broker`.

Only `runtime/execution_worker.py` builds one, and it hands it to the account's
`path`, which alone calls it. A broker imports `core` and nothing else of the
platform.
"""
