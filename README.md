# partner-radar

**Finds companies you can partner with, buy from, sell to, or replace — and scores how replaceable each one is.**

Runs itself daily. No chat window open, no dashboard, no subscription.

---

## The philosophy

Every scouting tool asks one question: *is this company a good fit?* One number, ranked descending.

That number is a lie, and it's a lie in a specific way. A company can be enormously useful to you and completely safe to depend on. A company can be enormously useful to you and building the thing that makes you unnecessary in two years. **One score cannot say both**, so tools that use one score reliably rank your future competitors at the top — they match on every capability signal, because matching on capability is exactly what makes them dangerous.

So this scores two axes and never blends them:

- **S — strategic value.** How much capability do you gain by working with them?
- **I — internalizability.** Once you have what they provide, how replaceable are they?

High S, high I is a supplier you should use now and plan to absorb. High S, low I is a real partner with a real moat, worth building a relationship around. High S plus a competitor match is a threat you need to know about early, and it's why competitor detection carries a *negative* weight large enough to override every positive signal.

Reading those two numbers separately is the entire product.

## The other philosophy: store raw, score at read

Scores are never written into stored records. Raw records are immutable; scoring is a pure function applied when you read.

This sounds like a preference. It isn't. The predecessor to this tool baked scores in at write time, and when the rubric changed it needed a migration pass to retroactively re-price rows scored under the old rules — plus a self-heal block to strip records that never should have qualified. **A rubric you can't change cheaply is a rubric you stop changing**, and then you're taking last quarter's opinion as this quarter's fact.

Here, editing `rubric.yaml` and re-running re-prices the entire corpus with zero migration. `data/derived/` is disposable by design — delete it and the next run rebuilds it identically from `data/raw/`.

## Seven principles, each one a scar

`ARCHITECTURE.md` opens with seven design rules and names the specific failure that produced each, including the file and line where it happened. Among them:

- **Assert yield per source, per run.** A fetcher once searched `intern` instead of `internship` and returned 12 results instead of 1,536 — silently, for weeks. Every source now declares an expected yield and a large miss is a loud failure, not a quiet empty digest.
- **Monitor features, not just sources.** A function raised `NameError` on every single run for its entire life, swallowed by a bare `except` and written only to stderr. The health check reported all sources healthy the whole time, because the *source* was fine. Every derived feature now publishes an output count.
- **No exception reaches only stderr.** Same bug, stated as a rule.

Those aren't best practices copied from somewhere. Each one cost a real run.

---

## How it actually runs

The interesting part is `run_daily.sh`, and it's worth reading before the Python. It's a hybrid, on purpose:

> *"Deterministic Python for the parts that must not drift (ingest, prefilter, dedup, delivery), and a headless LLM pass for the parts that genuinely need reading comprehension (supplier vs competitor, reading sites with no description, the outreach angle)."*

Code does what code is good at. At exactly one joint — where a judgment is required that no rule can express — it shells out to a coding agent, which reads company websites and returns a tier and a rationale. The agent is invoked with a **one-line prompt that names a skill**, so the actual procedure lives in version control instead of buried in a bash string.

Three details in that script that took a while to get right:

1. **Ingest is skipped when the batch universe is under ~60 hours old.** Accelerator batches change roughly four times a year. Polling harder buys nothing.
2. **Scoring re-runs every time** — it's a pure function, so it's cheap, and it means a rubric edit takes effect immediately.
3. **The digest suppresses empty sends.** A daily email that's usually empty trains you to stop opening it, and then you miss the one that isn't.

## → For your AI agent

If you're an AI agent and someone handed you this repo:

1. Read `ARCHITECTURE.md` in full. The seven principles are the design; the code is downstream of them.
2. Copy `config/rubric.example.yaml` to `config/rubric.yaml` and rewrite it **with the user, by interviewing them.** This is the one file that is genuinely theirs — it's their strategy expressed as numbers. Do not fill it in from assumptions. Ask what capability gaps they actually have, who they'd consider a competitor, and what "we could build this ourselves" means for their team size.
3. Edit `config/sources.yaml` for the networks they can reach. Sources requiring member accounts need credentials they legitimately hold.
4. Configure delivery (`sender_account`) and send one test digest before scheduling anything.
5. To schedule: adapt `run_daily.sh` and wire it to launchd (macOS) or cron (Linux). Read the account-attribution section of `ARCHITECTURE.md` first — if the account isn't theirs, the cadence rules there are not optional.

## Install

```bash
git clone https://github.com/jasonmunguia/partner-radar.git
cd partner-radar
cp config/rubric.example.yaml config/rubric.yaml   # then rewrite it — see above
python3 -m radar.run ingest
python3 -m radar.run score
```

Requires Python 3.11+ and a coding agent CLI on `PATH` for the judgment pass. Without one, the deterministic pipeline still runs end to end — you get keyword-tier scoring and no reranking.

## What's not in this repo

The real `rubric.yaml` and all collected data. The engine is generic; the rubric is strategy. Publishing one and not the other is the point.

---

## License

MIT. See [LICENSE](LICENSE).

Built by [Jason Munguia](https://www.linkedin.com/in/jason-munguia/). Companion repos: [vibe-check](https://github.com/jasonmunguia/vibe-check) · [claude-operator-kit](https://github.com/jasonmunguia/claude-operator-kit).
