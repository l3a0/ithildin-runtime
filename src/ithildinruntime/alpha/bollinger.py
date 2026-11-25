"""Chan's Bollinger band on the price spread between two instruments.

docs/architecture/01-introduction-and-goals.md's `## The strategy the skeleton
runs` gives the rule and says why the held unit is recomputed from prices at
every decision rather than remembered. The model keeps nothing between
decisions. It reads the snapshot and its strategy's `[alpha]` section, and no
clock, mode or book.

A decision reads a window of each leg's prices, one per session. The newest is
the decision price, which `core.mark` gives as of the decision time: the midpoint
of the leg's quote where the snapshot holds one, and the close of the decision
session's own bar where it holds none. Each price before it is the close of an
earlier session's bar. So one path serves a decision before the close and one at
it, and the model prices a leg the way its size and the gate do.

The section looks like this.

    [alpha]
    model = "bollinger_spread"
    dependent = "USO"
    independent = "GLD"
    hedge_sessions = 20
    band_sessions = 20
    entry = 1
    exit = 0

`dependent` is the leg held at one share per unit, and `independent` the leg
that hedges it. `hedge_sessions` is how many prices the hedge ratio is fitted
on, and `band_sessions` how many spreads the z-score is measured against.
`entry` and `exit` count standard deviations of the spread from its mean.

A section halts the model, naming the strategy and the key, on each of these.

1. A key this form lacks, or one it has and the section does not.
2. A `dependent` or an `independent` that is no text, or two legs that are
   not exactly the strategy's instruments.
3. A count of sessions that is no whole number, a `hedge_sessions` below 3 or
   a `band_sessions` below 2, where the fit or the deviation has no value.
4. A threshold that is no number, or thresholds outside −entry < exit < entry.
   With `exit` above `entry` one z-score would both enter and exit. Below
   −entry, an entry on one side would leave the other side held.
5. An `entry` no z-score can reach. The newest of n spreads sits inside its own
   mean and deviation, so its z-score stays within (n − 1)/√n, which is 4.2485
   at 20 spreads. A strategy with a higher entry would never hold a unit. This
   refuses what cannot happen, and no entry that is only rarely passed.

A decision halts, naming the strategy and the instrument or the session, on
each of these.

1. A leg that is missing, stale or has no daily bar, or any other instrument
   in the snapshot that is missing, stale or has no bar, in
   `Snapshot.require_fresh`'s own words.
2. A bar whose basis is not `adjusted`, because a split inside a window of
   bars as traded would read as a move of the market. A quote carries no basis
   to check. Data's stage halts first where one leg's bars are of two series,
   such as both `adjusted` and `raw` ones, or where a bar's price kind is
   unstated.
3. Legs whose bars name different sessions.
4. A newest bar that is of neither the decision's own session, meaning the
   latest session that opened by the decision, nor the session before it. With
   an older one the window would miss a session. A decision the calendar holds
   no session for, or none before its own, halts too.
5. A leg `core.mark` gives no price, in its words. It has none where the
   snapshot holds neither a quote of the leg nor a bar of the decision's
   session, where it holds both, where the judge of a quote finds a fault, and
   where a bar ended or was knowable after the decision.
6. Fewer prices than the newest z-score reads.
7. A window of `independent` prices that are all equal, where the slope has no
   value, and a band window whose spreads do not move, where the z-score has
   none.
8. A window that ends before it settles which unit is held.
9. A statistic the codec cannot hold.

Every figure is a `Decimal` at 28 digits, worked out inside a context of the
model's own, so the context a caller set changes no byte. The model decides on
the unrounded values and rounds only what it stores.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from datetime import date
from decimal import Context, Decimal, localcontext
from typing import NamedTuple

from ithildinruntime.core import (
    Basket,
    CodecError,
    Forecast,
    ForecastSet,
    Halt,
    InstrumentId,
    Signal,
    Snapshot,
    StrategyConfig,
    load_sessions,
    mark,
    places,
    to_decimal,
)

__all__ = ["SECTION", "BollingerSpread", "deviation", "ends", "held_unit", "mean", "slope"]

SECTION = "alpha"
_KEYS = frozenset(
    ("model", "dependent", "independent", "hedge_sessions", "band_sessions", "entry", "exit")
)
_INTERVAL = "1d"
_ADJUSTED = "adjusted"
_ARITHMETIC = Context(prec=28)
# A spread that never moves in exact arithmetic reads a deviation near 5e-27 at
# 28 digits, so a band at or below this has no z-score.
_FLAT = Decimal("1e-12")
_PLACES = places(Signal, "value")


def mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def deviation(values: Sequence[Decimal]) -> Decimal:
    """The standard deviation with the n − 1 divisor, as Chan's MATLAB computes it."""
    middle = mean(values)
    squares = sum(((v - middle) ** 2 for v in values), Decimal(0))
    return (squares / (len(values) - 1)).sqrt()


