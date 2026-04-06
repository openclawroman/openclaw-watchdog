# OpenClaw Heartbeat

Custom heartbeat watchdog system for monitoring OpenClaw agent health. Runs locally, uses ollama — **$0 cost**.

## Architecture

```
┌──────────────────────────┐    heartbeat/data/*.json    ┌─────────────────┐
│  Heartbeat Daemon         │ ──────────────────────────→  │  Watchdog       │
│  (LaunchAgent, 15min)    │   one JSON per agent         │  (loop, 5min)   │
│  ollama qwen2.5:3b-hb    │                               │  Telegram alert │
│  $0 cost • ctx=8K • KEEP_ALIVE=-1 │                      └────────┬────────┘
└──────────────────────────┘                                        │
                                                                    ▼
                                                           Telegram bot message
```

### 3 Layers

#### Layer 1: Heartbeat Writer (Daemon)

**Script:** `heartbeat/scripts/heartbeat-daemon.py`

- Writes one `.json` heartbeat file per agent into `data/`
- Runs every 15 min via macOS LaunchAgent (`ai.openclaw.heartbeat`, StartInterval=900s)
- **Staggered**: agents fire across 4-minute window (~3–4 agents/min), not all at once
- Checks local ollama (`http://127.0.0.1:11434/api/tags`) to verify it's alive
- Counts `.jsonl` session files per agent → `progress_counter`
- **Cost: $0** — ollama `qwen2.5:3b-hb` locally, no paid APIs
- **Lightweight model**: `num_ctx=8192` (4× smaller than default 32K)
- **Permanent residency**: `OLLAMA_KEEP_ALIVE=-1` keeps model in RAM forever, no cold starts

#### Layer 2: Heartbeat Data Files

- One `.json` per agent in `data/`
- Written atomically via `tempfile` + `os.rename()`
- Schema defined in `config.py:HeartbeatRecord`

#### Layer 3: Watchdog Scanner

**Script:** `heartbeat/watchdog.py` — runs as LaunchAgent `ai.openclaw.heartbeat-watchdog`

- Scan interval: 300s (5 min)
- Reads every `*.json` in `data/`, classifies each agent's health
- Sends Telegram alerts on state changes with **stable dedup keys** + cooldown
- Classifies via `checker.py:check_progress_stall()` (accounts for progress counter changes)

## Heartbeat Schema (v1)

Per-agent JSON (`data/coder.json`):

```json
{
  "version": 1,
  "agent_id": "coder",
  "run_id": "daemon-20260405-1900",
  "status": "alive",
  "updated_at": "2026-04-05T19:00:44+00:00",
  "progress_counter": 3,
  "task_id": null,
  "task_type": "heartbeat_monitor",
  "ollama_alive": true
}
```

### Required fields

| Field              | Type    | Description                          |
| ------------------ | ------- | ------------------------------------ |
| `version`          | int     | Schema version (always `1`)          |
| `agent_id`         | str     | Unique agent identifier              |
| `run_id`           | str     | Daemon run id (`daemon-YYYYMMDD-HHMM`)|
| `status`           | str     | `alive` / `degraded`                 |
| `updated_at`       | str     | ISO-8601 timestamp                   |
| `progress_counter` | int     | Active session count (changes = progress) |

### Optional fields

| Field               | Type    | Description                  |
| ------------------- | ------- | ---------------------------- |
| `task_id`           | str?    | Associated task id           |
| `task_type`         | str?    | Task type for thresholds     |
| `progress_message`  | str?    | Human-readable status        |
| `expected_duration_sec` | int?| Expected task duration       |
| `last_error`        | str?    | Last error message           |

Extra fields (like `ollama_alive`) are ignored gracefully by the checker.

## Status Classification

| Status    | Meaning                                          | Detects                         |
| --------- | ------------------------------------------------ | ------------------------------- |
| `ok`      | Recent heartbeat, progress moving or fresh       | Healthy agent                   |
| `stall`   | Heartbeat fresh but `progress_counter` unchanged | Agent alive but not working     |
| `dead`    | Exceeds `dead_after_sec` (900s)                  | Agent hasn't written heartbeat  |
| `missing` | No heartbeat file found for known agent          | Agent disappeared               |
| `corrupt` | File exists but can't parse JSON                 | Partial/corrupted write         |
| `error`   | File parseable but marked error state            | Agent reported error            |

