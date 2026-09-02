# Learnings

Append-only, newest first. `ARCHITECTURE.md` holds the 11 design principles
and the failure behind each. This file holds everything since.

---

## 2026-09-01 — A green pipeline with an empty product

A fresh-clone audit of the public edition followed the README exactly and got exit 0 at
every step — while the scorer classified 312 of 312 companies as no-fit and queued zero
for review. The hand-written `rubric.yaml.example` carried none of the six keys
`score.py` reads. Nothing errored, because absent keys default to empty and empty
scores to zero. **A leak audit proves nothing secret escaped; it says nothing about
whether what shipped works.** The example configs are now the real files, scrubbed, and
`tests/test_cold_clone.py` scores a perfect-fit row and fails if it lands on PASS.

Same audit, second lesson: a personal email address shipped in cleartext because it
shared no substring with any token on the scrub list — and the audit list was the
same list, so both agreed it was clean. A scrub and an audit that share a blind spot
always agree with each other. The fix is not one more token; it is a test that greps the
*exported tree* for the shapes of things (`@gmail.com`, `/Users/<name>`), not the
names you already thought of.

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
