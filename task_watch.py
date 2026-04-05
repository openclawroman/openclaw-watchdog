#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

WATCH_PATH = Path("/Users/openclaw/.openclaw/workspace/memory/main-task-watch.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_state() -> dict:
    return {
        "active": False,
        "title": "",
        "startedAt": "",
        "lastProgressAt": "",
        "status": "idle",
        "notes": "",
        "alertedStallAt": "",
        "completedAt": "",
        "verifiedResultAt": "",
        "replySentAt": "",
        "pendingUserUpdate": False,
        "lastResultSummary": "",
        "alertedPendingReplyAt": "",
    }


def load_state() -> dict:
    if WATCH_PATH.exists():
        state = json.loads(WATCH_PATH.read_text())
        merged = default_state()
        merged.update(state)
        return merged
    return default_state()


def save_state(state: dict) -> None:
    WATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCH_PATH.write_text(json.dumps(state, indent=2) + "\n")


def mark_active(args: argparse.Namespace) -> None:
    state = load_state()
    ts = now_iso()
    state["active"] = True
    state["title"] = args.title or state.get("title", "")
    state["status"] = args.status or "active"
    state["notes"] = args.note or state.get("notes", "")
    state["startedAt"] = state.get("startedAt") or ts
    state["lastProgressAt"] = ts
    save_state(state)
    print("marked active")


def mark_progress(args: argparse.Namespace) -> None:
    state = load_state()
    ts = now_iso()
    state["active"] = True
    if args.title:
        state["title"] = args.title
    if args.status:
        state["status"] = args.status
    if args.note:
        state["notes"] = args.note
    state["lastProgressAt"] = ts
    save_state(state)
    print("marked progress")


def mark_blocked(args: argparse.Namespace) -> None:
    state = load_state()
    ts = now_iso()
    state["active"] = True
    if args.title:
        state["title"] = args.title
    state["status"] = "blocked"
    state["notes"] = args.reason or state.get("notes", "")
    state["lastProgressAt"] = ts
    save_state(state)
    print("marked blocked")


def mark_verified(args: argparse.Namespace) -> None:
    state = load_state()
    ts = now_iso()
    if args.title:
        state["title"] = args.title
    if args.status:
        state["status"] = args.status
    if args.note:
        state["notes"] = args.note
    state["active"] = True
    state["lastProgressAt"] = ts
    state["verifiedResultAt"] = ts
    state["replySentAt"] = ""
    state["pendingUserUpdate"] = True
    state["lastResultSummary"] = args.summary or state.get("lastResultSummary", "")
    state["alertedPendingReplyAt"] = ""
    save_state(state)
    print("marked verified result")


def mark_replied(args: argparse.Namespace) -> None:
    state = load_state()
    ts = now_iso()
    state["replySentAt"] = ts
    state["pendingUserUpdate"] = False
    state["alertedPendingReplyAt"] = ""
    if args.note:
        state["notes"] = args.note
    save_state(state)
    print("marked reply sent")


def mark_done(args: argparse.Namespace) -> None:
    state = load_state()
    ts = now_iso()
    state["active"] = False
    state["status"] = "done"
    state["completedAt"] = ts
    if args.note:
        state["notes"] = args.note
    save_state(state)
    print("marked done")


def clear_pending(_: argparse.Namespace) -> None:
    state = load_state()
    state["pendingUserUpdate"] = False
    state["alertedPendingReplyAt"] = ""
    save_state(state)
    print("cleared pending user update")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Update memory/main-task-watch.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("mark-active", help="Mark that a task became active")
    a.add_argument("--title")
    a.add_argument("--status", default="active")
    a.add_argument("--note")
    a.set_defaults(func=mark_active)

    pr = sub.add_parser("mark-progress", help="Refresh task progress and summary")
    pr.add_argument("--title")
    pr.add_argument("--status", default="in_progress")
    pr.add_argument("--note")
    pr.set_defaults(func=mark_progress)

    b = sub.add_parser("mark-blocked", help="Mark task blocked with reason")
    b.add_argument("--title")
    b.add_argument("--reason", required=True)
    b.set_defaults(func=mark_blocked)

    v = sub.add_parser("mark-verified", help="Mark that a verified result exists and a user update is pending")
    v.add_argument("--title")
    v.add_argument("--summary")
    v.add_argument("--status", default="verified")
    v.add_argument("--note")
    v.set_defaults(func=mark_verified)

    r = sub.add_parser("mark-replied", help="Mark that a user-facing reply was sent")
    r.add_argument("--note")
    r.set_defaults(func=mark_replied)

    d = sub.add_parser("mark-done", help="Mark task complete")
    d.add_argument("--note")
    d.set_defaults(func=mark_done)

    c = sub.add_parser("clear-pending", help="Clear pending user update state")
    c.set_defaults(func=clear_pending)
    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
