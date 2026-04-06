
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

