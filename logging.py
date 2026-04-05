"""Heartbeat Watchdog v2.2 — Structured Logger (Phase 3).

Logs to file and stdout with format:
  YYYY-MM-DDTHH:MM:SSZ [level] [component] message
Components: scan, state, alert, self, error, notifier, service
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

LEVELS = {"info": 0, "warn": 1, "error": 2}

class Logger:
    def __init__(self, log_file: Optional[str] = None, min_level: str = "info"):
        self.log_file = log_file
        self.min_level = LEVELS.get(min_level, 0)
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)

    def _ts(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _write(self, level: str, component: str, msg: str):
        if LEVELS.get(level, 99) < self.min_level:
            return
        line = f"{self._ts()} [{level:5s}] [{component:10s}] {msg}"
        print(line, flush=True)
        if self.log_file:
            try:
                with open(self.log_file, "a") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    def info(self, component: str, msg: str):
        self._write("info", component, msg)

    def warn(self, component: str, msg: str):
        self._write("warn", component, msg)

    def error(self, component: str, msg: str):
        self._write("error", component, msg)

    def state_change(self, agent_id: str, old: str, new: str):
        self.info("state", f"{agent_id}: {old} → {new}")

    def alert_sent(self, agent_id: str, state: str):
        self.info("alert", f"SENT {state} for {agent_id}")

    def alert_suppressed(self, agent_id: str, state: str, reason: str):
        self.info("alert", f"SUPPRESSED {state} for {agent_id} ({reason})")

    def scan_cycle(self, cycle: int, agents: int, duration_ms: int):
        self.info("scan", f"cycle#{cycle}: {agents} agents, {duration_ms}ms")

    def parse_error(self, file: str, error: str):
        self.error("scan", f"corrupt {file}: {error}")

    def notifier_failure(self, error: str):
        self.error("notifier", f"send failed: {error}")

    def service_start(self, version: str):
        self.info("service", f"Starting Watchdog v{version}")

    def self_heartbeat(self, agents_checked: int, cycle_ms: int):
        self.info("self", f"self-heartbeat: {agents_checked} agents, {cycle_ms}ms")
