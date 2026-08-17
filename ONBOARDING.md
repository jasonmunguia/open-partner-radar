# Onboarding a new operator

For the agent bringing a human online — a Synphony teammate who will receive and act on the
daily digest. Target: 30–60 minutes, and the operator never needs to type a command; you run
them, they watch the checkpoints. Every checkpoint below is something *they can see* —
that's what makes the pipeline legible to them later.

**What they get:** one email per morning ("Partner Radar — N companies, M posts"), companies
grouped by action tier (partner now / absorb track / hardware / customers / competitors-watch),
each card with a why-it-matters line, an outreach angle, links. A red "Degraded this run"
footer at the bottom means something broke — that footer is their dashboard.

## What you need from them before starting (ask up front, in this order)

1. **Which email should receive the digest?** (their work address)
2. **Which mailbox sends it?** Needs a Gmail/Workspace **app password** — they create it at
   https://myaccount.google.com/apppasswords (requires 2-step verification on). Sender and
   recipient must differ, or Gmail buries the thread as self-send.
3. **The yc CLI stays authenticated as the authorized account holder (teammate@example.com).** This is policy,
   not a default — see README "The account rule". A new operator does NOT log in as
   themselves: their Bookface account may lack `tools run` access entirely (the 403 incident
   of 2026-08-09 was exactly this), and every query lands on the authenticated account's
   activity trail. If the account holder must approve a re-login, that is a human step: he runs
   `yc login --device` on this machine and follows the browser prompt. Budget 5 minutes for it.

## Steps

### 1. Get the code and Python deps (5 min)

```
git clone <repo-url> ~/Desktop/partner-radar   # or copy the folder; path can differ, but
cd ~/Desktop/partner-radar                     # then every absolute path in step 6 changes
python3 -m pip install -r requirements.txt
python3 -c "import yaml, certifi; print('deps ok')"
```

Checkpoint (them): you tell them "dependencies verified" only after `deps ok` printed.

### 2. Install and verify the yc CLI (5–10 min)

```
curl -fsSL https://bookface.ycombinator.com/cli/install.sh | bash
yc me
```

`yc me` must print **the authorized account holder** — if it prints anyone else or errors, run
`yc login --device` (browser login as teammate@example.com, the account holder in the loop), then re-check.
Then prove authorization, not just authentication:

```
yc tools run search.companies --input '{"limit":0,"group_counts_by":"batch"}' --json
```

Real JSON with per-batch counts = healthy. `Tool run failed (403): forbidden` = wrong
account or revoked access — stop and fix before continuing; nothing downstream works.

Checkpoint (them): read them the batch counts ("YC sees 248 companies in S26...").

### 3. Configure delivery to THEM (5 min)

Register their sender mailbox (name it after the person or org, not "personal"):

```
python3 ~/.claude/tools/mailer.py add <name> <sender-address>   # prompts for app password
python3 ~/.claude/tools/mailer.py test <name> <their-recipient-address>
```

Checkpoint (them): a "Mailer test" email lands in their inbox. Do not proceed until it does.

Then edit `config/sources.yaml` → `delivery:` — set `sender_account:` to the name just
registered and `to:` to their recipient address.

### 4. Dry run — prove the pipeline end-to-end without sending (5 min)

```
python3 -m radar.run score && python3 -m radar.run report
python3 -m radar.digest --dry > /tmp/digest-preview.html
open /tmp/digest-preview.html
```

Checkpoint (them): the digest opens in their browser. Walk them through one card: name,
tier color, the "Angle" line, the links. If a red "Degraded this run" box appears, read the
lines in it together — this is the moment they learn what broken looks like.

### 5. Real run they watch (5 min)

```
./run_daily.sh
```

Checkpoint (them): the digest email arrives in their inbox within a few minutes. This is
the proof-of-life moment — do not schedule until they have seen a real email arrive.

### 6. Schedule it (5 min)

The plist is not committed; create `~/Library/LaunchAgents/com.youruser.partner-radar.plist`
(rename the label for a new operator, e.g. `com.<name>.partner-radar`) with the repo path
from step 1 substituted in — this is the live file's exact shape:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.youruser.partner-radar</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string>
         <string>/Users/youruser/Desktop/partner-radar/run_daily.sh</string></array>
  <key>WorkingDirectory</key><string>/Users/youruser/Desktop/partner-radar</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>15</integer></dict>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/Users/youruser/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>SSL_CERT_FILE</key><string>/Library/Frameworks/Python.framework/Versions/3.11/lib/python3.11/site-packages/certifi/cacert.pem</string></dict>
  <key>StandardOutPath</key><string>/tmp/partner-radar.log</string>
  <key>StandardErrorPath</key><string>/tmp/partner-radar.log</string>
</dict>
</plist>
```

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.youruser.partner-radar.plist
launchctl kickstart gui/$(id -u)/com.youruser.partner-radar   # fire once now to prove the schedule path
```

Two machine-dependencies to check: the `SSL_CERT_FILE` path must exist on their machine
(`ls` it; if their certifi lives elsewhere, point at that), and the Mac must be awake at
09:15 — the operator's machine runs a separate `com.youruser.keepawake` LaunchAgent holding
`caffeinate -i`; without an equivalent, launchd fires the job at next wake instead.

Checkpoint (them): a second digest email, this one triggered by launchd, not by you.

### 7. Teach them broken (5 min — this is the part that makes them an operator)

Tell them, in these terms:

- **Normal:** one email every morning by ~09:30. A "quiet day" subject is fine — that means
  nothing new, and the email carries a standing shortlist instead.
- **Yellow:** the email arrives with a red **"Degraded this run"** footer. The pipeline ran
  but some source failed. One day of this is tolerable; two days running means tell the
  agent (or whoever maintains this) the exact lines in the red box.
- **Red:** **no email at all by 10:00, two days running.** The run itself is dying. The
  diagnosis path for whoever they escalate to: `/tmp/partner-radar.log` (grep WARN),
  `data/health.json` timestamps, and README "Failure signatures" — starting with the YC
  auth check, which is the failure that has actually happened.

Done = all seven checkpoints passed, and they can repeat the Normal/Yellow/Red triage back
to you unprompted.
