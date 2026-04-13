
## Watchdog Runtime

- Canonical live config: `~/.openclaw/watchdog.json`
- Legacy fallback: `~/.openclaw/workspace/heartbeat/watchdog.json`
- The watchdog and recovery sidecar both read the same config and continue to use `memory/main-task-watch.json` as the task-watch source of truth.
- Install or relaunch the service with the canonical path so launchd follows the same config the runtime expects.

## Heartbeat Policy

- Heartbeat data files are written locally under `~/.openclaw/workspace/heartbeat/data/`.
- Heartbeat checks use local Ollama only: `ollama/qwen2.5:3b-hb`.
- Default heartbeat cadence is `20m`; worker heartbeats use `21m` so they do not bunch together.
- The scheduler derives a stable per-agent phase offset, so agents keep their own timing instead of firing all at once.
- If Ollama is unavailable, the heartbeat is marked degraded. There is no paid-model fallback path.
- Watchdog scans still run every `5m` and classify the written heartbeat files.

## Telegram Task Spool (Workstream C)

- Spool directory: `~/.openclaw/workspace/state/telegram-task-spool/`
- One file per task: `{task_id}.json`
- Schema includes `idempotency_key`, `telegram_update_id`, `chat_id`, `message_id`, `prompt`, `attachments`, `status`, `retry_count`, timestamps.
- All writes are atomic (tempfile + os.replace).
- Dispatcher daemon: `heartbeat/telegram_spooler.py` (LaunchAgent `ai.openclaw.telegram-spooler`) polls every 5s, claims `queued` tasks, marks `started`, and spawns main agent via `openclaw sessions spawn`.
- Telegram ingestion should enqueue instead of direct dispatch.

## Single-Owner Rule (C6)

- Recovery sidecar uses `/tmp/main-recovery.lock` to ensure only one instance owns the recovery operation.
- Spooler uses atomic claim to prevent duplicate dispatch.
- Task status (`main-task-watch.json`) acts as the source of truth for active tasks; spool entry is auxiliary for replay and diagnostics.

## Recovery Invariants

- Never auto-recover if task-watch `status` in (`verified`, `replied`, `done`).
- Never retry more than once (`retry_count`).
- All writes to spool and task-watch are atomic.
- No active-stall alerts when task status is `recovering`.
- Single-owner enforced via lock file and status-based gating.
- Every spool task has an `idempotency_key` (chat_id:message_id) to prevent duplicates.


## Configuration Knobs

Environment variables or defaults in `config.py`:

| Env var                   | Default | Description |
|---------------------------|---------|-------------|
| `RECOVERY_STALL_SEC`      | 900     | Seconds of no progress before recovery triggers |
| `RECOVERY_GRACE_SEC`      | 45      | Grace period for SIGTERM before SIGKILL |
| `MAX_AUTO_RETRIES_PER_TASK` | 1    | Maximum automatic retries per task |
| `PROGRESS_HEARTBEAT_SEC`  | 120     | Expected progress interval for main task |
| `MAIN_LOG_TAIL_LINES`     | 50      | Number of log lines to include in snapshot |

Set these in the environment where `main_recovery.py` runs (e.g., in the LaunchAgent plist `<key>EnvironmentVariables</key>`).
