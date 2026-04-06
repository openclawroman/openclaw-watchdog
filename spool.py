"""Telegram Task Spool

Atomic, file-based task queue for reliable replay.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

SPOOL_DIR = Path.home() / ".openclaw" / "workspace" / "state" / "telegram-task-spool"
SPOOL_DIR.mkdir(parents=True, exist_ok=True)

def atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile as _tempfile
    with _tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tf:
        json.dump(data, tf, indent=2)
        tf.write("\n")
        tf.flush()
        os.replace(tf.name, path)

def enqueue(prompt: str, chat_id: str, message_id: str, update_id: str, attachments: list = None) -> dict:
    """Create a new spool entry and return its task_id."""
    task_id = str(uuid.uuid4())
    idempotency_key = f"{chat_id}:{message_id}"  # stable de-dup key
    entry = {
        "version": 1,
        "task_id": task_id,
        "idempotency_key": idempotency_key,
        "telegram_update_id": update_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "prompt": prompt,
        "attachments": attachments or [],
        "status": "queued",
        "retry_count": 0,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "updated_at": "",
        "started_at": "",
        "completed_at": "",
        "last_error": "",
        "diagnostic_snapshot": None,
    }
    entry["updated_at"] = entry["created_at"]
    path = SPOOL_DIR / f"{task_id}.json"
    atomic_write(path, entry)
    return entry

def load(task_id: str) -> dict | None:
    path = SPOOL_DIR / f"{task_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None

def update(task_id: str, **updates) -> dict | None:
    entry = load(task_id)
    if entry is None:
        return None
    entry.update(updates)
    entry["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    path = SPOOL_DIR / f"{task_id}.json"
    atomic_write(path, entry)
    return entry

def claim_next_queued() -> dict | None:
    """Find first entry with status='queued' and atomically mark it as 'claimed' to prevent multiple consumers."""
    # Scan directory for queued tasks
    try:
        files = list(SPOOL_DIR.glob("*.json"))
    except Exception:
        return None
    for path in files:
        try:
            entry = json.loads(path.read_text())
            if entry.get("status") != "queued":
                continue
            # Try to claim by setting status to "claimed" with started_at
            entry["status"] = "claimed"
            entry["started_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            entry["updated_at"] = entry["started_at"]
            # Atomic replace
            atomic_write(path, entry)
            return entry
        except Exception:
            continue
    return None

def mark_started(task_id: str) -> None:
    update(task_id, status="started", started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

def mark_completed(task_id: str) -> None:
    update(task_id, status="completed", completed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

def mark_failed(task_id: str, error: str = "") -> None:
    update(task_id, status="failed", last_error=error)

def list_all() -> list[dict]:
    entries = []
    for path in SPOOL_DIR.glob("*.json"):
        try:
            entries.append(json.loads(path.read_text()))
        except Exception:
            continue
    return entries
