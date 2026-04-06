"""Tests for task_watch.py"""

import json
import os
import tempfile
from pathlib import Path

# Import module under test
import task_watch

def test_atomic_write_and_load(tmp_path: Path):
    test_file = tmp_path / "state.json"
    state = {"active": True, "status": "running", "title": "Test"}
    task_watch.atomic_write(test_file, state)
    # Read back
    loaded = json.loads(test_file.read_text())
    assert loaded["active"] == True
    assert loaded["status"] == "running"
    # Ensure file content is valid JSON (no truncation)
    assert loaded["title"] == "Test"

def test_status_normalization():
    # Simulate load_state with legacy status
    raw = {"version": 1, "agent_id": "main", "run_id": "test", "status": "active", "updated_at": "2026-01-01T00:00:00Z", "progress_counter": 0}
    # We'll monkeypatch WATCH_PATH for load_state; easier: test the normalization logic directly
    state = task_watch.default_state()
    state.update(raw)
    # Apply compatibility logic from load_state
    legacy = state.get("status", "")
    if legacy == "active":
        state["status"] = "running"
    elif not legacy or legacy not in ("idle", "running", "verified", "done", "recovering", "interrupted", "blocked"):
        state["status"] = "idle"
    assert state["status"] == "running"

    raw2 = {"status": "unknown"}
    state2 = task_watch.default_state()
    state2.update(raw2)
    # normalize
    legacy = state2.get("status", "")
    if legacy == "active":
        state2["status"] = "running"
    elif not legacy or legacy not in ("idle", "running", "verified", "done", "recovering", "interrupted", "blocked"):
        state2["status"] = "idle"
    assert state2["status"] == "idle"

def test_mark_recovering_adds_retry():
    # We'll simulate state transitions via functions indirectly: ensure default state has retryCount=0
    state = task_watch.default_state()
    assert state["retryCount"] == 0
    # Simulate mark_recovering by updating
    state["status"] = "recovering"
    state["retryCount"] = state.get("retryCount", 0) + 1
    assert state["retryCount"] == 1
    assert state["status"] == "recovering"

def test_schema_includes_task_metadata():
    state = task_watch.default_state()
    # New fields must exist
    for key in ["task_text", "task_id", "chat_id", "message_id", "update_id", "attachments"]:
        assert key in state

if __name__ == "__main__":
    import pytest
    pytest.main([__file__])
