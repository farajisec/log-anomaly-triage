import math
import random

from conftest import point
from hypothesis import given, settings
from hypothesis import strategies as st

from triage.detectors.base import WARMUP_POINTS
from triage.detectors.cusum import Cusum
from triage.detectors.ewma import EwmaResidual
from triage.detectors.multivariate import LoadDecoupling
from triage.detectors.seasonal import MIN_DAYS, SeasonalModel, SeasonalRobustZ, transform

DAY = 1440


def daily_series(days: int, rng: random.Random, noise: float = 0.05, batch_at: int | None = None):
    """A flat-ish service with a daily shape, lognormal noise, and an optional 8-minute CPU job."""
    for i in range(days * DAY):
        m = i % DAY
        shape = 1 + 0.5 * math.sin(2 * math.pi * (m / DAY - 0.3))
        cpu = 40 * shape
        if batch_at is not None and batch_at <= m < batch_at + 8:
            cpu += 35
        yield i, point(i, requests_per_s=100 * shape * math.exp(rng.gauss(0, noise)),
                       latency_p95_ms=200 * (0.8 + 0.3 * shape) * math.exp(rng.gauss(0, noise)),
                       error_rate=0.5 * math.exp(rng.gauss(0, noise)), cpu_pct=cpu * math.exp(rng.gauss(0, noise / 2)))


def test_log_residual_is_level_free():
    assert abs((transform("requests_per_s", 200) - transform("requests_per_s", 100))
               - (transform("requests_per_s", 2000) - transform("requests_per_s", 1000))) < 0.06


def test_seasonal_model_learns_a_scheduled_job():
    rng = random.Random(1)
    model, det = SeasonalModel(), SeasonalRobustZ()
    scores = {}
    for i, p in daily_series(7, rng, batch_at=240):
        ctx = model.update(p)
        for s in det.observe(p, ctx):
            if s.metric == "cpu_pct" and 240 <= i % DAY < 248:
                scores.setdefault(i // DAY, []).append(s.score)
    # after MIN_DAYS of weekday history the job is in the profile and scores as ordinary
    late = [v for d, vs in scores.items() if d >= MIN_DAYS + 1 for v in vs]
    assert late and max(late) < 3.0, max(late)


def test_seasonal_model_withholds_down_until_own_history_exists():
    rng = random.Random(2)
    model, det = SeasonalModel(), SeasonalRobustZ()
    downs = []
    for i, p in daily_series(7, rng):
        if 5 * DAY <= i < 7 * DAY:                        # first weekend: borrowed profile
            p.values["requests_per_s"] *= 0.55
        ctx = model.update(p)
        for s in det.observe(p, ctx):
            if s.metric == "requests_per_s" and i >= 5 * DAY and s.direction == "down":
                downs.append(s.score)
    assert not downs or max(downs) == 0.0


def test_ewma_flags_a_spike_within_a_minute():
    rng = random.Random(3)
    model, ewma, cusum = SeasonalModel(), EwmaResidual(), Cusum()
    first_ewma = first_cusum = None
    spike_at = WARMUP_POINTS + 600
    for i, p in daily_series(5, rng):
        if spike_at <= i < spike_at + 4:
            p.values["error_rate"] *= 8
        ctx = model.update(p)
        for s in ewma.observe(p, ctx):
            if s.metric == "error_rate" and s.score >= 4 and first_ewma is None and i >= spike_at:
                first_ewma = i - spike_at
        for s in cusum.observe(p, ctx):
            if s.metric == "error_rate" and s.score >= 4 and first_cusum is None and i >= spike_at:
                first_cusum = i - spike_at
    assert first_ewma is not None and first_ewma <= 1
    # CUSUM may also cross on a spike this large (one point carries ~40 sigma), but never earlier
    assert first_cusum is None or first_cusum >= first_ewma


def test_cusum_catches_a_level_shift_the_ewma_cannot_hold():
    rng = random.Random(4)
    model, cusum = SeasonalModel(), Cusum()
    shift_at = WARMUP_POINTS + 300
    fired = []
    for i, p in daily_series(5, rng):
        if i >= shift_at:
            p.values["latency_p95_ms"] *= 1.5          # ~2.4 sigma at 5 % noise, persistent
        ctx = model.update(p)
        for s in cusum.observe(p, ctx):
            if s.metric == "latency_p95_ms" and s.score >= 5 and i >= shift_at:
                fired.append(i - shift_at)
    assert fired and fired[0] <= 60, fired[:3]


def test_cusum_ignores_slow_trend():
    rng = random.Random(5)
    model, cusum = SeasonalModel(), Cusum()
    fired = 0
    for i, p in daily_series(6, rng):
        p.values["latency_p95_ms"] *= 1 + 0.000004 * i          # +3.5 % over six days
        ctx = model.update(p)
        fired += sum(1 for s in cusum.observe(p, ctx) if s.metric == "latency_p95_ms" and s.score >= 5)
    assert fired == 0


def test_load_decoupling_reports_only_health_above_load():
    rng = random.Random(6)
    model, ld = SeasonalModel(), LoadDecoupling()
    up = []
    at = WARMUP_POINTS + 800
    for i, p in daily_series(5, rng):
        if at <= i < at + 30:
            p.values["latency_p95_ms"] *= 2.0            # latency up, traffic flat: decoupled
        ctx = model.update(p)
        for s in ld.observe(p, ctx):
            assert s.direction == "up"
            if s.metric == "latency_p95_ms" and at <= i < at + 30:
                up.append(s.score)
    assert up and max(up) >= 4


def test_load_decoupling_stays_quiet_on_a_surge():
    rng = random.Random(7)
    model, ld = SeasonalModel(), LoadDecoupling()
    scores = []
    at = WARMUP_POINTS + 800
    for i, p in daily_series(5, rng):
        if at <= i < at + 60:                             # traffic up, everything scales with it
            p.values["requests_per_s"] *= 1.8
            p.values["cpu_pct"] *= 1.8 ** 0.5
            p.values["latency_p95_ms"] *= 1.8 ** 0.2
        ctx = model.update(p)
        for s in ld.observe(p, ctx):
            if at <= i < at + 60:
                scores.append(s.score)
    assert scores and max(scores) < 4


@given(st.floats(min_value=0.0, max_value=1e6), st.floats(min_value=0.01, max_value=1e6))
@settings(max_examples=100)
def test_transform_is_monotonic_and_finite(a, b):
    lo, hi = sorted((a, b))
    assert transform("latency_p95_ms", lo) <= transform("latency_p95_ms", hi)
    assert math.isfinite(transform("error_rate", a))
