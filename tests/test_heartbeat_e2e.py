from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import main_recovery as mr
import watchdog as wd
from heartbeat.config import WatchdogConfig
from heartbeat.logging import Logger


def _write_config(path: Path, *, data_dir: Path, state_dir: Path, log_file: Path, telegram_chat_id: str = "123") -> None:
    config = {
        "heartbeat_data_dir": str(data_dir),
        "watchdog_state_dir": str(state_dir),
        "expected_agents_mode": "auto",
        "scan_interval_sec": 300,
        "startup_grace_sec": 600,
        "enable_telegram": False,
        "telegram_bot_token": None,
        "telegram_chat_id": telegram_chat_id,
        "log_file": str(log_file),
        "antiflap": {
            "sustain_sec": {
                "stall": 600,
                "dead": 300,
                "missing": 300,
                "corrupt": 120,
                "error": 60,
                "ok": 120,
            },
            "cooldown_sec": {
                "stall": 1800,
                "dead": 1800,
                "missing": 1800,
                "corrupt": 900,
                "error": 900,
                "ok": 300,
            },
        },
        "thresholds": {
            "min_stall_sec": 600,
            "expected_duration_multiplier": 1.5,
            "dead_after_sec": 900,
            "task_type_thresholds": {
                "short_task": 600,
                "normal_task": 1800,
                "long_task": 3600,
                "batch_task": 7200,
                "default": 1800,
            },
        },
    }
    path.write_text(json.dumps(config, indent=2) + "\n")


def test_generate_plist_includes_home_env_and_canonical_config(tmp_path):
    config_path = tmp_path / "watchdog.json"
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    log_file = tmp_path / "watchdog.log"
    data_dir.mkdir()
    state_dir.mkdir()
    _write_config(config_path, data_dir=data_dir, state_dir=state_dir, log_file=log_file)

    config = WatchdogConfig.from_file(str(config_path))
    plist = wd._generate_plist(config)

    assert "<key>HOME</key>" in plist
    assert f"<string>{wd.HOME}</string>" in plist
    assert f"<string>{wd.CANONICAL_CONFIG_PATH}</string>" in plist


def test_main_recovery_reads_watchdog_config_at_runtime(tmp_path, monkeypatch):
    watch_path = tmp_path / "main-task-watch.json"
    config_path = tmp_path / "runtime-watchdog.json"
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    log_file = tmp_path / "watchdog.log"
    data_dir.mkdir()
    state_dir.mkdir()
    _write_config(config_path, data_dir=data_dir, state_dir=state_dir, log_file=log_file, telegram_chat_id="runtime-123")

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    watch_path.write_text(
        json.dumps(
            {
                "active": True,
                "title": "Runtime config test",
                "startedAt": stale,
                "lastProgressAt": stale,
                "status": "running",
                "notes": "",
                "alertedStallAt": "",
                "completedAt": "",
                "verifiedResultAt": "",
                "replySentAt": "",
                "pendingUserUpdate": False,
                "lastResultSummary": "",
                "alertedPendingReplyAt": "",
                "recoveryStartedAt": "",
                "retryCount": 1,
                "recoveryReason": "",
                "task_text": "runtime path task",
                "task_id": "task-123",
                "chat_id": "",
                "message_id": "",
                "update_id": "",
                "attachments": [],
            },
            indent=2,
        )
        + "\n"
    )

    monkeypatch.setenv("WATCHDOG_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(mr, "WATCH_PATH", watch_path)
    monkeypatch.setattr(mr, "STALL_SEC", 1)
    monkeypatch.setattr(mr, "MAX_RETRIES", 1)

    captured: dict[str, dict] = {}

    def fake_send(text: str, cfg: dict) -> bool:
        captured["cfg"] = cfg
        return True

    monkeypatch.setattr(mr, "send_telegram_alert", fake_send)

    mr.process_once()

    assert captured["cfg"]["telegram_chat_id"] == "runtime-123"
    assert json.loads(watch_path.read_text())["status"] == "interrupted"


def test_watchdog_and_recovery_end_to_end(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    watch_dir = tmp_path / "watch"
    data_dir.mkdir()
    state_dir.mkdir()
    watch_dir.mkdir()

    config_path = tmp_path / "watchdog.json"
    log_file = tmp_path / "watchdog.log"
    _write_config(config_path, data_dir=data_dir, state_dir=state_dir, log_file=log_file)

    now = datetime.now(timezone.utc)
    recent = now.isoformat().replace("+00:00", "Z")
    stale = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    (data_dir / "demo-agent.json").write_text(
        json.dumps(
            {
                "version": 1,
                "agent_id": "demo-agent",
                "run_id": "run-1",
                "status": "alive",
                "updated_at": recent,
                "progress_counter": 3,
                "task_id": "task-1",
                "task_type": "normal_task",
                "progress_message": "demo progress",
                "last_error": "",
                "ollama_alive": True,
            },
            indent=2,
        )
        + "\n"
    )

    task_watch = watch_dir / "main-task-watch.json"
    task_watch.write_text(
        json.dumps(
            {
                "active": True,
                "title": "Demo main task",
                "startedAt": stale,
                "lastProgressAt": stale,
                "status": "running",
                "notes": "demo",
                "alertedStallAt": "",
                "completedAt": "",
                "verifiedResultAt": "",
                "replySentAt": "",
                "pendingUserUpdate": False,
                "lastResultSummary": "",
                "alertedPendingReplyAt": "",
                "recoveryStartedAt": "",
                "retryCount": 0,
                "recoveryReason": "",
                "task_text": "demo task text",
                "task_id": "task-1",
                "chat_id": "",
                "message_id": "",
                "update_id": "",
                "attachments": [],
            },
            indent=2,
        )
        + "\n"
    )

    cfg = WatchdogConfig.from_file(str(config_path))
    logger = Logger(log_file=cfg.log_file)
    monkeypatch.setattr(wd, "WATCH_PATH", task_watch)
    monkeypatch.setattr(wd, "MAIN_TASK_WATCH_PATH", task_watch)

    report, alerts = wd.run_cycle(cfg, logger, 1)

    assert report["total_discovered"] == 1
    assert alerts
    assert (state_dir / "watchdog_state.json").exists()
    assert (state_dir / "watchdog_last_seen.json").exists()

    monkeypatch.setenv("WATCHDOG_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(mr, "WATCH_PATH", task_watch)
    monkeypatch.setattr(mr, "STALL_SEC", 1)
    monkeypatch.setattr(mr, "MAX_RETRIES", 2)
    monkeypatch.setattr(mr, "find_main_pid", lambda: None)
    monkeypatch.setattr(mr, "capture_snapshot", lambda: "snapshot")
    monkeypatch.setattr(mr, "stop_main_gracefully", lambda timeout=1: True)

    recovered: list[str] = []
    monkeypatch.setattr(mr, "requeue_task", lambda task_text: recovered.append(task_text))
    monkeypatch.setattr(mr, "send_telegram_alert", lambda text, cfg: True)

    mr.process_once()

    updated = json.loads(task_watch.read_text())
    assert updated["status"] == "running"
    assert updated["retryCount"] == 1
    assert recovered == ["demo task text"]
