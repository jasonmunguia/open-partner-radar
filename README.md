# Partner Radar

Daily pipeline that reads robotics news across the open web and judges every item with an LLM
against one criterion — **does this shorten the path to a live deployment?** — then emails a
digest where each finding carries two links: the source it was found in, and the company's own
site.

Built by [Jason Munguia](https://github.com/jasonmunguia). Apache-2.0 — free to use, fork and build on, **with credit**. See [License and attribution](#license-and-attribution) for what that means in practice.

Discovery is **news-first by design**. Accelerator directories describe a company as it was at
*application time* and go stale; news describes what it just did. Directories are demoted to
enrichment (batch, headcount, founders) and never used as evidence of what a company currently
does. See ARCHITECTURE.md "v3 — news-first" for why the earlier directory-first build failed.

**Who this is for.** A Synphony teammate — or anyone with their own keys — who wants this exact
pipeline running for themselves. Bring your own Gmail app password (delivery), your own `claude`
CLI (the judge), and optionally your own YC access. The configs ship as working files scrubbed
of three things: the partner list, the do-not-contact list, and commercial terms. The method
and the thesis are intact, so a cold clone scores real companies into real tiers on the first run.

**Read in this order:** this file (setup, dependencies, failure signatures) →
`ARCHITECTURE.md` (design + the 11 principles, each traced to a real production failure) →
`config/*.yaml` (all tunable behaviour). Bringing a **human operator** online: `ONBOARDING.md`.

## First run (a teammate, from a cold clone)

```
python3 -m pip install -r requirements.txt
for f in config/*.yaml.example; do cp "$f" "${f%.example}"; done   # working configs; run as-is
cp skill/SKILL.md.example skill/SKILL.md                            # the judge's procedure
python3 -m pytest tests/ -q                                         # expect: all pass
python3 -m radar.run ingest-a16z                                    # keyless public API -> data/raw/a16z
python3 -m radar.run news                                           # keyless RSS/HN -> data/news/queue.jsonl
python3 -m radar.run score                                          # rubric prefilter over data/raw -> derived/scored.jsonl
python3 -m radar.run report                                         # tier counts — expect CANDIDATE_* rows, not all T6_PASS
python3 -m radar.digest --dry                                       # prints HTML, sends nothing
```

Nothing above needs an API key or an account. Two things to know about the order: `score`
reads `data/raw/` (a16z + YC shards), **not** the news queue, so `ingest-a16z` must run before
it; and `news` feeds the LLM judge (stage 4) and the digest directly, bypassing the scorer.
Everything past this block — sending mail, the LLM judge — needs the dependencies below.

## How it runs

launchd fires `run_daily.sh` daily (plist: `~/Library/LaunchAgents/com.youruser.partner-radar.plist`,
not in this repo — full contents reproduced in ONBOARDING.md step 6). Stages, in the order
`run_daily.sh` actually runs them:

1. **Enrich** (`python3 -m radar.run ingest-a16z`) — a16z/speedrun public API → `data/raw/a16z`.
   The YC lane (`ingest-yc`) is **retired**: `config/sources.yaml` ships it `enabled: false`
   because it rode one person's Bookface session (see "The account rule"). Flip it on only
   with your own `yc` access.
2. **Discover** (`python3 -m radar.run news`) — keyless RSS (Google News, HN, The Robot Report,
   IEEE Spectrum) → `data/news/queue.jsonl`. This is the primary lane.
3. **Score** (`python3 -m radar.run score`) — keyword prefilter over `data/raw/`,
   `config/rubric.yaml` → `data/derived/scored.jsonl`. Always reruns; rubric edits take
   effect with zero migration.
4. **Judge** — headless `claude -p` invoking the `partner-radar` skill over the news queue:
   reads each article and the company's own site, appends to `data/news/judged.jsonl`, and
   grows `config/discovered_queries.yaml` so tomorrow's fetch covers what today missed.
   Skipped with a WARN if `claude` is missing; the digest then ships prefilter rows.
5. **Leads + signals** (`radar.run leads`, `radar.run signals`) — co-mentioned companies the
   preset queries never reach, and signal-decay tracking.
6. **Digest** (`python3 -m radar.digest`) — builds HTML, sends via `tools/mailer.py` (SMTP,
   Gmail app password from the macOS Keychain).

Manual commands (run from repo root — `python3 -m` needs cwd here):

```
python3 -m radar.run ingest-a16z  # keyless enrichment shards -> data/raw/a16z
python3 -m radar.run news       # keyless discovery -> data/news/queue.jsonl
python3 -m radar.run ingest     # both tier-A lanes incl. YC (exit 2 on shard failure)
python3 -m radar.run score      # rebuild derived/scored.jsonl from raw/
python3 -m radar.run report     # tier counts + top candidates, stdout only
python3 -m radar.digest --dry   # print digest HTML, send nothing (works without mailer creds)
python3 -m radar.digest         # build + send for real
./run_daily.sh                  # the whole nightly sequence, exactly as launchd runs it
```

## Dependencies — obtain every one from here

| Dependency | Where it lives | How to get it | What breaks without it |
|---|---|---|---|
| Python 3.11 + PyYAML + certifi | `/Library/Frameworks/Python.framework/Versions/3.11/bin/python3` (falls back to `command -v python3`) | python.org installer, then `python3 -m pip install -r requirements.txt` | Everything. Stock macOS python3 has no CA bundle — `run_daily.sh` exports `SSL_CERT_FILE` from certifi to fix TLS for the a16z fetch and SMTP |
| `yc` CLI (official YC/Bookface CLI) — **optional, lane retired** | `~/.local/bin/yc` | `curl -fsSL https://bookface.ycombinator.com/cli/install.sh \| bash`, then `yc login --device` **as teammate@example.com** (see Account rule) | Nothing, as shipped: `yc` is `enabled: false`, and the two other things that ride this binary (`launches` posts, the Exa web sweep) fail soft into the digest's "Degraded" footer. Enable only with your own access |
| Exa (semantic web search) | No separate install or API key | Reached through `yc tools run web` — it is a yc CLI tool, so it lives and dies with the yc lane. There is no `EXA_API_KEY` anywhere in this code | `web_semantic` reports a failure in the digest footer; the news lane covers the same launches through indexed outlets |
| `claude` CLI (Claude Code) | `~/.local/bin/claude` | https://claude.com/claude-code | Stage-2 rerank skipped; digest sends prefilter candidates unreranked (weaker sort, no angle line) — by design, not a crash |
| `partner-radar` skill | `skill/SKILL.md.example` in this repo | `cp skill/SKILL.md.example skill/SKILL.md`, fill the four `<<< >>>` thesis blocks, install at `~/.claude/skills/partner-radar/SKILL.md` | The headless rerank prompt says "Use the partner-radar skill"; with no skill the pass has no procedure and produces meaningless tiers |
| `tools/mailer.py` (SMTP sender) | in this repo | `python3 tools/mailer.py add <name> <address>`, paste a Gmail app password (create one at https://myaccount.google.com/apppasswords — needs 2-step verification on; stored in the macOS Keychain, never a file), then `python3 tools/mailer.py test <name> <recipient>` | `radar.digest` exits at send time naming the exact `add` command to run. `--dry` works without it |
| launchd plist | `~/Library/LaunchAgents/com.youruser.partner-radar.plist` | Not in this repo — template + install commands in ONBOARDING.md | No schedule; pipeline only runs when invoked by hand |

## The account rule (do not improvise here)

The `yc` CLI must authenticate as **the authorized account holder (teammate@example.com)** — Synphony's
co-founder, who authorized it. Not the operator, not any new operator. Two reasons, both standing:

1. Every query lands on that Bookface account's activity trail — Synphony-lens work only.
   Schematic-lens prospecting through this account reopens compartmentalization the operator
   deliberately closed in July 2026.
2. **A different account may not have `tools run` access at all.** Verified 2026-08-09:
   authenticated as the operator (JFM05), every `yc tools run` call returns 403 while `yc me`
   succeeds. Logging in as the wrong person doesn't degrade the pipeline — it kills it,
   quietly (next section).

## Failure signatures — how to recognize a dead run

### YC auth failure (live incident, 2026-08-09)

The whole signature, each part verifiable in one command:

- `yc tools run search.companies --input '{"limit":0,"group_counts_by":"batch"}' --json`
  prints `Tool run failed (403): forbidden` — **and exits 0**. Never trust the yc exit code;
  trust the presence of JSON in stdout (the code does: `YCError: no JSON in yc output`).
- `yc me` **succeeds** but returns the wrong identity — `the operator (JFM05)` instead of
  the authorized account holder. Auth is present, authorization is not. `~/.yc/credentials.json` mtime shows
  when the account switched (2026-08-08 21:54 in the live incident).
- `data/raw/yc/*.jsonl` mtimes frozen past the 60h ingest gate (stuck at 2026-08-05 07:15
  in the live incident) — a run was due and never landed.
- **`data/health.json` still reads green** (`ingest_yc: failures 0, degraded false`). This is
  the trap: the 403 makes `batch_counts()` raise before `_publish_health` ever runs, the
  traceback dies in run_daily.sh's `|| log "WARN ingest returned $?"`, and health keeps the
  last *successful* run's entry. Green health + stale `last_run` epoch + stale shard mtimes
  = dead ingest. Cross-check timestamps; never read the `degraded` flag alone.
- `digest.failures` in health.json jumps to **22** = every yc-backed query failing
  (2 entities × 7 post queries + 8 web queries). 22 is the "yc is dead" number, not flaky
  network. The digest email shows the same thing as a red "Degraded this run" footer.

**Fix:** `yc login --device` as `teammate@example.com` → `yc me` must return the account holder → retry the
`group_counts_by` probe above (real JSON = healed) → `python3 -m radar.run ingest`.

### Other checks

- `/tmp/partner-radar.log` — full nightly transcript. Grep `WARN` — every swallowed
  non-zero exit lands there and only there.
- `/tmp/partner-radar-rerank.log` — the headless claude pass output.
- `health.json` features: `ingest_yc`, `ingest_a16z`, `score`, `digest`. `degraded` = zero
  output twice running (after ever producing) or any counted failure. Remember the blind
  spot above: a crash *before* the publish leaves stale green.

## Operator coupling — hardcoded paths and accounts

Everything here breaks or misroutes for a second operator until changed (walkthrough in
ONBOARDING.md):

- `config/sources.yaml` → `delivery.to` (recipient) and `delivery.sender_account` (the name you
  registered with `tools/mailer.py add`). As shipped these are the original operator's
  (`to: you@example.com`, `sender_account: personal` = you.personal@example.com) — change both.
  Sender and recipient must differ or Gmail buries the thread as a self-send.
- `radar/digest.py` → exits with the fix named if either delivery key is missing (no silent
  fallback address); imports mailer from `tools/` in this repo, then `~/.claude/tools`.
- `run_daily.sh` + plist → absolute paths: framework Python, `~/.local/bin/claude`,
  `/Users/youruser/Desktop/partner-radar`, `/tmp/partner-radar*.log`.
- Stage-2 rerank files findings to the operator's Obsidian vault (`~/Desktop/the operator OS + Memory`)
  via the skill — machine-specific.
- yc CLI account: the account holder's, per the account rule. Not swappable per-operator.

## Known doc-vs-code deltas (documented, deliberately unfixed in code)

- **The digest never suppresses.** ARCHITECTURE.md's "suppresses empty sends" and
  `suppress_empty_digest: true` describe the original design; `radar/digest.py` instead
  sends a "quiet day" email with a standing shortlist so the channel stays warm. Code wins.
- `sources.yaml` keys `digest_hour_local`, `suppress_empty_digest`, `instant_alert_tiers`,
  and the whole `retention:` block are **read by nothing yet**. Schedule truth is the plist
  (09:15); instant T1 alerts and retention pruning are unbuilt.
- ARCHITECTURE.md's cadence block lists `founders_inc`, `product_hunt`, `newsletters`,
  `x_robotics` — designed, never built. Built sources are exactly `config/sources.yaml`:
  `yc`, `a16z_speedrun`, `web_semantic`, `launch_yc_public` (inline in digest.py), and
  `x_semantic` (disabled — Exa cannot see X; see the comment in sources.yaml).
- **There is no `radar/rerank.py`.** Stage 2 is the headless claude pass in `run_daily.sh`
  step 3.
- **Rerank dedup defect (open):** the `run_daily.sh` claude prompt dedups against
  `reranked.jsonl` only. It must also skip `rerank_skipped.jsonl` — nightly
  passes have been re-reading already-adjudicated skips at full cost (measured 29/30 on
  2026-08-09). Fix belongs in the prompt string; left for a deliberate code change.

## License and attribution

Apache License 2.0 — **open for anyone to use, fork, and build on, with credit.**

Apache-2.0 was chosen over MIT deliberately. Both require that copies carry the copyright
notice, but Apache adds a `NOTICE` file whose contents must be reproduced **in the
documentation or display of any derivative work** (Section 4(d)) — which is the closest a
software licence gets to "credit me visibly" rather than "bury my name in a LICENSE file
nobody opens." It also grants patent rights explicitly, which protects both the author and
anyone shipping on top of this.

If you use this, keep `NOTICE` intact. Attribution is the only thing asked in return.

Original author: **Jason Munguia** — https://github.com/jasonmunguia/open-partner-radar
