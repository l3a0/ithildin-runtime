"""The alpha stage: read data's snapshot, then store the strategy's forecast and its signals.

Each strategy has an alpha stage of its own, named `alpha/<strategy>`, so one
strategy's settings and configuration hash are all its blobs' headers name.
The stage is deterministic: its bytes come from the snapshot's key and SHA-256,
the release and the strategy's file, and it reads no clock, mode or book.

Its handler does three things, in order.

1. **It reads data's result and the snapshot it lists**, through
   `core.StageDecision`. A result that holds a halt is passed on, as
   docs/design.md's `### Rule 7: a halt is passed on` says, naming data's
   result as its cause, before the strategy's section is read.
2. **It builds the model and asks it for the forecasts.** A halt from either,
   or a forecast that names another strategy, becomes the stage's own halt,
   whose `cause` is empty because nothing upstream halted.
3. **It stores the forecasts and then the signals, then returns the result**,
   which lists both in that order and which the loop stores last. A crash
   between the two writes stores the same bytes again.

A halt at any of the three becomes the stage's result, whose header names
each blob read and accepted by then.

The entry point hands in the stage's name, its kind and the stage it reads, as
the stage table declares them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol

from ithildinruntime.core import (
    FORECAST_OUTPUT,
    SIGNALS_OUTPUT,
    DecisionResult,
    Forecast,
    ForecastSet,
    Halt,
    Signal,
    Snapshot,
    StageKind,
    StageRun,
    Storage,
    StrategyConfig,
)

from .models import build_model

__all__ = ["COMPONENT", "AlphaStage"]

COMPONENT = "alpha"
# The format of both files and of the stage's result header.
_FORMAT = 1


class _Item(Protocol):
    """The receive loop's item, of which alpha reads only the decision time."""

    decision_at: datetime


class AlphaStage:
    """The handler the receive loop calls for one strategy's alpha stage."""

    def __init__(
        self,
        strategy: StrategyConfig,
        storage: Storage,
        *,
        stage: str,
        kind: StageKind,
        data_stage: str,
        deployment: str,
        run: str,
        release: str,
        config_hashes: Mapping[str, str],
    ) -> None:
        self._strategy = strategy
        self._stage = stage
        self._data_stage = data_stage
        self._run = StageRun(
            storage,
            format=_FORMAT,
            stage=stage,
            kind=kind,
            deployment=deployment,
            run=run,
            release=release,
            config_hashes=config_hashes,
        )

    def __call__(self, item: _Item, report: Callable[[], None]) -> DecisionResult:
        decision = self._run.decision(item.decision_at, report)
        try:
            snapshot = decision.snapshot(self._data_stage)
            made = self._made(snapshot)
            forecast = decision.put(FORECAST_OUTPUT, Forecast, made.forecasts)
            signals = decision.put(SIGNALS_OUTPUT, Signal, made.signals)
        except Halt as halt:
            return decision.halted(halt)
        return decision.done(forecast, signals)

    def _made(self, snapshot: Snapshot) -> ForecastSet:
        # The model is built at each decision, so it keeps nothing from the one
        # before, and a section it refuses halts this strategy's stage alone.
        made = build_model(self._strategy).forecast(snapshot)
        for forecast in made.forecasts:
            if forecast.strategy != self._strategy.name:
                raise Halt(
                    f"stage {self._stage} made a forecast of strategy {forecast.strategy}, and "
                    f"forecasts strategy {self._strategy.name}"
                )
        return made
