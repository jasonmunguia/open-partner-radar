#!/bin/bash
# Daily partner-radar run. Fires ~09:15 so the email is waiting when the operator gets in at 10.
#
# Hybrid by design: deterministic Python for the parts that must not drift (ingest, prefilter,
# dedup, delivery), and a headless Claude pass for the parts that genuinely need reading
# comprehension (supplier vs competitor, reading sites with no description, the outreach angle).
#
# Requires the Mac to be awake. com.partner-radar.keepawake already runs `caffeinate -i`. If the Mac
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
# a member's Bookface reputation for nothing.
SHARD=data/raw/yc/P26.jsonl
NEED_INGEST=1
if [ -f "$SHARD" ]; then
  AGE=$(( ( $(date +%s) - $(stat -f %m "$SHARD") ) / 3600 ))
  [ "$AGE" -lt 60 ] && NEED_INGEST=0 && log "ingest skipped (${AGE}h old, under 60h)"
fi
if [ "$NEED_INGEST" = "1" ]; then
  log "ingesting YC…"
  "$PY" -m radar.run ingest || log "WARN ingest returned $?"
fi

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
  "$CLAUDE" -p "Use the partner-radar skill and run the stage-2 rerank exactly as its Workflow section specifies: read data/derived/scored.jsonl, skip anything already in data/derived/reranked.jsonl, process at most 30 new companies (accelerants first, then hardware, then unknowns), fetch and read the website of any company whose description is thin, assign the final tier and the I score, write why_it_matters and angle, and append to data/derived/reranked.jsonl. Then file anything durable to the wiki dossier and log. Work autonomously; do not ask questions." \
      --allowedTools "Read,Write,Edit,Bash,WebFetch,WebSearch" \
      >/tmp/partner-radar-rerank.log 2>&1 \
    || log "WARN rerank exited $? (see /tmp/partner-radar-rerank.log)"
  [ -f data/derived/reranked.jsonl ] && \
    log "reranked rows: $(wc -l < data/derived/reranked.jsonl | tr -d ' ')"
else
  log "WARN claude binary not found — sending prefilter candidates unreranked"
fi

# The digest degrades gracefully either way: if reranked.jsonl is missing or the rerank
# failed, it sends the prefilter candidates — still useful, just less sharply sorted and
# without the angle line.

# --- 4. Build and send the digest ---------------------------------------------------------
log "sending digest…"
"$PY" -m radar.digest || log "WARN digest returned $?"
log "done"
