import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from triage.feedback import WEIGHT_MAX, WEIGHT_MIN, FeedbackStore, Verdict, cosine


def test_noise_verdicts_lower_weights_slowly_and_incidents_restore_them(tmp_path):
    store = FeedbackStore(tmp_path / "fb.db")
    for k in range(3):
        store.record(Verdict(f"c{k}", "svc", "noise", ["cusum"], [1.0, 0.2, 0.1, 0.1, 0.1, 0.1], "spike"))
    w = store.weights("svc")["cusum"]
    assert WEIGHT_MIN < w < 1.0
    for k in range(10):
        store.record(Verdict(f"i{k}", "svc", "incident", ["cusum"], [1.0, 0.2, 0.1, 0.1, 0.1, 0.1], "spike"))
    assert store.weights("svc")["cusum"] > 1.0
    store.close()


@given(st.lists(st.sampled_from(["incident", "benign", "noise"]), min_size=1, max_size=40))
@settings(max_examples=30, deadline=None)
def test_weights_stay_bounded(tmp_path_factory, verdicts):
    store = FeedbackStore(tmp_path_factory.mktemp("fb") / "fb.db")
    for k, v in enumerate(verdicts):
        store.record(Verdict(f"c{k}", "svc", v, ["ewma", "seasonal_z"], [0.5] * 6, "spike"))
    for w in store.weights("svc").values():
        assert WEIGHT_MIN <= w <= WEIGHT_MAX
    store.close()


def test_similar_past_verdicts_are_attached(tmp_path):
    store = FeedbackStore(tmp_path / "fb.db")
    store.record(Verdict("old", "svc", "noise", ["ewma"], [1.0, 0.5, 0.2, 0.0, 0.0, 0.0], "spike", "known flapper"))
    sim = store.similar("svc", [1.0, 0.5, 0.2, 0.0, 0.0, 0.1])
    assert sim and sim[0]["verdict"] == "noise" and sim[0]["note"] == "known flapper"
    assert store.similar("other", [1.0, 0.5, 0.2, 0.0, 0.0, 0.1]) == []
    store.close()


def test_rejects_unknown_verdict(tmp_path):
    store = FeedbackStore(tmp_path / "fb.db")
    with pytest.raises(ValueError):
        store.record(Verdict("x", "svc", "maybe", [], [0.0] * 6, "spike"))
    store.close()


def test_cosine_edge_cases():
    assert cosine([0, 0], [1, 1]) == 0.0
    assert cosine([1, 2], [1, 2, 3]) == 0.0
    assert abs(cosine([1, 1], [2, 2]) - 1.0) < 1e-9
