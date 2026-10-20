"""Tests for the anomaly detector and triage evaluation.

Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from detector import model  # noqa: E402

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def series(service, values):
    return [{"ts": (T0 + timedelta(minutes=i)).isoformat(),
             "service": service, "error_rate": v}
            for i, v in enumerate(values)]


class TestDetector(unittest.TestCase):
    def test_flags_sustained_elevation(self):
        vals = [1.0] * 200 + [12.0] * 20 + [1.0] * 50
        events = model.detect(series("api", vals))
        self.assertTrue(events, "detector missed a clear elevation")
        self.assertTrue(any(e.length >= 2 for e in events))

    def test_quiet_on_flat_normal(self):
        import random
        r = random.Random(0)
        vals = [1.0 + r.gauss(0, 0.1) for _ in range(300)]
        self.assertEqual(model.detect(series("api", vals)), [])

    def test_warmup_suppresses_cold_start(self):
        # a big value inside the warmup window must not open an event
        vals = [1.0, 50.0] + [1.0] * 200
        events = model.detect(series("api", vals))
        for e in events:
            self.assertGreaterEqual(
                datetime.fromisoformat(e.start),
                T0 + timedelta(minutes=model.WARMUP),
                "flagged a point inside the warmup window")

    def test_determinism(self):
        vals = [1.0] * 200 + [12.0] * 10 + [1.0] * 50
        a = [(e.service, e.start, e.length) for e in model.detect(series("api", vals))]
        b = [(e.service, e.start, e.length) for e in model.detect(series("api", vals))]
        self.assertEqual(a, b)

    def test_scores_only_on_history(self):
        # the current point must not be in its own baseline window
        s = model.score_series(series("api", [1.0] * 50 + [9.0]))
        # last point should score high because baseline excludes it
        self.assertGreater(s[-1][1], model.THRESHOLD)


class TestEndToEnd(unittest.TestCase):
    def test_pipeline_recall_and_precision(self):
        with tempfile.TemporaryDirectory() as d:
            met = os.path.join(d, "m.jsonl")
            lab = os.path.join(d, "l.json")
            q = os.path.join(d, "q.json")
            sq = os.path.join(d, "t.sqlite")
            ev = os.path.join(d, "e.json")
            env = {**os.environ, "PYTHONPATH": ROOT}
            subprocess.run([sys.executable, "pipeline/generate_metrics.py",
                            "--out", met, "--labels-out", lab], cwd=ROOT,
                           check=True, env=env, capture_output=True)
            subprocess.run([sys.executable, "detect.py", "--metrics", met,
                            "--out", q, "--sqlite-out", sq], cwd=ROOT,
                           check=True, env=env, capture_output=True)
            subprocess.run([sys.executable, "evaluate.py", "--queue", q,
                            "--labels", lab, "--out", ev], cwd=ROOT,
                           check=True, env=env, capture_output=True)
            with open(ev) as f:
                res = json.load(f)
            self.assertEqual(res["FN"], 0, "missed a labeled anomaly")
            self.assertEqual(res["FP"], 0, "raised a false anomaly")
            self.assertEqual(res["recall"], 1.0)

    def test_queue_is_advisory_only(self):
        with tempfile.TemporaryDirectory() as d:
            met = os.path.join(d, "m.jsonl")
            q = os.path.join(d, "q.json")
            sq = os.path.join(d, "t.sqlite")
            env = {**os.environ, "PYTHONPATH": ROOT}
            subprocess.run([sys.executable, "pipeline/generate_metrics.py",
                            "--out", met, "--labels-out",
                            os.path.join(d, "l.json")], cwd=ROOT, check=True,
                           env=env, capture_output=True)
            subprocess.run([sys.executable, "detect.py", "--metrics", met,
                            "--out", q, "--sqlite-out", sq], cwd=ROOT,
                           check=True, env=env, capture_output=True)
            with open(q) as f:
                data = json.load(f)
            for c in data["candidates"]:
                self.assertTrue(c["requires_human_review"])
                self.assertFalse(c["auto_action_taken"])


if __name__ == "__main__":
    unittest.main()
