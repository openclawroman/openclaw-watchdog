"""Heartbeat Watchdog v2.2 — Writer module (Phase 1).

Atomic heartbeat writes via tempfile + os.rename().
Supports agent heartbeat files and watchdog self-heartbeat.
"""

from __future__ import annotations

import os
import json
import tempfile
from typing import Optional

from heartbeat.config import HeartbeatRecord


class HeartbeatWriter:
    """Write heartbeat records atomically to disk.

    - Writes to a temp file in the same directory
    - Flushes and closes the file
    - Renames atomically to target
    - Never writes directly to target (avoids partial writes)
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

    @staticmethod
    def _target_path(data_dir: str, agent_id: str) -> str:
        """Compute target filename for an agent's heartbeat."""
        safe = agent_id.replace("/", "_").replace("\\", "_")
        return os.path.join(data_dir, f"{safe}.json")

    def write(self, agent_id: str, record: HeartbeatRecord) -> str:
        """Write a heartbeat record atomically. Returns target path."""
        target = self._target_path(self.data_dir, agent_id)
        data = record.to_dict()

        # Write to temp file in same directory (atomic rename requirement)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp", prefix=f"hb_{agent_id}_", dir=self.data_dir
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, target)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return target

    def write_raw(self, agent_id: str, data: dict) -> str:
        """Write raw dict as heartbeat (convenience for non-Record data)."""
        target = self._target_path(self.data_dir, agent_id)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp", prefix=f"hb_{agent_id}_", dir=self.data_dir
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return target

    def write_self_heartbeat(
        self,
        state_dir: str,
        version: str,
        cycle_duration_ms: int,
        checked_agents: int,
        status: str = "ok",
    ) -> str:
        """Write watchdog self-heartbeat to watchdog state directory.

        File: {state_dir}/watchdog_last_seen.json
        """
        os.makedirs(state_dir, exist_ok=True)
        import time
        from datetime import datetime, timezone

        data = {
            "service": "heartbeat-watchdog",
            "version": version,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "cycle_duration_ms": cycle_duration_ms,
            "checked_agents": checked_agents,
        }

        target = os.path.join(state_dir, "watchdog_last_seen.json")
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp", prefix="wd_self_", dir=state_dir
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return target
