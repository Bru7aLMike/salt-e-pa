---
name: readable-tokens-over-shorthand
description: Prefer readable key/value tokens over cryptic shorthand a stranger cannot decode.
type: output-discipline
---

# Readable tokens over shorthand

When you compress information into a token, keep it human-readable. Prefer forms
like "key=value" or "name times count" over cryptic abbreviations that only make
sense to whoever wrote them.

- Why: shorthand saves a few characters now and costs far more later, when a
  future session or another person has to reverse-engineer what it meant. Clear
  tokens stay legible without the original context.
- How to apply: spell out the label, use an explicit separator, and avoid
  invented abbreviations. If a reader would need a legend to decode it, expand
  it instead.