def slope(dependent: Sequence[Decimal], independent: Sequence[Decimal]) -> Decimal:
    """The least-squares slope of `dependent` on `independent` and a constant."""
    y_mean, x_mean = mean(dependent), mean(independent)
    moved = sum(
        ((x - x_mean) * (y - y_mean) for x, y in zip(independent, dependent, strict=True)),
        Decimal(0),
    )
    return moved / sum(((x - x_mean) ** 2 for x in independent), Decimal(0))


def ends(side: int, score: Decimal, exit_at: Decimal) -> bool:
    """Whether a position on `side`, 1 for long and −1 for short, ends at this z-score.

    A long ends above −exit and a short below exit. It stands apart from the
    entry so that another entry rule can be run with the same exit.
    """
    return score > exit_at.copy_negate() if side > 0 else score < exit_at


def held_unit(
    scores: Iterable[Decimal], entry: Decimal, exit_at: Decimal
) -> tuple[int, int] | None:
    """The unit held after the newest z-score, and how many z-scores settled it.

    `scores` runs from the newest back, and only as far as it is read. The long
    side is settled by the newest z-score that enters it or ends it, and the
    short side by its own. `None` means `scores` ran out before both settled.
    A z-score on a threshold settles neither.
    """
    long = short = None
    for read, score in enumerate(scores, start=1):
        if long is None:
            if score < entry.copy_negate():
                long = 1
            elif ends(1, score, exit_at):
                long = 0
        if short is None:
            if score > entry:
                short = 1
            elif ends(-1, score, exit_at):
                short = 0
        if long is not None and short is not None:
            return long - short, read
    return None


class _Band(NamedTuple):
    """The spread's mean, deviation and z-score on one session."""

    mean: Decimal
    deviation: Decimal
    score: Decimal


