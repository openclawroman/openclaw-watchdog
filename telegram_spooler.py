#!/usr/bin/env python3
"""Telegram Task Spooler

Watches spool directory for queued tasks and dispatches them to main agent via sessions_spawn.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from spool import claim_next_queued, mark_started, mark_failed, update, SPOOL_DIR


def resolve_dispatch_log_path() -> Path:
    override = os.environ.get("TELEGRAM_SPOOL_DISPATCH_LOG")
    if override:
        return Path(override).expanduser()
    return SPOOL_DIR / "dispatch.jsonl"


DISPATCH_LOG_PATH = resolve_dispatch_log_path()


def resolve_task_watch_command() -> list[str]:
    """Resolve the task_watch entrypoint for the current environment."""
    override = os.environ.get("TASK_WATCH_COMMAND")
    if override:
        return shlex.split(override)

    script_override = os.environ.get("TASK_WATCH_SCRIPT")
    if script_override:
        return [sys.executable, str(Path(script_override).expanduser())]

    local_script = Path(__file__).resolve().with_name("task_watch.py")
    if local_script.exists():
        return [sys.executable, str(local_script)]

    legacy_script = Path.home() / ".openclaw" / "workspace" / "heartbeat" / "task_watch.py"
    if legacy_script.exists():
        return [sys.executable, str(legacy_script)]

    shim = shutil.which("task_watch")
    if shim:
        return [shim]

    return [sys.executable, str(local_script)]


def resolve_openclaw_command() -> list[str]:
    override = os.environ.get("OPENCLAW_COMMAND")
    if override:
        return shlex.split(override)
    return ["openclaw"]


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def extract_path_excerpt() -> list[str]:
    raw = os.environ.get("PATH", "")
    if not raw:
        return []
    return raw.split(os.pathsep)[:8]


def extract_session_metadata(*texts: str) -> dict[str, str]:
    joined = "\n".join(text for text in texts if text)
    patterns = {
        "thread_id": [
            r'"thread_id"\s*:\s*"([^"]+)"',
            r"\bthread[_ -]?id\b\s*[:=]\s*([A-Za-z0-9:_./-]+)",
        ],
        "session_id": [
            r'"session_id"\s*:\s*"([^"]+)"',
            r"\bsession[_ -]?id\b\s*[:=]\s*([A-Za-z0-9:_./-]+)",
        ],
    }
    metadata: dict[str, str] = {}
    for key, regexes in patterns.items():
        for pattern in regexes:
            match = re.search(pattern, joined, re.IGNORECASE)
            if match:
                metadata[key] = match.group(1)
                break
    return metadata


def append_dispatch_event(event: str, handoff_id: str, entry: dict, **details) -> None:
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "handoff_id": handoff_id,
        "task_id": entry.get("task_id", ""),
        "idempotency_key": entry.get("idempotency_key", ""),
        "chat_id": str(entry.get("chat_id", "") or ""),
        "message_id": str(entry.get("message_id", "") or ""),
        "update_id": str(entry.get("update_id") or entry.get("telegram_update_id") or ""),
        "prompt_sha256": prompt_sha256(str(entry.get("prompt", "") or "")),
        "attachments_count": len(entry.get("attachments") or []),
        "cwd": os.getcwd(),
        "path_excerpt": extract_path_excerpt(),
    }
    payload.update(details)
    DISPATCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DISPATCH_LOG_PATH, "a") as f:
        f.write(json.dumps(payload) + "\n")


def dispatch_task(entry: dict, handoff_id: str) -> bool:
    """Spawn a main agent session with the given task payload."""
    task_id = entry["task_id"]
    prompt = entry["prompt"]
    task_watch_cmd = resolve_task_watch_command()
    openclaw_cmd = resolve_openclaw_command()
    openclaw_binary = shutil.which(openclaw_cmd[0]) or openclaw_cmd[0]
    task_watch_binary = shutil.which(task_watch_cmd[0]) or task_watch_cmd[0]

    try:
        task_watch_args = ["mark-active", "--task-id", task_id, "--task-text", prompt]
        if entry.get("chat_id"):
            task_watch_args.extend(["--chat-id", str(entry["chat_id"])])
        if entry.get("message_id"):
            task_watch_args.extend(["--message-id", str(entry["message_id"])])
        update_id = entry.get("update_id") or entry.get("telegram_update_id")
        if update_id:
            task_watch_args.extend(["--update-id", str(update_id)])
        attachments = entry.get("attachments") or []
        if attachments:
            task_watch_args.append("--attachments")
            task_watch_args.extend(str(item) for item in attachments)

        result = subprocess.run(
            [*task_watch_cmd, *task_watch_args],
            timeout=5,
            check=True,
            capture_output=True,
            text=True,
        )
        append_dispatch_event(
            "mark_active",
            handoff_id,
            entry,
            task_watch_command=task_watch_cmd,
            task_watch_binary=task_watch_binary,
            task_watch_exit_code=result.returncode,
            task_watch_stdout=result.stdout,
            task_watch_stderr=result.stderr,
        )
    except FileNotFoundError:
        append_dispatch_event(
            "mark_active",
            handoff_id,
            entry,
            task_watch_command=task_watch_cmd,
            task_watch_binary=task_watch_binary,
            task_watch_error="command_not_found",
        )
        print(f"Warning: task_watch command not found: {task_watch_cmd[0]}", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        append_dispatch_event(
            "mark_active",
            handoff_id,
            entry,
            task_watch_command=task_watch_cmd,
            task_watch_binary=task_watch_binary,
            task_watch_exit_code=e.returncode,
            task_watch_stdout=e.stdout,
            task_watch_stderr=e.stderr,
        )
        details = (e.stderr or e.stdout or "").strip()
        if details:
            print(f"Warning: mark-active failed ({e.returncode}): {details}", file=sys.stderr)
        else:
            print(f"Warning: mark-active failed: exit {e.returncode}", file=sys.stderr)
    except Exception as e:
        append_dispatch_event(
            "mark_active",
            handoff_id,
            entry,
            task_watch_command=task_watch_cmd,
            task_watch_binary=task_watch_binary,
            task_watch_error=str(e),
        )
        print(f"Warning: mark-active failed: {e}", file=sys.stderr)

    spawn_cmd = [*openclaw_cmd, "sessions", "spawn", "--agent", "main", "--task", prompt]
    append_dispatch_event(
        "spawn_openclaw",
        handoff_id,
        entry,
        openclaw_command=spawn_cmd,
        openclaw_binary=openclaw_binary,
    )
    try:
        result = subprocess.run(
            spawn_cmd,
            timeout=30,
            check=True,
            capture_output=True,
            text=True,
        )
        metadata = extract_session_metadata(result.stdout, result.stderr)
        if metadata:
            update(task_id, **metadata)
        append_dispatch_event(
            "spawn_result",
            handoff_id,
            entry,
            openclaw_command=spawn_cmd,
            openclaw_binary=openclaw_binary,
            openclaw_exit_code=result.returncode,
            openclaw_stdout=result.stdout,
            openclaw_stderr=result.stderr,
            **metadata,
        )
        return True
    except FileNotFoundError:
        append_dispatch_event(
            "dispatch_failed",
            handoff_id,
            entry,
            openclaw_command=spawn_cmd,
            openclaw_binary=openclaw_binary,
            openclaw_error="command_not_found",
        )
        print(f"Failed to dispatch {task_id}: openclaw command not found", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        metadata = extract_session_metadata(e.stdout, e.stderr)
        if metadata:
            update(task_id, **metadata)
        append_dispatch_event(
            "dispatch_failed",
            handoff_id,
            entry,
            openclaw_command=spawn_cmd,
            openclaw_binary=openclaw_binary,
            openclaw_exit_code=e.returncode,
            openclaw_stdout=e.stdout,
            openclaw_stderr=e.stderr,
            **metadata,
        )
        details = (e.stderr or e.stdout or "").strip()
        if details:
            print(f"Failed to dispatch {task_id}: {details}", file=sys.stderr)
        else:
            print(f"Failed to dispatch {task_id}: exit {e.returncode}", file=sys.stderr)
        return False
    except Exception as e:
        append_dispatch_event(
            "dispatch_failed",
            handoff_id,
            entry,
            openclaw_command=spawn_cmd,
            openclaw_binary=openclaw_binary,
            openclaw_error=str(e),
        )
        print(f"Failed to dispatch {task_id}: {e}", file=sys.stderr)
        return False

def main_loop(interval: int = 5) -> None:
    print(f"telegram-spooler started, watching {SPOOL_DIR}, interval {interval}s")
    while True:
        try:
            entry = claim_next_queued()
            if entry:
                task_id = entry["task_id"]
                prompt = entry["prompt"]
                handoff_id = uuid.uuid4().hex
                print(f"Dispatching task {task_id} from spool (chat_id={entry['chat_id']})")
                update(task_id, handoff_id=handoff_id)
                append_dispatch_event("claim", handoff_id, entry)
                # Mark started before dispatch to avoid double-dispatch
                mark_started(task_id)
                ok = dispatch_task(entry, handoff_id)
                if not ok:
                    mark_failed(task_id, error="dispatch_failed")
                # If dispatch succeeded, we consider it handed off; main will mark active and progress.
                # We don't wait for completion; spool entry stays as 'started' for forensics.
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nSpooler interrupted.")
            break
        except Exception as e:
            print(f"Spooler error: {e}", file=sys.stderr)
            time.sleep(interval)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=int, default=5, help="Poll interval seconds")
    args = p.parse_args()
    try:
        main_loop(args.interval)
    except Exception as e:
        print(f"Fatal: {e}", file=sys.stderr)
        sys.exit(1)
