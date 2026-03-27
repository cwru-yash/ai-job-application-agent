---
name: job-agent-activity
description: Show daily ApplyPilot activity totals and recent session history from the local event ledger.
user-invocable: true
metadata: {"openclaw":{"os":["darwin"],"requires":{"bins":["zsh","python3"]}}}
---
When this skill is invoked, run:

`zsh "{baseDir}/../../scripts/openclaw_activity.sh"`

Return the script output directly with no extra framing.
