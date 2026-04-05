#!/usr/bin/env python3
"""Heartbeat Watchdog v2.2 — CLI Runner (Phase 3).

Phase 3 additions:
  - launchd service mode (--loop with logging)
  - startup grace / boot warm-up
  - structured logging
  - hardened error handling (one bad file won't crash the scan)
  - --check-self with freshness detection
  - --install / --uninstall launchd helpers

Usage:
  python3 watchdog.py --once
  python3 watchdog.py --loop
  python3 watchdog.py --report
  python3 watchdog.py --check-self
  python3 watchdog.py --install     # install launchd service
  python3 watchdog.py --uninstall   # remove launchd service
  python3 watchdog.py --status      # check launchd service status
  python3 watchdog.py --config path/to/watchdog.json
"""

from __future__ import annotations

import sys
import os
import time
import json
import argparse
import signal
import subprocess
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from heartbeat.config import (
    WatchdogConfig, HeartbeatRecord, StallKind, STATE_LABELS,
)
from heartbeat.writer import HeartbeatWriter
from heartbeat.checker import (
    scan_and_parse,
    discover_agents,
    classify_state,
    check_progress_stall,
    read_self_heartbeat,
    PersistentStateStore,
    AntiFlapEngine,
    extended_report,
    print_report_v2,
)
from heartbeat.notifier import TelegramNotifier
from heartbeat.logging import Logger

__version__ = "2.2"
__phase__ = "3 — service + self-monitoring + hardening"
PLIST_ID = "ai.openclaw.heartbeat-watchdog"
HOME = os.path.expanduser("~")
WATCHDOG_DIR = os.path.join(HOME, ".openclaw/workspace/heartbeat")
WATCHDOG_PY = os.path.join(WATCHDOG_DIR, "watchdog.py")
PLIST_PATH = os.path.join(HOME, "Library/LaunchAgents", f"{PLIST_ID}.plist")
LOG_PATH = os.path.join(HOME, ".openclaw/workspace/heartbeat/watchdog/watchdog.log")
SELF_HB_PATH = os.path.join(HOME, ".openclaw/workspace/heartbeat/watchdog/watchdog_last_seen.json")
MAIN_TASK_WATCH_PATH = os.path.join(HOME, ".openclaw/workspace/memory/main-task-watch.json")
PENDING_REPLY_ALERT_SEC = 300
ACTIVE_PROGRESS_ALERT_SEC = 300


# ---------------------------------------------------------------------------
# Signal handling for graceful shutdown
# ---------------------------------------------------------------------------
_running = True

