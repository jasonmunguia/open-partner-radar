# Partner Radar — architecture

Finds companies across accelerators and launch channels that Synphony can (a) partner with,
(b) buy from, (c) sell to, or (d) internalize. Built 2026-07-29.

> Setup, the dependency manifest, and the failure signatures live in `README.md` — start
> there cold. Bringing a human operator online: `ONBOARDING.md`. This file is design only.

## Why this exists / what it is not

This is **not** the internship radar with different keywords. The internship radar answers
"is this role a fit for the operator." This answers a two-sided question: *is this company a source
of capability we can orchestrate, and how replaceable is it once we have?* Those are different
scores and they are deliberately kept separate (see `S` and `I` below).

## The seven principles (each one is a scar from internship-radar)

| # | Principle | The failure it prevents |
|---|---|---|
| 1 | **Store raw, score at read** | `poll.py:225` needed a `self-heal` block to retroactively strip rows scored before the US gate existed. Baked-in scores can't be re-priced. Here, raw records are immutable and scoring is a pure function applied on read — a rubric change re-prices everything with zero migration. |
| 2 | **Assert yield per source per run** | Amazon fetcher searched `intern` not `internship`: 12 results instead of 1,536, silently, for weeks. Every source declares an expected yield; a large miss is a loud failure, not a quiet empty digest. YC gives ground truth free via `group_counts_by`. |
| 3 | **Monitor features, not just sources** | `refresh_funded()` in poll.py raised `NameError: re` on every run (module never imports `re`), swallowed by `except Exception` → stderr. `funded_watch.json` has contained only `{"_last_run": ...}` for its entire life. The SEC Form D promotion never fired once, while `heartbeat.json` reported `dark_sources: []`. Every derived feature must publish an output count. |
| 4 | **No exception reaches only stderr** | Same bug. A caught exception increments a named counter that the heartbeat exposes and the digest footer prints. |
| 5 | **Dedup on canonical entity identity** | `af48586` had to retrofit "alert once per role, not once per board." The same company will appear in YC *and* a16z *and* X. Key = YC `id` where available, else normalized registrable domain. Alerted once, ever. |
| 6 | **Delivery decided on day one: SMTP, explicit named account** | Six separate delivery rewrites (Composio self-send burial, GitHub not emailing self-created issues, sender drift, employer-mailbox guard). Use the existing `mailer` skill path: SMTP + Keychain app password, sender named explicitly. |
| 7 | **Bounded state, and git is not the database** | ~20 of 50 commits are `state: <ts> [skip ci]`; mirror-semantics self-healing sync was needed for AHEAD/BEHIND divergence between the cloud and local writers. Retention is declared at design time; one writer owns each file. |

## Cadence is per-source, not global

```
Tier A — batch sources     poll every ~60h   universe changes 4x/year
  yc, a16z_speedrun, founders_inc

Tier B — continuous        poll daily        launches land any day
  x_robotics, launch_yc_public, product_hunt, newsletters
```

Design vs. built: `founders_inc`, `product_hunt`, and `newsletters` are designed, not built;
`x_robotics` is built but disabled (Exa cannot see X — see `config/sources.yaml`). The
running set is exactly what `config/sources.yaml` enables: `yc`, `a16z_speedrun`,
`web_semantic`, and `launch_yc_public` (inline in `radar/digest.py`).

The digest runs daily. Original design suppressed empty sends; the shipped `radar/digest.py`
instead sends a "quiet day" email carrying a standing shortlist, so the channel never trains
you to ignore it — code wins over the `suppress_empty_digest` key, which nothing reads.
Tier A results appear in whichever digest follows their poll. Instant out-of-band T1 alerts
are designed (`instant_alert_tiers`), not yet built.

## Data flow

```
  adapters/            raw/                    derived/              delivery
  ─────────            ────                    ───────               ────────
  ingest_yc     ──▶  raw/yc/<batch>.jsonl  ──▶  scored.jsonl   ──▶  digest email
  ingest_a16z   ──▶  raw/a16z/...          ──▶  (pure function)     instant T1 alert
  ingest_x      ──▶  raw/x/...                  entities.json
                                                (canonical dedup)
```

`raw/` is append-or-replace-whole-shard and never edited by the scorer. `derived/` is
disposable — delete it and the next run rebuilds it identically from `raw/`.

## Scoring — five axes, Synphony lens

Deliberately five separate scores, not one blended number, because they answer different
questions and a high `S` with a high `I` is a completely different action than a high `S`
with a low `I`.

