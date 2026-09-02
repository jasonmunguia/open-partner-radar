#!/bin/bash
# Daily partner-radar run. Fires ~09:15 so the email is waiting when the operator gets in at 10.
#
# Hybrid by design: deterministic Python for the parts that must not drift (ingest, prefilter,
# dedup, delivery), and a headless Claude pass for the parts that genuinely need reading
# comprehension (supplier vs competitor, reading sites with no description, the outreach angle).
#
# Requires the Mac to be awake. com.youruser.keepawake already runs `caffeinate -i`. If the Mac
# was asleep at 09:15, launchd runs this the moment it wakes.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3
[ -x "$PY" ] || PY=$(command -v python3)
CLAUDE="$HOME/.local/bin/claude"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
# Stock python3 on this Mac has no usable CA bundle. The plist sets this too, but export it
# here so a manual `./run_daily.sh` behaves identically to the scheduled run.
CERTS=/Library/Frameworks/Python.framework/Versions/3.11/lib/python3.11/site-packages/certifi/cacert.pem
[ -f "$CERTS" ] && export SSL_CERT_FILE="$CERTS"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# --- 1. Ingest, but only when the batch universe is actually stale (~60h) ------------------
# Tier A sources only change when a batch drops, 4x/year. Polling harder just spends
# the account holder's Bookface reputation for nothing.
# SOURCE DECOUPLING (2026-08-15). Each source's schedule is gated on ITS OWN artifact.
#
# The previous version gated *all* ingest on the age of a YC shard. When YC's auth died,
# a16z — which needs no auth and was working perfectly — went 274h stale behind a dead
# source's freshness check. A healthy lane must never be blocked by a broken one, and a
# shared gate makes one outage look like total silence.
#
# Rule: no source's schedule, health, or failure may depend on another source's artifact.
stale_gate() {   # $1=artifact  $2=max_age_hours  -> 0 if a refresh is due
  [ -f "$1" ] || return 0
  local age=$(( ( $(date +%s) - $(stat -f %m "$1") ) / 3600 ))
  if [ "$age" -lt "$2" ]; then
    log "  skip $(basename "$1") (${age}h < ${2}h)"
    return 1
  fi
  return 0
}

if stale_gate data/raw/a16z/companies.jsonl 60; then
  log "ingesting a16z…"
  "$PY" -m radar.run ingest-a16z || log "WARN a16z ingest returned $?"
fi

# --- 1b. News: the PRIMARY discovery lane (keyless feeds, no yc dependency) ----------------
# Directories describe a company at application time; news describes what it just did.
# Runs before scoring so the judge always has today's items.
log "fetching news…"
"$PY" -m radar.run news || log "WARN news returned $?"

# --- 2. Prefilter (pure function; cheap, always rerun so rubric edits take effect) ---------
log "scoring…"
"$PY" -m radar.run score || log "WARN score returned $?"

# --- 3. Stage-2 rerank (the operator authorised unattended operation 2026-07-30) ------------------
# The judgment pass — supplier vs competitor, reading sites with no description, the outreach
# angle — needs an LLM with web and file access. The detailed procedure lives in the
# `partner-radar` skill, so this prompt stays a one-liner that invokes it. Tools are scoped
# to what the pass actually needs rather than bypassing permissions wholesale.
if [ -x "$CLAUDE" ]; then
  log "reranking via claude…"
  # One log per day, kept: the single overwritten file meant a failed judge left no evidence
  # by the time anyone asked why the digest was empty.
  RERANK_LOG="/tmp/partner-radar-rerank-$(date +%Y-%m-%d).log"
  JUDGED_BEFORE=0
  [ -f data/news/judged.jsonl ] && JUDGED_BEFORE=$(wc -l < data/news/judged.jsonl | tr -d ' ')
  "$CLAUDE" -p "Use the partner-radar skill and run its Workflow steps 2-6 on today's news. Read data/news/queue.jsonl. Triage every item on title+summary, keeping only those where a specific company did a specific thing touching the thesis; expect to keep 10-20 percent. For each survivor, fetch the article AND the company's own website, then append one record to data/news/judged.jsonl with: company, company_url (their site), source_url, source_publisher, source_title, published, what_happened, why_it_matters (naming the specific Synphony task or bottleneck), tier (v4: PARTNER / ABSORB / WATCH / INTEL / PASS — the single criterion is whether the company's technology sits INSIDE Synphony's specialty of models, fine-tuning and deployment, in which case ABSORB, or OUTSIDE it like arms, sensors, tactile, teleop and services, in which case PARTNER), I (set ONLY on ABSORB rows; meaningless where we will never build it), validation_question, companies_mentioned (an array of any OTHER company names the article names - comparables, competitors, investors' other bets; this is how we find companies our preset queries never reach, and it is free because you have already read the article), and known (true if already in data/derived/reranked.jsonl, in which case why_it_matters must describe what CHANGED). Drop anything where you cannot name a specific Synphony bottleneck. Cap at 25 deep reads. THEN do three search steps, in this order. (A) LEAD RESEARCH: read data/news/leads.jsonl for co-mentioned companies not yet judged; for each, WebSearch the company, fetch its own site, and judge it into judged.jsonl using the same schema. These are companies our preset queries never reach. (B) GAP SEARCH: pick 2-3 thesis areas today's headlines did NOT cover (robot hands, teleoperation, tactile sensing, cheap humanoids, manipulation policies, deployment competitors) and run live WebSearches on them now; judge anything real that surfaces. (C) COVERAGE CRITIC: compare today's headlines against config/discovered_queries.yaml plus the preset list in radar/news.py, name any category or GEOGRAPHY absent entirely, and append 1-3 new queries to config/discovered_queries.yaml so tomorrow's deterministic fetch covers it permanently. Do not duplicate an existing query. Then file anything durable to the wiki dossier and log. Work autonomously; do not ask questions." \
      --allowedTools "Read,Write,Edit,Bash,WebFetch,WebSearch" \
      >"$RERANK_LOG" 2>&1
  JUDGE_RC=$?
  cp "$RERANK_LOG" /tmp/partner-radar-rerank.log      # stable name for the README's diagnosis path
  [ "$JUDGE_RC" -eq 0 ] || log "WARN rerank exited $JUDGE_RC (see $RERANK_LOG)"
  # Measure the file the judge actually writes. This previously reported
  # data/derived/reranked.jsonl — the OLD directory-rerank output — so the log showed
  # "141 rows" every night while the news judge's real output went uncounted.
  JUDGED_AFTER=0
  [ -f data/news/judged.jsonl ] && JUDGED_AFTER=$(wc -l < data/news/judged.jsonl | tr -d ' ')
  log "judged news rows: $JUDGED_AFTER (+$((JUDGED_AFTER - JUDGED_BEFORE)) this run)"
  # Publish the judge as a health lane. Before 2026-09-02 a dead judge produced a WARN in
  # this log and nothing else: the digest went out saying "nothing new" with a clean health
  # box, twice in one week, and the only evidence was overwritten by the next night's run.
  "$PY" -c "from radar.run import _publish_health; _publish_health('judge', $((JUDGED_AFTER - JUDGED_BEFORE)), $([ "$JUDGE_RC" -eq 0 ] && echo 0 || echo 1), {'exit': $JUDGE_RC, 'log': '$RERANK_LOG'})" \
    || log "WARN could not publish judge health"