def _handle_signal(signum, frame):
    global _running
    _running = False

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# launchd management
# ---------------------------------------------------------------------------
def _generate_plist(config: WatchdogConfig) -> str:
    """Generate launchd plist XML content."""
    log_dir = os.path.dirname(config.log_file)
    os.makedirs(log_dir, exist_ok=True)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_ID}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>{WATCHDOG_PY}</string>
        <string>--loop</string>
        <string>--config</string>
        <string>{os.path.join(WATCHDOG_DIR, "watchdog.json")}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{WATCHDOG_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>SuccessfulExit</key>
    <false/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{config.log_file}</string>
    <key>StandardErrorPath</key>
    <string>{config.log_file}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>Nice</key>
    <integer>20</integer>
</dict>
</plist>
'''


def cmd_install(config: WatchdogConfig) -> bool:
    """Install launchd service."""
    if os.path.exists(PLIST_PATH):
        print(f"Already installed: {PLIST_PATH}")
        print("Run --uninstall first to reinstall.")
        return False

    plist = _generate_plist(config)
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "w") as f:
        f.write(plist)
    print(f"Created: {PLIST_PATH}")

    # Load the service
    try:
        subprocess.run(["launchctl", "load", PLIST_PATH], check=True, capture_output=True)
        print("Service loaded successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Warning: launchctl load returned: {e.stderr.decode().strip()}")
        print(f"Try: launchctl load {PLIST_PATH}")
    return True


def cmd_uninstall() -> bool:
    """Remove launchd service."""
    if not os.path.exists(PLIST_PATH):
        print(f"Not installed: {PLIST_PATH}")
        return True

    # Unload first
    try:
        subprocess.run(["launchctl", "unload", PLIST_PATH], check=True, capture_output=True)
        print("Service unloaded.")
    except subprocess.CalledProcessError as e:
        print(f"Warning: launchctl unload: {e.stderr.decode().strip()}")
        print(f"Try: launchctl unload {PLIST_PATH}")

    os.unlink(PLIST_PATH)
    print(f"Removed: {PLIST_PATH}")
    return True


def cmd_status() -> None:
    """Check launchd service status."""
    # Check plist
    plist_exists = os.path.exists(PLIST_PATH)
    print(f"  launchd plist : {'installed ✅' if plist_exists else 'not installed ❌'}")

    # Check if watchdog process is running
    try:
        result = subprocess.run(
            ["launchctl", "list", PLIST_ID],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print(f"  service status: running ✅")
        else:
            print(f"  service status: not running ❌")
    except Exception:
        print(f"  service status: unknown")

    # Check self-heartbeat freshness
    if os.path.exists(SELF_HB_PATH):
        try:
            with open(SELF_HB_PATH) as f:
                hb = json.load(f)
            updated = hb.get("updated_at", "?")
            try:
                dt = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - dt).total_seconds()
                if age < 600:
                    print(f"  watchdog fresh  : {age:.0f}s ago ✅")
                else:
                    print(f"  watchdog fresh  : {age:.0f}s ago ⚠️ STALE")
            except Exception:
                print(f"  watchdog fresh  : parse error")
        except Exception:
            print(f"  watchdog fresh  : cannot read")
    else:
        print(f"  watchdog fresh  : no data")


# ---------------------------------------------------------------------------
# Watchdog cycle (hardened)
# ---------------------------------------------------------------------------
def _load_main_task_watch(logger: Logger) -> dict | None:
    try:
        if not os.path.exists(MAIN_TASK_WATCH_PATH):
            return None
        with open(MAIN_TASK_WATCH_PATH) as f:
            return json.load(f)
    except Exception as e:
        logger.error("task-watch", f"Failed to read main-task-watch.json: {e}")
        return None


def _save_main_task_watch(data: dict, logger: Logger) -> None:
    try:
        with open(MAIN_TASK_WATCH_PATH, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    except Exception as e:
        logger.error("task-watch", f"Failed to persist main-task-watch.json: {e}")


def _parse_iso_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _check_pending_user_update(now: float, logger: Logger) -> list[tuple[str, str]]:
    alerts: list[tuple[str, str]] = []
    data = _load_main_task_watch(logger)
    if not data:
        return alerts
    if not data.get("pendingUserUpdate"):
        return alerts
    if data.get("replySentAt"):
        return alerts
    verified_ts = _parse_iso_ts(data.get("verifiedResultAt"))
    if not verified_ts:
        return alerts
    if (now - verified_ts) < PENDING_REPLY_ALERT_SEC:
        return alerts
    if data.get("alertedPendingReplyAt"):
        return alerts

    title = data.get("title") or "main task"
    summary = data.get("lastResultSummary") or data.get("notes") or "Verified result exists but no chat update was sent."
    alerts.append(("main-task-watch", f"📣 **Pending user update** — {title}\n{summary}"))
    data["alertedPendingReplyAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _save_main_task_watch(data, logger)
    return alerts


def _check_active_task_progress(now: float, logger: Logger) -> list[tuple[str, str]]:
    alerts: list[tuple[str, str]] = []
    data = _load_main_task_watch(logger)
    if not data:
        return alerts
    if not data.get("active"):
        return alerts
    if data.get("pendingUserUpdate"):
        return alerts
    progress_ts = _parse_iso_ts(data.get("lastProgressAt")) or _parse_iso_ts(data.get("startedAt"))
    if not progress_ts:
        return alerts
    if (now - progress_ts) < ACTIVE_PROGRESS_ALERT_SEC:
        return alerts
    if data.get("alertedStallAt"):
        return alerts

    title = data.get("title") or "main task"
    status = data.get("status") or "active"
    notes = data.get("notes") or "Task is still active but no fresh user-facing update was sent."
    alerts.append(("main-task-watch", f"⏳ **Active task still running** — {title}\nstatus={status}\n{notes}"))
    data["alertedStallAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _save_main_task_watch(data, logger)
    return alerts


def run_cycle(config: WatchdogConfig, logger: Logger, cycle_num: int) -> tuple[dict, list]:
    """Execute one full watchdog scan cycle with error handling.

    Returns: (report_data, alerts_to_send)
    """
    start_ms = time.monotonic()
    log = logger
    startup_grace = config.is_in_startup_grace()

    # Load persistent state
    try:
        state_store = PersistentStateStore(config.watchdog_state_dir)
    except Exception as e:
        log.error("scan", f"Failed to load state store: {e}")
        state_store = PersistentStateStore.__new__(PersistentStateStore)
        state_store._agents = {}
        state_store.state_dir = config.watchdog_state_dir
        state_store.path = os.path.join(config.watchdog_state_dir, "watchdog_state.json")

    try:
        antiflap = AntiFlapEngine(config.antiflap)
    except Exception:
        antiflap = AntiFlapEngine.__new__(AntiFlapEngine)
        antiflap.config = config.antiflap

    # Scan + parse (individual file errors won't crash)
    alerts_to_send: list[tuple[str, str]] = []
    try:
        parsed = scan_and_parse(config.heartbeat_data_dir)
    except Exception as e:
        log.error("scan", f"Directory scan failed: {e}")
        parsed = []

    # Log parse errors
    for entry in parsed:
        if entry["status"] != "valid":
            log.parse_error(entry["file"], entry.get("error", "unknown"))

    # Discover agents
    agents = discover_agents(parsed)

    # Known agents (for missing detection)
    known_agents = state_store.all_known_agents()

    log.scan_cycle(cycle_num, len(agents), 0)

    now = time.time()

    # Classify each discovered agent
    for entry in parsed:
        record = entry.get("record")
        if not record:
            continue

        agent_id = record.agent_id

        # Step 1: read previous state BEFORE any mutation
        try:
            prev_state_obj = state_store.get(agent_id)
        except Exception as e:
            log.error("state", f"Failed to read prev state for {agent_id}: {e}")
            prev_state_obj = None

        # Step 2: classify with progress-stall detection against the PREVIOUS state
        try:
            current_state, last_progress_change_at = check_progress_stall(
                record, prev_state_obj, now=now, thresholds=config.thresholds
            )
        except Exception as e:
            log.error("state", f"Classification failed for {agent_id}: {e}")
            continue

        # Step 3: update state store
        try:
            agent_state = state_store.update(agent_id, record, now=now)
        except Exception as e:
            log.error("state", f"Failed to update state for {agent_id}: {e}")
            continue

        # If progress was not advancing, persist the original last_progress_change_at
        # so that sustained stall tracking remains accurate across cycles.
        if last_progress_change_at != now and prev_state_obj is not None:
            agent_state.last_progress_change_at = last_progress_change_at

        try:
            antiflap.update_state(agent_state, current_state, now=now)
        except Exception:
            pass

        # Log state change
        prev_label = agent_state.last_state if hasattr(agent_state, 'last_state') else "unknown"
        if agent_state.sustained_state != current_state.value:
            log.state_change(agent_id, prev_label, current_state.value)

        # Check if we should alert (skip during startup grace for non-critical states)
        if startup_grace and current_state in (StallKind.MISSING, StallKind.DEAD, StallKind.STALL):
            log.alert_suppressed(agent_id, current_state.value, "startup grace")
            continue

        try:
            should_send, _ = antiflap.should_alert(agent_state, current_state, now=now)
        except Exception:
            should_send = False

        if should_send:
            label = STATE_LABELS.get(current_state, current_state.value)
            task_info = f" task={record.task_id}" if record.task_id else ""
            msg = f"{label} **{agent_id}**{task_info}"
            if record.progress_message:
                msg += f"\n{record.progress_message}"
            if record.last_error:
                msg += f"\n⚠️ Error: {record.last_error}"

            # Stable dedup key: depends on agent + run + state, NOT on msg wording
            dedup_key = f"{agent_id}:{record.run_id or 'none'}:{current_state.value}"
            alerts_to_send.append((agent_id, msg, dedup_key))
            try:
                agent_state.last_alert_sent_at[current_state.value] = now
                if current_state == StallKind.OK and agent_state.last_state not in ("ok", "unknown"):
                    agent_state.last_recovery_alert_at = now
            except Exception:
                pass

    # Check for missing agents (known but no heartbeat file)
    for known in known_agents:
        if known not in agents:
            try:
                agent_state = state_store.get_or_create(known, now=now)
                agent_state.last_seen_at = now
            except Exception:
                continue

            missing_state = StallKind.MISSING
            if startup_grace:
                log.alert_suppressed(known, "missing", "startup grace")
                continue

            try:
                antiflap.update_state(agent_state, missing_state, now=now)
                should_send, _ = antiflap.should_alert(agent_state, missing_state, now=now)
            except Exception:
                should_send = False

            if should_send:
                alerts_to_send.append((
                    known,
                    f"{STATE_LABELS[missing_state]} **{known}** (missing) — no heartbeat file found"
                ))
                dedup_key = f"{known}:none:missing"
                alerts_to_send.append((known, f"{STATE_LABELS[missing_state]} **{known}** (missing) — no heartbeat file found", dedup_key))
                log.alert_sent(known, dedup_key)
                try:
                    agent_state.last_alert_sent_at["missing"] = now
                except Exception:
                    pass

    # Task watchdog: pending reply + active-task progress nags
    try:
        for origin, msg in _check_pending_user_update(now, log):
            alerts_to_send.append((origin, msg, "main-task-watch:pending-reply"))
        for origin, msg in _check_active_task_progress(now, log):
            alerts_to_send.append((origin, msg, "main-task-watch:active-stall"))
    except Exception as e:
        log.error("task-watch", f"Task watch checks failed: {e}")

    # Persist state (one bad agent won't corrupt the file)
    try:
        state_store.save()
    except Exception as e:
        log.error("state", f"Failed to save state: {e}")

    # Write self-heartbeat (must always succeed if possible)
    duration_ms = int((time.monotonic() - start_ms) * 1000)
    try:
        writer = HeartbeatWriter(config.watchdog_state_dir)
        writer.write_self_heartbeat(
            state_dir=config.watchdog_state_dir,
            version=__version__,
            cycle_duration_ms=duration_ms,
            checked_agents=len(agents),
            status="ok" if not alerts_to_send else "alerts",
        )
        log.self_heartbeat(len(agents), duration_ms)
    except Exception as e:
        log.error("self", f"Failed to write self-heartbeat: {e}")

    # Build extended report
    try:
        report_data = extended_report(
            parsed, agents, known_agents, state_store, antiflap, config,
            startup_grace=startup_grace,
        )
    except Exception as e:
        log.error("report", f"Failed to generate report: {e}")
        report_data = {
            "total_discovered": len(agents),
            "total_known": len(known_agents),
            "agents": [],
            "stalled": [], "dead": [], "corrupt": [], "error": [], "recovered": [],
            "corrupt_files": [], "missing": [],
            "cycle_duration_ms": duration_ms,
            "alerts_generated": 0,
            "startup_grace": startup_grace,
        }

    report_data["cycle_duration_ms"] = duration_ms
    report_data["alerts_generated"] = len(alerts_to_send)
    report_data["startup_grace"] = startup_grace
    return report_data, alerts_to_send


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------
def cmd_once(config: WatchdogConfig, logger: Logger) -> None:
    """Run one scan and output JSON."""
    report_data, alerts = run_cycle(config, logger, 1)
    print(json.dumps(report_data, indent=2, default=str))

    if alerts:
        notifier = TelegramNotifier(config, logger)
        for agent_id, msg, dedup_key in alerts:
            notifier.send(f"🔔 Watchdog Alert:\n{msg}", key=dedup_key)


def cmd_loop(config: WatchdogConfig, logger: Logger) -> None:
    """Run continuous scan loop."""
    notifier = TelegramNotifier(config, logger)
    cycle = 0
    print(f"[watchdog v{__version__}] loop mode — interval={config.scan_interval_sec}s", flush=True)
    print(f"[watchdog] startup_grace={config.startup_grace_sec}s", flush=True)
    if config.log_file:
        print(f"[watchdog] log file: {config.log_file}", flush=True)

    while _running:
        cycle += 1
        try:
            report_data, alerts = run_cycle(config, logger, cycle)
            print_report_v2(
                report_data,
                cycle_ms=report_data.get("cycle_duration_ms", 0),
                self_hb=read_self_heartbeat(config.watchdog_state_dir),
                startup_grace=report_data.get("startup_grace", False),
                log_file=config.log_file,
            )

            if alerts:
                for _, msg, dedup_key in alerts:
                    notifier.send(f"🔔 Watchdog Alert:\n{msg}", key=dedup_key)
        except Exception as e:
            logger.error("service", f"Cycle {cycle} crashed: {e}")
            # Don't crash the loop — log and continue

        # Sleep in small increments for responsive signal handling
        for _ in range(config.scan_interval_sec * 2):
            if not _running:
                break
            time.sleep(0.5)

    logger.info("service", "Watchdog shutting down")


def cmd_report(config: WatchdogConfig, logger: Logger) -> None:
    """Run scan and print human-readable report."""
    report_data, alerts = run_cycle(config, logger, 1)
    print_report_v2(
        report_data,
        cycle_ms=report_data.get("cycle_duration_ms", 0),
        self_hb=read_self_heartbeat(config.watchdog_state_dir),
        startup_grace=report_data.get("startup_grace", False),
        log_file=config.log_file,
    )

    if alerts:
        print(f"\n  Pending alerts: {len(alerts)}")
        for agent_id, msg, dedup_key in alerts:
            print(f"    -> [{dedup_key}] {msg}")
    else:
        print("\n  No alerts pending ✅")


def cmd_check_self(config: WatchdogConfig, logger: Logger) -> None:
    """Check watchdog self-heartbeat freshness. Exits non-zero if stale."""
    self_hb = read_self_heartbeat(config.watchdog_state_dir)
    if self_hb is None:
        print("❌ Watchdog self-heartbeat: NEVER (watchdog not running)")
        sys.exit(1)

    updated = self_hb.get("updated_at", "unknown")
    status = self_hb.get("status", "unknown")
    version = self_hb.get("version", "unknown")
    cycle_ms = self_hb.get("cycle_duration_ms", "N/A")
    checked = self_hb.get("checked_agents", "N/A")

    # Calculate freshness
    try:
        from datetime import datetime, timezone as tz
        dt = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        age = (datetime.now(tz.utc) - dt).total_seconds()
        stale_threshold = 2 * config.scan_interval_sec + config.startup_grace_sec

        if age < config.scan_interval_sec * 2:
            print(f"✅ Watchdog: FRESH ({age:.0f}s ago)")
            sys.exit(0)
        elif age < stale_threshold:
            print(f"⚠️ Watchdog: STALE ({age:.0f}s ago, threshold={stale_threshold}s)")
            sys.exit(1)
        else:
            print(f"❌ Watchdog: DEAD ({age:.0f}s ago, threshold={stale_threshold}s)")
            sys.exit(1)
    except Exception as e:
        print(f"❌ Watchdog: parse error — {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Heartbeat Watchdog v{__version__} — Phase 3"
    )
    parser.add_argument("--config", help="Path to watchdog config JSON")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--once", action="store_true", help="Run one scan, output JSON")
    group.add_argument("--loop", action="store_true", help="Run continuous scan loop")
    group.add_argument("--report", action="store_true", help="Print human-readable report")
    group.add_argument("--check_self", action="store_true", help="Check watchdog freshness (exit non-zero if stale)")
    group.add_argument("--install", action="store_true", help="Install launchd service")
    group.add_argument("--uninstall", action="store_true", help="Remove launchd service")
    group.add_argument("--status", action="store_true", help="Check service status")

    args = parser.parse_args()
    config = WatchdogConfig.from_file(args.config)
    logger = Logger(log_file=config.log_file)

    if args.status:
        cmd_status()
    elif args.install:
        cmd_install(config)
    elif args.uninstall:
        cmd_uninstall()
    elif args.report:
        cmd_report(config, logger)
    elif args.loop:
        cmd_loop(config, logger)
    elif args.check_self:
        cmd_check_self(config, logger)
    else:
        cmd_once(config, logger)


if __name__ == "__main__":
    main()
