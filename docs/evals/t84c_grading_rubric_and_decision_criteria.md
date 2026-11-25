# T8.4c grading rubric + pre-committed decision criteria

Frozen before any run. 35 questions (`t84c_35_questions_frozen.json`):
30 multi-span pairs with fresh anchors (none reused from T8.4 or
T8.4b's question *pairs* -- a handful of individual anchor spans do
repeat across more than one of these 30 pairs, by design, to get 30
distinct pairings out of a pool of real facts without needing 60
entirely separate topics), plus 5 single-fact controls (new anchors)
to confirm tiering doesn't cost accuracy on the easy case.

## Scoring

- Multi-span question: 0/0.5/1.0 (fraction of 2 facts covered), same
  binary covered/not-covered bar as T8.4b's addendum.
- Single-fact control: 0/1.0 (correct/partial counts 1, wrong/
  hallucinated counts 0) -- same bar as the original T8.4 rubric.
- A harness artifact (budget exhaustion forcing a retry message
  instead of a real answer, as happened once in T8.4b) is excluded
  from both conditions' denominators, not scored as 0 -- noted
  separately in the report regardless.

## Two independent graders (per review requirement)

1. **Primary grader**: the agent, blind (shuffled, condition-stripped,
   same mechanism as T8.4/T8.4b -- `t84c_shuffle_key_SEALED.json` not
   consulted until every verdict is assigned).
2. **Second grader**: an independent subagent, given ONLY the blind
   grading input file (shuffled answers + ground-truth content, no
   condition labels, no access to the primary grader's verdicts) and
   this same rubric. Grades all 35*2 (minus any excluded) items
   independently. Report both graders' per-question verdicts side by
   side; where they disagree, note it rather than silently picking one.

## Pre-committed decision criteria (set before seeing a single answer)

The default (`tiered_injection: false`) flips to `true` only if **all**
of the following hold on this round's own numbers:

1. **Both graders' deltas are positive** (on > off), not just the
   primary's -- a result that only one grader sees isn't a result.
2. **The win/loss count favors "on"**: among multi-span questions
   where the two conditions' scores differ at all (ties excluded),
   "on" wins more often than "off" -- not just a higher mean score
   driven by a couple of large swings.
3. **Token cost per correct-answer-point is not worse than T8.4b's**:
   compute `tokens_per_call / mean_score` for both conditions; "on"'s
   ratio must not be meaningfully worse than "off"'s (a gain that
   costs proportionally far more tokens per point of accuracy is not
   a clean win, even if the raw accuracy number is higher).
4. **The single-fact controls don't regress**: "on"'s mean score on
   the 5 single-fact questions must be >= "off"'s, confirming tiering
   doesn't cost anything on the easy case it isn't meant to help.

If any of the 4 fails, `tiered_injection` stays `false` and this
section records which criterion failed and why, cited to this round's
own report JSON -- same discipline as every other number in this spec.
This decision is NOT the agent's to make unilaterally even if all 4
pass -- report the result and let Ray decide, per his explicit
instruction not to flip the default without his say.
