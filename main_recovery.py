#!/usr/bin/env python3
"""Main Agent Recovery Sidecar

Monitors main-task-watch.json for stall and performs one safe retry:
- Graceful shutdown of main agent (SIGTERM → wait → SIGKILL)
- Requeue original task via sessions_spawn
- Limited to one retry; second stall triggers high-priority Telegram alert.

Runs as loop (default 30s). Intended as launchd service ai.openclaw.main-recovery.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Resolve same watch path as task_watch.py
def resolve_watch_path() -> Path:
    env = os.environ.get("TASK_WATCH_PATH")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("OPENCLAW_HOME") or Path.home()
    return Path(home, ".openclaw", "workspace", "memory", "main-task-watch.json")

WATCH_PATH = resolve_watch_path()

# Path to watchdog config for Telegram alerts
WATCHDOG_CONFIG = Path.home() / ".openclaw" / "workspace" / "heartbeat" / "watchdog.json"

# Stall threshold
STALL_SEC = 600

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def load_state() -> dict:
    if WATCH_PATH.exists():
        try:
            state = json.loads(WATCH_PATH.read_text())
        except Exception:
            state = {}
    else:
        state = {}
    # Ensure minimal keys
    defaults = {
        "active": False,
        "status": "idle",
        "lastProgressAt": "",
        "retryCount": 0,
        "recoveryStartedAt": "",
        "recoveryReason": "",
        "task_text": "",
    }
    for k, v in defaults.items():
        state.setdefault(k, v)
    return state

def atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile as _tempfile
    with _tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tf:
        json.dump(data, tf, indent=2)
        tf.write("\n")
        tf.flush()
        os.replace(tf.name, path)

def save_state(state: dict) -> None:
    atomic_write(WATCH_PATH, state)

def send_telegram_alert(text: str, config: dict) -> bool:
    """Send Telegram alert if config has token/chat_id and enable_telegram true."""
    if not config.get("enable_telegram", True):
        return False
    token = config.get("telegram_bot_token")
    chat_id = config.get("telegram_chat_id")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "Markdown",
    }).encode()
    try:
        subprocess.run(
            ["curl", "-s", "-X", "POST", "-H", "Content-Type: application/json", "-d", payload, url],
            timeout=10,
        )
        return True
    except Exception:
        return False

def find_main_pid() -> int | None:
    """Return PID of main agent process, if any."""
    # Look for process containing 'agent:main' in command line
    try:
        output = subprocess.check_output(
            ["pgrep", "-f", "agent:main"],
            text=True
        ).strip()
        pids = [int(pid) for pid in output.split() if pid]
        return pids[0] if pids else None
    except subprocess.CalledProcessError:
        return None

def stop_main_gracefully(timeout: int = 30) -> bool:
    """Send SIGTERM, wait, then SIGKILL if needed. Returns True if stopped."""
    pid = find_main_pid()
    if not pid:
        return True  # already stopped
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    # Wait
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)  # check existence
            time.sleep(1)
        except ProcessLookupError:
            return True
    # Force kill
    try:
        os.kill(pid, signal.SIGKILL)
        time.sleep(1)
        return True
    except ProcessLookupError:
        return True
    except Exception:
        return False

def requeue_task(task_text: str) -> None:
    """Spawn a new main agent session with the given task text."""
    try:
        subprocess.run(
            ["openclaw", "sessions", "spawn", "--agent", "main", "--task", task_text],
            timeout=30,
        )
    except Exception as e:
        print(f"Failed to requeue task: {e}", file=sys.stderr)


def recover_task_text_from_sessions() -> str | None:
    """Fallback: extract the first user message from the most recent main session log."""
    sessions_dir = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
    if not sessions_dir.exists():
        return None
    try:
        files = sorted(sessions_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            return None
        latest = files[0]
        with open(latest, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("role") == "user":
                        return entry.get("content", "")
                except Exception:
                    continue
    except Exception:
        return None
    return None

def main_loop(interval: int) -> None:
    print(f"main-recovery started, monitoring {WATCH_PATH}, interval {interval}s")
    while True:
        try:
            state = load_state()
            if state.get("active") and state.get("status") not in ("done", "interrupted", "recovering", "blocked"):
                last_progress = state.get("lastProgressAt", "")
                if last_progress:
                    try:
                        last_dt = datetime.fromisoformat(last_progress.rstrip("Z")).replace(tzinfo=timezone.utc)
                        age = (datetime.now(timezone.utc) - last_dt).total_seconds()
                        if age > STALL_SEC:
                            # Stall detected
                            retry_count = int(state.get("retryCount", 0))
                            if retry_count >= 1:
                                # Second stall → alert and mark interrupted
                                state["status"] = "interrupted"
                                state["recoveryReason"] = "stall_after_retry"
                                save_state(state)
                                # Try Telegram alert
                                try:
                                    cfg = {}
                                    if WATCHDOG_CONFIG.exists():
                                        cfg = json.loads(WATCHDOG_CONFIG.read_text())
                                except Exception:
                                    cfg = {}
                                alert_text = f"🔴 Main agent stalled again (retryCount={retry_count}). Recovery stopped. Human intervention needed."
                                send_telegram_alert(alert_text, cfg)
                                print("Second stall; interrupted and alerted.")
                                time.sleep(interval)
                                continue
                            # First stall → attempt recovery
                            print(f"Stall detected (age={age:.0f}s). Starting recovery (retryCount={retry_count}).")
                            state["status"] = "recovering"
                            state["recoveryStartedAt"] = now_iso()
                            state["recoveryReason"] = "stall"
                            state["retryCount"] = retry_count + 1
                            save_state(state)
                            # Graceful shutdown
                            stopped = stop_main_gracefully(timeout=30)
                            if not stopped:
                                print("Warning: main agent did not stop cleanly", file=sys.stderr)
                            # Requeue original task
                            task_text = state.get("task_text", "")
                            if not task_text:
                                task_text = recover_task_text_from_sessions() or ""
                            if task_text:
                                requeue_task(task_text)
                                print("Task requeued.")
                            else:
                                print("No task_text available to requeue.", file=sys.stderr)
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nInterrupted, exiting.")
            break
        except Exception as e:
            print(f"Error in loop: {e}", file=sys.stderr)
            time.sleep(interval)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=int, default=30, help="Check interval in seconds")
    args = p.parse_args()
    try:
        main_loop(args.interval)
    except Exception as e:
        print(f"Fatal: {e}", file=sys.stderr)
        sys.exit(1)