#!/usr/bin/env python3
"""Synthetic service-metric generator for the log-anomaly triage lab.

Emits a per-minute error-rate series for several generic services with a
daily seasonal shape plus noise, then injects a set of labeled anomalies:
short spikes and sustained elevations ("outages"). Ground-truth anomaly
windows are written to a separate file and never enter the detector input.

Synthetic throughout: the service names are generic placeholders.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timedelta, timezone

SERVICES = ["api", "auth", "ingest", "render"]
STEP = timedelta(minutes=1)


def seasonal(minute_of_day: int) -> float:
    # a gentle daily bump: busier mid-day, quieter overnight
    frac = minute_of_day / 1440.0
    return 1.0 + 0.6 * math.sin(2 * math.pi * (frac - 0.25))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--out", default="data/metrics.jsonl")
    ap.add_argument("--labels-out", default="data/labels.json")
    args = ap.parse_args()

    r = random.Random(args.seed)
    start = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
    n = args.days * 1440

    # baseline error-rate per service (percent)
    base = {s: r.uniform(0.4, 1.0) for s in SERVICES}

    # schedule labeled anomalies (service, type, start_idx, length, mag)
    anomalies = [
        ("api", "spike", int(n * 0.20), 4, 14.0),
        ("auth", "outage", int(n * 0.35), 70, 6.0),
        ("ingest", "spike", int(n * 0.55), 3, 20.0),
        ("render", "outage", int(n * 0.72), 45, 5.0),
        ("api", "spike", int(n * 0.88), 5, 12.0),
    ]
    anom_by_service: dict[str, list] = {s: [] for s in SERVICES}
    for svc, typ, s0, length, mag in anomalies:
        anom_by_service[svc].append((typ, s0, s0 + length, mag))

    rows, labels = [], []
    for svc in SERVICES:
        for typ, s0, s1, mag in anom_by_service[svc]:
            labels.append({
                "service": svc, "type": typ,
                "start": (start + s0 * STEP).isoformat(),
                "end": (start + s1 * STEP).isoformat(),
            })

    for i in range(n):
        t = start + i * STEP
        mod = (t.hour * 60 + t.minute)
        for svc in SERVICES:
            val = base[svc] * seasonal(mod) + r.gauss(0, 0.15)
            for typ, s0, s1, mag in anom_by_service[svc]:
                if s0 <= i < s1:
                    if typ == "spike":
                        val += mag * (1.0 if i == s0 else r.uniform(0.6, 1.0))
                    else:  # sustained outage-style elevation
                        val += mag * r.uniform(0.8, 1.1)
            rows.append({"ts": t.isoformat(), "service": svc,
                         "error_rate": round(max(0.0, val), 3)})

    rows.sort(key=lambda x: (x["ts"], x["service"]))
    with open(args.out, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    with open(args.labels_out, "w") as f:
        json.dump({"anomalies": labels, "point_count": len(rows)}, f, indent=2)

    print(f"wrote {len(rows)} points ({len(SERVICES)} services x {n} min) "
          f"-> {args.out}")
    print(f"wrote {len(labels)} labeled anomalies -> {args.labels_out}")


if __name__ == "__main__":
    main()
