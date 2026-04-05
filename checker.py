"""Heartbeat Watchdog v2.2 — Checker / Scanner (Phase 2).

Adds:
  - Full state classification (ok/stall/dead/missing/error/corrupt)
  - Progress-aware stall detection (not time-only)
  - Dynamic thresholds via expected_duration_sec or task_type
  - Anti-flap logic (sustained-state + cooldown)
  - Recovery detection with cooldown
"""

from __future__ import annotations

import os
import json
import time
from typing import Optional
from .config import (
    HeartbeatRecord,
    StallKind,
    STATE_LABELS,
    AgentState,
    WatchdogConfig,
    ThresholdConfig,
    AntiFlapConfig,
)


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------
def classify_state(
    record: Optional[HeartbeatRecord],
    now: Optional[float] = None,
    thresholds: Optional[ThresholdConfig] = None,
) -> StallKind:
    """Full classification: ok / stall / dead / error / corrupt.

    Priority:
      1. If record is None → MISSING (caller should handle separately)
      2. If JSON invalid or required fields missing → CORRUPT (handled in scan_and_parse)
      3. If last_error present and status suggests failure → ERROR
      4. If updated_at too old → DEAD
      5. If progress_counter stuck too long → STALL
      6. Otherwise → OK
    """
    if record is None:
        return StallKind.MISSING

    if now is None:
        now = time.time()

    updated = record.updated_timestamp()
    if updated == 0.0:
        return StallKind.CORRUPT

    # Check for explicit error state
    if record.status in ("error", "failed", "crashed") or (
        record.last_error and record.last_error.strip()
    ):
        return StallKind.ERROR

    threshold_cfg = thresholds or ThresholdConfig()
    dead_after = threshold_cfg.resolve_dead_after(record)
    stall_after = threshold_cfg.resolve_stall_after(record)

    age = now - updated
    if age > dead_after:
        return StallKind.DEAD

    if age > stall_after:
        return StallKind.STALL

    return StallKind.OK


def check_progress_stall(
    record: HeartbeatRecord,
    prev_state: Optional[AgentState],
    now: Optional[float] = None,
    thresholds: Optional[ThresholdConfig] = None,
) -> tuple[StallKind, float]:
    """Progress-aware stall detection.

    If progress_counter hasn't changed since last check and enough time has passed,
    classify as STALL even if updated_at is fresh (warm but not moving).

    Returns: (state, last_progress_change_at)
    """
    if now is None:
        now = time.time()

    base_state = classify_state(record, now=now, thresholds=thresholds)

    # Check if progress is actually moving
    if prev_state and prev_state.last_progress_counter > 0:
        if record.progress_counter == prev_state.last_progress_counter:
            # Progress hasn't changed — check stall_after
            threshold_cfg = thresholds or ThresholdConfig()
            stall_after = threshold_cfg.resolve_stall_after(record)
            idle_time = now - prev_state.last_progress_change_at
            if idle_time > stall_after and base_state == StallKind.OK:
                return StallKind.STALL, prev_state.last_progress_change_at

    # Progress changed or first detection
    if prev_state and record.progress_counter != prev_state.last_progress_counter:
        return base_state, now

    return base_state, now


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
def scan_and_parse(data_dir: str) -> list[dict]:
    """Scan data directory and parse all heartbeat JSON files.

    Returns list of dicts: {file, status, record, error}
    """
    results: list[dict] = []
    if not os.path.isdir(data_dir):
        return results

    for fname in sorted(os.listdir(data_dir)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(data_dir, fname)
        entry: dict = {"file": fname, "status": "valid", "record": None, "error": None}

        try:
            with open(fpath, "r") as f:
                raw = json.load(f)
            record = HeartbeatRecord.from_dict(raw)
            entry["record"] = record
            entry["status"] = "valid"
        except json.JSONDecodeError as e:
            entry["status"] = "corrupt"
            entry["error"] = f"Invalid JSON: {e}"
        except Exception as e:
            entry["status"] = "unreadable"
            entry["error"] = str(e)

        results.append(entry)

    return results


def discover_agents(parsed_results: list[dict]) -> list[str]:
    """Extract unique agent IDs from parsed results. No hardcoded names."""
    agents: set[str] = set()
    for entry in parsed_results:
        record = entry.get("record")
        if record and record.agent_id:
            agents.add(record.agent_id)
    return sorted(agents)


# ---------------------------------------------------------------------------
# Persistent state store
# ---------------------------------------------------------------------------
class PersistentStateStore:
    """Read/write watchdog_state.json across runs."""

    PATH = "watchdog_state.json"

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, self.PATH)
        self._agents: dict[str, AgentState] = {}
        self._load()

    def _load(self) -> None:
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r") as f:
                    data = json.load(f)
                self._agents = {
                    k: AgentState.from_dict(v) for k, v in data.get("agents", {}).items()
                }
            except Exception:
                self._agents = {}

    def save(self) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        fd, tmp = self._tmp_path()
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {"agents": {k: v.to_dict() for k, v in self._agents.items()}},
                    f,
                    indent=2,
                )
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _tmp_path(self):
        import tempfile
        return tempfile.mkstemp(suffix=".tmp", prefix="wd_state_", dir=self.state_dir)

    def get(self, agent_id: str) -> Optional[AgentState]:
        return self._agents.get(agent_id)

    def get_or_create(self, agent_id: str, now: Optional[float] = None) -> AgentState:
        if now is None:
            now = time.time()
        if agent_id not in self._agents:
            self._agents[agent_id] = AgentState(
                agent_id=agent_id, first_seen_at=now, last_seen_at=now,
            )
        return self._agents[agent_id]

    def update(self, agent_id: str, record: HeartbeatRecord, now: Optional[float] = None) -> AgentState:
        if now is None:
            now = time.time()
        state = self.get_or_create(agent_id, now=now)
        state.run_id = record.run_id
        state.task_id = record.task_id
        state.last_seen_at = now

        # Track progress changes
        if record.progress_counter != state.last_progress_counter:
            state.last_progress_change_at = now
            state.last_progress_counter = record.progress_counter

        return state

    def all_known_agents(self) -> list[str]:
        """Return all agent IDs ever seen (for missing detection)."""
        return sorted(self._agents.keys())

    def mark_missing(self, agent_id: str, now: Optional[float] = None) -> AgentState:
        if now is None:
            now = time.time()
        state = self.get_or_create(agent_id, now=now)
        state.last_seen_at = now
        return state


