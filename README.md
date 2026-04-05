# OpenClaw Heartbeat

Custom heartbeat watchdog system for monitoring OpenClaw agent health. Runs locally, uses ollama — $0 cost.

## Architecture

```
Agent Heartbeat Daemon  →  heartbeat/data/*.json  →  Watchdog Scanner
   (LaunchAgent)              (data files)           (loop scan)
```

### 3 Layers

#### Layer 1: Heartbeat Writer

**Script:** `scripts/heartbeat-daemon.py`

- Writes one `.json` heartbeat file per agent into `data/`
- Runs every 15 min via macOS LaunchAgent (`ai.openclaw.heartbeat`, StartInterval=900s)
- Staggered: 10 seconds between each agent (~120s total for 13 agents)
- Checks local ollama (`http://127.0.0.1:11434/api/tags`) to verify it's alive
- Counts `.jsonl` session files per agent
- **Cost: $0** — ollama `qwen2.5:3b` locally, no paid APIs

**Per-agent output** (`data/coder.json`):
```json
{
  "agent_id": "coder",
  "timestamp": "2026-04-05T09:44:00+00:00",
  "status": "alive",
  "model": "ollama/qwen2.5:3b",
  "ollama_alive": true,
  "session_count": 3
}
```

#### Layer 2: Heartbeat Data Files

- One `.json` per agent in `data/`
- Format defined by `HeartbeatRecord` in `config.py`
- Written atomically via `tempfile` + `os.rename()` (from `writer.py`)

#### Layer 3: Watchdog Scanner

**Script:** `watchdog.py` — runs as LaunchAgent `ai.openclaw.heartbeat-watchdog`

- Scan interval: 300s (5 min)
- Reads every `*.json` in `data/`, classifies each agent's health
- Sends Telegram alerts on state changes with dedup + cooldown

**Status Classification** (from `checker.py`):

| Status    | Meaning                                    |
| --------- | ------------------------------------------ |
| `ok`      | Recent heartbeat, within expected interval |
| `stall`   | Timestamp old, within dead threshold       |
| `dead`    | Exceeds `dead_after_sec` (900s)            |
| `missing` | No heartbeat file found                    |
| `corrupt` | File exists but can't parse JSON           |
| `error`   | File parseable but marked error state      |

**Anti-flap protection:**
- `sustain_sec` — bad state must persist before alerting
- `cooldown_sec` — minimum time between repeated alerts of same type
- Prevents alert storms on flapping agents

## Thresholds

| Setting                      | Value                                |
| ---------------------------- | ------------------------------------ |
| Scan interval                | 300s                                 |
| Startup grace                | 600s (no alerts at boot)             |
| Dead after                   | 900s (15 min)                        |
| Min stall                    | 600s                                 |
| Stall sustained              | 600s before alert                    |
| Dead sustained               | 300s before alert                    |
| Cooldown (stall/dead/missing)| 1800s between repeated alerts        |

## Usage

```bash
# One-shot scan
python3 watchdog.py --config watchdog.json

# Loop mode (continuous)
python3 watchdog.py --loop --config watchdog.json
```

## Configuration

Edit `watchdog.json`:

| Key                  | Description                                  |
| -------------------- | -------------------------------------------- |
| `heartbeat_data_dir` | Directory where agents write heartbeat files |
| `watchdog_state_dir` | Where watchdog persists its own state        |
| `scan_interval_sec`  | Seconds between scan cycles                  |
| `startup_grace_sec`  | Initial period with suppressed alerts        |
| `telegram_chat_id`   | Chat ID for alerts                           |
| `telegram_bot_token` | Bot token (null for local-only)              |

## Setup

1. Copy config: `cp watchdog.example.json watchdog.json`
2. Edit `watchdog.json` — set `heartbeat_data_dir` to your agents' data directory
3. Install `scripts/heartbeat-daemon.py` as a LaunchAgent (runs every 15 min)
4. Start watchdog: `python3 watchdog.py --loop --config watchdog.json`

## License

MIT
