"""EWMA on seasonal residuals.

Tracks the recent level of the residual with an exponentially weighted
moving average and scores how far the current residual sits from it, in
units of the seasonal model's robust scale. On a healthy service the
residual level hovers near zero; a spike throws the point far from the
level within a minute or two, which makes this the fast detector of the
ensemble. It is also the one most easily fooled by a slow drift, because
the level follows the drift. That is why it is one voice among several.
"""
from __future__ import annotations

import math

from ..model import METRICS, Point, Signal
from .base import Context, Detector

ALPHA = 0.1


class EwmaResidual(Detector):
    name = "ewma"

    def __init__(self) -> None:
        self.level: dict[str, float] = {m: 0.0 for m in METRICS}
        self.n = 0

    def observe(self, p: Point, ctx: Context) -> list[Signal]:
        out: list[Signal] = []
        for m in METRICS:
            c = ctx[m]
            dev = c.residual - self.level[m]
            if self.n >= self.warmup:
                z = dev / c.scale
                if z < 0 and not c.own:
                    z = 0.0
                out.append(Signal(self.name, m, abs(z), "up" if z >= 0 else "down",
                                  f"{(math.exp(dev) - 1) * 100:+.0f}% vs the recent residual level", c.mature))
            # clamp the update so a single outlier cannot drag the level
            step = max(-3 * c.scale, min(3 * c.scale, dev))
            self.level[m] += ALPHA * step
        self.n += 1
        return out
