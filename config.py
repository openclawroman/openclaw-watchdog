"""Heartbeat Watchdog v2.2 — Configuration & Data Models (Phase 2).

Adds:
  - Dynamic threshold config (expected_duration, task_type mapping)
  - Anti-flap config (sustain_sec, cooldown_sec per state)
  - Persistent agent state schema
"""

from __future__ import annotations

import os
import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------
class StallKind(Enum):
    """Full health classification for Phase 2."""
    OK = "ok"
    STALL = "stall"
    DEAD = "dead"
    MISSING = "missing"
    ERROR = "error"
    CORRUPT = "corrupt"


# Human-readable labels
STATE_LABELS = {
    StallKind.OK:      "✅ OK",
    StallKind.STALL:   "🟡 Stall",
    StallKind.DEAD:    "🔴 Dead",
    StallKind.MISSING: "⚪ Missing",
    StallKind.ERROR:   "❌ Error",
    StallKind.CORRUPT: "💥 Corrupt",
}


# ---------------------------------------------------------------------------
# Heartbeat record (schema v1)
# ---------------------------------------------------------------------------
@dataclass
class HeartbeatRecord:
    """Normalized heartbeat record parsed from a JSON file.

    Required: version, agent_id, run_id, status, updated_at, progress_counter
    Optional: task_id, task_type, progress_message, expected_duration_sec, last_error
    """
    version: int
    agent_id: str
    run_id: str
    status: str
    updated_at: str            # ISO-8601
    progress_counter: int
    task_id: Optional[str] = None
    task_type: Optional[str] = None
    progress_message: Optional[str] = None
    expected_duration_sec: Optional[int] = None
    last_error: Optional[str] = None

    REQUIRED_FIELDS = frozenset(
        ["version", "agent_id", "run_id", "status", "updated_at", "progress_counter"]
    )

    @classmethod
    def from_dict(cls, data: dict) -> HeartbeatRecord:
        missing = cls.REQUIRED_FIELDS - set(data.keys())
        if missing:
            raise ValueError(f"Missing required heartbeat fields: {missing}")
        return HeartbeatRecord(
            version=int(data["version"]),
            agent_id=str(data["agent_id"]),
            run_id=str(data["run_id"]),
            status=str(data["status"]),
            updated_at=str(data["updated_at"]),
            progress_counter=int(data["progress_counter"]),
            task_id=data.get("task_id"),
            task_type=data.get("task_type"),
            progress_message=data.get("progress_message"),
            expected_duration_sec=data.get("expected_duration_sec"),
            last_error=data.get("last_error"),
        )

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items()}

    def updated_timestamp(self) -> float:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# Persistent agent state (survives restarts)
