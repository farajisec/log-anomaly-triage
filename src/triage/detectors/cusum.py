"""Two-sided CUSUM on seasonal residuals.

CUSUM accumulates small deviations and raises a signal when the sum
crosses a decision boundary. That makes it the opposite of the EWMA: slow
to react to a single spike, excellent at a level shift or a creeping
drift that no single point would flag. Because it runs on residuals, the
daily ramp contributes nothing; only departures from the expected shape
accumulate.

Two things about real metrics shape the parameters. Residuals are
autocorrelated, so partial sums wander much further than the textbook
i.i.d. analysis assumes; the boundary ``H`` is set for that, not for
white noise. And the sums only run against a mature profile: against the
hour-of-day fallback the residual carries a slow bias within each hour,
which is exactly what a CUSUM is built to accumulate.

The score maps the boundary above the fusion threshold, so a crossing
can open a run on its own, and the sums reset once they reach twice the
boundary so a long shift produces a series of crossings rather than one
unbounded number.
"""
from __future__ import annotations

from ..model import METRICS, Point, Signal
from .base import Context, Detector

SLACK = 0.75           # k: deviations smaller than this (in sigma) are ignored
H = 28.0               # decision boundary in sigma-units, sized for autocorrelated residuals
BOUNDARY_SCORE = 5.0   # score at the boundary; above the fusion threshold by design
RESET_AT = 2.0
BIAS_ALPHA = 0.005     # slow bias tracker: time constant ~3 hours


class Cusum(Detector):
    name = "cusum"

    def __init__(self) -> None:
        self.pos: dict[str, float] = dict.fromkeys(METRICS, 0.0)
        self.neg: dict[str, float] = dict.fromkeys(METRICS, 0.0)
        self.bias: dict[str, float] = dict.fromkeys(METRICS, 0.0)
        self.was_own: dict[str, bool] = dict.fromkeys(METRICS, True)
        self.n = 0

    def observe(self, p: Point, ctx: Context) -> list[Signal]:
        out: list[Signal] = []
        for m in METRICS:
            c = ctx[m]
            if self.n < self.warmup or not c.mature:
                self.pos[m] = self.neg[m] = 0.0
                continue
            if c.own != self.was_own[m]:            # profile changed under us: start the sums afresh
                self.pos[m] = self.neg[m] = 0.0
                self.was_own[m] = c.own
            # subtract a very slow estimate of residual bias, so a trend the profile has not
            # caught up with yet does not accumulate. A real shift outruns it by far. The
            # bias is learned only against the day-type's own profile: a borrowed weekday
            # profile sits a constant factor above the weekend and would poison it.
            z = c.z - self.bias[m]
            if c.own and abs(z) < 3.0:
                self.bias[m] += BIAS_ALPHA * z
            if not c.own:
                z = max(0.0, c.z)
            self.pos[m] = max(0.0, self.pos[m] + z - SLACK)
            self.neg[m] = max(0.0, self.neg[m] - z - SLACK)
            s = max(self.pos[m], self.neg[m]) / H * BOUNDARY_SCORE
            out.append(Signal(self.name, m, s, "up" if self.pos[m] >= self.neg[m] else "down",
                              f"cumulative deviation {max(self.pos[m], self.neg[m]):.1f}σ", True))
            if s >= BOUNDARY_SCORE * RESET_AT:
                self.pos[m] = self.neg[m] = 0.0
        self.n += 1
        return out
