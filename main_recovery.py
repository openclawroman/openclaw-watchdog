#!/usr/bin/env python3
"""Main Agent Recovery Sidecar (clean version)"""

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

WATCH_PATH = Path(os.environ.get("TASK_WATCH_PATH", str(Path.home() / ".openclaw" / "workspace" / "memory" / "main-task-watch.json")))
WATCHDOG_CONFIG = Path.home() / ".openclaw" / "workspace" / "heartbeat" / "watchdog.json"
STALL_SEC = 600
LOCK_PATH = Path("/tmp/main-recovery.lock")


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
    if state.get("status") == "active":
        state["status"] = "running"
    defaults = {
        "active": False, "title": "", "startedAt": "", "lastProgressAt": "", "status": "idle",
        "notes": "", "alertedStallAt": "", "completedAt": "", "verifiedResultAt": "",
        "replySentAt": "", "pendingUserUpdate": False, "lastResultSummary": "",
        "alertedPendingReplyAt": "", "recoveryStartedAt": "", "retryCount": 0,
        "recoveryReason": "", "task_text": "", "chat_id": "", "message_id": "",
        "update_id": "", "attachments": [],
    }
    for k, v in defaults.items():
        state.setdefault(k, v)
    return state


def save_state(state: dict) -> None:
    WATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    import tempfile as _tempfile
    with _tempfile.NamedTemporaryFile("w", dir=WATCH_PATH.parent, delete=False) as tf:
        json.dump(state, tf, indent=2)
        tf.write("\n")
        tf.flush()
        os.replace(tf.name, WATCH_PATH)


def send_telegram_alert(text: str, config: dict) -> bool:
    if not config.get("enable_telegram", True):
        return False
    token = config.get("telegram_bot_token")
    chat_id = config.get("telegram_chat_id")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text[:4096], "parse_mode": "Markdown"}).encode()
    try:
        subprocess.run(["curl", "-s", "-X", "POST", "-H", "Content-Type: application/json", "-d", payload, url], timeout=10)
        return True
    except Exception:
        return False


def find_main_pid() -> int | None:
    try:
        out = subprocess.check_output(["pgrep", "-f", "agent:main"], text=True).strip()
        pids = [int(p) for p in out.split() if p]
        return pids[0] if pids else None
    except subprocess.CalledProcessError:
        return None


def acquire_lock() -> bool:
    try:
        if LOCK_PATH.exists():
            try:
                old_pid = int(LOCK_PATH.read_text().strip())
                os.kill(old_pid, 0)
                return False
            except (ValueError, ProcessLookupError):
                LOCK_PATH.unlink()
        LOCK_PATH.write_text(str(os.getpid()))
        return True
    except Exception:
        return False


def release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except Exception:
        pass


def capture_snapshot() -> str:
    lines = []
    try:
        pid = find_main_pid()
        if pid:
            lines.append(f"PID: {pid}")
            out = subprocess.run(["ps", "-p", str(pid), "-o", "pid,%cpu,stat,threads,cmd"], capture_output=True, text=True, timeout=5)
            lines.append(out.stdout.strip())
        else:
            lines.append("No main agent process found")
    except Exception as e:
        lines.append(f"Snapshot error: {e}")
    try:
        log_path = Path.home() / ".openclaw" / "agents" / "main" / "main.log"
        if log_path.exists():
            tail = subprocess.run(["tail", "-50", str(log_path)], capture_output=True, text=True, timeout=5)
            lines.append("=== Last 50 lines of main.log ===")
            lines.append(tail.stdout.strip())
    except Exception:
        pass
    return "\n".join(lines)


def stop_main_gracefully(timeout: int = 30) -> bool:
    try:
        out = subprocess.check_output(["openclaw", "sessions", "list", "--agent", "main", "--json"], text=True, timeout=5)
        try:
            sessions = json.loads(out)
        except Exception:
            sessions = []
        if sessions:
            for s in sessions:
                sess_id = s.get("session_id") or s.get("id")
                if sess_id:
                    subprocess.run(["openclaw", "sessions", "cancel", sess_id], timeout=5)
                    time.sleep(2)
                    if not find_main_pid():
                        return True
                    break
    except Exception:
        pass
    pid = find_main_pid()
    if not pid:
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
            time.sleep(1)
        except ProcessLookupError:
            return True
    try:
        os.kill(pid, signal.SIGKILL)
        time.sleep(1)
        return True
    except ProcessLookupError:
        return True
    except Exception:
        return False


def requeue_task(task_text: str) -> None:
    try:
        subprocess.run(["openclaw", "sessions", "spawn", "--agent", "main", "--task", task_text], timeout=30)
    except Exception as e:
        print(f"Failed to requeue task: {e}", file=sys.stderr)


def recover_task_text_from_sessions() -> str | None:
    sessions_dir = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
    if not sessions_dir.exists():
        return None
    try:
        files = sorted(sessions_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            return None
        latest = files[0]
        lines = []
        with open(latest, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    role = entry.get("role", "?")
                    content = entry.get("content", "")
                    if content:
                        lines.append(f"{role.capitalize()}: {content}")
                except Exception:
                    continue
        if not lines:
            return None
        return "\n\n".join(lines)
    except Exception:
        return None


def process_once() -> None:
    state = load_state()
    if not state.get("active") or state.get("status") in ("done", "interrupted", "recovering", "blocked"):
        return
    last_progress = state.get("lastProgressAt", "")
    if not last_progress:
        return
    try:
        last_dt = datetime.fromisoformat(last_progress.rstrip("Z")).replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - last_dt).total_seconds()
    except Exception:
        return
    if age <= STALL_SEC:
        return
    retry_count = int(state.get("retryCount", 0))
    if retry_count >= 1:
        state["status"] = "interrupted"
        state["recoveryReason"] = "stall_after_retry"
        save_state(state)
        try:
            cfg = json.loads(WATCHDOG_CONFIG.read_text()) if WATCHDOG_CONFIG.exists() else {}
        except Exception:
            cfg = {}
        alert_text = f"🔴 Main agent stalled again (retryCount={retry_count}). Recovery stopped. Human intervention needed."
        send_telegram_alert(alert_text, cfg)
        print("Second stall; interrupted and alerted.")
        return
    # First stall -> recovery
    print(f"Stall detected (age={age:.0f}s). Starting recovery (retryCount={retry_count}).")
    snapshot = capture_snapshot()
    print("--- Pre-recovery snapshot ---")
    print(snapshot)
    state["status"] = "recovering"
    state["recoveryStartedAt"] = now_iso()
    state["recoveryReason"] = "stall"
    state["retryCount"] = retry_count + 1
    save_state(state)
    stopped = stop_main_gracefully(timeout=30)
    if not stopped:
        print("Warning: main agent did not stop cleanly", file=sys.stderr)
    task_text = state.get("task_text", "") or recover_task_text_from_sessions() or ""
    if task_text:
        requeue_task(task_text)
        print("Task requeued.")
    else:
        print("No task_text available to requeue.", file=sys.stderr)
    state["lastProgressAt"] = now_iso()
    state["status"] = "running"
    save_state(state)


def main() -> None:
    interval = 30
    print(f"main-recovery started, monitoring {WATCH_PATH}, interval {interval}s")
    while True:
        if not acquire_lock():
            time.sleep(interval)
            continue
        try:
            process_once()
        except Exception as e:
            print(f"Error in loop: {e}", file=sys.stderr)
        finally:
            release_lock()
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted, exiting.")
    except Exception as e:
        print(f"Fatal: {e}", file=sys.stderr)
        sys.exit(1)