# ---------------------------------------------------------------------------
@dataclass
class AgentState:
    """Per-agent persistent state stored in watchdog_state.json."""
    agent_id: str
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    last_state: str = "unknown"
    sustained_state: str = "unknown"      # state held for >= sustain_sec
    state_entered_at: float = 0.0
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0
    last_progress_counter: int = 0
    last_progress_change_at: float = 0.0
    last_alert_sent_at: dict = field(default_factory=dict)   # {state_key: timestamp}
    last_recovery_alert_at: Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict) -> AgentState:
        return AgentState(
            agent_id=data["agent_id"],
            run_id=data.get("run_id"),
            task_id=data.get("task_id"),
            last_state=data.get("last_state", "unknown"),
            sustained_state=data.get("sustained_state", "unknown"),
            state_entered_at=data.get("state_entered_at", 0.0),
            first_seen_at=data.get("first_seen_at", 0.0),
            last_seen_at=data.get("last_seen_at", 0.0),
            last_progress_counter=data.get("last_progress_counter", 0),
            last_progress_change_at=data.get("last_progress_change_at", 0.0),
            last_alert_sent_at=data.get("last_alert_sent_at", {}) or {},
            last_recovery_alert_at=data.get("last_recovery_alert_at"),
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Threshold configuration
# ---------------------------------------------------------------------------
@dataclass
class ThresholdConfig:
    """Dynamic threshold configuration for stall/dead detection."""
    min_stall_sec: int = 600
    expected_duration_multiplier: float = 1.5
    dead_after_sec: int = 900

    task_type_thresholds: dict = field(default_factory=lambda: {
        "short_task": 600,
        "normal_task": 1800,
        "long_task": 3600,
        "batch_task": 7200,
        "default": 1800,
    })

    def resolve_stall_after(self, record: Optional[HeartbeatRecord] = None) -> int:
        """Compute stall_after for a specific heartbeat record.

        Priority:
          1. expected_duration_sec * multiplier
          2. task_type threshold from config
          3. default threshold
          4. min_stall_sec (floor)
        """
        if record and record.expected_duration_sec and record.expected_duration_sec > 0:
            return max(self.min_stall_sec, int(record.expected_duration_sec * self.expected_duration_multiplier))

        task_type = record.task_type if record else None
        if task_type and task_type in self.task_type_thresholds:
            return max(self.min_stall_sec, self.task_type_thresholds[task_type])

        return max(self.min_stall_sec, self.task_type_thresholds.get("default", 1800))

    def resolve_dead_after(self, record: Optional[HeartbeatRecord] = None) -> int:
        """Dead after is typically stall_after * 1.5, with a floor."""
        stall = self.resolve_stall_after(record)
        return max(self.dead_after_sec, int(stall * 1.5))


# ---------------------------------------------------------------------------
# Anti-flap configuration
# ---------------------------------------------------------------------------
@dataclass
class AntiFlapConfig:
    """Sustained-state + cooldown config to prevent alert flapping."""
    sustain_sec: dict = field(default_factory=lambda: {
        "stall": 600,
        "dead": 300,
        "missing": 300,
        "corrupt": 120,
        "error": 60,
        "ok": 120,
    })

    cooldown_sec: dict = field(default_factory=lambda: {
        "stall": 1800,
        "dead": 1800,
        "missing": 1800,
        "corrupt": 900,
        "error": 900,
        "ok": 300,
    })

    def get_sustain(self, kind: StallKind) -> int:
        return self.sustain_sec.get(kind.value, 120)

    def get_cooldown(self, kind: StallKind) -> int:
        return self.cooldown_sec.get(kind.value, 900)


# ---------------------------------------------------------------------------
# Watchdog configuration
# ---------------------------------------------------------------------------
@dataclass
class WatchdogConfig:
    """Configuration for the watchdog runner."""
    heartbeat_data_dir: str = field(default_factory=lambda:
        os.path.expanduser("~/.openclaw/workspace/heartbeat/data"))
    watchdog_state_dir: str = field(default_factory=lambda:
        os.path.expanduser("~/.openclaw/workspace/heartbeat/watchdog"))
    expected_agents_mode: str = "auto"          # "auto" | "explicit"
    scan_interval_sec: int = 300

    # Anti-flap
    antiflap: AntiFlapConfig = field(default_factory=AntiFlapConfig)
    # Thresholds
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)

    # Service / startup
    startup_grace_sec: int = 600
    service_started_at: Optional[float] = None

    # Logging
    log_file: str = field(default_factory=lambda:
        os.path.expanduser("~/.openclaw/workspace/heartbeat/watchdog/watchdog.log"))

    # Telegram notification
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    def is_in_startup_grace(self, now: Optional[float] = None) -> bool:
        """Check if still within warm-up window after service start."""
        if now is None:
            now = time.time()
        if self.service_started_at is None:
            return False  # not tracking, skip grace
        return (now - self.service_started_at) < self.startup_grace_sec

    def ensure_dirs(self) -> None:
        os.makedirs(self.heartbeat_data_dir, exist_ok=True)
        os.makedirs(self.watchdog_state_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

    @classmethod
    def from_file(cls, path: Optional[str] = None) -> "WatchdogConfig":
        """Load config from JSON, falling back to defaults."""
        if path is None:
            path = os.path.join(
                os.path.expanduser("~/.openclaw/workspace/heartbeat"),
                "watchdog.json",
            )
        if not os.path.isfile(path):
            cfg = cls()
            cfg.service_started_at = time.time()
            cfg.ensure_dirs()
            return cfg

        with open(path, "r") as f:
            data = json.load(f)

        antiflap_data = data.get("antiflap", {})
        thresholds_data = data.get("thresholds", {})

        _default_data = os.path.expanduser("~/.openclaw/workspace/heartbeat/data")
        _default_watch = os.path.expanduser("~/.openclaw/workspace/heartbeat/watchdog")
        _default_log = os.path.expanduser("~/.openclaw/workspace/heartbeat/watchdog/watchdog.log")
        cfg = cls(
            heartbeat_data_dir=os.path.expanduser(
                data.get("heartbeat_data_dir", _default_data)),
            watchdog_state_dir=os.path.expanduser(
                data.get("watchdog_state_dir", _default_watch)),
            expected_agents_mode=data.get("expected_agents_mode", cls.expected_agents_mode),
            scan_interval_sec=int(data.get("scan_interval_sec", cls.scan_interval_sec)),
            startup_grace_sec=int(data.get("startup_grace_sec", cls.startup_grace_sec)),
            service_started_at=time.time(),
            log_file=os.path.expanduser(data.get("log_file", _default_log)),
            telegram_bot_token=data.get("telegram_bot_token"),
            telegram_chat_id=str(data["telegram_chat_id"]) if data.get("telegram_chat_id") else None,
        )
        cfg.ensure_dirs()

        if antiflap_data:
            cfg.antiflap = AntiFlapConfig(
                sustain_sec={**cfg.antiflap.sustain_sec, **antiflap_data.get("sustain_sec", {})},
                cooldown_sec={**cfg.antiflap.cooldown_sec, **antiflap_data.get("cooldown_sec", {})},
            )
        if thresholds_data:
            cfg.thresholds = ThresholdConfig(
                min_stall_sec=int(thresholds_data.get("min_stall_sec", cfg.thresholds.min_stall_sec)),
                expected_duration_multiplier=float(thresholds_data.get(
                    "expected_duration_multiplier", cfg.thresholds.expected_duration_multiplier)),
                dead_after_sec=int(thresholds_data.get("dead_after_sec", cfg.thresholds.dead_after_sec)),
                task_type_thresholds={**cfg.thresholds.task_type_thresholds,
                                      **thresholds_data.get("task_type_thresholds", {})},
            )

        return cfg

    def save(self, path: Optional[str] = None) -> None:
        if path is None:
            path = os.path.join(
                os.path.expanduser("~/.openclaw/workspace/heartbeat"),
                "watchdog.json",
            )
        with open(path, "w") as f:
            json.dump({
                "heartbeat_data_dir": self.heartbeat_data_dir,
                "watchdog_state_dir": self.watchdog_state_dir,
                "expected_agents_mode": self.expected_agents_mode,
                "scan_interval_sec": self.scan_interval_sec,
                "startup_grace_sec": self.startup_grace_sec,
                "log_file": self.log_file,
                "telegram_chat_id": self.telegram_chat_id,
                "antiflap": {
                    "sustain_sec": self.antiflap.sustain_sec,
                    "cooldown_sec": self.antiflap.cooldown_sec,
                },
                "thresholds": {
                    "min_stall_sec": self.thresholds.min_stall_sec,
                    "expected_duration_multiplier": self.thresholds.expected_duration_multiplier,
                    "dead_after_sec": self.thresholds.dead_after_sec,
                    "task_type_thresholds": self.thresholds.task_type_thresholds,
                },
            }, f, indent=2)
