"""The alpha stage: read data's snapshot, then store the strategy's forecast and its signals.

Each strategy has an alpha stage of its own, named `alpha/<strategy>`, so one
strategy's settings and configuration hash are all its blobs' headers name.
The stage is deterministic: its bytes come from the snapshot's key and SHA-256,
the release and the strategy's file, and it reads no clock, mode or book.

Its handler does three things, in order.

1. **It reads data's result and the snapshot it lists**, through
   `core.read_snapshot`. A result that holds a halt is passed on, as
   docs/design.md's `### Rule 7: a halt is passed on` says, naming data's
   result as its cause, before the strategy's section is read.
2. **It builds the model and asks it for the forecasts.** A halt from either,
   or a forecast that names another strategy, becomes the stage's own halt.
   Its header names data's result and the snapshot, so the halt says what it
   read, and its `cause` is empty because nothing upstream halted.
3. **It stores the forecasts and then the signals, then returns the result**,
   which lists both in that order and which the loop stores last. A crash
   between the two writes stores the same bytes again.

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
    BlobHeader,
    BlobRef,
    DecisionResult,
    Forecast,
    ForecastSet,
    Halt,
    HaltRecord,
    OutputFile,
    Signal,
    Snapshot,
    StageKind,
    Storage,
    StrategyConfig,
    encode_output,
    output_key,
    read_snapshot,
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
        self._storage = storage
        self._stage = stage
        self._kind = StageKind(kind)
        self._data_stage = data_stage
        self._deployment = deployment
        self._run = run
        self._release = release
        self._config_hashes = dict(config_hashes)

    def __call__(self, item: _Item, report: Callable[[], None]) -> DecisionResult:
        at = item.decision_at
        read = read_snapshot(
            self._storage,
            deployment=self._deployment,
            run=self._run,
            decision_at=at,
            stage=self._stage,
            data_stage=self._data_stage,
        )
        if isinstance(read, HaltRecord):
            return DecisionResult(header=self._header(at, read.cause), outputs=(), halt=read)
        report()
        header = self._header(at, read.result, read.file)
        try:
            made = self._made(read.snapshot)
        except Halt as halt:
            return DecisionResult(header=header, outputs=(), halt=self._storage.halt_record(halt))
        forecast = self._put(at, FORECAST_OUTPUT, Forecast, header, made.forecasts)
        report()
        signals = self._put(at, SIGNALS_OUTPUT, Signal, header, made.signals)
        report()
        return DecisionResult(header=header, outputs=(forecast, signals), halt=None)

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

    def _put[T](
        self, at: datetime, name: str, entry: type[T], header: BlobHeader, entries: tuple[T, ...]
    ) -> BlobRef:
        key = output_key(self._deployment, self._run, at, self._stage, name)
        data = encode_output(entry, OutputFile(header, entries, False))
        return self._storage.put(self._kind, key, data).ref

    def _header(self, at: datetime, *read: BlobRef) -> BlobHeader:
        return BlobHeader(
            format=_FORMAT,
            deployment=self._deployment,
            run=self._run,
            stage=self._stage,
            kind=str(self._kind),
            decision_at=at,
            inputs={ref.key: ref.sha256 for ref in read},
            release=self._release,
            config_hashes=self._config_hashes,
        )
