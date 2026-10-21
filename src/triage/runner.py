"""Run every service through the detector ensemble and build the queue."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .ensemble import Scored, ServiceScorer, runs_from_scores
from .feedback import FeedbackStore
from .model import METRICS, Candidate, Point, parse_ts
from .queue import build_candidates


def read_points(path: Path) -> list[Point]:
    pts: list[Point] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            pts.append(Point(parse_ts(r["ts"]), r["service"], {m: float(r[m]) for m in METRICS}))
    pts.sort(key=lambda p: (p.ts, p.service))
    return pts


def read_changes(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                out.append({**r, "ts": parse_ts(r["ts"])})
    return out


def score_all(points: list[Point], store: FeedbackStore | None = None) -> dict[str, list[Scored]]:
    by_service: dict[str, list[Point]] = defaultdict(list)
    for p in points:
        by_service[p.service].append(p)
    out: dict[str, list[Scored]] = {}
    for svc, pts in sorted(by_service.items()):
        scorer = ServiceScorer(store.weights(svc) if store else None)
        out[svc] = [scorer.score(p) for p in pts]
    return out


def detect(points: list[Point], criticality: dict[str, float], store: FeedbackStore | None = None,
           threshold: float | None = None, changes: list[dict] | None = None
           ) -> tuple[list[Candidate], list[Candidate], dict[str, list[Scored]]]:
    scored = score_all(points, store)
    runs = []
    for svc, ss in scored.items():
        runs.extend(runs_from_scores(svc, ss, threshold) if threshold else runs_from_scores(svc, ss))
    queue, held = build_candidates(runs, criticality, store, changes)
    return queue, held, scored


def write_queue(path: Path, queue: list[Candidate], held: list[Candidate], threshold: float) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"threshold": threshold, "advisory": True,
                   "candidates": [c.to_dict() for c in queue],
                   "held_out": [c.to_dict() for c in held]}, f, indent=1)


def write_scores(path: Path, scored: dict[str, list[Scored]]) -> None:
    """Per-point fused scores, for the report's threshold sweep."""
    with open(path, "w", encoding="utf-8") as f:
        for svc, ss in scored.items():
            for s in ss:
                f.write(json.dumps({"ts": s.point.ts, "service": svc, "score": round(s.score, 3)},
                                   separators=(",", ":")) + "\n")
