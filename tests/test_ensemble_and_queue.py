from conftest import T0, point
from hypothesis import given
from hypothesis import strategies as st

from triage.ensemble import COOLDOWN, MIN_RUN, MIN_RUN_VOLUME_ONLY, THRESHOLD, Run, Scored, classify_shape, fuse, runs_from_scores
from triage.queue import build_candidates


def scored(i, score, per=None, direction=None, seasonal=None, service="svc"):
    per = per or {"latency_p95_ms": score}
    best = dict.fromkeys(per, "seasonal_z")
    return Scored(point(i, service), score, per, best, direction or dict.fromkeys(per, "up"), [], seasonal or dict(per))


def test_fuse_does_not_inflate_many_mediocre_signals():
    assert fuse([2.5, 2.5, 2.5, 2.5]) == 2.5
    assert fuse([5.0]) == 5.0
    assert fuse([5.0, 2.0]) == 5.0
    assert fuse([5.0, 4.5]) == 6.0


@given(st.lists(st.floats(min_value=0, max_value=20), min_size=1, max_size=4))
def test_fuse_is_bounded_by_max_plus_one(values):
    f = fuse(values)
    assert max(values) <= f <= max(values) + 1.0 + 1e-9


def test_runs_need_min_run_and_cooldown_and_merge():
    pts = [scored(i, 1.0) for i in range(60)]
    for i in range(10, 13):
        pts[i] = scored(i, 6.0)                        # exactly MIN_RUN points
    for i in range(20, 30):
        pts[i] = scored(i, 6.0)                        # 7 minutes later: merged (gap < 20)
    for i in range(55, 57):
        pts[i] = scored(i, 6.0)                        # too short to open
    runs = runs_from_scores("svc", pts)
    assert len(runs) == 1
    assert runs[0].start == pts[10].point.ts and runs[0].end == pts[29].point.ts
    assert MIN_RUN == 3 and COOLDOWN == 5


def test_volume_only_runs_need_ten_minutes():
    pts = [scored(i, 1.0) for i in range(40)]
    for i in range(5, 10):
        pts[i] = scored(i, 6.0, per={"requests_per_s": 6.0})
    assert runs_from_scores("svc", pts) == []
    for i in range(5, 5 + MIN_RUN_VOLUME_ONLY):
        pts[i] = scored(i, 6.0, per={"requests_per_s": 6.0})
    assert len(runs_from_scores("svc", pts)) == 1


def test_shape_classification():
    spike = Run("svc", [scored(i, 6.0) for i in range(5)])
    assert classify_shape(spike) == "spike"
    sustained = Run("svc", [scored(i, 6.0) for i in range(60)])
    assert classify_shape(sustained) == "sustained"
    drift = Run("svc", [scored(i, 4 + i / 40, seasonal={"latency_p95_ms": 4 + i / 40}) for i in range(200)])
    assert classify_shape(drift) == "drift"
    level = Run("svc", [scored(i, 6.0) for i in range(300)])
    assert classify_shape(level) == "level_shift"
    dip = Run("svc", [scored(i, 6.0, per={"requests_per_s": 6.0}, direction={"requests_per_s": "down"})
                      for i in range(20)])
    assert classify_shape(dip) == "dip"
    surge = Run("svc", [scored(i, 6.0, per={"requests_per_s": 6.0}) for i in range(20)])
    assert classify_shape(surge) == "surge"


def test_change_correlated_runs_are_held_out():
    run = Run("svc", [scored(i, 6.0) for i in range(3)])
    queue, held = build_candidates([run], {"svc": 1.0}, None, [{"ts": T0 - 120, "service": "svc", "kind": "deploy"}])
    assert not queue and len(held) == 1 and held[0].suppressed_reason.startswith("change-correlated")
    queue, held = build_candidates([run], {"svc": 1.0}, None, [{"ts": T0 - 3600, "service": "svc", "kind": "deploy"}])
    assert len(queue) == 1 and not held


def test_recurring_runs_are_held_out_after_three_days():
    runs = [Run("svc", [scored(d * 1440 + 600 + i, 6.0) for i in range(8)]) for d in range(5)]
    queue, held = build_candidates(runs, {"svc": 1.0})
    assert len(queue) == 3 and len(held) == 2
    assert all(h.suppressed_reason.startswith("recurring") for h in held)


def test_candidates_are_advisory_and_ranked():
    runs = [Run("low", [scored(i, 5.0, service="low") for i in range(5)]),
            Run("crit", [scored(i, 5.0, service="crit") for i in range(5)])]
    queue, _ = build_candidates(runs, {"low": 0.3, "crit": 1.0})
    assert [c.service for c in queue] == ["crit", "low"]
    for c in queue:
        assert c.requires_human_review and not c.auto_action_taken
        assert c.recommended_next_step and c.explanation
        assert 0 < c.confidence < 1
    assert THRESHOLD == 4.0