class BollingerSpread:
    """One strategy's band on a spread, which implements `core.AlphaModel`."""

    def __init__(self, strategy: StrategyConfig) -> None:
        with localcontext(_ARITHMETIC):
            self._read(strategy)

    def _read(self, strategy: StrategyConfig) -> None:
        section = strategy.section(SECTION)

        def refuse(what: str) -> Halt:
            return Halt(
                f"strategy {strategy.name}'s configuration {strategy.source} has an "
                f"[{SECTION}] section that {what}"
            )

        if missing := sorted(_KEYS - set(section)):
            raise refuse(f"states no {', '.join(missing)}")
        if unknown := sorted(set(section) - _KEYS):
            raise refuse(f"holds {', '.join(unknown)}, which this model does not have")

        def leg(key: str) -> InstrumentId:
            value = section[key]
            if not isinstance(value, str):
                raise refuse(f"has {key} = {value!r}, which is not an instrument id")
            return InstrumentId(value)

        def count(key: str, least: int) -> int:
            value = section[key]
            # A bool is an int to Python, and no count.
            if isinstance(value, bool) or not isinstance(value, int) or value < least:
                raise refuse(f"has {key} = {value}, which is not a whole number of {least} or more")
            return value

        def threshold(key: str) -> Decimal:
            value = section[key]
            if isinstance(value, bool) or not isinstance(value, int | Decimal):
                raise refuse(f"has {key} = {value!r}, which is not a number")
            return Decimal(value)

        self._strategy = strategy.name
        self._dependent, self._independent = leg("dependent"), leg("independent")
        if sorted((self._dependent, self._independent)) != sorted(strategy.instruments):
            raise refuse(
                f"names the legs {self._dependent} and {self._independent}, which are not "
                f"exactly the strategy's instruments, {', '.join(strategy.instruments)}"
            )
        self._hedge_sessions = count("hedge_sessions", 3)
        self._band_sessions = count("band_sessions", 2)
        self._entry, self._exit = threshold("entry"), threshold("exit")
        if not self._entry.copy_negate() < self._exit < self._entry:
            raise refuse(
                f"has entry = {self._entry} and exit = {self._exit}, and an exit sits between "
                "−entry and entry"
            )
        # Squared and in whole numbers, so the comparison is exact. An entry equal
        # to the bound is refused too, because a z-score on a threshold enters nothing.
        spreads = self._band_sessions
        top, bottom = self._entry.as_integer_ratio()
        if top * top * spreads >= ((spreads - 1) * bottom) ** 2:
            raise refuse(
                f"has entry = {self._entry}, which no z-score over {spreads} spreads can pass"
            )

    def forecast(self, snapshot: Snapshot) -> ForecastSet:
        """The basket, the unit held of it and the six fitted values behind them."""
        with localcontext(_ARITHMETIC):
            try:
                return self._forecast(snapshot)
            except CodecError as error:
                raise Halt(
                    f"strategy {self._strategy} cannot store its forecast for "
                    f"{snapshot.decision_at.isoformat()}: {error}"
                ) from error

    def _forecast(self, snapshot: Snapshot) -> ForecastSet:
        fits = self._fits(snapshot)
        found = held_unit(fits.scores(), self._entry, self._exit)
        if found is None:
            raise Halt(
                f"strategy {self._strategy} cannot tell which unit it holds: no z-score from "
                f"{fits.days[fits.reads - 1]} to {fits.days[-1]} settles both sides"
            )
        unit, read = found
        newest = len(fits.days) - 1
        hedge, band = fits.hedge(newest), fits.band(newest)

        def signal(name: str, value: Decimal, sessions: int) -> Signal:
            return Signal(name, _stored(value), fits.days[-sessions], fits.days[-1])

        return ForecastSet(
            forecasts=(
                Forecast(
                    strategy=self._strategy,
                    basket=Basket(
                        {self._dependent: _stored(Decimal(1)), self._independent: _stored(-hedge)}
                    ),
                    direction=unit,
                    horizon_sessions=1,
                    confidence=_stored(Decimal(1)),
                ),
            ),
            signals=(
                signal("hedge_ratio", hedge, self._hedge_sessions),
                signal("spread", fits.spread(newest), self._hedge_sessions),
                signal("spread_mean", band.mean, fits.reads),
                signal("spread_std", band.deviation, fits.reads),
                signal("z_score", band.score, fits.reads),
                signal("unit", Decimal(unit), fits.reads + read - 1),
            ),
        )

    def _fits(self, snapshot: Snapshot) -> _Fits:
        """The two legs' prices, after the checks a window of them must pass."""

        def refuse(what: str) -> Halt:
            return Halt(f"strategy {self._strategy} {what}")

        legs = (self._dependent, self._independent)
        snapshot.require_fresh({leg: [_INTERVAL] for leg in legs})
        bars = {leg: snapshot.bars[leg][_INTERVAL] for leg in legs}
        for leg in legs:
            for bar in bars[leg]:
                if bar.basis != _ADJUSTED:
                    raise refuse(
                        f"reads {_ADJUSTED} prices, and {leg}'s bar of {bar.session} has the "
                        f"basis {bar.basis}"
                    )
        days = {leg: [bar.session for bar in bars[leg]] for leg in legs}
        if days[self._dependent] != days[self._independent]:
            apart = sorted(set(days[self._dependent]) ^ set(days[self._independent]))
            raise refuse(
                f"reads both legs on the same sessions, and {self._dependent} and "
                f"{self._independent} have bars of different ones"
                + (f", such as {apart[0]}" if apart else "")
            )
        table = load_sessions()
        try:
            own = table.opened_by(snapshot.decision_at).day
            before = table.before(own).day
        except Halt as halt:
            raise refuse(f"cannot place its decision on a session: {halt.reason}") from None
        newest = days[self._dependent][-1]
        if newest not in (before, own):
            raise refuse(
                f"decides on the session {own}, and the newest bar of {self._dependent} and "
                f"{self._independent} is {newest}'s"
            )

        def prices(leg: InstrumentId) -> list[Decimal]:
            try:
                price = mark(snapshot, leg, before, snapshot.decision_at)
            except Halt as halt:
                raise refuse(f"has no price for {own}: {halt.reason}") from None
            return [*(bar.close for bar in bars[leg] if bar.session != own), price]

        dependent, independent = prices(self._dependent), prices(self._independent)
        reads = self._hedge_sessions + self._band_sessions - 1
        if len(dependent) < reads:
            raise refuse(
                f"reads {reads} sessions of {self._dependent} and {self._independent}, and the "
                f"snapshot holds {len(dependent)} to {own}"
            )
        return _Fits(
            refuse=refuse,
            days=[*(day for day in days[self._dependent] if day != own), own],
            dependent=dependent,
            independent=independent,
            hedged_by=self._independent,
            hedge_sessions=self._hedge_sessions,
            band_sessions=self._band_sessions,
        )


