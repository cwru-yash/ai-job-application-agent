---
name: job-agent-jobs
description: Inspect the ready queue, recent applied jobs, or failure lists for the local ApplyPilot setup.
user-invocable: true
metadata: {"openclaw":{"os":["darwin"],"requires":{"bins":["zsh","python3"]}}}
---
Use this skill when the user asks for ready jobs, recent applied jobs, or failure lists.

Allowed modes:
- `ready`
- `recent`
- `failures`
- `overview`

If the user does not specify a mode, default to `ready`.

Run:

`zsh "{baseDir}/../../scripts/openclaw_jobs.sh" <mode>`

Return the script output directly with no extra framing.
