# Log Anomaly Triage

An unsupervised anomaly detector for service metrics that produces a ranked
queue of candidate incidents for a human to review. It learns each
service's normal behavior from the recent past, flags departures from it,
and recommends a next step, but it never takes an action on its own.

Everything is synthetic and reproducible. The service names are generic
placeholders and the data is generated, so there is nothing real to leak.

## Why it exists

Two ideas the brief behind this work cares about meet here. The first is
operational: turn a firehose of metrics into a short, ranked list worth a
person's attention. The second is about using AI in operations safely:
automation can decide what to look at, but a human decides what to do. The
queue this produces is advisory by construction, and a test enforces that.

## How the detector works

Two transparent signals, combined by taking the stronger:

1. **Robust rolling z-score.** Over a trailing window, the median and the
   median absolute deviation give a center and spread that outliers do not
   distort. The score is how far the current point sits above that center.
2. **EWMA residual.** An exponentially weighted moving average tracks the
   expected level; the residual above it, scaled by a running estimate of
   residual size, is the second score.

It is unsupervised (it learns "normal," not "attack"), one-sided (it flags
elevations, not dips), warmed up (it will not flag before it has a stable
baseline), debounced (a short run is required to open an event), and
deterministic. A point is never part of its own baseline.

## Results on the bundled scenario set

Synthetic data only; numbers describe this lab. Four services over two days
(11,520 points) with five injected anomalies (short spikes and sustained
elevations).

| Metric | Value |
|---|---|
| True positives | 5 / 5 |
| False positives | 0 |
| False negatives | 0 |
| Precision | 1.000 |
| Recall | 1.000 |
| F1 | 1.000 |
| Mean detection latency | 0.0 min |

Latency is zero here because the injected anomalies start sharply; on a
slower ramp the detector would open the event a few points in.

## The guardrail

Every candidate is marked `requires_human_review: true` and
`auto_action_taken: false`. There is no code path that restarts anything,
pages anyone, or changes state. See `docs/human-in-the-loop.md`. The
end-to-end test asserts the queue stays advisory.

## Run it

No third-party dependencies. Python 3.11+.

```bash
python3 pipeline/generate_metrics.py   # -> data/metrics.jsonl, data/labels.json
python3 detect.py                      # -> data/triage_queue.json, data/triage.sqlite
python3 evaluate.py                    # -> data/eval.json
```

Or `./run.sh`. Tests: `python3 -m unittest discover -s tests -v`.

## Layout

```
pipeline/generate_metrics.py  synthetic metrics + labeled anomalies (labels separate)
detector/model.py             the unsupervised detector
detect.py                     runs the detector, emits an advisory triage queue
evaluate.py                   scores the queue vs labels, with detection latency
tests/                        unit, property, warmup, and end-to-end tests
docs/human-in-the-loop.md     the guardrail and its honest limits
```

## Provenance

Built with AI assistance under explicit requirements. The method, the
guardrails, their limits, and the validation are documented here and
enforced by the tests in this repository.

## License

MIT. See `LICENSE`.
