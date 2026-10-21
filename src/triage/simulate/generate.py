"""Synthetic metrics for a small service fleet over several weeks.

Each service emits four correlated metrics per minute. Normal behaviour has
a daily shape, a weekly shape (weekends quieter), slow drift, a noise level
that differs per service, scheduled batch jobs that spike CPU on a timer,
and deploys that cause a brief, harmless wobble. Incidents are injected on
top with a label; benign events are also labeled so the evaluator can
check that the detector leaves them alone.

Everything is generated from a seed. Service names are placeholders.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..model import METRICS, STEP_S, fmt_ts

START = datetime(2026, 2, 2, tzinfo=timezone.utc).timestamp()      # a Monday
DAY = 86400

SERVICES = {
    # name: (criticality, base error %, base p95 ms, base rps, base cpu %, noise)
    "api-gateway": (1.0, 0.6, 180.0, 420.0, 45.0, 0.08),
    "auth": (1.0, 0.3, 90.0, 150.0, 30.0, 0.06),
    "checkout": (0.9, 0.8, 260.0, 60.0, 35.0, 0.12),
    "search": (0.6, 1.2, 320.0, 200.0, 55.0, 0.15),
    "ingest": (0.5, 0.5, 400.0, 900.0, 60.0, 0.10),
    "reports": (0.3, 1.5, 900.0, 8.0, 25.0, 0.18),
}


# what a tuned queue should do with each benign kind:
#   silent  - must not appear in the queue at all
#   held    - may be detected but must be held out (change-correlated / recurring), not queued
#   surge   - may be queued only as a low-severity "surge"
#   noisy   - acceptable to queue; this is the analyst-load cost the feedback loop exists for
BENIGN_EXPECTATION = {"deploy_wobble": "held", "batch_job": "held", "traffic_surge": "surge",
                      "flapping": "noisy"}


@dataclass
class Label:
    id: str
    service: str
    kind: str              # incident kinds: spike, sustained, drift, level_shift, dip
    benign: bool           # benign kinds: deploy_wobble, batch_job, traffic_surge, flapping
    start: str
    end: str
    metrics: list[str]
    description: str


def daily(frac: float) -> float:
    """Traffic multiplier by time of day: quiet at 04:00, peak at 14:00."""
    return 1.0 + 0.55 * math.sin(2 * math.pi * (frac - 0.33))


def weekly(weekday: int) -> float:
    return 0.55 if weekday >= 5 else 1.0


def generate(seed: int, out_dir: Path, days: int = 21) -> dict:
    rng = random.Random(seed)
    n = days * 1440
    out_dir.mkdir(parents=True, exist_ok=True)
    labels: list[Label] = []
    rows: list[dict] = []

    # per-service slow drift and deploy schedule
    drift = {s: rng.uniform(-0.000004, 0.000008) for s in SERVICES}     # up to ~+25% over three weeks
    deploys: dict[str, list[int]] = {s: sorted(rng.sample(range(1440 * 2, n - 1440, 60),
                                                           rng.randint(4, 8))) for s in SERVICES}
    batch = {"ingest": (0, 240), "reports": (6 * 60, 1440)}      # (first minute, period)

    # incidents: (service, kind, start_idx, length_min, magnitude)
    inc = [
        ("api-gateway", "spike", int(n * 0.165), 6, 9.0),
        ("checkout", "sustained", int(n * 0.21), 95, 4.0),
        ("auth", "level_shift", int(n * 0.33), 600, 2.2),
        ("search", "drift", int(n * 0.44), 420, 3.0),
        ("ingest", "dip", int(n * 0.52), 50, 0.25),
        ("api-gateway", "sustained", int(n * 0.63), 140, 5.5),
        ("checkout", "spike", int(n * 0.71), 4, 12.0),
        ("auth", "sustained", int(n * 0.79), 70, 3.2),
        ("search", "level_shift", int(n * 0.86), 900, 1.8),
        ("reports", "sustained", int(n * 0.92), 110, 2.5),
    ]
    benign = [
        ("api-gateway", "traffic_surge", int(n * 0.27), 90, 1.8),
        ("checkout", "traffic_surge", int(n * 0.58), 60, 1.6),
        ("search", "flapping", int(n * 0.38), 300, 1.0),
    ]
    inc_by: dict[str, list] = {s: [] for s in SERVICES}
    for k, (svc, kind, s0, length, mag) in enumerate(inc):
        inc_by[svc].append((kind, s0, s0 + length, mag, False, f"I{k + 1:02d}"))
    for k, (svc, kind, s0, length, mag) in enumerate(benign):
        inc_by[svc].append((kind, s0, s0 + length, mag, True, f"B{k + 1:02d}"))

    kind_metrics = {
        "spike": ["error_rate", "latency_p95_ms"], "sustained": ["error_rate", "latency_p95_ms"],
        "level_shift": ["latency_p95_ms"], "drift": ["latency_p95_ms", "cpu_pct"],
        "dip": ["requests_per_s"], "traffic_surge": ["requests_per_s", "cpu_pct"],
        "flapping": ["error_rate"], "deploy_wobble": ["latency_p95_ms", "error_rate"],
        "batch_job": ["cpu_pct"],
    }
    descr = {
        "spike": "short burst of errors and latency, gone in minutes",
        "sustained": "elevated errors and latency for over an hour",
        "level_shift": "latency steps up and stays there after a bad release",
        "drift": "latency and CPU creep upward over hours (leak-like)",
        "dip": "request volume collapses while upstream looks healthy",
        "traffic_surge": "legitimate traffic surge: volume and CPU up, errors flat",
        "flapping": "a noisy dependency makes error rate twitchy but bounded",
        "deploy_wobble": "a deploy causes a two-minute latency blip",
        "batch_job": "scheduled batch job: CPU spike on a timer",
    }
    for svc, items in inc_by.items():
        for kind, s0, s1, _mag, is_b, lid in items:
            labels.append(Label(lid, svc, kind, is_b, fmt_ts(START + s0 * STEP_S),
                                fmt_ts(START + s1 * STEP_S), kind_metrics[kind], descr[kind]))
    dep_id = 0
    for svc, ds in deploys.items():
        for d in ds:
            dep_id += 1
            labels.append(Label(f"D{dep_id:02d}", svc, "deploy_wobble", True,
                                fmt_ts(START + d * STEP_S), fmt_ts(START + (d + 3) * STEP_S),
                                kind_metrics["deploy_wobble"], descr["deploy_wobble"]))
    bj_id = 0
    for svc, (first, period) in batch.items():
        for tb in range(first, n, period):
            bj_id += 1
            labels.append(Label(f"J{bj_id:03d}", svc, "batch_job", True,
                                fmt_ts(START + tb * STEP_S), fmt_ts(START + (tb + 8) * STEP_S),
                                kind_metrics["batch_job"], descr["batch_job"]))

    # state for autocorrelated noise
    ar = {s: {m: 0.0 for m in METRICS} for s in SERVICES}
    flap_phase = {s: 0 for s in SERVICES}
    for i in range(n):
        t = START + i * STEP_S
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
        frac = (dt.hour * 60 + dt.minute) / 1440.0
        season = daily(frac) * weekly(dt.weekday())
        for svc, (_crit, e0, l0, r0, c0, noise) in SERVICES.items():
            # autocorrelated noise per metric (AR(1)), scaled by the service's noise level
            for m in METRICS:
                ar[svc][m] = 0.6 * ar[svc][m] + rng.gauss(0, noise)
            load = season * (1 + drift[svc] * i)
            # multiplicative (log-normal) noise: a service never has negative or zero latency
            rps = r0 * load * math.exp(ar[svc]["requests_per_s"])
            cpu = c0 * (0.6 + 0.4 * load) * math.exp(0.5 * ar[svc]["cpu_pct"])
            lat = l0 * (0.85 + 0.3 * load ** 1.5) * math.exp(ar[svc]["latency_p95_ms"])
            err = e0 * (0.8 + 0.4 * load) * math.exp(1.5 * ar[svc]["error_rate"])

            if svc in batch:
                first, period = batch[svc]
                if 0 <= (i - first) % period < 8:
                    cpu += 35.0 * rng.uniform(0.8, 1.0)
            for d in deploys[svc]:
                if d <= i < d + 3:
                    lat *= 1.6
                    err += 1.2

            for kind, s0, s1, mag, _b, _id in inc_by[svc]:
                if not s0 <= i < s1:
                    continue
                p = (i - s0) / max(1, s1 - s0)
                if kind == "spike":
                    frac = 1.0 if i - s0 < 2 else rng.uniform(0.5, 0.9)
                    err += mag * frac
                    lat *= 1 + 0.8 * frac
                elif kind == "sustained":
                    err += mag * rng.uniform(0.8, 1.15)
                    lat *= 1.5 + 0.1 * rng.random()
                elif kind == "level_shift":
                    lat *= mag
                elif kind == "drift":
                    lat *= 1 + (mag - 1) * p
                    cpu += 30 * p
                elif kind == "dip":
                    rps *= mag
                elif kind == "traffic_surge":
                    rps *= mag
                    cpu *= mag ** 0.5                   # resources follow load, sub-linearly
                    lat *= mag ** 0.2
                elif kind == "flapping":
                    flap_phase[svc] = (flap_phase[svc] + 1) % 7
                    if flap_phase[svc] < 2:
                        err += mag * rng.uniform(0.6, 1.0)
            rows.append({"ts": fmt_ts(t), "service": svc,
                         "error_rate": round(max(0.0, err), 3),
                         "latency_p95_ms": round(max(1.0, lat), 1),
                         "requests_per_s": round(max(0.0, rps), 2),
                         "cpu_pct": round(min(100.0, max(0.0, cpu)), 1)})

    with open(out_dir / "metrics.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    # the change feed: what a deploy pipeline would publish. Detection may read this.
    with open(out_dir / "changes.jsonl", "w", encoding="utf-8") as f:
        for svc, ds in deploys.items():
            for d in ds:
                f.write(json.dumps({"ts": fmt_ts(START + d * STEP_S), "service": svc,
                                    "kind": "deploy", "ref": f"rel-{d // 60:04d}"}) + "\n")
    with open(out_dir / "labels.json", "w", encoding="utf-8") as f:
        json.dump({"seed": seed, "days": days, "labels": [asdict(x) for x in labels],
                   "criticality": {s: v[0] for s, v in SERVICES.items()},
                   "benign_expectation": BENIGN_EXPECTATION}, f, indent=1)
    return {"points": len(rows), "services": len(SERVICES),
            "incidents": sum(1 for x in labels if not x.benign),
            "benign_events": sum(1 for x in labels if x.benign)}
