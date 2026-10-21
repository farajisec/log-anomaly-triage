"""Generate three weeks, detect, evaluate. The numbers in the README come from this path."""
import json
import re
from pathlib import Path

import pytest

from triage.evaluate import evaluate, load_labels
from triage.runner import detect, read_changes, read_points
from triage.simulate import generate


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    build = tmp_path_factory.mktemp("seed42")
    generate(42, build)
    labels = load_labels(build / "labels.json")
    pts = read_points(build / "metrics.jsonl")
    queue, held, _ = detect(pts, labels["criticality"], None, None, read_changes(build / "changes.jsonl"))
    return build, labels, queue, held, evaluate(queue, held, labels, pts)


def test_labels_never_reach_the_metrics(run):
    build = run[0]
    text = Path(build / "metrics.jsonl").read_text(encoding="utf-8")
    assert "benign" not in text and "kind" not in text
    assert not re.search(r"\bI\d\d\b", text)


def test_every_incident_is_detected(run):
    rep = run[4]
    missed = [i.id for i in rep.incidents if not i.detected]
    assert not missed, missed
    assert rep.overall["recall"] == 1.0


def test_false_positive_budget(run):
    rep = run[4]
    assert rep.overall["fp"] <= 15, [f["candidate_id"] for f in rep.false_positives]
    assert rep.overall["precision"] >= 0.4


def test_scheduled_work_is_not_queued(run):
    rep = run[4]
    assert rep.benign_events["batch_job"]["queued_wrongly"] <= 3
    assert rep.benign_events["deploy_wobble"]["held_out"] >= 15


def test_ensemble_beats_naive_baseline_on_precision(run):
    rep = run[4]
    assert rep.overall["precision"] > 3 * (rep.naive["precision"] or 0)
    assert rep.overall["recall"] >= (rep.naive["recall"] or 0)


def test_queue_puts_real_incidents_first(run):
    rep = run[4]
    assert rep.queue_quality["top5_true_incidents"] >= 4
    assert rep.queue_quality["median_incident_rank"] <= 10


def test_everything_is_advisory(run):
    queue, held = run[2], run[3]
    for c in queue + held:
        assert c.requires_human_review is True
        assert c.auto_action_taken is False
    # and the package has no way to act: no subprocess, no sockets, no shell
    src = Path(__file__).resolve().parents[1] / "src" / "triage"
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "subprocess" not in text and "socket" not in text and "os.system" not in text, py


def test_detection_is_deterministic(run):
    build, labels, queue = run[0], run[1], run[2]
    pts = read_points(build / "metrics.jsonl")
    again, _, _ = detect(pts, labels["criticality"], None, None, read_changes(build / "changes.jsonl"))
    assert [c.candidate_id for c in again] == [c.candidate_id for c in queue]


def test_candidate_serialises_round_trip(run):
    from triage.model import Candidate
    c = run[2][0]
    assert Candidate(**json.loads(json.dumps(c.to_dict()))) == c
