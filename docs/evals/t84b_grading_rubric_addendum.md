# T8.4b grading rubric addendum (10-question multi-span round)

Each question in `t84b_10_multispan_questions_frozen.json` combines TWO
previously-verified real ground-truth anchors (`gt_span_ids: [A, B]`),
each already confirmed correct against the real corpus in the T8.4
round (same spans, reused -- not re-verified blind this time, since
they were already independently grounded then). The question asks for
BOTH facts explicitly ("X, AND Y"), specifically so a single top vector
match can't cover the whole answer and the "off" condition has to
either hold both spans in budget or lose one -- the regime the original
40-question round never hit.

## Per-question scoring (different from the T8.4 rubric -- two facts, not one)

- Score = (facts correctly covered) / 2, so 0.0, 0.5, or 1.0.
- A fact counts as covered if the answer states the actual content of
  that anchor span with no material error -- same correct/partial/
  wrong/hallucinated bar as the T8.4 rubric, collapsed to "covered"
  (correct or clearly-partial-but-substantively-right) vs "not covered"
  (wrong, hallucinated, or simply omitted/denied) per fact.
- Grading is blind to condition (off/on), same mechanism as T8.4:
  shuffle, strip labels, grade, re-attach.

## What this round answers that T8.4 didn't

T8.4's headline (-0.013 delta) was explained as "pointers have nothing
to add when the budget was never actually binding." This round is
designed so each question needs two distinct older facts at once --
if tiering helps, it should show up here as a HIGHER per-fact coverage
rate on the "on" condition, specifically on the second fact (the one
more likely to get budget-starved in "off"). If this round also shows
no gain, that's the stronger result: not just "this question set never
stressed the budget" but "even deliberately stressing the budget
didn't make pointers earn their keep" -- the shelve/keep decision in
Spec 8 section 8.5.1 should name whichever of the two actually happened,
cited to this round's own report JSON.
