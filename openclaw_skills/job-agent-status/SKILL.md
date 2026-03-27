---
name: job-agent-status
description: Show current runtime status, pipeline metrics, queue state, recent apply activity, and config for the local ApplyPilot setup.
user-invocable: true
metadata: {"openclaw":{"os":["darwin"],"requires":{"bins":["zsh","python3"]}}}
---
When this skill is invoked, run:

`zsh "{baseDir}/../../scripts/openclaw_status.sh"`

Return the script output directly with no extra framing.
