"""The risk model's package, which holds the one pre-trade gate every order passes.

It imports only `core`. The execution worker's entry point hands `Gate` to the
order path, so execution imports no `risk`. The risk model's own stage, which
writes each decision's limits, is #115.
"""

from .gate import Gate

__all__ = ["Gate"]
