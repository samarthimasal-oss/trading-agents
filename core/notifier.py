"""Telegram notifier: push fills, closes, vetoes and warnings to the phone.

Configure in config.json:
    "telegram": {
        "enabled": true,
        "bot_token": "123456:ABC-DEF...",          # from @BotFather
        "chat_id": 0,                              # 0 = auto-detect first chat
        "max_len": 900
    }

Failure-safe: send errors are logged at WARNING and never propagate into the
trading loop.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request

log = logging.getLogger("telegram")


class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.token = (self.cfg.get("bot_token") or "").strip()
        token_file = self.cfg.get("token_file") or ""
        if not self.token and token_file and os.path.exists(token_file):
            try:
                self.token = open(token_file, encoding="utf-8").read().strip()
            except Exception:
                pass
        self.chat_id = self.cfg.get("chat_id") or 0
        self.enabled = bool(self.cfg.get("enabled", False)) and bool(self.token)
        if self.enabled and not self.chat_id:
            self.enabled = self._autodetect_chat_id()
        if self.enabled:
            log.info("telegram notifier active (chat %s)", self.chat_id)
        else:
            log.info("telegram notifier disabled")

    # ------------------------------------------------------------------ setup
    def _autodetect_chat_id(self) -> bool:
        """chat_id==0: find the first private chat that messaged the bot.
        (The user must have sent at least one message to the bot once.)"""
        try:
            url = (f"https://api.telegram.org/bot{self.token}/getUpdates"
                   f"?timeout=0&limit=5")
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for upd in data.get("result", []):
                msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post")
                chat = (msg or {}).get("chat") or {}
                if chat:
                    self.chat_id = chat["id"]
                    log.info("telegram chat auto-detected: %s (%s)",
                             self.chat_id, chat.get("type"))
                    return True
            log.warning("telegram enabled but no chat found yet — message the bot once")
            return False
        except Exception as exc:                            # noqa: BLE001
            log.warning("telegram autodetect failed: %s", exc)
            return False

    # ------------------------------------------------------------------ send
    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        text = (text or "")[: int(self.cfg.get("max_len", 900))]
        try:
            payload = urllib.parse.urlencode({
                "chat_id": self.chat_id, "text": text,
                "parse_mode": "HTML", "disable_notification": False,
            }).encode()
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                ok = bool(json.loads(resp.read().decode("utf-8")).get("ok"))
            return ok
        except Exception as exc:                            # noqa: BLE001
            log.warning("telegram send failed: %s", exc)
            return False

    # ------------------------------------------------------------------ sugar
    def trade(self, kind: str, detail: str) -> None:
        emoji = {"fill": "⚡", "close": "🏁", "reject": "🚫", "sl": "🛡", "guard": "🛑"}.get(kind, "•")
        self.send(f"{emoji} <b>{kind.upper()}</b> {detail}")


_NOTIFIER_LOCK = {}
