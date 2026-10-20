# Human-in-the-Loop and Guardrails

This detector is built to assist a person, not to replace one. The design
reflects a simple rule: automated analysis can decide what to *look at*,
but a human decides what to *do*.

## What the system does and does not do

- It **reads** metrics and **ranks** candidate anomalies.
- It **recommends** a next step for each candidate.
- It does **not** act. There is no code path that restarts a service,
  pages anyone, files a ticket, or changes any state. Every candidate in
  the queue is marked `requires_human_review: true` and
  `auto_action_taken: false`, and the end-to-end test asserts that both
  stay that way.

## Why unsupervised, and why one-sided

The detector is unsupervised: it learns a per-service baseline from the
recent past rather than from labeled training data. That keeps it honest
about what it can claim. It has not learned "what an attack looks like";
it has learned "what normal looks like here" and flags departures from it.
It is one-sided on purpose, flagging elevations in error rate, because a
drop in errors is not an incident.

## Why a human stays in the loop

An unsupervised detector will surface things that are unusual but benign: a
planned load test, a deploy, a seasonal shift it has not seen before. The
value is in triage, narrowing a stream of metrics down to a short ranked
list worth a human's attention, with the context to judge quickly. Taking
action automatically on that list would turn a useful filter into an
unreviewed actor. The recommended next step is a prompt for the analyst,
not an instruction to the system.

## Honest limits

- The baseline adapts, so a slow, sustained drift can be absorbed into
  "normal" over time. Sudden changes are what this catches best.
- Thresholds are set for this lab's synthetic data. Real tuning depends on
  a service's own noise profile and the cost of a missed detection versus
  the cost of a noisy queue.
- The detection-latency metric assumes clean label windows. In production,
  "when did the incident really start" is itself a judgment call.
