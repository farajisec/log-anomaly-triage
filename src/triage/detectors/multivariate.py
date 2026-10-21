"""Load decoupling: a multivariate check that single-metric detectors miss.

Latency, CPU and error rate normally move with request volume. When they
rise while volume is flat, something is wrong with the service; when
volume rises and they rise with it, that is a traffic surge, which is
capacity planning's problem, not an incident. This detector fits a
running power-law relation (linear in log-log) between each health metric
and load over a twelve-hour window, refitted every thirty points, and
scores the residual from that relation with a robust scale. It works on
raw values, not seasonal residuals, because the relation it models is
between metrics, not between a metric and the clock.

Only the "up" direction is reported: health being *better* than load
predicts is not a problem anyone needs paged for. Volume itself is left
to the seasonal model.
"""
from __future__ import annotations

import statistics
from collections import deque

from ..model import Point, Signal
from .base import Context, Detector
from .seasonal import transform

WINDOW = 720
REFIT_EVERY = 30
HEALTH = ("latency_p95_ms", "error_rate", "cpu_pct")
SCALE_FLOOR = 0.03


def fit_line(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    return b, my - b * mx


class LoadDecoupling(Detector):
    name = "load_decoupling"

    def __init__(self) -> None:
        self.hist: deque = deque(maxlen=WINDOW)
        self.fits: dict[str, tuple[float, float, float, float]] = {}
        self.since_fit = REFIT_EVERY
        self.n = 0

    def _refit(self) -> None:
        xs = [h["requests_per_s"] for h in self.hist]
        for m in HEALTH:
            ys = [h[m] for h in self.hist]
            b, a = fit_line(xs, ys)
            resid = [y - (a + b * x) for x, y in zip(xs, ys, strict=True)]
            med = statistics.median(resid)
            sig = max(statistics.median([abs(r - med) for r in resid]) * 1.4826, SCALE_FLOOR)
            self.fits[m] = (b, a, med, sig)
        self.since_fit = 0

    def observe(self, p: Point, ctx: Context) -> list[Signal]:
        out: list[Signal] = []
        lx = transform("requests_per_s", p.values["requests_per_s"])
        row = {"requests_per_s": lx, **{m: transform(m, p.values[m]) for m in HEALTH}}
        if self.n >= self.warmup and len(self.hist) >= WINDOW:
            if self.since_fit >= REFIT_EVERY:
                self._refit()
            self.since_fit += 1
            for m in HEALTH:
                b, a, med, sig = self.fits[m]
                z = (row[m] - (a + b * lx) - med) / sig
                out.append(Signal(self.name, m, max(0.0, z), "up",
                                  f"{m} vs load: {(2.718281828 ** (row[m] - (a + b * lx)) - 1) * 100:+.0f}% "
                                  f"from what {p.values['requests_per_s']:.0f} rps predicts"))
        if not out or max(s.score for s in out) < 3.0:
            self.hist.append(row)
        self.n += 1
        return out
