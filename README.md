<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a 6-stage autonomous job application pipeline. It discovers jobs across 5+ boards, scores them against your resume with AI, tailors your resume per job, writes cover letters, and **submits applications for you**. It navigates forms, uploads documents, answers screening questions, all hands-free.

Three commands. That's it.

```bash
pip install applypilot
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
applypilot apply         # autonomous browser-driven submission
applypilot apply -w 3    # parallel apply (3 Chrome instances)
applypilot apply --dry-run  # fill forms without submitting
```

> **Why two install commands?** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine at runtime with any modern numpy. The `--no-deps` flag bypasses the resolver; the second command installs jobspy's actual runtime dependencies. Everything except `python-jobspy` installs normally.

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, Node.js (for npx), Gemini API key (free), Claude Code CLI, Chrome

Runs all 6 stages, from job discovery to autonomous application submission. This is the full power of ApplyPilot.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, Gemini API key (free)

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday employer portals + 30 direct career sites |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | Claude Code navigates application forms, fills fields, uploads documents, answers questions, and submits |

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | 5 boards + Workday + direct sites | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs, 46 Workday portals, 28 direct sites | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| Gemini API key | Scoring, tailoring, cover letters | Free tier (15 RPM / 1M tokens/day) is enough |
| Chromium-based browser | Auto-apply | Chrome, Arc, Brave, Chromium, and Edge are supported |
| Claude Code CLI | Auto-apply with `APPLYPILOT_APPLY_BACKEND=claude` | Install from [claude.ai/code](https://claude.ai/code) |
| Command backend | Auto-apply with `APPLYPILOT_APPLY_BACKEND=command` | Set `APPLYPILOT_AGENT_COMMAND` to your local or third-party apply agent |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). OpenAI and local models (Ollama/llama.cpp) are also supported.

### Optional

| Component | What It Does |
|-----------|-------------|
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile, FunCaptcha). Without it, CAPTCHA-blocked applications just fail gracefully |

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine with modern numpy at runtime.

---

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults. Powers scoring, tailoring, and form auto-fill.

### `searches.yaml`
Job search queries, target titles, locations, boards. Run multiple searches with different parameters.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `LLM_MODEL`, `CAPSOLVER_API_KEY` (optional).
Optional stage-specific model overrides are also supported: `SCORING_LLM_MODEL`, `TAILOR_LLM_MODEL`, `COVER_LLM_MODEL`.

Auto-apply backend selection also lives here:
- `APPLYPILOT_APPLY_BACKEND=claude` uses Claude Code (default)
- `APPLYPILOT_APPLY_BACKEND=command` runs `APPLYPILOT_AGENT_COMMAND`
- `APPLYPILOT_AGENT_COMMAND` supports `{model}`, `{mcp_config}`, `{worker_dir}`, `{port}`, `{worker_id}`
- `scripts/local_apply_agent.py` is a local browser-driving agent for Workday and Greenhouse flows
- `APPLYPILOT_ACCOUNT_PASSWORD` can be scoped with `APPLYPILOT_ACCOUNT_PASSWORD_HOSTS`
- `APPLYPILOT_ACCOUNT_PASSWORDS_FILE` supports per-host passwords without putting them in git
- `APPLYPILOT_IMAP_*` enables mailbox polling for password-reset and verification emails
- `~/.applypilot/question_memory/` stores per-company ATS question memory learned from successful and diagnostic runs

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/sites.yaml` - Direct career sites (30+), blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes 48 Workday employer portals (configurable in `employers.yaml`). Hits 30 direct career sites with custom extractors. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
The Claude backend launches Chrome plus the Playwright MCP stack. The local command backend can drive Workday and Greenhouse flows directly with Playwright, fill personal information and work history, upload the tailored resume and cover letter, handle account bootstrap, use mailbox verification flows when needed, and submit when the form flow is stable. A live dashboard shows progress in real-time, and the HTML dashboard now refreshes automatically after prep and apply runs.

The Playwright MCP server is configured automatically at runtime per worker. No manual MCP setup needed.

```bash
# Utility modes (no Chrome/Claude needed)
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset all failed jobs for retry
applypilot apply --gen --url URL       # generate prompt file for manual debugging
```

---

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot apply                        # Launch auto-apply
applypilot apply --workers 3            # Parallel browser workers
applypilot apply --dry-run              # Fill forms without submitting
applypilot apply --continuous           # Run forever, polling for new jobs
applypilot apply --headless             # Headless browser mode
applypilot apply --url URL              # Apply to a specific job
applypilot status                       # Pipeline statistics
applypilot report --section all         # Structured runtime + pipeline report
applypilot report --section ready --format markdown
applypilot dashboard                    # Open HTML results dashboard
```

