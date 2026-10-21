from datetime import datetime, timedelta, timezone

import pytest

from triage.model import METRICS, Point

T0 = datetime(2026, 2, 2, tzinfo=timezone.utc).timestamp()     # a Monday


def point(i: int, service: str = "svc", **over) -> Point:
    vals = {"error_rate": 0.5, "latency_p95_ms": 200.0, "requests_per_s": 100.0, "cpu_pct": 40.0}
    vals.update(over)
    return Point(T0 + i * 60, service, {m: float(vals[m]) for m in METRICS})


@pytest.fixture
def mk_point():
    return point


def minute_of(ts: float) -> int:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.hour * 60 + dt.minute


__all__ = ["T0", "point", "minute_of", "timedelta"]