class _Fits:
    """The values fitted on each session, worked out only for a session that is asked for."""

    def __init__(
        self,
        *,
        refuse,
        days: Sequence[date],
        dependent: Sequence[Decimal],
        independent: Sequence[Decimal],
        hedged_by: InstrumentId,
        hedge_sessions: int,
        band_sessions: int,
    ) -> None:
        self.days = days
        # How many sessions one z-score reads.
        self.reads = hedge_sessions + band_sessions - 1
        self._refuse = refuse
        self._dependent = dependent
        self._independent = independent
        self._hedged_by = hedged_by
        self._hedge_sessions = hedge_sessions
        self._band_sessions = band_sessions
        self._hedges: dict[int, Decimal] = {}

    def hedge(self, at: int) -> Decimal:
        """The slope fitted on the prices that end at `at`."""
        if at not in self._hedges:
            window = slice(at - self._hedge_sessions + 1, at + 1)
            prices = self._independent[window]
            if all(price == prices[0] for price in prices):
                raise self._refuse(
                    f"cannot fit a hedge ratio on {self.days[at]}: {self._hedged_by}'s last "
                    f"{self._hedge_sessions} prices are all {prices[0]}"
                )
            self._hedges[at] = slope(self._dependent[window], prices)
        return self._hedges[at]

    def spread(self, at: int) -> Decimal:
        """The spread at `at`'s prices, under the ratio fitted on that session."""
        return self._dependent[at] - self.hedge(at) * self._independent[at]

    def band(self, at: int) -> _Band:
        spreads = [self.spread(n) for n in range(at - self._band_sessions + 1, at + 1)]
        middle, wide = mean(spreads), deviation(spreads)
        if wide <= _FLAT:
            raise self._refuse(
                f"has no z-score on {self.days[at]}: the last {self._band_sessions} spreads' "
                f"standard deviation is {wide}"
            )
        return _Band(middle, wide, (spreads[-1] - middle) / wide)

    def scores(self) -> Iterator[Decimal]:
        """Each session's z-score, from the newest back to the oldest the window holds."""
        for at in range(len(self.days) - 1, self.reads - 2, -1):
            yield self.band(at).score


def _stored(value: Decimal) -> Decimal:
    """A statistic at the places it is stored at, and a zero with no sign."""
    rounded = to_decimal(value, _PLACES)
    return rounded.copy_abs() if rounded.is_zero() else rounded