The HTML dashboard is written to `~/.applypilot/dashboard.html`. It is regenerated automatically by the prep/apply runners, and you can still force a refresh manually with:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
PYTHONPATH=src python3 -m applypilot.cli dashboard
open ~/.applypilot/dashboard.html
```

## Run Without Codex

Manual daily run from this repo:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/run_daily.sh
```

The runner does:

```bash
python3 scripts/daily_concurrent.py
applypilot status
```

Default knobs:
- `APPLYPILOT_DAILY_MIN_SCORE=8`
- `APPLYPILOT_DAILY_SCORE_LIMIT=90`
- `APPLYPILOT_DAILY_TAILOR_LIMIT=35`
- `APPLYPILOT_DAILY_COVER_LIMIT=35`
- `APPLYPILOT_DAILY_TARGET_SUBMISSIONS=25`
- `APPLYPILOT_DAILY_APPLY_BATCH=5`
- `APPLYPILOT_DAILY_MAX_CYCLES=6`
- `APPLYPILOT_DAILY_VALIDATION=lenient`
- `APPLYPILOT_DAILY_WORKERS=1`
- `APPLYPILOT_APPLY_RETRY_COOLDOWN_HOURS=18`
- `APPLYPILOT_SUPPORTED_AUTOAPPLY_PATTERNS=myworkdayjobs.com,greenhouse.io,...`
- `APPLYPILOT_PREP_PREFERRED_SITES=greenhouse.io,BMO,RBC,Cisco,...`
- `APPLYPILOT_PREP_DEPRIORITIZE_SITES=Netflix,Adobe,Thomson Reuters,Moderna`

Why this is better:
- discovery and enrichment still run daily for fresh jobs
- scoring, tailoring, and cover-letter generation are budgeted instead of trying to clear the whole backlog
- prep work and apply work now run in parallel loops during the same daily session
- the controller loops toward completed submissions rather than firing one big apply burst
- failed jobs cool down before retry, so fresh ready jobs get priority
- Greenhouse and other easier supported ATS targets can be preferred ahead of sticky tenants via env hints
- the run is quieter and more likely to reach the apply stage every day on a smaller local machine

Override them for a single run:

```bash
APPLYPILOT_DAILY_APPLY_LIMIT=5 ./scripts/run_daily.sh
```

Check whether the daily pipeline is currently running:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/is_running.sh
```

Get a Terminal summary of sourced jobs, daily pipeline activity, ready-to-apply jobs, and recent apply outcomes:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/check_applications.sh
```

Open the numbered local control menu:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/job_agent_menu.sh
```

Emit structured reports for automation, OpenClaw, or shell scripting:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
PYTHONPATH=src python3 -m applypilot.cli report --section all --format json
PYTHONPATH=src python3 -m applypilot.cli report --section runtime --format table
PYTHONPATH=src python3 -m applypilot.cli report --section ready --format markdown
```

Run the system in true always-on mode:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/reload_always_on.sh
```

That stops the noon-only batch launcher, starts a detached always-on supervisor immediately, and keeps restarting the concurrent session after each batch completes.

Install login-time auto-start for the always-on supervisor:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/install_always_on.sh
```

