"""The brokers an order can reach, each an adapter of `core.Broker`.

In `src/`, `OrderPath` alone calls one, and `runtime/execution_worker.py` is the
one file to build one. A broker imports `core` and nothing else of the platform.
"""
