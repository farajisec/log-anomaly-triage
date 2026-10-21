"""Shared types."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

METRICS = ("error_rate", "latency_p95_ms", "requests_per_s", "cpu_pct")
STEP_S = 60


def parse_ts(text: str) -> float:
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def fmt_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Point:
    ts: float
    service: str
    values: dict[str, float]


@dataclass
class Signal:
    """One detector's view of one point."""
    detector: str
    metric: str
    score: float                  # standardised: ~0 normal, >1 suspicious, >3 clear
    direction: str                # "up" | "down"
    note: str = ""
    mature: bool = True           # False while the baseline behind the score is still thin


@dataclass
class Candidate:
    """A contiguous run of anomalous points on one service: what the queue ranks."""
    candidate_id: str
    service: str
    start: str
    end: str
    duration_min: int
    peak_score: float
    mean_score: float
    shape: str                    # spike | sustained | drift | level_shift | dip
    metrics: list[str]
    detectors: list[str]
    contributions: dict[str, float]
    severity: str
    priority: float
    confidence: float
    slo_burn_rate: float | None
    recommended_next_step: str
    similar_past: list[dict] = field(default_factory=list)
    requires_human_review: bool = True
    auto_action_taken: bool = False
    explanation: str = ""
    features: list[float] = field(default_factory=list)
    suppressed_reason: str | None = None   # set when the candidate is held out of the queue

    def to_dict(self) -> dict:
        return asdict(self)