**Stall detection:** Uses `check_progress_stall()` which compares current `progress_counter` with the previous persisted value. If counter hasn't changed AND elapsed time > `min_stall_sec` → STALL.

## Anti-flap & Dedup

### Stable dedup keys

Alerts use **state-based keys** instead of message text, so editing alert wording doesn't reset cooldowns:

| Alert type       | Dedup key format                           |
| ---------------- | ------------------------------------------ |
| Per-agent state  | `{agent_id}:{run_id or 'none'}:{state}`    |
| Missing agent    | `{agent_id}:none:missing`                  |
| Task-watch reply | `main-task-watch:pending-reply`            |
| Task-watch stall | `main-task-watch:active-stall`             |

### Cooldown & sustain

| Setting                      | Value     |
| ---------------------------- | --------- |
| Sustain (stall)              | 600s      |
| Sustain (dead/missing)       | 300s      |
| Cooldown (stall/dead/missing)| 1800s     |
| Cooldown (corrupt/error)     | 900s      |

Alert only fires after state persists for `sustain_sec`, then not again for `cooldown_sec`.

## Thresholds

| Setting                      | Value                                |
| ---------------------------- | ------------------------------------ |
| Scan interval                | 300s                                 |
| Startup grace                | 600s (no alerts at boot)             |
| Dead after                   | 900s (15 min)                        |
| Min stall                    | 600s                                 |
| Dynamic stall                | `expected_duration_sec × 1.5` or task_type threshold |

## Task Watch

`task_watch.py` manages `main-task-watch.json` for tracking the main agent's active task state. Supports portable paths via `TASK_WATCH_PATH` environment variable.

### Commands

```bash
# Mark task active
python3 task_watch.py mark-active --title "Building feature X"

# Mark progress
python3 task_watch.py mark-progress --notes "Completed DB migration"

# Mark verified result (pending user update)
python3 task_watch.py mark-verified --summary "Feature built successfully"

# Mark replied (user notified)
python3 task_watch.py mark-replied

# Mark done
python3 task_watch.py mark-done
```

### Environment variables

| Variable           | Description                             |
| ------------------ | --------------------------------------- |
| `TASK_WATCH_PATH`  | Full path to main-task-watch.json       |
| `OPENCLAW_HOME`    | OpenClaw home directory (fallback)      |

## Usage

```bash
# One-shot scan + report
python3 heartbeat/watchdog.py --config heartbeat/watchdog.json

# Continuous loop mode
python3 heartbeat/watchdog.py --loop --config heartbeat/watchdog.json

# Task watch helpers
python3 heartbeat/task_watch.py mark-active --title "My task"
```

## Configuration

Edit `heartbeat/watchdog.json`:

| Key                  | Description                                  |
| -------------------- | -------------------------------------------- |
| `heartbeat_data_dir` | Directory where agents write heartbeat files |
| `watchdog_state_dir` | Where watchdog persists its own state        |
| `scan_interval_sec`  | Seconds between scan cycles                  |
| `startup_grace_sec`  | Initial period with suppressed alerts        |
| `enable_telegram`    | Set `false` to disable all Telegram alerts   |
| `telegram_chat_id`   | Chat ID for alerts (required if `enable_telegram=true`) |
| `telegram_bot_token` | Bot token (required if `enable_telegram=true`) |

## LaunchAgents

| Label                          | Script                  | Interval |
| ------------------------------ | ----------------------- | -------- |
| `ai.openclaw.heartbeat`        | `heartbeat-daemon.py`   | 15 min   |
| `ai.openclaw.heartbeat-watchdog`| `watchdog.py --loop`   | persistent|

## Optimizations (April 2026)

### 1. Heartbeat Model Isolation (Strict Ollama-Only)
- Heartbeat daemon and watchdog **never** use OpenRouter or any external provider for heartbeat checks
- Direct local HTTP to `http://127.0.0.1:11434` only; if Ollama is down, mark agents `degraded` and **do not** attempt alternative models
- No fallback, no retry to external APIs; no shared queue/circuit breaker with user requests
- This ensures heartbeat never triggers rate limits or external dependencies

### 2. Lightweight Heartbeat Model
- Created `qwen2.5:3b-hb` with `num_ctx=8192` (4× smaller than default 32K)
- Reduces per-request CPU/RAM overhead while still fitting full heartbeat payloads
- Model size: ~1.9GB, `num_predict=2048` sufficient for heartbeat responses

