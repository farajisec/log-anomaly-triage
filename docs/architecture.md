# Architecture

```
metrics.jsonl ─┐                      ┌─ seasonal_z        (departure from this minute's profile)
changes.jsonl ─┤  SeasonalModel ──────┼─ ewma              (fast: residual vs recent residual level)
               │  (per service)       ├─ cusum             (slow: accumulated residual, shifts and drifts)
               │                      └─ load_decoupling   (health vs load, power-law fit; raw values)
               │                               │
               │                       fuse per point ─► runs ─► classify shape ─► Candidate
               │                                                      │
               └───────────────── change feed ────────────────────────┤  held out: change-correlated,
                                                                      │            recurring
labels.json  (evaluation only) ──► evaluate ──► report.{json,md,html} ◄┘
feedback.db  (analyst verdicts) ──► detector weights, similar past candidates
```

## The seasonal model

One `SeasonalModel` per service owns the baseline. For each metric it
keeps the median value per (day-type, minute-of-day), pooled over ±2
minutes and the last eight days of that day-type. Residuals are taken in
log space (`log(x + c) − log(expected + c)`), so a 20 % excursion is the
same surprise at 40 rps and at 400 rps and one robust scale (MAD of
recent residuals) serves the whole day.

A slot is *own-mature* after three days of its day-type. Until then the
model borrows the other day-type's profile for the same minute, if that
is mature, and otherwise falls back to an hour-of-day median; in both
cases it withholds the "down" direction, because a drop against a baseline
that has never seen a weekend is almost always the first weekend. Points
that score as anomalous against an own-mature profile are not learned.

## Detectors

Every detector receives the model's context (residual, scale, expected,
maturity) and emits standardised scores per metric:

| Detector | Input | Catches | Weak at |
|---|---|---|---|
| `seasonal_z` | residual / scale | anything far from this minute's normal | slow changes under the threshold |
| `ewma` | residual vs its own recent level | spikes, within a minute or two | drifts (the level follows them) |
| `cusum` | residual minus a very slow bias estimate, accumulated | level shifts and creeping drifts | single spikes (by design) |
| `load_decoupling` | health metrics vs request volume (log-log fit over 12 h) | latency/errors/CPU rising while traffic is flat | nothing about volume itself |

CUSUM runs only against an own-mature profile and resets when the
profile changes under it; its boundary is sized for autocorrelated
residuals, not textbook white noise.

## Fusion

Per metric, the strongest detector wins, after two vetoes: if the
seasonal profile is mature and says the value is ordinary for this
minute, the other detectors' scores on that metric are cut (they have no
notion of "normal for 03:00"); and if volume is clearly up while the
load model says a health metric is in line with that volume, the health
metric's scores are cut (that is a surge, not a fault). "Down" on a health
metric is halved: latency falling does not page anyone.

Per point, the fused score is the strongest metric plus a bonus for a
second metric that is notable on its own. A soft-max over all metrics
was tried first and rejected: four metrics idling at 2.5 summed to 3.2 and
turned ordinary noise into runs.

## Runs, shapes, candidates

A run opens after three consecutive points over the threshold and closes
after five below it; runs less than twenty minutes apart merge; a run
whose only departing metric is volume must last ten minutes. Shape is
read off the run: `spike` (≤10 min), `sustained`, `drift` (the seasonal
departure keeps growing), `level_shift` (≥4 h, flat), `dip` (volume
down), `surge` (volume up, health keeping pace).

`queue.py` turns runs into candidates with severity, priority (severity ×
service criticality × confidence, boosted by SLO burn rate), a playbook
step, an explanation naming the detectors and their shares, similar past
verdicts, and a feature vector. Two kinds are held out of the queue and
reported separately: runs starting within six minutes of a recorded
deploy and lasting under fifteen, and runs that repeat at the same
ten-minute slot with similar duration on three earlier days.

## Feedback

`feedback.py` stores verdicts in SQLite. A `noise` verdict nudges the
flagged detectors' weights for that service toward 0.5, an `incident`
verdict toward 1.5, by 15 % of the gap per verdict; `benign` teaches
nothing (the detector was right to flag). The three most similar past
verdicts (cosine on the feature vector, same service) ride along on new
candidates. Nothing in the package can act on a service: there is no
subprocess, socket or shell call, and a test greps for them.
