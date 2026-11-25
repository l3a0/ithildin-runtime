"""The execution worker: the one path an order takes to a broker.

This file imports nothing, because it runs whenever `execution.brokers` is
imported, which may import only `core`. docs/architecture/05-building-blocks.md's
`#### Only OrderPath holds a Broker` says what the package holds.
"""