### 3. Permanent Model Residency
- Set `OLLAMA_KEEP_ALIVE=-1` on the LaunchAgent
- Model stays loaded in RAM indefinitely — no cold-start latency or cache misses
- Verified via `curl /api/ps` shows `expires=2318` (far future)

### 4. Staggered Heartbeat Intervals
- Previously: all agents on 15m → simultaneous burst of 12+ requests (CPU overload)
- Now: intervals spread across 18–21m window → ~3–4 agents per minute
- Spread: 18m (researcher/orchestrator/lead/backlog), 19m (reviewer/planner/decompositor), 20m (critique/manager/main), 21m (coder/qa/gateway)
- Peak concurrency reduced from ~12 to ~4 simultaneous requests

### 5. Main Agent Heartbeat Enabled
- `main` agent now has explicit heartbeat config (previously inherited undefined)
- Uses same `ollama/qwen2.5:3b-hb` with 20m interval
- All 13 agents now monitored consistently

### 6. Telegram Notifications Control (Watchdog)
- Add `enable_telegram: false` to `watchdog.json` to suppress all Telegram alerts (`OK`, `Stall`, `Dead`, etc.)
- Watchdog daemon continues to write logs and state; only Telegram output is disabled
- To re-enable: set `enable_telegram: true` and restart the watchdog LaunchAgent

### 7. Adaptive Reasoning Default

### 8. Main Agent Auto-Recovery (Sidecar)
- New daemon: `heartbeat/main_recovery.py` (LaunchAgent `ai.openclaw.main-recovery`)
- Monitors `main-task-watch.json` for stalls (`lastProgressAt` > 600s)
- On first stall: sets `status: recovering`, gracefully stops main (via `openclaw sessions cancel` or SIGTERM), requeues task, increments `retryCount`
- On second stall: sends Telegram alert (if `enable_telegram`), sets `status: interrupted`, stops trying
- Requires `task_text` populated at task start; falls back to reconstructing from `agents/main/sessions/*.jsonl` if missing
- Uses same dedup keys; respects `enable_telegram` from `watchdog.json`

### 9. Task Metadata Requirements
- For reliable replay, call `task_watch mark-active` with:
  `--task-text "<original instruction>"`
  Optional: `--chat-id`, `--message-id`, `--update-id`, `--attachments <paths>`
- Example: `task_watch mark-active --title "..." --task-text "..." --chat-id "428798118" --message-id "123"`
- Without these, recovery falls back to session log reconstruction, which may be incomplete for multi-turn/tool tasks.

- `agents.defaults.thinkingDefault: adaptive` — Step 3.5 Flash automatically adjusts reasoning depth per task
- Low-cost for simple queries, deeper for complex analysis
- Independent of heartbeat (heartbeat uses local Ollama without reasoning)

## Verifiable State

Heartbeat daemon and watchdog validate model availability before each run.

- Daemon: `ollama http check` → if down, marks agents `degraded` but doesn't crash
- Watchdog: ignores missing/invalid heartbeat files gracefully
- Each state change includes a stable 3‑tuple key: `(agent_id, run_id, state)` for dedup

## Safety

- Heartbeat writer exits gracefully if ollama is down (marks agents as degraded, not crashed)
- Watchdog never restarts agents automatically — alert-only mode
- State file corruption in one agent doesn't affect others
- Atomic writes prevent partial data files

## License

MIT

## Main Agent Graceful Shutdown (B2)

The main agent itself should trap SIGTERM and perform graceful shutdown:

- Stop accepting new work
- Mark task as `recovering` or `interrupted` via `task_watch`
- Flush partial state
- Cancel/await in-flight async tasks where possible
- Update mirrored task-watch state
- Exit cleanly

Example pattern for a Python-based agent:

```python
import signal, sys, subprocess, os
from datetime import datetime, timezone
import json
from pathlib import Path

WATCH_PATH = Path.home() / ".openclaw" / "workspace" / "memory" / "main-task-watch.json"

def handle_sigterm(signum, frame):
    # Mark task as interrupted/recovering
    state = {}
    try:
        state = json.loads(WATCH_PATH.read_text())
    except: pass
    state["status"] = "recovering"
    state["recoveryReason"] = "SIGTERM"
    state["recoveryStartedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    WATCH_PATH.write_text(json.dumps(state, indent=2))
    # Perform cleanup, cancel async tasks, flush state...
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
```

Adapt to your agent’s language/runtime.
