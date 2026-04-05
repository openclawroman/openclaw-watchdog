# OpenClaw Heartbeat

Heartbeat watchdog system for monitoring OpenClaw agent health.

## Features

- **Agent health scanning** — discovers and monitors agents via heartbeat data files
- **Status classification** — ok, stall, dead, missing, corrupt, error
- **Anti-flap protection** — cooldowns and sustain periods to prevent alert storms
- **Telegram alerts** — sends notifications for agent status changes
- **Task watchdog** — monitors pending user updates and active task progress nags

## Usage

```bash
# Clone
git clone https://github.com/dasdsafagasasdsa/openclaw-watchdog.git

# One-shot scan
python3 watchdog.py --config watchdog.json

# Loop mode (continuous)
python3 watchdog.py --loop --config watchdog.json
```

## Configuration

| Key | Description |
|---|---|
| `heartbeat_data_dir` | Directory where agents write heartbeat files |
| `watchdog_state_dir` | Where watchdog persists its own state |
| `scan_interval_sec` | Seconds between scan cycles |
| `startup_grace_sec` | Initial period with suppressed alerts |
| `telegram_chat_id` | Chat ID for alerts |
| `telegram_bot_token` | Bot token (null for local-only) |

## Project Structure

```
.
├── __init__.py      # Package init
├── watchdog.py      # Main scanner + loop
├── checker.py       # Status classification logic
├── config.py        # Config loading + defaults
├── writer.py        # Heartbeat data writer
├── notifier.py      # Telegram notifier
├── logging.py       # Structured logging
├── task_watch.py    # Task watchdog state management
├── data/            # Agent heartbeat data (empty, created by agents)
└── watchdog/        # State directory (runtime files)
```

## License

MIT
