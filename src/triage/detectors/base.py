from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..model import Point, Signal

WARMUP_POINTS = 3 * 1440      # three days: enough for a weekday minute profile before judging


@dataclass
class MetricContext:
    """What the seasonal model knows about one metric at one point."""
    residual: float      # observed minus expected-for-this-minute (log space)
    scale: float         # robust spread of recent residuals
    expected: float
    mature: bool         # a real minute profile stands behind ``expected`` (own or borrowed)
    own: bool = True     # ...and it is this day-type's own history, so "down" is trustworthy

    @property
    def z(self) -> float:
        return self.residual / self.scale


Context = dict[str, MetricContext]


class Detector(ABC):
    """A detector sees one service's points in time order and emits signals.

    Every detector receives the seasonal model's context for the point:
    the residual after removing the expected minute-of-day level, and a
    robust scale for it. Detectors that reason about change over time
    (EWMA, CUSUM) work on residuals, so the morning ramp is not a change.

    Rules every detector follows:
    * a point never contributes to the baseline it is scored against;
    * nothing is emitted during the three-day warm-up;
    * scores are standardised so the fusion layer can compare detectors.
    """
    name: str
    warmup: int = WARMUP_POINTS

    @abstractmethod
    def observe(self, p: Point, ctx: Context) -> list[Signal]: ...
