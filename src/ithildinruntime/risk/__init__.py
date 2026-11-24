"""The risk model's package: the one pre-trade gate every order passes, and the risk stage.

It imports only `core`. The execution worker's entry point hands `Gate` to the
order path, so execution imports no `risk`. The risk worker's entry point runs
`RiskStage`, which writes each decision's limits.
"""

from .gate import Gate
from .model import SECTION, LimitRisk
from .stage import RiskStage

__all__ = ["SECTION", "Gate", "LimitRisk", "RiskStage"]
