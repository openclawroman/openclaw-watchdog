"""Configuration and state models for the heartbeat watchdog."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class StallKind(str, Enum):
    OK = "ok"
    STALL = "stall"
    DEAD = "dead"
    MISSING = "missing"
    ERROR = "error"
    CORRUPT = "corrupt"


STATE_LABELS = {
    StallKind.OK: "🟢 OK",
    StallKind.STALL: "🟡 STALL",
    StallKind.DEAD: "🔴 DEAD",
    StallKind.MISSING: "⚪ MISSING",
    StallKind.ERROR: "❌ ERROR",
    StallKind.CORRUPT: "💥 CORRUPT",
}


def _expand_path(value: str) -> str:
    return str(Path(value).expanduser())


def _parse_iso_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


@dataclass
class HeartbeatRecord:
    version: int = 1
    agent_id: str = ""
    run_id: str = ""
    status: str = "ok"
    updated_at: str = ""
    progress_counter: int = 0
    task_id: str = ""
    task_type: str = ""
    progress_message: str = ""
    last_error: str = ""
    expected_duration_sec: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HeartbeatRecord":
        return cls(
            version=int(data.get("version", 1) or 1),
            agent_id=str(data.get("agent_id", "") or ""),
            run_id=str(data.get("run_id", "") or ""),
            status=str(data.get("status", "ok") or "ok"),
            updated_at=str(data.get("updated_at", "") or ""),
            progress_counter=int(data.get("progress_counter", 0) or 0),
            task_id=str(data.get("task_id", "") or ""),
            task_type=str(data.get("task_type", "") or ""),
            progress_message=str(data.get("progress_message", "") or ""),
            last_error=str(data.get("last_error", "") or ""),
            expected_duration_sec=(
                int(data["expected_duration_sec"])
                if data.get("expected_duration_sec") not in (None, "")
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "version": self.version,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "status": self.status,
            "updated_at": self.updated_at,
            "progress_counter": self.progress_counter,
            "task_id": self.task_id,
            "task_type": self.task_type,
            "progress_message": self.progress_message,
            "last_error": self.last_error,
        }
        if self.expected_duration_sec is not None:
            data["expected_duration_sec"] = self.expected_duration_sec
        return data

    def updated_timestamp(self) -> float:
        return _parse_iso_timestamp(self.updated_at)


@dataclass
class AgentState:
    agent_id: str
    first_seen_at: float
    last_seen_at: float
    run_id: str = ""
    task_id: str = ""
    last_progress_counter: int = 0
    last_progress_change_at: float = 0.0
    last_state: str = "unknown"
    sustained_state: str = "unknown"
    state_entered_at: float = 0.0
    last_alert_sent_at: dict[str, float] = field(default_factory=dict)
    last_recovery_alert_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentState":
        return cls(
            agent_id=str(data.get("agent_id", "") or ""),
            first_seen_at=float(data.get("first_seen_at", time.time()) or time.time()),
            last_seen_at=float(data.get("last_seen_at", time.time()) or time.time()),
            run_id=str(data.get("run_id", "") or ""),
            task_id=str(data.get("task_id", "") or ""),
            last_progress_counter=int(data.get("last_progress_counter", 0) or 0),
            last_progress_change_at=float(data.get("last_progress_change_at", 0.0) or 0.0),
            last_state=str(data.get("last_state", "unknown") or "unknown"),
            sustained_state=str(data.get("sustained_state", "unknown") or "unknown"),
            state_entered_at=float(data.get("state_entered_at", 0.0) or 0.0),
            last_alert_sent_at={
                str(k): float(v)
                for k, v in dict(data.get("last_alert_sent_at", {}) or {}).items()
            },
            last_recovery_alert_at=float(data.get("last_recovery_alert_at", 0.0) or 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "last_progress_counter": self.last_progress_counter,
            "last_progress_change_at": self.last_progress_change_at,
            "last_state": self.last_state,
            "sustained_state": self.sustained_state,
            "state_entered_at": self.state_entered_at,
            "last_alert_sent_at": dict(self.last_alert_sent_at),
            "last_recovery_alert_at": self.last_recovery_alert_at,
        }


@dataclass
class ThresholdConfig:
    min_stall_sec: int = 600
    expected_duration_multiplier: float = 1.5
    dead_after_sec: int = 900
    task_type_thresholds: dict[str, int] = field(
        default_factory=lambda: {
            "short_task": 600,
            "normal_task": 1800,
            "long_task": 3600,
            "batch_task": 7200,
            "default": 1800,
        }
    )

    def resolve_stall_after(self, record: HeartbeatRecord) -> int:
        if record.expected_duration_sec:
            return max(
                self.min_stall_sec,
                int(record.expected_duration_sec * self.expected_duration_multiplier),
            )
        if record.task_type and record.task_type in self.task_type_thresholds:
            return max(self.min_stall_sec, int(self.task_type_thresholds[record.task_type]))
        return max(self.min_stall_sec, int(self.task_type_thresholds.get("default", 1800)))

    def resolve_dead_after(self, record: HeartbeatRecord) -> int:
        if self.dead_after_sec:
            return int(self.dead_after_sec)
        return max(self.resolve_stall_after(record), int(self.min_stall_sec))


@dataclass
class AntiFlapConfig:
    sustain_sec: dict[str, int] = field(
        default_factory=lambda: {
            "stall": 600,
            "dead": 300,
            "missing": 300,
            "corrupt": 120,
            "error": 60,
            "ok": 120,
        }
    )
    cooldown_sec: dict[str, int] = field(
        default_factory=lambda: {
            "stall": 1800,
            "dead": 1800,
            "missing": 1800,
            "corrupt": 900,
            "error": 900,
            "ok": 300,
        }
    )

    def get_sustain(self, state: StallKind | str) -> int:
        key = state.value if isinstance(state, StallKind) else str(state)
        return int(self.sustain_sec.get(key, self.sustain_sec.get("ok", 120)))

    def get_cooldown(self, state: StallKind | str) -> int:
        key = state.value if isinstance(state, StallKind) else str(state)
        return int(self.cooldown_sec.get(key, self.cooldown_sec.get("ok", 300)))


@dataclass
class WatchdogConfig:
    heartbeat_data_dir: str = "~/.openclaw/workspace/heartbeat/data"
    watchdog_state_dir: str = "~/.openclaw/workspace/heartbeat/watchdog"
    expected_agents_mode: str = "auto"
    scan_interval_sec: int = 300
    startup_grace_sec: int = 600
    telegram_chat_id: str | None = None
    telegram_bot_token: str | None = None
    log_file: str = "~/.openclaw/workspace/heartbeat/watchdog/watchdog.log"
    antiflap: AntiFlapConfig = field(default_factory=AntiFlapConfig)
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    enable_telegram: bool = True
    show_ok: bool = False
    _loaded_at: float = field(default_factory=time.time, repr=False)

    def __post_init__(self) -> None:
        self.heartbeat_data_dir = _expand_path(self.heartbeat_data_dir)
        self.watchdog_state_dir = _expand_path(self.watchdog_state_dir)
        self.log_file = _expand_path(self.log_file)

    @classmethod
    def from_file(cls, path: str | None = None) -> "WatchdogConfig":
        if not path:
            return cls()
        with open(Path(path).expanduser(), "r") as f:
            raw = json.load(f)
        config = cls(
            heartbeat_data_dir=raw.get("heartbeat_data_dir", cls.heartbeat_data_dir),
            watchdog_state_dir=raw.get("watchdog_state_dir", cls.watchdog_state_dir),
            expected_agents_mode=raw.get("expected_agents_mode", cls.expected_agents_mode),
            scan_interval_sec=int(raw.get("scan_interval_sec", cls.scan_interval_sec)),
            startup_grace_sec=int(raw.get("startup_grace_sec", cls.startup_grace_sec)),
            telegram_chat_id=raw.get("telegram_chat_id"),
            telegram_bot_token=raw.get("telegram_bot_token"),
            log_file=raw.get("log_file", cls.log_file),
            enable_telegram=bool(raw.get("enable_telegram", True)),
            show_ok=bool(raw.get("show_ok", raw.get("showOk", False))),
        )
        thresholds = raw.get("thresholds", {})
        if thresholds:
            config.thresholds = ThresholdConfig(
                min_stall_sec=int(thresholds.get("min_stall_sec", config.thresholds.min_stall_sec)),
                expected_duration_multiplier=float(
                    thresholds.get(
                        "expected_duration_multiplier",
                        config.thresholds.expected_duration_multiplier,
                    )
                ),
                dead_after_sec=int(thresholds.get("dead_after_sec", config.thresholds.dead_after_sec)),
                task_type_thresholds=dict(
                    thresholds.get(
                        "task_type_thresholds",
                        config.thresholds.task_type_thresholds,
                    )
                ),
            )
        antiflap = raw.get("antiflap", {})
        if antiflap:
            config.antiflap = AntiFlapConfig(
                sustain_sec=dict(antiflap.get("sustain_sec", config.antiflap.sustain_sec)),
                cooldown_sec=dict(antiflap.get("cooldown_sec", config.antiflap.cooldown_sec)),
            )
        return config

    def is_in_startup_grace(self, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        return (current - self._loaded_at) < self.startup_grace_sec
