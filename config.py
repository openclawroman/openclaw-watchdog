"""Configuration for heartbeat and recovery systems.

Reads from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass

@dataclass
class HeartbeatConfig:
    # Recovery thresholds
    RECOVERY_STALL_SEC: int = int(os.getenv("RECOVERY_STALL_SEC", "900"))
    RECOVERY_GRACE_SEC: int = int(os.getenv("RECOVERY_GRACE_SEC", "45"))
    MAX_AUTO_RETRIES_PER_TASK: int = int(os.getenv("MAX_AUTO_RETRIES", "1"))
    PROGRESS_HEARTBEAT_SEC: int = int(os.getenv("PROGRESS_HEARTBEAT_SEC", "120"))
    MAIN_LOG_TAIL_LINES: int = int(os.getenv("MAIN_LOG_TAIL_LINES", "50"))

# Global config instance (can be overridden in tests)
heartbeat_cfg = HeartbeatConfig()
