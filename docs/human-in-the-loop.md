# The human in the loop

The queue decides what a person looks at. A person decides what happens.
That split is not a disclaimer; it is enforced in three places.

1. **Every candidate says so.** `requires_human_review` is always true
   and `auto_action_taken` is always false, on queued and held-out
   candidates alike. The end-to-end test checks every one.
2. **The package cannot act.** There is no code path to a restart, a
   page, a rollback or a scale-out. A test greps the source for
   `subprocess`, `socket` and `os.system` and fails if any appears.
   The "recommended next step" is a sentence from a playbook, for a
   person to read.
3. **Feedback is bounded.** Verdicts move detector weights by 15 % of the
   remaining gap per verdict, inside [0.5, 1.5], per service. A single
   bad afternoon of clicking cannot blind a detector, and the system
   recovers as verdicts change.

What the loop gives back:

- **Calibration without code.** A detector that keeps producing noise on
  one service loses weight on that service only.
- **Memory.** The three most similar past verdicts ride along on each new
  candidate. "Last time this shape appeared here, someone called it
  noise" is the most useful line in a triage queue, and it is the one
  automation usually forgets.
- **Audit.** Held-out candidates are written next to the queue with the
  reason they were held, so the decision not to show something can be
  reviewed as easily as the decision to show it.

Record a verdict with:

```bash
triage feedback <candidate-id> incident|benign|noise --note "..."
triage detect          # next run uses the updated weights and memory
```
