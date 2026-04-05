# OpenClaw Heartbeat

Custom heartbeat watchdog system for monitoring OpenClaw agent health. Runs locally, uses ollama — **$0 cost**.

## Architecture

```
┌──────────────────────────┐    heartbeat/data/*.json    ┌─────────────────┐
│  Heartbeat Daemon         │ ──────────────────────────→  │  Watchdog       │
│  (LaunchAgent, 15min)    │   one JSON per agent         │  (loop, 5min)   │
│  ollama qwen2.5:3b       │                               │  Telegram alert │
│  $0 cost                 │                               └────────┬────────┘
└──────────────────────────┘                                        │
                                                                    ▼
                                                           Telegram bot message
```

### 3 Layers

#### Layer 1: Heartbeat Writer (Daemon)

**Script:** `heartbeat/scripts/heartbeat-daemon.py`

- Writes one `.json` heartbeat file per agent into `data/`
- Runs every 15 min via macOS LaunchAgent (`ai.openclaw.heartbeat`, StartInterval=900s)
- Staggered: 10 seconds between each agent (~130s total for 13 agents)
- Checks local ollama (`http://127.0.0.1:11434/api/tags`) to verify it's alive
- Counts `.jsonl` session files per agent → `progress_counter`
- **Cost: $0** — ollama `qwen2.5:3b` locally, no paid APIs

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
| `telegram_chat_id`   | Chat ID for alerts                           |
| `telegram_bot_token` | Bot token (set for Telegram notifications)   |

## LaunchAgents

| Label                          | Script                  | Interval |
| ------------------------------ | ----------------------- | -------- |
| `ai.openclaw.heartbeat`        | `heartbeat-daemon.py`   | 15 min   |
| `ai.openclaw.heartbeat-watchdog`| `watchdog.py --loop`   | persistent|

## Fixes Applied

1. **Stall detection order** — `check_progress_stall()` runs BEFORE `state_store.update()`, correctly reading previous state
2. **Report/alert consistency** — both code paths use `check_progress_stall()`, no divergence
3. **Normalized dedup keys** — state-based keys, not message text; cooldown survives wording changes
4. **Portable paths** — `task_watch.py` uses `TASK_WATCH_PATH` env, no hardcoded paths
5. **Schema alignment** — daemon output matches checker's `HeartbeatRecord` contract exactly

## Safety

- Heartbeat writer exits gracefully if ollama is down (marks agents as degraded, not crashed)
- Watchdog never restarts agents automatically — alert-only mode
- State file corruption in one agent doesn't affect others
- Atomic writes prevent partial data files

## License

MIT