Stop the always-on supervisor:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/stop_always_on.sh
```

Inspect learned ATS/company question memory:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/show_question_memory.sh --list
./scripts/show_question_memory.sh --ats greenhouse --list
./scripts/show_question_memory.sh "Grafana Labs"
./scripts/show_question_memory.sh "Thomson Reuters" --ats workday
```

## Schedule Daily On macOS

Install a `launchd` job that runs every day at 12:00 PM local time:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/install_launchd.sh 12:00
```

Useful commands:

```bash
launchctl kickstart -k gui/$(id -u)/com.ai-job-application-agent.daily
launchctl print gui/$(id -u)/com.ai-job-application-agent.daily
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.ai-job-application-agent.daily.plist
```

After editing `~/.applypilot/.env`, `profile.json`, or `searches.yaml`, the safest way to refresh the scheduled setup is:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/reload_daily.sh
```

If you want to reload the config and start a fresh run immediately:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/reload_daily.sh --run-now
```

Logs land in:

```bash
~/.applypilot/logs/
```

At the start of each scheduled run, the previous launcher logs are archived to:

```bash
~/.applypilot/logs/archive/
```

How `launchd` works here:
- `launchd` does not understand your job pipeline itself.
- It schedules a tiny wrapper under `~/.applypilot/bin/` that opens Terminal and runs `scripts/run_daily.sh`.
- That script runs `scripts/daily_concurrent.py`, which does the orchestration.
- This Terminal-backed launch is intentional. On macOS, background agents often cannot read repos under `~/Documents`, while Terminal can because it runs with your normal interactive permissions.
- The daily supervisor now uses two loops at the same time:
- prep loop: `discover/enrich/score/tailor/cover/pdf`
- apply loop: applies ready jobs in batches
- So yes, now it can prepare later jobs while applying already-ready jobs in parallel during the same session.
- With `APPLYPILOT_DAILY_WORKERS=1`, apply itself is single-browser-worker. If you raise workers later, apply can parallelize across multiple ready jobs.

## OpenClaw Wrappers

If you want OpenClaw on top, use it as a thin command runner over this repo.

Wrapper commands:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
zsh ./scripts/openclaw_status.sh
zsh ./scripts/openclaw_activity.sh
zsh ./scripts/openclaw_jobs.sh ready
zsh ./scripts/openclaw_control.sh reload-always-on
```

What these do:
- `openclaw_status.sh`: prints runtime, overview, queue, failures, config, and history in Markdown
- `openclaw_activity.sh`: prints daily activity plus recent session events in Markdown
- `openclaw_jobs.sh`: prints ready jobs, recent apply activity, or failure lists in Markdown
- `openclaw_control.sh`: runs runtime controls and returns compact Markdown/text

