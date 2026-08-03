# Partner Radar — architecture

Finds companies across accelerators and launch channels that your company can (a) partner with,
(b) buy from, (c) sell to, or (d) internalize. Built 2026-07-29.

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

The digest runs daily and **suppresses empty sends**. Tier A results appear in whichever digest
follows their poll. T1 hits alert immediately, out of band.

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

## Scoring — five axes, your company lens

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
  Microfactory at $18,000 against a $24,950 list price, papered hardware-only.

**Competitor separation is a hard gate, not a score.** A large share of YC robotics companies
*do what your company does* — deploy arms into industrial settings. Those are `T5 WATCH`, never
contacted. Confusing "sells me a component" with "competes for my customer" is the single
most expensive classification error available here.

### Tiers

| Tier | Meaning | Rule of thumb |
|---|---|---|
| `T1_PARTNER_NOW` | High `S`, high `L`, buy/partner immediately | component or model we need, favorable terms available |
| `T2_ABSORB_TRACK` | High `S`, high `I` | partner, deploy, internalize on a 2–3 quarter clock |
| `T3_CUSTOMER` | High `D` | route to the your company ICP pipeline |
| `T4_CHANNEL` | High `C` | borrowed distribution |
| `T5_WATCH` | Competitor | monitor, never contact |
| `T6_PASS` | — | no fit |
| `T0_EXISTING` | Already a partner/relationship | suppressed from alerts, never re-surfaced |

## Exclusions

`config/exclusions.yaml` holds three lists, all checked before alerting:

1. **existing** — current partners and relationships (Generalist AI, Dyna Robotics,
   Microfactory, your company itself). Classified `T0_EXISTING`, never alerted as new.
2. **do_not_contact** — competitors we deliberately study but never approach (the Cyntronic
   rule). Alerting one of these a partnership request is an unforced credibility error.
3. **crm_seen** — seeded read-only from the Notion CRM so the radar never re-surfaces a
   company already in the pipeline. Same discipline as the your company lead-gen run, which
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

## Account attribution, when the account is not yours

Accelerator networks (YC's Bookface, and most private founder communities) have
no public API. Access runs through a member account, which for many operators
means an account belonging to a colleague or co-founder rather than to them.

If that describes you, two rules are not optional:

1. **Volume is a shared-reputation cost, not a rate limit.** Every query is
   attributed to the account holder, not to you. This is the actual reason the
   batch-source cadence is ~60 hours rather than hourly — the universe only
   changes a few times a year, so polling harder buys nothing and spends someone
   else's standing.

2. **One purpose per account.** Do not run prospecting for two different ventures
   through the same member's activity trail. Anyone who can see that trail can
   reconstruct what both are working on, and the account holder did not consent
   to being the join key between them.

Personal-data endpoints — candidate directories, member profiles — are excluded
outright. Those exist for hiring, and mining them for company research is both
outside what the account was granted for and the fastest way to lose it.

