"""Unsupervised anomaly detector for per-service metric series.

Two transparent, explainable signals, combined by taking the stronger:

1. Robust rolling z-score. Over a trailing window, the median and the
   median absolute deviation (MAD) give a center and spread that are not
   dragged around by the very outliers we are trying to catch. The score
   is the one-sided robust z of the current point above that center.

2. EWMA residual. An exponentially weighted moving average tracks the
   expected level; the current point's residual above it, scaled by a
   running estimate of residual size, is the second score.

The detector is one-sided (it flags elevations, not dips), debounced to
require a short run before opening an event, and fully deterministic. It
emits candidate events for a human to triage. It never takes an action.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime

WINDOW = 120            # trailing points for the robust baseline
THRESHOLD = 4.0         # anomaly score to flag a point
MIN_RUN = 2             # consecutive flagged points to open an event
WARMUP = 30             # points to learn a stable baseline before flagging
EWMA_ALPHA = 0.1
_MAD_TO_SIGMA = 1.4826  # scale MAD to a std-dev-equivalent


@dataclass
class Event:
    service: str
    start: str
    end: str
    peak_score: float
    length: int
    points: list = field(default_factory=list)


def _robust_z(window: list[float], x: float) -> float:
    if len(window) < 10:
        return 0.0
    med = statistics.median(window)
    mad = statistics.median([abs(v - med) for v in window]) or 1e-9
    return (x - med) / (mad * _MAD_TO_SIGMA)


def score_series(points: list[dict]) -> list[tuple[dict, float]]:
    """Return (point, anomaly_score) for one service, time-ordered."""
    pts = sorted(points, key=lambda p: p["ts"])
    window: list[float] = []
    ewma = None
    ewma_abs = None
    scored = []
    for idx, p in enumerate(pts):
        x = p["error_rate"]
        if idx < WARMUP:
            # still learning the baseline; cannot score reliably yet
            score = 0.0
        else:
            rz = _robust_z(window, x)
            resid = x - ewma
            scale = (ewma_abs or 1e-9) * _MAD_TO_SIGMA
            resid_z = resid / scale if scale else 0.0
            score = max(rz, resid_z)      # one-sided: only elevations
        scored.append((p, max(0.0, score)))
        # update baselines AFTER scoring (so a point never explains itself)
        window.append(x)
        if len(window) > WINDOW:
            window.pop(0)
        if ewma is None:
            ewma, ewma_abs = x, 0.0
        else:
            prev = ewma
            ewma = EWMA_ALPHA * x + (1 - EWMA_ALPHA) * ewma
            ewma_abs = EWMA_ALPHA * abs(x - prev) + (1 - EWMA_ALPHA) * ewma_abs
    return scored


def events_from_scores(service: str,
                       scored: list[tuple[dict, float]]) -> list[Event]:
    events, run = [], []
    for p, s in scored:
        if s >= THRESHOLD:
            run.append((p, s))
        else:
            if len(run) >= MIN_RUN:
                events.append(_make_event(service, run))
            run = []
    if len(run) >= MIN_RUN:
        events.append(_make_event(service, run))
    return events


def _make_event(service: str, run: list[tuple[dict, float]]) -> Event:
    return Event(
        service=service,
        start=run[0][0]["ts"],
        end=run[-1][0]["ts"],
        peak_score=round(max(s for _, s in run), 2),
        length=len(run),
        points=[p["ts"] for p, _ in run],
    )


def detect(points: list[dict]) -> list[Event]:
    by_service: dict[str, list[dict]] = {}
    for p in points:
        by_service.setdefault(p["service"], []).append(p)
    events = []
    for svc, pts in sorted(by_service.items()):
        events.extend(events_from_scores(svc, score_series(pts)))
    events.sort(key=lambda e: (e.service, e.start))
    return events