- **S — Supply fit.** Do they make something we can put in the stack? Arms, grippers,
  end-effectors, actuators, VLA/foundation models, imitation learning, teleop, sim2real,
  edge inference, 3D vision, tactile/force-torque sensing, motion planning, ROS tooling.
- **D — Demand fit.** Do they *run* physical operations we could sell a cell into?
  Contract manufacturing, food processing, fulfillment/3PL, e-waste, recycling.
- **C — Channel fit.** Do they already sell to plant floors? MES, manufacturing ERP,
  QMS/CMMS, supply-chain execution, system integrators.
- **I — Internalizability.** How hard to rebuild in-house. HIGH = thin app layer, small team,
  no proprietary data. LOW = own weights trained on captured data, custom silicon, novel
  hardware. **Inverse of durability**: high `I` means the dependency is cheap to remove later;
  low `I` means partner properly or pool.
- **L — Leverage asymmetry.** Their stage vs. ours. Small team + recent batch + active =
  they need a deployed design-partner logo more than we need them. This is what produced
  Example Partner C at $18,000 against a $24,950 list price, papered hardware-only.

**Competitor separation is a hard gate, not a score.** A large share of YC robotics companies
*do what Synphony does* — deploy arms into industrial settings. Those are `T5 WATCH`, never
contacted. Confusing "sells me a component" with "competes for my customer" is the single
most expensive classification error available here.

### Tiers

| Tier | Meaning | Rule of thumb |
|---|---|---|
| `T1_PARTNER_NOW` | High `S`, high `L`, buy/partner immediately | component or model we need, favorable terms available |
| `T2_ABSORB_TRACK` | High `S`, high `I` | partner, deploy, internalize on a 2–3 quarter clock |
| `T3_CUSTOMER` | High `D` | route to the Synphony ICP pipeline |
| `T4_CHANNEL` | High `C` | borrowed distribution |
| `T5_WATCH` | Competitor | monitor, never contact |
| `T6_PASS` | — | no fit |
| `T0_EXISTING` | Already a partner/relationship | suppressed from alerts, never re-surfaced |

## Exclusions

`config/exclusions.yaml` holds three lists, all checked before alerting:

1. **existing** — current partners and relationships (Example Partner A, Example Partner B,
   Example Partner C, Synphony itself). Classified `T0_EXISTING`, never alerted as new.
2. **do_not_contact** — competitors we deliberately study but never approach (the Example Competitor B
   rule). Alerting one of these a partnership request is an unforced credibility error.
3. **crm_seen** — seeded read-only from the Notion CRM so the radar never re-surfaces a
   company already in the pipeline. Same discipline as the Synphony lead-gen run, which
   excluded 109 contacts + 89 companies this way.

## Access notes (verified 2026-07-29)

- YC data comes through the official `yc` CLI → `yc tools run search.companies`.
- **`attach_csv` is unusable from the CLI**: it returns only metadata (`file_name`,
  `row_count`, `columns`) and writes no file. Real records arrive as an inline CSV *string*
  in `result.csv_results` on a normal search.
- `limit` max 200, `page` 0-indexed. Sharding by batch keeps every shard ≤ 2 pages, so
  Algolia deep-pagination limits are never reached.
- **`latest_round_raised` is 98% empty** on recent batches (16 populated out of 880 for
  S25–S26). Unusable as a filter. `team_size` + batch recency carry `L` instead.
- `tags` (e.g. `"Hard Tech, Drones, Aerospace"`) classifies far better than the coarse
  `industry` facet and is the primary scoring input.
- Batch names are short form: `W25`, `P25`, `S25`, `F25`, `P26`. Four batches per year.
- CSV quirks to handle: URLs carry a leading `'` (spreadsheet-safety prefix) and empty
  repeated fields render as `""""`.

## Account attribution

The CLI authenticates as **the authorized account holder** (`teammate@example.com`), Synphony's co-founder —
not the operator. Every query is attributed to his account. A third consequence, learned 2026-08-09:
authenticating as anyone else can revoke `tools run` outright — every call 403s while
`yc me` still succeeds, killing all three yc-riding lanes (ingest, posts, Exa web) at once
while `health.json` stays green. Full signature and fix: README.md "Failure signatures".
Two standing consequences:

