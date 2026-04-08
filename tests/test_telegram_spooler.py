import json
import subprocess

import telegram_spooler


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_dispatch_logs_mark_active_and_openclaw_failure(tmp_path, monkeypatch, capsys):
    log_path = tmp_path / "dispatch.jsonl"
    entry = {
        "task_id": "task-1",
        "idempotency_key": "chat:msg",
        "chat_id": "chat",
        "message_id": "msg",
        "update_id": "upd",
        "prompt": "Fix the failure",
        "attachments": ["a.png"],
    }

    monkeypatch.setattr(telegram_spooler, "DISPATCH_LOG_PATH", log_path)
    monkeypatch.setattr(telegram_spooler, "update", lambda *args, **kwargs: None)
    monkeypatch.setattr(telegram_spooler, "resolve_task_watch_command", lambda: ["task_watch"])
    monkeypatch.setattr(telegram_spooler, "resolve_openclaw_command", lambda: ["openclaw"])
    monkeypatch.setattr(telegram_spooler.shutil, "which", lambda name: f"/mock/bin/{name}")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "task_watch":
            return subprocess.CompletedProcess(cmd, 0, stdout="marked active\n", stderr="")
        raise subprocess.CalledProcessError(7, cmd, output="", stderr="spawn task_watch ENOENT")

    monkeypatch.setattr(telegram_spooler.subprocess, "run", fake_run)

    ok = telegram_spooler.dispatch_task(entry, "handoff-1")

    assert ok is False
    assert calls[0][0] == "task_watch"
    assert calls[1][0] == "openclaw"

    lines = _read_jsonl(log_path)
    assert [line["event"] for line in lines] == ["mark_active", "spawn_openclaw", "dispatch_failed"]
    assert lines[0]["task_watch_exit_code"] == 0
    assert lines[2]["openclaw_exit_code"] == 7
    assert "spawn task_watch ENOENT" in lines[2]["openclaw_stderr"]

    captured = capsys.readouterr()
    assert "spawn task_watch ENOENT" in captured.err


def test_dispatch_extracts_thread_and_session_ids(tmp_path, monkeypatch):
    log_path = tmp_path / "dispatch.jsonl"
    entry = {
        "task_id": "task-2",
        "idempotency_key": "chat:msg2",
        "chat_id": "chat",
        "message_id": "msg2",
        "update_id": "upd2",
        "prompt": "Run task",
        "attachments": [],
    }

    updates = []

    monkeypatch.setattr(telegram_spooler, "DISPATCH_LOG_PATH", log_path)
    monkeypatch.setattr(telegram_spooler, "resolve_task_watch_command", lambda: ["task_watch"])
    monkeypatch.setattr(telegram_spooler, "resolve_openclaw_command", lambda: ["openclaw"])
    monkeypatch.setattr(telegram_spooler.shutil, "which", lambda name: f"/mock/bin/{name}")
    monkeypatch.setattr(telegram_spooler, "update", lambda task_id, **kwargs: updates.append((task_id, kwargs)))

    def fake_run(cmd, **kwargs):
        if cmd[0] == "task_watch":
            return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")
        stdout = json.dumps({"thread_id": "thread-123", "session_id": "sess-456"})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(telegram_spooler.subprocess, "run", fake_run)

    ok = telegram_spooler.dispatch_task(entry, "handoff-2")

    assert ok is True
    assert updates[-1] == ("task-2", {"thread_id": "thread-123", "session_id": "sess-456"})

    lines = _read_jsonl(log_path)
    assert lines[-1]["event"] == "spawn_result"
    assert lines[-1]["thread_id"] == "thread-123"
    assert lines[-1]["session_id"] == "sess-456"
