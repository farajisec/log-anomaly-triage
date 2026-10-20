#!/usr/bin/env python3
"""Run the anomaly detector and emit a human triage queue.

Output is an advisory queue: ranked candidate anomalies, each flagged as
requiring human review. Nothing here acts on the findings. A recommended
next step is attached for the analyst, but it is a suggestion, not an
action, and the pipeline has no capability to execute it.
"""
from __future__ import annotations
import argparse, json, sqlite3
from datetime import datetime, timezone
from detector.model import detect, THRESHOLD

MAX_POINTS = 10_000_000

def load(path):
    pts = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if len(pts) >= MAX_POINTS:
                raise SystemExit("input exceeds MAX_POINTS")
            pts.append(json.loads(line))
    return pts

RECO = {
    "short": "Review recent deploys and upstream health; likely transient.",
    "sustained": "Page on-call to confirm; sustained elevation, not a blip.",
}

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--metrics", default="data/metrics.jsonl")
    ap.add_argument("--out", default="data/triage_queue.json")
    ap.add_argument("--sqlite-out", default="data/triage.sqlite")
    args = ap.parse_args()

    events = detect(load(args.metrics))
    events.sort(key=lambda e: e.peak_score, reverse=True)
    queue = []
    for e in events:
        kind = "sustained" if e.length >= 10 else "short"
        queue.append({
            "service": e.service, "start": e.start, "end": e.end,
            "peak_score": e.peak_score, "length_min": e.length,
            "requires_human_review": True,
            "auto_action_taken": False,
            "recommended_next_step": RECO[kind],
        })

    with open(args.out, "w") as f:
        json.dump({"threshold": THRESHOLD, "candidates": queue}, f, indent=2)

    con = sqlite3.connect(args.sqlite_out)
    con.execute("DROP TABLE IF EXISTS triage")
    con.execute("CREATE TABLE triage (service TEXT, start TEXT, end TEXT, "
                "peak_score REAL, length_min INT, requires_human_review INT, "
                "auto_action_taken INT, created_utc TEXT)")
    now = datetime.now(timezone.utc).isoformat()
    con.executemany("INSERT INTO triage VALUES (?,?,?,?,?,?,?,?)",
                    [(q["service"], q["start"], q["end"], q["peak_score"],
                      q["length_min"], 1, 0, now) for q in queue])
    con.commit(); con.close()

    print(f"{len(queue)} candidate anomalies queued for human review")
    for q in queue:
        print(f"  [{q['peak_score']:5.1f}] {q['service']:7} "
              f"{q['start']} ({q['length_min']} min)")

if __name__ == "__main__":
    main()