1. Volume is a shared-reputation cost. Tier A's ~60h cadence is deliberately modest.
2. **Synphony lens only on this account.** Schematic-lens prospecting must not run through
   the account holder's Bookface activity trail — that reopens the compartmentalization the operator closed in
   July from a new direction. `candidates` (the personal-data candidate index) is excluded
   from the pipeline entirely; it adds nothing to partner prospecting.

---

# v3 — news-first, source-decoupled (2026-08-15)

## Prior art: what was mined, taken, and rejected

Done *after* v1 shipped, which was the mistake. Recorded here so it is never skipped again.

| Tool | What it does | Verdict |
|---|---|---|
| [gitdealflow](https://signals.gitdealflow.com/) | 4 GitHub signals — contributor growth >50%, 3+ new repos/30d, commit velocity +150%, framework migration. 369 signals, 15 sectors incl. Robotics | **Took the idea**: rank on momentum, not description. Not the implementation |
| [Harmonic.ai](https://harmonic.ai/) | 35M companies, cohort-based **daily refresh**; Scout: brief → companies + warmest intro path + draft outreach | **Took**: the output shape (event + why + next action). Rejected the platform — enterprise priced, generic thesis |
| Specter | Cross-channel growth signals (web traffic, hiring, product) | **Rejected the hiring half** — the operator ruled ATS signals out explicitly |
| Tracxn / Dealroom / Crunchbase | Company databases, free tiers | **Rejected as primary** — directories, the exact failure mode of v1 |
| internship-radar (own) | 139 ATS sources, `seen.json` delta feed | **Took the delta-feed architecture. Rejected the sources.** |

**The lesson the whole industry already knew:** entities are the substrate, *signals* are the
product. Nobody sells a static directory. v1 built one.

## Why v1's email was useless

Two independent causes, both mine.

1. **Catalogue, not feed.** internship-radar works because *jobs are events* — they appear,
   alert once, and are gone. partner-radar failed because *companies are entities* that sit
   unchanged, so re-scoring static text daily either suppressed everything (dedup) or
   re-dumped the map. Both landed in the operator's inbox.
2. **Directories describe application-time, not now.** Synphony's own Bookface entry still
   reads "robots for strawberries," months out of date. Ranking on that text cannot work.

Fix: **news discovers, directories enrich.** Never the reverse.

## Principles added in v3

8. **No source's schedule, health, or failure may depend on another source's artifact.**
   `run_daily.sh` gated *all* ingest on a YC shard's age. When YC's auth died, a16z — no
   auth, working perfectly — sat 274h stale behind a dead lane's freshness check. Each
   source now gates on its own artifact via `stale_gate`, with per-source entry points
   (`ingest-yc`, `ingest-a16z`).

9. **The discovery lane may not depend on credentials that belong to another person.**
   YC/Bookface auth was the account holder's; it expired and only he could refresh it. All four discovery
   feeds are now keyless (Google News RSS, HN Algolia, The Robot Report, IEEE Spectrum).
   Verified: 330 items/sweep, 0 errors.

10. **Retired ≠ broken.** Health flagged the deliberately-disabled YC lane as `STALE`
    forever. A health report that cries wolf is one you stop reading — the exact failure the
    health layer exists to prevent. Disabling a source in `sources.yaml` is now the single
    switch that also silences its alarm.

11. **Corpus-derived stopwords beat hand-maintained lists.** Story dedup on raw Jaccard
    merged *"Figure AI raises funding for humanoid robots"* with *"Apptronik raises funding
    for humanoid robots"* at 0.667 — two companies, silently collapsed. In this corpus
    `robot`/`humanoid`/`funding` carry no story identity; the company name does. Generic
    tokens are now derived by document frequency from the batch itself, so they self-tune
    instead of rotting. Over-merging hides a company (expensive); under-merging shows a
    duplicate (cheap) — both gates are set conservatively toward the cheap failure.

## Current shape

```
discovery (keyless, daily)          judgement (LLM)           delivery
──────────────────────────          ───────────────           ────────
Google News RSS ─┐
HN Algolia       ├─→ news.py ──→ queue.jsonl ──→ judged.jsonl ──→ digest
The Robot Report │   (dedup:                     (tier, I, why,     (source link +
IEEE Spectrum   ─┘    URL + story)                ask, known)        company link)

enrichment (no auth)                state
────────────────────                ─────
a16z API ──→ raw/a16z ──→ score ──→ scored.jsonl · reranked.jsonl (dossier)
```

`yc` is retired but not deleted — 725 rows keep scoring, the dossier stands, and reviving it
is one config flag plus a device login.
