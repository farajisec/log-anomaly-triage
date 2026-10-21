"""The seasonal model, and the z-score detector built directly on it.

For each metric the model keeps a profile of the expected value per
(day-type, minute-of-day): the median over previous days of the values
seen within two minutes of that minute. A nightly batch job that spikes
CPU for eight minutes at 04:00 is therefore *in the profile* after three
days, as an eight-minute bump, and stops being an anomaly. Robust
statistics on hour-wide buckets cannot learn that, because a minority
pattern inside a bucket is exactly what a median rejects; the profile has
to be as fine as the pattern.

Residuals are taken in log space: ``log(x + c) - log(expected + c)``.
Service metrics are multiplicative, so a 20 % excursion is the same
surprise at 40 rps and at 400 rps, and one scale per metric then serves
every hour of the day. The scale is the MAD of recent residuals.

A profile slot is *mature* once it has ``MIN_DAYS`` days of its own
day-type. Before that the expected value falls back to the hour-of-day
median over whatever days exist, and the model withholds the "down"
direction: a drop against a baseline that has never seen a weekend is far
more often the first weekend than an outage. Points that score as
anomalous against a mature profile are not learned, so an outage does
not become next week's normal; the documented cost is that a permanent
change keeps looking like a change for about a week.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict, deque
from datetime import datetime, timezone

from ..model import METRICS, Point, Signal
from .base import Context, Detector, MetricContext

MAD_TO_SIGMA = 1.4826
POOL_MIN = 2                   # profile minute pools +-2 minutes across days
MIN_DAYS = 3
PROFILE_DAYS = 8               # remember this many days per slot: tracks slow trends
SCALE_WINDOW = 2 * 1440
SCALE_REFRESH = 60
LEARN_GATE_Z = 4.0
OFFSET = {"error_rate": 0.5, "latency_p95_ms": 5.0, "requests_per_s": 5.0, "cpu_pct": 5.0}
SCALE_FLOOR = 0.03             # 3 % in log space: nothing is measured more precisely than that


def transform(metric: str, x: float) -> float:
    return math.log(max(x, 0.0) + OFFSET[metric])


def _key(ts: float) -> tuple[int, int]:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (1 if dt.weekday() >= 5 else 0), dt.hour * 60 + dt.minute


class SeasonalModel:
    def __init__(self) -> None:
        self.profile: dict[str, dict[tuple[int, int], deque]] = {
            m: defaultdict(lambda: deque(maxlen=PROFILE_DAYS)) for m in METRICS}
        self.hourly: dict[str, dict[int, deque]] = {m: defaultdict(lambda: deque(maxlen=600)) for m in METRICS}
        self.resid: dict[str, deque] = {m: deque(maxlen=SCALE_WINDOW) for m in METRICS}
        self.scale: dict[str, float] = dict.fromkeys(METRICS, 0.1)
        self.since_scale = 0
        self.n = 0

    def _pooled(self, m: str, daytype: int, minute: int) -> tuple[list[float], int]:
        vals: list[float] = []
        days = len(self.profile[m][(daytype, minute)])
        for d in range(-POOL_MIN, POOL_MIN + 1):
            vals.extend(self.profile[m][(daytype, (minute + d) % 1440)])
        return vals, days

    def update(self, p: Point) -> Context:
        daytype, minute = _key(p.ts)
        if self.since_scale >= SCALE_REFRESH:
            for m in METRICS:
                r = self.resid[m]
                if len(r) >= 120:
                    med = statistics.median(r)
                    self.scale[m] = max(statistics.median([abs(v - med) for v in r]) * MAD_TO_SIGMA, SCALE_FLOOR)
            self.since_scale = 0
        self.since_scale += 1
        ctx: Context = {}
        for m in METRICS:
            x = p.values[m]
            vals, days = self._pooled(m, daytype, minute)
            own = days >= MIN_DAYS
            borrowed = False
            if own:
                expected = statistics.median(vals)
            else:
                # the first weekend: borrow the weekday profile for this minute. Its level is
                # off by a roughly constant factor, so "up" stays meaningful and "down" is
                # withheld; it still knows where the scheduled jobs are.
                other, odays = self._pooled(m, 1 - daytype, minute)
                if odays >= MIN_DAYS:
                    expected, borrowed = statistics.median(other), True
                else:
                    pool = self.hourly[m][minute // 60]
                    expected = statistics.median(pool) if len(pool) >= 30 else x
            residual = transform(m, x) - transform(m, expected)
            mc = MetricContext(residual, self.scale[m], expected, own or borrowed, own)
            ctx[m] = mc
            if (not own) or abs(mc.z) < LEARN_GATE_Z:
                self.profile[m][(daytype, minute)].append(x)
                self.hourly[m][minute // 60].append(x)
                if not borrowed:
                    self.resid[m].append(residual)
        self.n += 1
        return ctx


class SeasonalRobustZ(Detector):
    name = "seasonal_z"

    def __init__(self) -> None:
        self.n = 0

    def observe(self, p: Point, ctx: Context) -> list[Signal]:
        out: list[Signal] = []
        if self.n >= self.warmup:
            for m in METRICS:
                c = ctx[m]
                z = c.z
                if z < 0 and not c.own:
                    z = 0.0
                pct = (math.exp(c.residual) - 1) * 100
                note = (f"{pct:+.0f}% vs {c.expected:.2f} expected at this minute" if c.own
                        else "vs borrowed weekday profile" if c.mature
                        else "vs hour-of-day fallback (no history for this day type yet)")
                out.append(Signal(self.name, m, abs(z), "up" if z >= 0 else "down", note, c.mature))
        self.n += 1
        return out
