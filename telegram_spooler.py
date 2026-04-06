#!/usr/bin/env python3
"""Telegram Task Spooler

Watches spool directory for queued tasks and dispatches them to main agent via sessions_spawn.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from spool import claim_next_queued, mark_started, mark_failed, SPOOL_DIR

def dispatch_task(task_id: str, prompt: str) -> bool:
    """Spawn a main agent session with the given prompt."""
    try:
        subprocess.run(
            ["openclaw", "sessions", "spawn", "--agent", "main", "--task", prompt],
            timeout=30,
            check=False,
        )
        return True
    except Exception as e:
        print(f"Failed to dispatch {task_id}: {e}", file=sys.stderr)
        return False

def main_loop(interval: int = 5) -> None:
    print(f"telegram-spooler started, watching {SPOOL_DIR}, interval {interval}s")
    while True:
        try:
            entry = claim_next_queued()
            if entry:
                task_id = entry["task_id"]
                prompt = entry["prompt"]
                print(f"Dispatching task {task_id} from spool (chat_id={entry['chat_id']})")
                # Mark started before dispatch to avoid double-dispatch
                mark_started(task_id)
                ok = dispatch_task(task_id, prompt)
                if not ok:
                    mark_failed(task_id, error="dispatch_failed")
                # If dispatch succeeded, we consider it handed off; main will mark active and progress.
                # We don't wait for completion; spool entry stays as 'started' for forensics.
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nSpooler interrupted.")
            break
        except Exception as e:
            print(f"Spooler error: {e}", file=sys.stderr)
            time.sleep(interval)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=int, default=5, help="Poll interval seconds")
    args = p.parse_args()
    try:
        main_loop(args.interval)
    except Exception as e:
        print(f"Fatal: {e}", file=sys.stderr)
        sys.exit(1)
