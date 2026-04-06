"""Tests for spool module"""

import json
import os
from pathlib import Path
import tempfile

import spool

def test_enqueue_and_load(tmp_path: Path):
    # Temporarily override SPOOL_DIR
    original_spool_dir = spool.SPOOL_DIR
    spool.SPOOL_DIR = tmp_path / "spool"
    spool.SPOOL_DIR.mkdir(parents=True)
    entry = spool.enqueue(
        prompt="test prompt",
        chat_id="123",
        message_id="456",
        update_id="789",
        attachments=[]
    )
    task_id = entry["task_id"]
    assert entry["status"] == "queued"
    assert entry["prompt"] == "test prompt"
    # Load it back
    loaded = spool.load(task_id)
    assert loaded is not None
    assert loaded["task_id"] == task_id
    assert loaded["idempotency_key"] == "123:456"
    # Restore
    spool.SPOOL_DIR = original_spool_dir

def test_atomic_write_is_durable(tmp_path: Path):
    test_file = tmp_path / "data.json"
    data = {"x": 1, "y": 2}
    spool.atomic_write(test_file, data)
    # File should exist and contain valid JSON
    raw = test_file.read_text()
    loaded = json.loads(raw)
    assert loaded == data

def test_claim_next_queued_atomicity(tmp_path: Path):
    original_spool_dir = spool.SPOOL_DIR
    spool.SPOOL_DIR = tmp_path / "spool"
    spool.SPOOL_DIR.mkdir(parents=True)
    # Create two queued entries
    e1 = spool.enqueue("p1", "c1", "m1", "u1", [])
    e2 = spool.enqueue("p2", "c2", "m2", "u2", [])
    # Claim one
    claimed = spool.claim_next_queued()
    assert claimed is not None
    assert claimed["task_id"] in (e1["task_id"], e2["task_id"])
    assert claimed["status"] == "claimed"
    # The other should still be queued
    remaining = spool.claim_next_queued()
    assert remaining is not None
    # No more queued tasks
    third = spool.claim_next_queued()
    assert third is None
    spool.SPOOL_DIR = original_spool_dir

def test_mark_started_completed_failed(tmp_path: Path):
    original_spool_dir = spool.SPOOL_DIR
    spool.SPOOL_DIR = tmp_path / "spool"
    spool.SPOOL_DIR.mkdir(parents=True)
    entry = spool.enqueue("p", "c", "m", "u", [])
    spool.mark_started(entry["task_id"])
    updated = spool.load(entry["task_id"])
    assert updated["status"] == "started"
    assert updated["started_at"] != ""
    spool.mark_completed(entry["task_id"])
    completed = spool.load(entry["task_id"])
    assert completed["status"] == "completed"
    assert completed["completed_at"] != ""
    # Reset and test failed
    entry2 = spool.enqueue("p2", "c2", "m2", "u2", [])
    spool.mark_failed(entry2["task_id"], error="boom")
    failed = spool.load(entry2["task_id"])
    assert failed["status"] == "failed"
    assert failed["last_error"] == "boom"
    spool.SPOOL_DIR = original_spool_dir

if __name__ == "__main__":
    import pytest
    pytest.main([__file__])
