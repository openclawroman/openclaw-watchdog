"""Tests for watchdog suppression of alerts during recovering"""

import json
import time

from watchdog import _check_active_task_progress

def test_active_task_progress_suppressed_when_recovering(monkeypatch, tmp_path):
    # Simulate main-task-watch.json with status='recovering'
    # Create a temporary watch file
    watch_file = tmp_path / "main-task-watch.json"
    state = {
        "active": True,
        "status": "recovering",
        "lastProgressAt": "",
        "title": "Test",
        "notes": ""
    }
    watch_file.write_text(json.dumps(state))
    # Patch WATCH_PATH to point to temp file
    import watchdog
    monkeypatch.setattr(watchdog, "WATCH_PATH", watch_file)
    now = time.time()
    alerts = _check_active_task_progress(now, None)
    assert alerts == []  # no alert when recovering

def test_active_task_alert_when_running_and_stale(monkeypatch, tmp_path):
    watch_file = tmp_path / "main-task-watch.json"
    stale_time = "2026-01-01T00:00:00Z"
    state = {
        "active": True,
        "status": "running",
        "lastProgressAt": stale_time,
        "title": "Stale",
        "notes": ""
    }
    watch_file.write_text(json.dumps(state))
    import watchdog
    monkeypatch.setattr(watchdog, "WATCH_PATH", watch_file)
    now = time.time() + 10000  # far in future
    alerts = _check_active_task_progress(now, None)
    assert len(alerts) == 1
    assert "⏳" in alerts[0][1]

if __name__ == "__main__":
    import pytest
    pytest.main([__file__])
