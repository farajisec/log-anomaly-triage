"""Fuse detector signals into one per-point anomaly score, then into runs.

Fusion, per metric: take the strongest detector, but let the seasonal
detector veto. If the (day-type, hour) profile is mature and says this
value is ordinary for this hour, the other detectors' scores on that
metric are cut, because they have no notion of "normal for 03:00". That
single rule is what stops a nightly batch job and the morning traffic
ramp from being reported every day by the EWMA and CUSUM detectors.

Per point: metrics combine with a soft maximum, so two metrics agreeing
score higher than one alone while one screaming metric is still enough.
Detector weights come from analyst calibration (see ``feedback.py``).

A run opens after ``MIN_RUN`` consecutive points above threshold and
closes after ``COOLDOWN`` points below it, so a flapping metric produces
one candidate, not thirty. Shape is classified from duration, direction
and which metrics dominate: blip, outage, slow leak, step, dip, or surge.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from .detectors import DETECTORS, Detector
from .detectors.seasonal import SeasonalModel
from .model import METRICS, Point, Signal, fmt_ts

THRESHOLD = 4.0
AGREEMENT_FLOOR = 2.5
MIN_RUN = 3
MIN_RUN_VOLUME_ONLY = 10      # a volume-only excursion must last ten minutes to be worth a human
COOLDOWN = 5
MAX_GAP_FOR_MERGE_MIN = 20
SEASONAL_VETO_BELOW = 2.0      # seasonal z under this on a mature slot means "ordinary for this hour"
VETO_FACTOR = 0.35
HEALTH_DOWN_FACTOR = 0.5
LOAD_EXPLAINED_FLOOR = 1.5     # any clear volume elevation lets load-decoupling vouch for the health metrics
LOAD_EXPLAINED_FACTOR = 0.25


@dataclass
class Scored:
    point: Point
    score: float
    per_metric: dict[str, float]
    best_detector: dict[str, str]
    direction: dict[str, str]
    signals: list[Signal] = field(default_factory=list)
    seasonal: dict[str, float] = field(default_factory=dict)   # raw seasonal z per metric, for shape


def fuse(values: list[float]) -> float:
    """Strongest metric, plus a bonus for a second metric that is notable on its own.

    A soft-max over all metrics was tried first and rejected: four metrics
    idling at 2.5 summed to 3.2, which turned ordinary noise into runs.
    Agreement should count only when each voice would be heard alone.
    """
    if not values:
        return 0.0
    top = sorted(values, reverse=True)
    bonus = 0.5 * max(0.0, top[1] - AGREEMENT_FLOOR) if len(top) > 1 else 0.0
    return top[0] + min(bonus, 1.0)


class ServiceScorer:
    def __init__(self, weights: dict[str, float] | None = None):
        self.model = SeasonalModel()
        self.detectors: list[Detector] = [cls() for cls in DETECTORS]
        self.weights = {d.name: 1.0 for d in self.detectors}
        if weights:
            self.weights.update(weights)

    def score(self, p: Point) -> Scored:
        ctx = self.model.update(p)
        signals: list[Signal] = []
        for d in self.detectors:
            signals.extend(d.observe(p, ctx))
        seasonal = {s.metric: s for s in signals if s.detector == "seasonal_z"}
        load = {s.metric: s for s in signals if s.detector == "load_decoupling"}
        volume = seasonal.get("requests_per_s")
        volume_up = volume is not None and volume.direction == "up" and volume.score >= LOAD_EXPLAINED_FLOOR
        per_metric: dict[str, float] = {}
        best: dict[str, str] = {}
        direction: dict[str, str] = {}
        for s in signals:
            w = s.score * self.weights.get(s.detector, 1.0)
            sz = seasonal.get(s.metric)
            if s.detector != "seasonal_z" and sz is not None and sz.mature and sz.score < SEASONAL_VETO_BELOW:
                w *= VETO_FACTOR
            if s.direction == "down" and s.metric != "requests_per_s":
                w *= HEALTH_DOWN_FACTOR          # latency or errors falling is not what pages people
            ld = load.get(s.metric)
            if volume_up and ld is not None and ld.score < SEASONAL_VETO_BELOW and s.detector != "load_decoupling":
                w *= LOAD_EXPLAINED_FACTOR       # health is up because traffic is up: a surge, not a fault
            if w > per_metric.get(s.metric, -1):
                per_metric[s.metric] = w
                best[s.metric] = s.detector
                direction[s.metric] = s.direction
        total = fuse(list(per_metric.values()))
        return Scored(p, total, per_metric, best, direction, signals,
                      {m: s.score for m, s in seasonal.items()})


@dataclass
class Run:
    service: str
    points: list[Scored]

    @property
    def start(self) -> float:
        return self.points[0].point.ts

    @property
    def end(self) -> float:
        return self.points[-1].point.ts


def runs_from_scores(service: str, scored: list[Scored], threshold: float = THRESHOLD) -> list[Run]:
    runs: list[Run] = []
    cur: list[Scored] = []
    below = 0
    for s in scored:
        if s.score >= threshold:
            cur.append(s)
            below = 0
        elif cur:
            below += 1
            cur.append(s)
            if below >= COOLDOWN:
                core = cur[:-below]
                if len(core) >= MIN_RUN:
                    runs.append(Run(service, core))
                cur, below = [], 0
    if cur:
        core = cur[:-below] if below else cur
        if len(core) >= MIN_RUN:
            runs.append(Run(service, core))
    merged: list[Run] = []
    for r in runs:
        if merged and (r.start - merged[-1].end) / 60 <= MAX_GAP_FOR_MERGE_MIN:
            merged[-1].points.extend(r.points)
        else:
            merged.append(r)
    return [r for r in merged
            if dominant_metrics(r, threshold) != ["requests_per_s"] or len(r.points) >= MIN_RUN_VOLUME_ONLY]


def dominant_metrics(run: Run, threshold: float = THRESHOLD) -> list[str]:
    tot: dict[str, float] = defaultdict(float)
    for p in run.points:
        for m, v in p.per_metric.items():
            if v >= threshold * 0.7:
                tot[m] += v
    ranked = sorted(tot.items(), key=lambda kv: -kv[1])
    return [m for m, _ in ranked[:3]]


def _direction_of(run: Run, metric: str) -> str:
    ups = sum(1 for p in run.points if p.direction.get(metric) == "up")
    return "up" if ups >= len(run.points) / 2 else "down"


def classify_shape(run: Run) -> str:
    n = len(run.points)
    metrics = dominant_metrics(run)
    health = [m for m in metrics if m != "requests_per_s"]
    if metrics and metrics[0] == "requests_per_s":
        if _direction_of(run, "requests_per_s") == "down":
            return "dip"
        if not health:
            return "surge"                       # volume up, health keeping pace: not an incident
    if n <= 10:
        return "spike"
    # trend of the seasonal departure itself (not the fused score, which saw-tooths when CUSUM resets)
    lead = metrics[0] if metrics else "latency_p95_ms"
    series = [p.seasonal.get(lead, 0.0) for p in run.points]
    q = max(3, n // 4)
    head, tail = statistics.fmean(series[:q]), statistics.fmean(series[-q:])
    if n >= 120 and tail >= head * 1.5 and tail - head >= 1.0:
        return "drift"
    if n >= 240:
        return "level_shift"
    return "sustained"


def detector_contributions(run: Run, threshold: float = THRESHOLD) -> dict[str, float]:
    tot: dict[str, float] = defaultdict(float)
    for p in run.points:
        for m, d in p.best_detector.items():
            if p.per_metric[m] >= threshold * 0.7:
                tot[d] += p.per_metric[m]
    s = sum(tot.values()) or 1.0
    return {d: round(v / s, 2) for d, v in sorted(tot.items(), key=lambda kv: -kv[1])}


def run_id(run: Run) -> str:
    raw = f"{run.service}|{fmt_ts(run.start)}|{fmt_ts(run.end)}"
    return "cand-" + hashlib.sha1(raw.encode()).hexdigest()[:10]


def feature_vector(run: Run) -> list[float]:
    """Compact description of a run for similarity search against past verdicts."""
    n = len(run.points)
    per_m = {m: statistics.fmean(p.per_metric.get(m, 0.0) for p in run.points) for m in METRICS}
    peak = max(p.score for p in run.points)
    return [math.log1p(n), peak / 10.0] + [min(per_m[m] / 10.0, 1.0) for m in METRICS]