Recommended OpenClaw usage:
- Register this repo's skills into OpenClaw:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/install_openclaw_integration.sh
```

- Then start a new OpenClaw session and use:
  - `openclaw dashboard`
  - `/skill job-agent-help`
  - `/skill job-agent-status`
  - `/skill job-agent-activity`
  - `/skill job-agent-jobs ready`
  - `/skill job-agent-control reload-always-on`
- The repo-owned skills live under `openclaw_skills/` and are registered through `skills.load.extraDirs` in `~/.openclaw/openclaw.json`.
- Terminal, `launchd`, and OpenClaw all share the same runtime path and config.

## Metrics and History

Structured session events are written to:

```bash
~/.applypilot/metrics/session_events/YYYY-MM-DD.jsonl
```

Event types include:
- `supervisor_started`
- `startup_delay`
- `wake_detected`
- `session_started`
- `session_finished`
- `session_failed`
- `control_action`

Use `applypilot report --section history` or `./scripts/openclaw_activity.sh` to inspect the recent ledger.

## Local Command Backend

The fork includes [`scripts/local_apply_agent.py`](scripts/local_apply_agent.py), a local Playwright-based apply agent that lets the
`command` backend use either:
- a local Ollama server
- a remote Ollama server via `OLLAMA_HOST` or `--base-url`
- a remote OpenAI-compatible endpoint via `--provider openai`

The local agent now controls the browser for Workday and Greenhouse flows and supports:
- account sign-in
- account creation
- password-reset fallback
- optional IMAP polling for reset / verification emails
- Greenhouse verification-code retrieval over IMAP
- per-company ATS question memory and reuse
- command-backend silence timeouts so stuck agent runs fail fast instead of hanging forever
- ATS-aware prep/apply prioritization so easier supported targets can stay ahead of noisier tenants
- dry-run and live-submit result reporting back into ApplyPilot

Useful runtime tuning:
- `APPLYPILOT_AGENT_TIMEOUT`
- `APPLYPILOT_AGENT_SILENCE_TIMEOUT`
- `APPLYPILOT_SUPPORTED_AUTOAPPLY_PATTERNS`
- `APPLYPILOT_PREP_PREFERRED_SITES`
- `APPLYPILOT_PREP_DEPRIORITIZE_SITES`

Recommended small model for local development on low-memory machines:
- `qwen3:4b` for the best instruction-following / agent tradeoff
- `llama3.2:latest` as a zero-download fallback if you already have it

```bash
ollama pull qwen3:4b

export APPLYPILOT_APPLY_BACKEND=command
export APPLYPILOT_AGENT_COMMAND='python scripts/local_apply_agent.py --provider ollama --model qwen3:4b'

# Generate a prompt for a specific job and print the exact command to run
applypilot apply --gen --url URL --agent-backend command

# Or test the bridge directly with a saved prompt
python scripts/local_apply_agent.py --provider ollama --model qwen3:4b < /path/to/prompt.txt
```

Remote URL migration examples:

```bash
# Remote Ollama
python scripts/local_apply_agent.py --provider ollama --base-url http://REMOTE_HOST:11434 --model qwen3:4b

# Remote OpenAI-compatible server
python scripts/local_apply_agent.py --provider openai --base-url http://REMOTE_HOST:8000/v1 --model qwen3:4b
```

Mailbox automation example:

```bash
export APPLYPILOT_IMAP_HOST=imap.gmail.com
export APPLYPILOT_IMAP_PORT=993
export APPLYPILOT_IMAP_USER=you@gmail.com
export APPLYPILOT_IMAP_PASSWORD=your_app_password
export APPLYPILOT_IMAP_FOLDER=INBOX
```

If you keep a fixed password for one Workday tenant, scope it:

```bash
export APPLYPILOT_ACCOUNT_PASSWORD=your_existing_password
export APPLYPILOT_ACCOUNT_PASSWORD_HOSTS=thomsonreuters.wd5.myworkdayjobs.com
```

If different Workday tenants use different passwords, store them locally:

```json
{
  "thomsonreuters.wd5.myworkdayjobs.com": "existing-tenant-password",
  "netflix.wd1.myworkdayjobs.com": "different-tenant-password"
}
```

Save that as `~/.applypilot/account_passwords.json`, or point `APPLYPILOT_ACCOUNT_PASSWORDS_FILE` at another JSON file.

### Question Memory

The local agent keeps ATS-specific company memory under:

```bash
~/.applypilot/question_memory/
```

Current structure:

- `greenhouse/<Company>.json`
- `workday/<Company>.json`

Each file stores:
- `questions`: reusable learned answers the agent can apply automatically later
- `seen_questions`: questions encountered on prior runs, with optional suggested answers for review

This memory is loaded after explicit host/company overrides and before global defaults, so curated overrides still win.

Quick inspection commands:

```bash
cd /Users/yashm/Documents/ai-job-application-agent
./scripts/show_question_memory.sh --list
./scripts/show_question_memory.sh --ats workday --list
./scripts/show_question_memory.sh "Ōura" --ats greenhouse
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
