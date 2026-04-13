#!/usr/bin/env python3
"""heartbeat-daemon.py — write heartbeat data files for all agents.
Intended to run every 20 minutes via LaunchAgent.
Uses only local ollama heartbeat model qwen2.5:3b-hb — no paid models.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME, ".openclaw", "workspace", "heartbeat", "data")
AGENTS_DIR = os.path.join(HOME, ".openclaw", "agents")
OLLAMA = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen2.5:3b-hb"

# All agents that have heartbeat configured
AGENTS = [
    "main",
    "coder",
    "reviewer",
    "researcher",
    "critique",
    "lead-424a563a-623f-4303-a833-e6957602dbd0",
    "planner",
    "project-manager",
    "qa",
    "aqa",
    "plan-decompositor",
    "backlog-creator",
    "mc-gateway-8f3a411b-0599-4f9b-9b1a-99480675460a",
]


def count_sessions(agent_id: str) -> int:
    sessions_dir = os.path.join(AGENTS_DIR, agent_id, "sessions")
    if not os.path.isdir(sessions_dir):
        return 0
    try:
        return len([f for f in os.listdir(sessions_dir) if f.endswith(".jsonl")])
    except OSError:
        return 0


def check_ollama_alive() -> bool:
    """Quick check if ollama is responding."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-m", "10", f"{OLLAMA}/api/tags"],
            capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0 and "models" in result.stdout
    except Exception:
        return False


def write_heartbeat(agent_id: str, ollama_ok: bool, sessions: int, progress_counter: int):
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(timezone.utc)
    record = {
        "version": 1,
        "agent_id": agent_id,
        "run_id": f"daemon-{now.strftime('%Y%m%d-%H%M')}",
        "status": "alive" if ollama_ok else "degraded",
        "updated_at": now.isoformat(),
        "progress_counter": progress_counter,
        "task_id": None,
        "task_type": "heartbeat_monitor",
        "progress_message": f"sessions={sessions}",
        "ollama_alive": ollama_ok,
    }
    path = os.path.join(DATA_DIR, f"{agent_id}.json")
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")


def load_prev_counter(agent_id: str) -> int:
    """Read the heartbeat file to get the last progress_counter, if any."""
    path = os.path.join(DATA_DIR, f"{agent_id}.json")
    if not os.path.isfile(path):
        return 0
    try:
        with open(path) as f:
            data = json.load(f)
        return int(data.get("progress_counter", 0))
    except Exception:
        return 0


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    
    ollama_ok = check_ollama_alive()
    
    for i, agent_id in enumerate(AGENTS):
        # Serialize writes a bit so all files do not land at the exact same instant.
        if i > 0:
            time.sleep(10)
        
        sessions = count_sessions(agent_id)
        prev_counter = load_prev_counter(agent_id)
        progress_counter = prev_counter + 1 if prev_counter >= 0 else 1
        
        write_heartbeat(agent_id, ollama_ok, sessions, progress_counter)
        
        mark = "✅" if ollama_ok else "⚠️"
        status_label = "alive" if ollama_ok else "degraded"
        print(f"  [{i+1:2d}/{len(AGENTS):2d}] {mark} {agent_id}: {status_label} ({sessions} sessions, pc {progress_counter})")
    
    print(f"Done. All {len(AGENTS)} heartbeats written.")


if __name__ == "__main__":
    main()