# ---------------------------------------------------------------------------
# Anti-flap engine
# ---------------------------------------------------------------------------
class AntiFlapEngine:
    """Determines whether to alert based on sustained state + cooldown."""

    def __init__(self, config: AntiFlapConfig):
        self.config = config

    def should_alert(
        self,
        agent_state: AgentState,
        current_state: StallKind,
        now: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Check if we should send an alert for this state.

        Alert triggers only when:
          1. The state has been sustained for the required duration, AND
          2. The cooldown has expired

        For recovery (→ok), also tracks separate cooldown.
        Returns: (should_alert, alert_key_for_cooldown)
        """
        if now is None:
            now = time.time()

        key = f"{agent_state.agent_id}:{agent_state.run_id or 'none'}:{current_state.value}"
        last_alert = agent_state.last_alert_sent_at.get(current_state.value, 0.0)
        cooldown = self.config.get_cooldown(current_state)
        if now - last_alert < cooldown:
            return False, key

        is_recovery = current_state == StallKind.OK and agent_state.last_state not in ("ok", "unknown")
        if is_recovery:
            # Recovery also requires sustained OK for sustain_sec
            if agent_state.sustained_state != current_state.value:
                return False, key
            return True, key

        # Non-recovery: only alert if sustained
        if agent_state.sustained_state != current_state.value:
            return False, key

        return True, key

    def update_state(
        self,
        agent_state: AgentState,
        current_state: StallKind,
        now: Optional[float] = None,
    ) -> bool:
        """Update persistent state tracking. Returns True if sustained state changed."""
        if now is None:
            now = time.time()

        if current_state.value != agent_state.last_state:
            # State changed — start tracking entry time
            agent_state.state_entered_at = now
            agent_state.last_state = current_state.value
            return False  # Not yet sustained

        # State is the same as last time — check if sustained
        sustain_required = self.config.get_sustain(current_state)
        if now - agent_state.state_entered_at >= sustain_required:
            if agent_state.sustained_state != current_state.value:
                agent_state.sustained_state = current_state.value
                return True  # New sustained state
            return True  # Already sustained this state

        return False  # Not yet sustained


# ---------------------------------------------------------------------------
# Extended report
# ---------------------------------------------------------------------------
def extended_report(
    parsed_results: list[dict],
    agents: list[str],
    known_agents: list[str],
    state_store: PersistentStateStore,
    antiflap: AntiFlapEngine,
    config: WatchdogConfig,
    startup_grace: bool = False,
) -> dict:
    """Generate extended Phase 2 report."""
    report_data: dict = {
        "total_discovered": len(agents),
        "total_known": len(known_agents),
        "agents": [],
        "stalled": [],
        "dead": [],
        "corrupt": [],
        "error": [],
        "recovered": [],
        "corrupt_files": [],
    }

    # Classify each discovered agent
    for entry in parsed_results:
        record = entry.get("record")
        agent_id = record.agent_id if record else entry.get("file", "unknown")
        prev = state_store.get(agent_id) if record else None

        if entry["status"] == "corrupt":
            report_data["corrupt_files"].append(entry["file"])
            continue

        if not record:
            continue

        new_state = classify_state(record, thresholds=config.thresholds)
        agent_state = state_store.get_or_create(agent_id)
        antiflap.update_state(agent_state, new_state)

        agent_info = {
            "agent_id": agent_id,
            "run_id": record.run_id,
            "task_id": record.task_id,
            "state": new_state.value,
            "label": STATE_LABELS.get(new_state, new_state.value),
            "progress_counter": record.progress_counter,
            "progress_message": record.progress_message,
        }
        report_data["agents"].append(agent_info)

        # Categorize
        if new_state == StallKind.STALL:
            report_data["stalled"].append(agent_id)
        elif new_state == StallKind.DEAD:
            report_data["dead"].append(agent_id)
        elif new_state == StallKind.ERROR:
            report_data["error"].append(agent_id)
        elif new_state == StallKind.CORRUPT:
            report_data["corrupt"].append(agent_id)
        elif new_state == StallKind.OK and prev and prev.last_state not in ("ok", "unknown"):
            report_data["recovered"].append(agent_id)

    # Missing agents (known but no file this cycle)
    missing_agents = []
    for known in known_agents:
        if known not in agents:
            missing_agents.append(known)
    report_data["missing"] = missing_agents

    return report_data


# ---------------------------------------------------------------------------
# Print report helper
# ---------------------------------------------------------------------------
def print_report_v2(report_data: dict, cycle_ms: int = 0, self_hb: Optional[dict] = None,
                    startup_grace: bool = False, log_file: Optional[str] = None) -> None:
    """Print enhanced Phase 2 report."""
    print("\n=== Heartbeat Watchdog v2.2 Report ===")
    print(f"  Discovered agents   : {report_data['total_discovered']}")
    print(f"  Known agents (total): {report_data['total_known']}")

    for info in report_data["agents"]:
        state_icon = STATE_LABELS.get(StallKind(info["state"]), info["state"])
        print(f"    {info['agent_id']:40s} {state_icon}  progress={info['progress_counter']}  run={info['run_id'][:20] if info['run_id'] else 'N/A'}")

    if report_data["stalled"]:
        print(f"  🟡 STALLED          : {', '.join(report_data['stalled'])}")
    if report_data["dead"]:
        print(f"  🔴 DEAD             : {', '.join(report_data['dead'])}")
    if report_data["error"]:
        print(f"  ❌ ERROR            : {', '.join(report_data['error'])}")
    if report_data["corrupt_files"]:
        print(f"  💥 CORRUPT FILES    : {', '.join(report_data['corrupt_files'])}")
    if report_data.get("missing"):
        print(f"  ⚪ MISSING          : {', '.join(report_data['missing'])}")
    if report_data["recovered"]:
        print(f"  🔄 RECOVERED        : {', '.join(report_data['recovered'])}")

    if startup_grace:
        print(f"  ⏳ STARTUP GRACE    : active (alerts suppressed)")

    if self_hb:
        print(f"  Watchdog self       : {self_hb.get('updated_at', 'never')} ({self_hb.get('status', '?')})")
    print(f"  Scan duration       : {cycle_ms}ms")
    if log_file and os.path.exists(log_file):
        print(f"  Log file            : {log_file} ({os.path.getsize(log_file)} bytes)")
    print("=" * 50)


# ---------------------------------------------------------------------------
# Self-heartbeat reader
# ---------------------------------------------------------------------------
def read_self_heartbeat(state_dir: str) -> Optional[dict]:
    """Read watchdog self-heartbeat (watchdog_last_seen.json)."""
    path = os.path.join(state_dir, "watchdog_last_seen.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None
