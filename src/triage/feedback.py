"""The human in the loop, as code.

Analysts record a verdict on a candidate: ``incident``, ``benign`` or
``noise``, with an optional note. Verdicts live in SQLite and are used in
two ways, both bounded so a handful of clicks cannot wreck the detector:

1. **Calibration.** For each service, a detector that keeps producing
   candidates marked ``noise`` has its weight reduced, and one that keeps
   finding real incidents has it restored. Weights stay within
   [0.5, 1.5], and a weight moves by a fixed fraction of the gap per
   verdict, so the system learns slowly and recovers.

2. **Similar past candidates.** A new candidate is compared with past
   ones by cosine similarity on a small feature vector, and the three
   closest past verdicts are attached. "Last time this shape appeared on
   this service an analyst called it noise" is the single most useful
   line in a triage queue.

Nothing here acts on a service. There is no code path from a verdict to
a restart, a page or a rollback, and the tests assert that.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

VERDICTS = ("incident", "benign", "noise")
WEIGHT_MIN, WEIGHT_MAX, STEP = 0.5, 1.5, 0.15


@dataclass
class Verdict:
    candidate_id: str
    service: str
    verdict: str
    detectors: list[str]
    features: list[float]
    shape: str
    note: str = ""


class FeedbackStore:
    def __init__(self, path: Path):
        self.con = sqlite3.connect(path)
        self.con.execute("""CREATE TABLE IF NOT EXISTS verdicts (
            candidate_id TEXT PRIMARY KEY, service TEXT, verdict TEXT, detectors TEXT,
            features TEXT, shape TEXT, note TEXT, recorded_utc TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.con.execute("""CREATE TABLE IF NOT EXISTS weights (
            service TEXT, detector TEXT, weight REAL, PRIMARY KEY (service, detector))""")
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    # --- verdicts -----------------------------------------------------------
    def record(self, v: Verdict) -> None:
        if v.verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")
        self.con.execute("INSERT OR REPLACE INTO verdicts VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                         (v.candidate_id, v.service, v.verdict, json.dumps(v.detectors),
                          json.dumps(v.features), v.shape, v.note))
        self._calibrate(v)
        self.con.commit()

    def verdicts(self, service: str | None = None) -> list[Verdict]:
        q = "SELECT candidate_id, service, verdict, detectors, features, shape, note FROM verdicts"
        rows = self.con.execute(q + (" WHERE service=?" if service else ""),
                                (service,) if service else ()).fetchall()
        return [Verdict(r[0], r[1], r[2], json.loads(r[3]), json.loads(r[4]), r[5], r[6]) for r in rows]

    # --- calibration ----------------------------------------------------------
    def weights(self, service: str) -> dict[str, float]:
        rows = self.con.execute("SELECT detector, weight FROM weights WHERE service=?", (service,))
        return {d: w for d, w in rows}

    def _calibrate(self, v: Verdict) -> None:
        if v.verdict == "benign":
            return                                   # right to flag, nothing to learn
        target = WEIGHT_MIN if v.verdict == "noise" else WEIGHT_MAX
        current = self.weights(v.service)
        for d in v.detectors:
            w = current.get(d, 1.0)
            w = w + STEP * (target - w)
            w = min(WEIGHT_MAX, max(WEIGHT_MIN, w))
            self.con.execute("INSERT OR REPLACE INTO weights VALUES (?,?,?)", (v.service, d, w))

    # --- similarity -------------------------------------------------------------
    def similar(self, service: str, features: list[float], k: int = 3) -> list[dict]:
        past = self.verdicts(service)
        scored = []
        for p in past:
            sim = cosine(features, p.features)
            scored.append((sim, p))
        scored.sort(key=lambda t: -t[0])
        return [{"candidate_id": p.candidate_id, "verdict": p.verdict, "shape": p.shape,
                 "similarity": round(s, 2), "note": p.note} for s, p in scored[:k] if s > 0.6]


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
