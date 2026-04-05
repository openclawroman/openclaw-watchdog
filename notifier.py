"""Heartbeat Watchdog v2.2 — Telegram Notifier (Phase 3).

Sends alerts via Telegram with:
  - State-change dedup (cooldown)
  - Failure resilience (logs, retries next cycle)
  - Graceful degradation if no token configured
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from typing import Optional
from .config import WatchdogConfig
from .logging import Logger


class TelegramNotifier:
    """Send alerts to Telegram via Bot API."""

    def __init__(self, config: WatchdogConfig, logger: Optional[Logger] = None):
        self.config = config
        self._state_path = os.path.join(config.watchdog_state_dir, "notify_state.json")
        self._last_sent: dict = {}
        self._load_state()
        self._log = logger or Logger()

    def _load_state(self) -> None:
        if os.path.isfile(self._state_path):
            try:
                with open(self._state_path, "r") as f:
                    self._last_sent = json.load(f)
            except Exception:
                self._last_sent = {}

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            with open(self._state_path, "w") as f:
                json.dump(self._last_sent, f, indent=2)
        except Exception:
            pass

    def get_cooldown(self, state: str) -> int:
        """Default cooldown per state. Phase 3: matches AntiFlapConfig values."""
        _cooldowns = {
            "stall": 1800, "dead": 1800, "missing": 1800,
            "corrupt": 900, "error": 900, "ok": 300,
        }
        return _cooldowns.get(state, 900)

    def can_send(self, key: str, force: bool = False) -> bool:
        """Check cooldown. Returns True if message is eligible to send."""
        if force:
            return True
        last = self._last_sent.get(key, 0)
        cooldown = self.get_cooldown(key.split(":")[-1] if ":" in key else "unknown")
        return (time.time() - last) >= cooldown

    def send(self, text: str, key: Optional[str] = None) -> bool:
        """Send Telegram alert with cooldown + error handling.

        Returns True if sent, False if suppressed or failed.
        """
        alert_key = key or text
        if not self.can_send(alert_key):
            msg_preview = text[:60].replace("\n", " ")
            self._log.info("alert", f"SUPPRESSED (cooldown): {msg_preview}...")
            return False

        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            self._log.warn("notifier", f"No Telegram config: {text[:80]}")
            self._last_sent[alert_key] = time.time()
            self._save_state()
            return True  # "sent" in the sense that we processed it

        try:
            url = (
                f"https://api.telegram.org/bot{self.config.telegram_bot_token}"
                f"/sendMessage"
            )
            payload = json.dumps({
                "chat_id": self.config.telegram_chat_id,
                "text": text[:4096],  # Telegram limit
                "parse_mode": "Markdown",
            }).encode()
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
            self._last_sent[alert_key] = time.time()
            self._save_state()
            self._log.info("notifier", f"SENT → {text[:80]}...")
            return True

        except urllib.error.HTTPError as e:
            self._log.error("notifier", f"HTTP {e.code}: {e.reason}")
            return False
        except Exception as e:
            self._log.error("notifier", f"send failed: {e}")
            return False
