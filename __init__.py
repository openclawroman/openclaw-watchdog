"""Heartbeat Watchdog v2.2 — Agent liveness, progress monitoring & auto-discovery.

Phase 1: Foundation + basic Watchdog skeleton
  - heartbeat schema + atomic writes
  - directory scan + agent auto-discovery (no hardcoded names)
  - basic parsing/validation (valid/corrupt/unreadable)
  - watchdog self-heartbeat
  - config file + minimal CLI (--once, --loop, --report, --check-self)
"""

__version__ = "2.2"
__phase__ = "1 — foundation"