else
  log "WARN claude binary not found — sending prefilter candidates unreranked"
fi

# The digest degrades gracefully either way: if reranked.jsonl is missing or the rerank
# failed, it sends the prefilter candidates — still useful, just less sharply sorted and
# without the angle line.

# --- 3b. Harvest co-mentioned companies into the research queue -------------------------
# Pure bookkeeping on what the judge already extracted: dedup against the known universe and
# queue the rest. Fixes the coverage failure that hid Example Competitor C for five months.
log "harvesting leads…"
"$PY" -m radar.run leads || log "WARN leads returned $?"

# --- 3c. Bookface founder posts (needs Arc awake; degrade, never fail the run) -------------
# Wired in 2026-08-29. Built and hand-verified on 08-16 but never added here, so the shard went
# 308h stale and the digest flagged one failure every run for twelve days. A capability that
# only runs when someone types it is not wired in.
if [ -f scripts/arc_bookface.py ]; then
  log "reading bookface via Arc…"
  "$PY" scripts/arc_bookface.py --feed launch_bookface --feed recruiting --out data/raw/bookface \
    || log "WARN bookface returned $? (Arc closed or debug port down — digest degrades this lane only)"
else
  log "bookface lane not present (optional; operator-local capture script) — skipping"
fi

# --- 3d. Classify news into decayed signals -----------------------------------------------
# Same omission as above: signals ran three times by hand on 08-16 then sat STALE for 306h.
log "classifying signals…"
"$PY" -m radar.run signals || log "WARN signals returned $?"

# --- 4. Build and send the digest ---------------------------------------------------------
log "sending digest…"
"$PY" -m radar.digest || log "WARN digest returned $?"

# --- 5. Mirror production state to the private repo ---------------------------------------
# This machine IS production; GitHub is the backup. Without this step the two drift silently:
# on 2026-08-17 the entire news lane had been running for a day while radar/news.py was still
# untracked, and the only symptom was a stale timestamp nobody was looking at.
#
# Deliberately narrow. Principle 7 says git is not the database, and internship-radar showed
# what ignoring that costs — ~20 of 50 commits there are timestamped state churn that buries
# the real history. So this commits the accumulated judgements and raw shards (expensive to
# regenerate, genuinely lost if the disk dies) and leaves derived/ alone, which is disposable
# by design and rebuilt from raw on every run.
#
# Never fails the run. A push problem is a backup problem, not a pipeline problem, and the
# email has already gone out by this point.
log "syncing state to private repo…"
git add -A radar config scripts tests skill \
        data/raw data/news/judged.jsonl data/news/leads.jsonl \
        data/derived/reranked.jsonl ./*.md ./*.sh 2>/dev/null

if git diff --cached --quiet 2>/dev/null; then
  log "  nothing to sync"
else
  CHANGED=$(git diff --cached --name-only | wc -l | tr -d ' ')
  JUDGED=$(wc -l < data/news/judged.jsonl 2>/dev/null | tr -d ' ' || echo 0)
  if git commit -q -m "state $(date '+%Y-%m-%d'): ${CHANGED} files, ${JUDGED} judged rows" 2>/dev/null; then
    if git push -q origin HEAD 2>/dev/null; then
      log "  synced ${CHANGED} files"
    else
      log "  WARN commit ok, push failed — local is ahead, will retry tomorrow"
    fi
  else
    log "  WARN commit failed"
  fi
fi
log "done"
