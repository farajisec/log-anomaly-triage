"""Score the queue against labeled windows, and against a naive baseline.

A queued candidate is a true positive if it overlaps a labeled incident
window on its service; an incident with no overlapping queued candidate is
a false negative. A queued candidate that overlaps nothing is a false
positive. A queued candidate that overlaps a benign event is judged by
what the label says a tuned queue should do with that kind of event:

    silent   it should not have been queued            -> false positive
    held     it should have been held out of the queue -> false positive
    surge    it may be queued only as a low "surge"    -> FP unless it is
    noisy    queueing it is the accepted analyst cost  -> benign true positive

Held-out candidates are reported, not scored: they are what the system
decided not to show the analyst, and the report lists them so that
decision can be audited.

Detection latency is minutes from the incident's labeled start to the
candidate's start (earlier counts as zero).

Two extra views keep the headline honest: a threshold sweep of the fused
score gives precision and recall at every operating point, so the chosen
threshold is visibly a choice; and a naive baseline (trailing-window
z-score, the thing most teams start with) is scored the same way, so the
value of the seasonal, multivariate, change-aware ensemble is measured
rather than asserted.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ensemble import MIN_RUN
from .model import METRICS, Candidate, Point, parse_ts

NAIVE_WINDOW = 120
NAIVE_Z = 4.0


@dataclass
class IncidentResult:
    id: str
    service: str
    kind: str
    detected: bool
    latency_min: float | None
    candidate_id: str | None
    shape_reported: str | None
    shape_correct: bool | None
    severity: str | None
    rank: int | None


@dataclass
class Report:
    seed: int
    points: int
    queued: int
    held_out: int
    incidents: list[IncidentResult]
    benign_events: dict[str, dict]
    overall: dict
    by_kind: dict[str, dict]
    false_positives: list[dict]
    held_out_summary: dict[str, int]
    sweep: list[dict]
    naive: dict
    queue_quality: dict
    noise: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 <= b1 and b0 <= a1


def _match(cands: list[tuple[str, float, float, dict]], labels: list[dict], expectation: dict[str, str]):
    inc = [dict(x, s=parse_ts(x["start"]), e=parse_ts(x["end"]), hit=None, lat=None, rank=None)
           for x in labels if not x["benign"]]
    ben = [dict(x, s=parse_ts(x["start"]), e=parse_ts(x["end"]), hits=0, bad=0)
           for x in labels if x["benign"]]
    fps: list[dict] = []
    tp = benign_tp = 0
    for rank, (cid, cs, ce, meta) in enumerate(cands, 1):
        matched = False
        for w in inc:
            if w["service"] == meta["service"] and _overlap(cs, ce, w["s"], w["e"]):
                matched = True
                if w["hit"] is None:
                    w["hit"], w["lat"], w["meta"], w["rank"] = cid, max(0.0, (cs - w["s"]) / 60), meta, rank
        if matched:
            tp += 1
            continue
        hit_benign = [b for b in ben if b["service"] == meta["service"] and _overlap(cs, ce, b["s"], b["e"])]
        if hit_benign:
            ok = False
            for b in hit_benign:
                b["hits"] += 1
                want = expectation.get(b["kind"], "silent")
                if want == "noisy" or (want == "surge" and meta.get("shape") == "surge"):
                    ok = True
                else:
                    b["bad"] += 1
            if ok:
                benign_tp += 1
                continue
        fps.append({"candidate_id": cid, "rank": rank, **meta})
    fn = sum(1 for w in inc if w["hit"] is None)
    fp = len(fps)
    prec = tp / (tp + fp) if tp + fp else None
    rec = (len(inc) - fn) / len(inc) if inc else None
    return inc, ben, fps, {"tp": tp, "fp": fp, "fn": fn, "benign_tp": benign_tp, "precision": prec,
                           "recall": rec, "f1": (2 * prec * rec / (prec + rec)) if prec and rec else None}


def naive_candidates(points: list[Point]) -> list[tuple[str, float, float, dict]]:
    """Trailing-window z-score per metric: no seasonality, no fusion, no change feed."""
    by_s: dict[str, list[Point]] = defaultdict(list)
    for p in points:
        by_s[p.service].append(p)
    out = []
    for svc, pts in by_s.items():
        win: dict[str, deque] = {m: deque(maxlen=NAIVE_WINDOW) for m in METRICS}
        run: list[Point] = []
        for p in pts:
            flagged = False
            for m in METRICS:
                w = win[m]
                if len(w) >= 30:
                    mu, sd = statistics.fmean(w), statistics.pstdev(w) or 1e-9
                    if abs(p.values[m] - mu) / sd >= NAIVE_Z:
                        flagged = True
                w.append(p.values[m])
            if flagged:
                run.append(p)
            elif run:
                if len(run) >= MIN_RUN:
                    out.append((f"naive-{svc}-{int(run[0].ts)}", run[0].ts, run[-1].ts,
                                {"service": svc, "shape": "?"}))
                run = []
    return out


def evaluate(queue: list[Candidate], held: list[Candidate], labels_doc: dict, points: list[Point],
             scores_path: Path | None = None) -> Report:
    labels = labels_doc["labels"]
    expectation = labels_doc.get("benign_expectation", {})
    rows = [(c.candidate_id, parse_ts(c.start), parse_ts(c.end),
             {"service": c.service, "shape": c.shape, "severity": c.severity, "priority": c.priority,
              "metrics": c.metrics, "start": c.start, "end": c.end}) for c in queue]
    inc, ben, fps, overall = _match(rows, labels, expectation)

    results = []
    for w in inc:
        meta = w.get("meta") or {}
        results.append(IncidentResult(w["id"], w["service"], w["kind"], w["hit"] is not None,
                                      round(w["lat"], 1) if w["lat"] is not None else None, w["hit"],
                                      meta.get("shape"), (meta.get("shape") == w["kind"]) if meta else None,
                                      meta.get("severity"), w["rank"]))
    by_kind: dict[str, dict] = {}
    for r in results:
        d = by_kind.setdefault(r.kind, {"incidents": 0, "detected": 0, "latencies": []})
        d["incidents"] += 1
        d["detected"] += int(r.detected)
        if r.latency_min is not None:
            d["latencies"].append(r.latency_min)
    for d in by_kind.values():
        d["median_latency_min"] = statistics.median(d["latencies"]) if d["latencies"] else None
        del d["latencies"]

    # what happened to each benign kind, including held-out candidates
    held_rows = [(c.candidate_id, parse_ts(c.start), parse_ts(c.end), {"service": c.service}) for c in held]
    benign_summary: dict[str, dict] = {}
    for b in ben:
        d = benign_summary.setdefault(b["kind"], {"events": 0, "queued": 0, "queued_wrongly": 0, "held_out": 0,
                                                 "expectation": expectation.get(b["kind"], "silent")})
        d["events"] += 1
        d["queued"] += int(b["hits"] > 0)
        d["queued_wrongly"] += int(b["bad"] > 0)
        d["held_out"] += int(any(b["service"] == m["service"] and _overlap(cs, ce, b["s"], b["e"])
                                 for _, cs, ce, m in held_rows))
    held_summary: dict[str, int] = defaultdict(int)
    for c in held:
        held_summary[(c.suppressed_reason or "").split(":")[0]] += 1

    lats = [r.latency_min for r in results if r.latency_min is not None]
    detected = [r for r in results if r.detected]
    overall.update({
        "median_latency_min": statistics.median(lats) if lats else None,
        "shape_accuracy": round(sum(1 for r in detected if r.shape_correct) / len(detected), 3) if detected else None,
    })

    sweep = []
    if scores_path and scores_path.exists():
        per_s: dict[str, list[dict]] = defaultdict(list)
        with open(scores_path, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                per_s[rec["service"]].append(rec)
        for thr in (2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 8.0):
            srows = []
            for svc, rs in per_s.items():
                run: list[dict] = []
                for rec in rs:
                    if rec["score"] >= thr:
                        run.append(rec)
                    elif run:
                        if len(run) >= MIN_RUN:
                            srows.append((f"s{thr}-{svc}-{int(run[0]['ts'])}", run[0]["ts"], run[-1]["ts"],
                                          {"service": svc, "shape": "?"}))
                        run = []
            _, _, _, o = _match(srows, labels, {k: "silent" for k in expectation})
            sweep.append({"threshold": thr, "candidates": len(srows),
                          **{k: o[k] for k in ("precision", "recall", "f1")}})

    _, _, _, naive = _match(naive_candidates(points), labels, {k: "silent" for k in expectation})
    naive["candidates"] = naive["tp"] + naive["fp"]

    fp_ids = {f["candidate_id"] for f in fps}
    inc_ids = {r.candidate_id for r in results if r.candidate_id}
    queue_quality = {
        "top5_true_incidents": sum(1 for c in queue[:5] if c.candidate_id in inc_ids),
        "first_false_positive_rank": next((i + 1 for i, c in enumerate(queue) if c.candidate_id in fp_ids), None),
        "critical_services_in_top3": sum(1 for c in queue[:3] if labels_doc["criticality"].get(c.service, 0) >= 0.9),
        "median_incident_rank": statistics.median([r.rank for r in results if r.rank]) if detected else None,
    }
    days = labels_doc.get("days", 21)
    noise = {"candidates_per_day": round(len(queue) / days, 2),
             "advisory_only": all(c.requires_human_review and not c.auto_action_taken for c in queue + held)}
    return Report(labels_doc["seed"], len(points), len(queue), len(held), results, benign_summary, overall,
                  by_kind, fps, dict(held_summary), sweep, naive, queue_quality, noise)


def load_labels(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
