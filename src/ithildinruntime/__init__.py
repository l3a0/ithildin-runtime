"""ithildinruntime: one runtime for backtest, paper and live trading.

The modules follow the boxes in Rishi Narang's *Inside the Black Box*, and
docs/design.md carries the reasoning. Only `core`, the code every module
shares, the two stores in `adapters` and the journal's writer and reader in
`journal` are built so far.
"""

__all__: list[str] = []
