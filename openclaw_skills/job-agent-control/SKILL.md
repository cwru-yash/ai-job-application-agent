---
name: job-agent-control
description: Control the local ApplyPilot runtime by reloading always-on mode, stopping it, opening the dashboard, or launching the menu.
user-invocable: true
metadata: {"openclaw":{"os":["darwin"],"requires":{"bins":["zsh","python3"]}}}
---
Use this skill when the user wants to control the local ApplyPilot runtime.

Allowed actions:
- `status`
- `reload-always-on`
- `stop-always-on`
- `open-dashboard`
- `menu`
- `run-daily`

If the user does not specify an action, default to `status`.

Run:

`zsh "{baseDir}/../../scripts/openclaw_control.sh" <action>`

Return the script output directly with no extra framing.
