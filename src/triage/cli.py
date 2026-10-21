"""Command-line entry point: ``triage <command>``.

    generate   synthetic metrics + labels
    detect     score, build the advisory queue (uses feedback.db if present)
    evaluate   score the queue against labels; write report.{json,md,html}
    feedback   record an analyst verdict on a candidate
    all        generate -> detect -> evaluate
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .ensemble import THRESHOLD
from .evaluate import evaluate, load_labels
from .feedback import FeedbackStore, Verdict
from .report import html_page, markdown
from .runner import detect, read_changes, read_points, write_queue, write_scores
from .simulate import generate


def cmd_generate(a):
    s = generate(a.seed, a.build, a.days)
    print(f"generated {s['points']:,} points, {s['incidents']} incidents, {s['benign_events']} benign events -> {a.build}")
    return 0


def cmd_detect(a):
    labels = load_labels(a.build / "labels.json")
    store = FeedbackStore(a.build / "feedback.db") if (a.build / "feedback.db").exists() or a.feedback else None
    pts = read_points(a.build / "metrics.jsonl")
    changes = read_changes(a.build / "changes.jsonl")
    cands, held, scored = detect(pts, labels["criticality"], store, a.threshold, changes)
    write_queue(a.build / "queue.json", cands, held, a.threshold or THRESHOLD)
    write_scores(a.build / "scores.jsonl", scored)
    if store:
        store.close()
    print(f"{len(pts):,} points -> {len(cands)} candidates for human review, {len(held)} held out")
    for c in cands[:8]:
        print(f"  [{c.severity:8s} p={c.priority:.2f}] {c.service:12s} {c.shape:11s} {c.start} ({c.duration_min} min)")
    return 0


def cmd_evaluate(a):
    from .model import Candidate
    with open(a.build / "queue.json", encoding="utf-8") as f:
        q = json.load(f)
    cands = [Candidate(**c) for c in q["candidates"]]
    held = [Candidate(**c) for c in q.get("held_out", [])]
    labels = load_labels(a.build / "labels.json")
    pts = read_points(a.build / "metrics.jsonl")
    rep = evaluate(cands, held, labels, pts, a.build / "scores.jsonl")
    with open(a.build / "report.json", "w", encoding="utf-8") as f:
        json.dump(rep.to_dict(), f, indent=1)
    with open(a.build / "report.md", "w", encoding="utf-8") as f:
        f.write(markdown(rep))
    with open(a.build / "report.html", "w", encoding="utf-8") as f:
        f.write(html_page(rep, q["candidates"]))
    o = rep.overall
    print(f"ensemble: P {o['precision']} R {o['recall']} F1 {o['f1']} latency {o['median_latency_min']} min "
          f"| naive: P {rep.naive['precision']} R {rep.naive['recall']}")
    print(f"queued {rep.queued}, held out {rep.held_out} {rep.held_out_summary}, false positives {o['fp']}")
    missed = [i for i in rep.incidents if not i.detected]
    for m in missed:
        print(f"  MISSED {m.id} {m.service} {m.kind}")
    for k, v in rep.benign_events.items():
        print(f"  benign {k:14s} events {v['events']:3d} queued {v['queued']:3d} wrongly {v['queued_wrongly']:3d} "
              f"held {v['held_out']:3d} (expect: {v['expectation']})")
    if a.strict and (missed or o["fp"] > a.max_fp):
        return 1
    return 0


def cmd_feedback(a):
    with open(a.build / "queue.json", encoding="utf-8") as f:
        cands = {c["candidate_id"]: c for c in json.load(f)["candidates"]}
    if a.candidate not in cands:
        print(f"unknown candidate {a.candidate}")
        return 1
    c = cands[a.candidate]
    store = FeedbackStore(a.build / "feedback.db")
    store.record(Verdict(a.candidate, c["service"], a.verdict, c["detectors"],
                         c.get("features", [0.0] * 6), c["shape"], a.note or ""))
    print(f"recorded {a.verdict} for {a.candidate}; weights for {c['service']}: {store.weights(c['service'])}")
    store.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="triage", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--build", type=Path, default=Path("build"))
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--days", type=int, default=21)
    d = sub.add_parser("detect")
    d.add_argument("--threshold", type=float, default=None)
    d.add_argument("--feedback", action="store_true", help="create feedback.db if missing")
    e = sub.add_parser("evaluate")
    e.add_argument("--strict", action="store_true")
    e.add_argument("--max-fp", type=int, default=15)
    fb = sub.add_parser("feedback")
    fb.add_argument("candidate")
    fb.add_argument("verdict", choices=["incident", "benign", "noise"])
    fb.add_argument("--note")
    al = sub.add_parser("all")
    al.add_argument("--seed", type=int, default=42)
    al.add_argument("--days", type=int, default=21)
    al.add_argument("--threshold", type=float, default=None)
    al.add_argument("--feedback", action="store_true")
    al.add_argument("--strict", action="store_true")
    al.add_argument("--max-fp", type=int, default=15)
    a = p.parse_args(argv)
    if a.cmd == "all":
        return cmd_generate(a) or cmd_detect(a) or cmd_evaluate(a)
    return {"generate": cmd_generate, "detect": cmd_detect, "evaluate": cmd_evaluate,
            "feedback": cmd_feedback}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
