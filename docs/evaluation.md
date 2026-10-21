# How the numbers are produced, and what they do not mean

## Ground truth

`generate` writes `metrics.jsonl` (what detection sees), `changes.jsonl`
(the deploy feed, which detection may read as context) and `labels.json`
(incident windows, benign-event windows, and what a tuned queue is
expected to do with each benign kind). Nothing in the metrics says which
points belong to an incident; a test asserts that.

## Scoring

A queued candidate that overlaps an incident window on its service is a
true positive; an incident with no overlapping queued candidate is a
false negative; a queued candidate overlapping nothing is a false
positive. A queued candidate overlapping a benign event is judged by the
label's expectation:

| expectation | meaning | if queued |
|---|---|---|
| `held` | should have been held out (deploys, scheduled jobs) | false positive |
| `surge` | may be queued only as a low-severity `surge` | FP unless shape is `surge` |
| `noisy` | queueing it is the accepted analyst cost | benign true positive |

Held-out candidates are listed, not scored. Detection latency is minutes
from the incident's labeled start to the candidate's start.

The report also carries a **threshold sweep** (precision and recall of
the raw fused-score runs at every operating point, so 4.0 is visibly a
choice) and a **naive baseline**: a trailing-window z-score per metric
with no seasonality, no fusion and no change feed, scored the same way.

`--strict` fails the run on any missed incident or on more than
`--max-fp` false positives; CI runs it on two seeds.

## What the numbers do not say

- **They describe this simulation.** Ten incidents of five kinds, three
  weeks, six services. Recall of 1.0 means these ten were caught, not
  that the detector catches everything.
- **The false positives are real and reported.** About one every two
  days across six services at the chosen threshold. Most are three-to-
  seven-minute excursions on the noisiest services; the sweep shows what
  a higher threshold buys and costs.
- **Thresholds are illustrative constants** with a comment on why each
  is what it is. A real environment tunes them from its own baseline, and
  the feedback loop exists so analysts can move them without editing code.
- **Three days of warm-up** before anything is reported, and the first
  weekend is judged against a borrowed weekday profile. Both are the
  honest price of a baseline that is actually learned.
