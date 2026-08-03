# Learnings

Append-only, newest first. `ARCHITECTURE.md` holds the seven design principles
and the failure behind each. This file holds everything since.

---

## 2026-08-03 — Depersonalizing surfaced borrowed-credential detail

The private version documented that the accelerator CLI authenticates as a
colleague's member account, and that prospecting for a second venture must not
run through that account's activity trail.

Both facts are operationally correct and neither belongs in a public repo. But
deleting them outright would have thrown away a real rule, so they were rewritten
as the general principle: when the account is not yours, volume is a shared
*reputation* cost rather than a rate limit, and you run one purpose per account
because anyone reading that trail can otherwise reconstruct both.

**The pattern:** the sensitive thing and the valuable thing are usually the same
sentence at different altitudes. Raise the altitude instead of cutting.

## 2026-08-03 — Two axes, never blended, and why competitor detection is negative

The tempting design is one "fit" score. It fails in a specific and expensive way:
a company building what you build matches on every capability signal, so a single
blended score ranks your closest future competitor first and calls it your best
partner.

Separating strategic value (S) from internalizability (I) is what makes the
output readable, and giving competitor detection a negative weight large enough
to override every positive signal is what stops the ranking from inverting.

A low I score is **not** a bad score. It means a real moat, which makes them a
better long-term partner and a worse acquisition target. Anyone tuning this
rubric will want to "fix" low-I companies. Don't.

## 2026-08-03 — The digest suppresses empty sends

A daily email that is usually empty trains you to stop opening it. Then you miss
the one that isn't, which is the only one that mattered.

Cheap to implement, easy to skip, and the failure it prevents is invisible until
it has already happened.
