# Log Anomaly Triage

[![ci](https://github.com/farajisec/log-anomaly-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/farajisec/log-anomaly-triage/actions/workflows/ci.yml)

Unsupervised, multi-signal anomaly detection for service metrics, with a
ranked, explained, **advisory** triage queue and an analyst feedback loop
that recalibrates the detectors. It learns each service's normal from
three weeks of synthetic telemetry — daily and weekly seasonality,
scheduled batch jobs, deploy wobbles, legitimate traffic surges, slow
trends — and is measured against ten labeled incidents and 187 labeled
benign events, with a naive z-score baseline scored alongside it so the
value of the design is measured rather than claimed.

Everything is synthetic and reproducible: generic service names, generated
data, no real systems. The numbers below come from running the code here,
and CI fails if they regress. Standard library only, Python 3.10+.

```bash
pip install -e ".[dev]"
triage all --seed 42 --strict      # generate -> detect -> evaluate
open build/report.html
```

## How it works

One **seasonal model** per service holds the expected value for every
(day-type, minute-of-day) and produces log-space residuals with a robust
scale. Four detectors read those residuals and vote per metric:

| Detector | Catches |
|---|---|
| **seasonal z** | anything far from this minute's normal, batch jobs included once learned |
| **EWMA residual** | spikes, within a minute or two |
| **CUSUM** (bias-corrected, sized for autocorrelated data) | level shifts and creeping drifts no single point would flag |
| **load decoupling** (power-law fit of health vs traffic) | latency, errors or CPU rising while traffic is flat — and *not* firing when they rise because traffic did |

Fusion takes the strongest voice per metric with two vetoes (the seasonal
profile saying "ordinary for this minute"; the load model saying "explained
by traffic"), then opens a **run** after three consecutive points over
threshold, merges near neighbours, and classifies its **shape**: spike,
sustained, drift, level shift, dip or surge.

Runs become **candidates** ranked by severity × service criticality ×
confidence, boosted by SLO burn rate, each carrying a playbook step, an
explanation naming the detectors and their shares, and the most similar
past analyst verdicts. Two kinds are **held out** and reported rather than
queued: runs that start within minutes of a recorded deploy, and runs that
recur at the same slot on three earlier days.

**Analyst verdicts** (`incident` / `benign` / `noise`) are stored in SQLite,
nudge per-service detector weights within bounds, and are attached by
similarity to future candidates. Nothing in the package can act on a
service; a test greps the source to prove it.

<!-- results:start -->
## Measured results

Seed 42: 181,440 points over three weeks, six services, 10 injected incidents and 187 labeled benign events. 22 candidates queued (1.05/day), 24 held out of the queue as change-correlated or recurring.

| | Ensemble | Naive z-score |
|---|---:|---:|
| Incidents detected | 10 / 10 | 9 / 10 |
| False positives | 10 | 176 |
| Precision | 0.545 | 0.064 |
| Recall | 1.000 | 0.900 |
| F1 | 0.706 | 0.119 |
| Median detection latency | 0.0 min | — |
| Shape classified correctly | 80% | — |

### By incident type

| Type | Incidents | Detected | Median latency |
|---|---:|---:|---:|
| dip | 1 | 1 | 0.0 min |
| drift | 1 | 1 | 116.0 min |
| level_shift | 2 | 2 | 121.5 min |
| spike | 2 | 2 | 0.0 min |
| sustained | 4 | 4 | 0.0 min |

### Benign events

| Event | Occurrences | Expected handling | Queued | Queued wrongly | Held out |
|---|---:|---|---:|---:|---:|
| batch_job | 147 | held | 0 | 0 | 2 |
| deploy_wobble | 37 | held | 1 | 1 | 24 |
| flapping | 1 | noisy | 0 | 0 | 0 |
| traffic_surge | 2 | surge | 2 | 2 | 0 |

### Operating point

Precision / recall across fused-score thresholds (the lab runs at 4.0):

| Threshold | Candidates | Precision | Recall | F1 |
|---:|---:|---:|---:|---:|
| 2.5 | 515 | 0.146 | 1.000 | 0.254 |
| 3.0 | 190 | 0.337 | 1.000 | 0.504 |
| 3.5 | 94 | 0.521 | 1.000 | 0.685 |
| 4.0 | 79 | 0.671 | 1.000 | 0.803 |
| 5.0 | 71 | 0.718 | 1.000 | 0.836 |
| 6.0 | 69 | 0.710 | 1.000 | 0.831 |
| 8.0 | 53 | 0.755 | 1.000 | 0.860 |

### Queue quality

- Top 5 of the queue: 5 real incidents.
- First false positive appears at rank 7.
- Critical services in the top 3: 3.
- Every candidate is advisory (`requires_human_review`, no `auto_action_taken`): yes.
<!-- results:end -->

## Design decisions worth reading

- **Minute-level seasonal profiles, not hourly buckets.** An eight-minute
  batch job inside an hour-wide bucket is a minority pattern that a median
  rejects forever. At minute granularity it is in the profile after three
  days and stops being news.
- **Log-space residuals.** Service metrics are multiplicative. One scale
  per metric then covers the whole day; the alternative (one scale, raw
  units) under-covered daytime and over-covered night.
- **Fusion that does not reward mediocrity.** A soft-max over four metrics
  turned four 2.5s into a 3.2 and manufactured runs from noise. The
  strongest metric plus a bonus for a second one that is notable alone
  fixed most of the false positives in one change.
- **CUSUM on residuals, with a slow bias tracker, against own history
  only.** Three separate lessons: the daily ramp must not accumulate; a
  trend the profile has not caught up with must not accumulate; and a
  borrowed weekend profile must not poison the bias estimate for Monday.
- **A change feed is context, not an afterthought.** Thirty-seven deploy
  wobbles in three weeks; the pipeline reads `changes.jsonl` and holds
  the short ones out, listing them for audit rather than deleting them.
- **The queue is advisory by construction.** See
  [docs/human-in-the-loop.md](docs/human-in-the-loop.md).

## Layout

```
src/triage/simulate/   synthetic fleet: seasonality, trends, batch jobs, deploys, labeled incidents and benign events
src/triage/detectors/  seasonal model + seasonal_z, ewma, cusum, load_decoupling
src/triage/ensemble.py fusion, runs, shape classification
src/triage/queue.py    ranking, explanations, playbooks, change-correlated and recurring hold-outs
src/triage/feedback.py analyst verdicts: bounded calibration, similar-past lookup (SQLite)
src/triage/evaluate.py scoring vs labels, threshold sweep, naive baseline; report.py renders it
tests/                 unit, property-based (Hypothesis), end-to-end on the real simulation
docs/                  architecture, evaluation method, the human-in-the-loop guardrail
```

## Honest limits

This is a lab on a simulation it defines. Thresholds are named constants
with comments, not values tuned from a real fleet. The warm-up is three
days and the first weekend is judged against a borrowed weekday profile.
The remaining false positives are listed in every report, with the sweep
that shows what a higher threshold would buy and cost. The feedback loop
is the mechanism by which a real deployment would move those numbers
without editing code.

## Provenance

Built with AI assistance under explicit requirements. The method, the
guardrails, their limits, and the validation are documented here, in
`docs/`, and in the code, and enforced by the tests in this repository.

## License

MIT. See `LICENSE`.
