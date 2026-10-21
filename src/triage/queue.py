"""Turn runs into ranked, explained, advisory candidates.

Priority is not the anomaly score. A score says how unusual something is;
an analyst needs to know how much it matters. Priority combines severity
(peak, mean, duration), the service's criticality from the catalogue,
confidence (how many detectors and metrics agree), and an SLO burn-rate
estimate when error rate dominates, so a candidate spending the error
budget at 10x outranks a latency wobble.

Two kinds of candidate are held out of the queue and reported separately
rather than deleted, because an analyst should be able to see what the
system decided not to show them:

* **change-correlated**: a short run that starts within minutes of a
  recorded deploy on the same service. Deploys wobble; the change feed is
  the context that says so.
* **recurring**: a run whose service, metric, ten-minute slot of the day and
  rough duration match runs on at least three earlier days. That is a schedule, not an anomaly, and
  the seasonal profile will have learned it; this catches the first days
  before it has.

Every candidate carries ``requires_human_review: True`` and
``auto_action_taken: False``. The recommended next step is text for a
person; nothing in this package can execute it.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timezone

from .ensemble import Run, classify_shape, detector_contributions, dominant_metrics, feature_vector, run_id
from .feedback import FeedbackStore
from .model import Candidate, fmt_ts

SLO_ERROR_BUDGET_PCT = 1.0        # the service may fail 1% of requests over the window
DEPLOY_GRACE_MIN = 6              # a deploy within this many minutes of the run's start explains it
DEPLOY_MAX_DURATION_MIN = 15      # ...only if it is also short; a long one is a bad deploy
RECURRING_DAYS = 3
PLAYBOOK = {
    "spike": "Check the last deploy and upstream dependency health; if it has already recovered, "
             "annotate and close. Escalate only if it recurs within the hour.",
    "sustained": "Page the service owner. Compare error rate against the SLO burn rate; roll back "
                 "the most recent change if one shipped in the last two hours.",
    "level_shift": "Latency stepped up and stayed. Correlate with the deploy timeline; a rollback is "
                   "the usual fix. Not urgent if error rate is flat, but do not let it ride.",
    "drift": "Slow creep: likely a resource leak or growing queue. Schedule a restart or scale-out "
             "before it becomes an outage; capture heap/queue metrics first.",
    "dip": "Request volume collapsed while the service looks healthy: suspect the caller, the load "
           "balancer or DNS, not this service. Check upstream and edge first.",
    "surge": "Traffic is up and the service is keeping pace. No action; note it for capacity "
             "planning and watch for the health metrics starting to decouple.",
}


def severity(peak: float, duration_min: int, shape: str) -> str:
    if shape == "surge":
        return "low"
    if peak >= 12 or (peak >= 7 and duration_min >= 60):
        return "critical"
    if peak >= 7 or duration_min >= 60:
        return "high"
    if peak >= 5:
        return "medium"
    return "low"


def burn_rate(run: Run) -> float | None:
    errs = [p.point.values["error_rate"] for p in run.points]
    if not errs or "error_rate" not in dominant_metrics(run):
        return None
    return round(statistics.fmean(errs) / SLO_ERROR_BUDGET_PCT, 1)


def confidence(run: Run) -> float:
    dets = detector_contributions(run)
    mets = dominant_metrics(run)
    agree = sum(1 for v in dets.values() if v >= 0.15)
    return round(min(0.4 + 0.15 * min(agree, 3) + 0.1 * min(len(mets), 2), 0.99), 2)


def _slot10(ts: float) -> int:
    """Ten-minute slot of the day: a schedule repeats at the same minute, not just the same hour."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (dt.hour * 60 + dt.minute) // 10


def _dur_bucket(minutes: int) -> int:
    """Durations within a factor of two land in the same bucket."""
    return max(minutes, 1).bit_length()


def _day(ts: float) -> int:
    return int(ts // 86400)


def build_candidates(runs: list[Run], criticality: dict[str, float], store: FeedbackStore | None = None,
                     changes: list[dict] | None = None) -> tuple[list[Candidate], list[Candidate]]:
    """Returns (queue, held_out)."""
    changes = changes or []
    by_svc_changes: dict[str, list[float]] = defaultdict(list)
    for c in changes:
        by_svc_changes[c["service"]].append(c["ts"])

    # recurrence: (service, metric, hour) -> set of days seen, built in time order
    seen_days: dict[tuple[str, str, int, int], set[int]] = defaultdict(set)
    queue: list[Candidate] = []
    held: list[Candidate] = []
    for r in sorted(runs, key=lambda x: x.start):
        scores = [p.score for p in r.points]
        peak, mean = max(scores), statistics.fmean(scores)
        dur = max(1, int(round((r.end - r.start) / 60)) + 1)
        shape = classify_shape(r)
        mets = dominant_metrics(r)
        dets = detector_contributions(r)
        sev = severity(peak, dur, shape)
        conf = confidence(r)
        br = burn_rate(r)
        feats = feature_vector(r)
        crit = criticality.get(r.service, 0.5)
        base = {"critical": 1.0, "high": 0.75, "medium": 0.5, "low": 0.3}[sev]
        prio = base * (0.5 + 0.5 * crit) * (0.6 + 0.4 * conf)
        if br is not None and br > 1:
            prio *= min(1.5, 1 + 0.05 * br)
        similar = store.similar(r.service, feats) if store else []
        if any(s["verdict"] == "noise" and s["similarity"] >= 0.9 for s in similar):
            prio *= 0.6

        reason = None
        if dur <= DEPLOY_MAX_DURATION_MIN and any(
                -DEPLOY_GRACE_MIN <= (t - r.start) / 60 <= DEPLOY_GRACE_MIN
                for t in by_svc_changes.get(r.service, [])):
            reason = "change-correlated: starts within minutes of a recorded deploy and is short"
        else:
            key = (r.service, mets[0] if mets else "-", _slot10(r.start), _dur_bucket(dur))
            if len(seen_days[key]) >= RECURRING_DAYS and _day(r.start) not in seen_days[key]:
                reason = (f"recurring: {key[1]} on {r.service} around {key[2] // 6:02d}:{(key[2] % 6) * 10:02d} "
                          f"for ~{dur} min on {len(seen_days[key])} earlier days")
            seen_days[key].add(_day(r.start))

        explanation = (f"{shape} on {r.service}: {', '.join(mets) or 'mixed metrics'} out of range for "
                       f"{dur} min (peak score {peak:.1f}); flagged by "
                       + ", ".join(f"{d} {int(v * 100)}%" for d, v in dets.items()))
        cand = Candidate(run_id(r), r.service, fmt_ts(r.start), fmt_ts(r.end), dur, round(peak, 2),
                         round(mean, 2), shape, mets, list(dets), dets, sev, round(prio, 3), conf, br,
                         PLAYBOOK[shape], similar, True, False, explanation,
                         [round(x, 4) for x in feats], reason)
        (held if reason else queue).append(cand)
    queue.sort(key=lambda c: (-c.priority, c.start))
    return queue, held
