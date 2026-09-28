"""Telegram notifier — pushes entry/exit/halt/health alerts to a chat.

SECURITY: the bot TOKEN is a credential and is read ONLY from the environment
(``ARROW_TELEGRAM_BOT_TOKEN``), never from settings.yaml — the same rule as the
Arrow API keys. The chat id and the on/off toggles are non-secret and live in
config. Disabled or unconfigured ⇒ a silent no-op (never raises into a caller).

The HTTP sender is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional, Tuple

from loguru import logger

TOKEN_ENV = "ARROW_TELEGRAM_BOT_TOKEN"


def _http_send(token: str, chat_id: str, text: str, timeout: float = 5.0) -> Tuple[bool, str]:
    """POST to the Telegram sendMessage API. Returns (ok, detail)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
            return bool(body.get("ok")), body.get("description", "sent")
    except Exception as exc:                             # noqa: BLE001
        return False, str(exc)


class TelegramNotifier:
    def __init__(self, params_provider: Callable[[], Dict],
                 token_provider: Optional[Callable[[], str]] = None,
                 send_fn: Callable[[str, str, str], Tuple[bool, str]] = _http_send):
        # params_provider() -> {enabled, chat_id, notify_trades, notify_health, notify_errors}
        self._params = params_provider
        self._token = token_provider or (lambda: os.environ.get(TOKEN_ENV, "") or "")
        self._send = send_fn

    def token_set(self) -> bool:
        return bool(self._token())

    def configured(self) -> bool:
        p = self._params() or {}
        return bool(self._token()) and bool(p.get("chat_id"))

    def _kind_enabled(self, kind: str) -> bool:
        p = self._params() or {}
        if not p.get("enabled"):
            return False
        return bool(p.get({"trade": "notify_trades", "health": "notify_health",
                           "error": "notify_errors"}.get(kind, "notify_trades"), True))

    def notify(self, text: str, kind: str = "trade") -> bool:
        """Send if enabled, configured, and this KIND is turned on. Silent no-op
        otherwise; never raises — a notification must not break a trade."""
        try:
            if not self._kind_enabled(kind) or not self.configured():
                return False
            ok, detail = self._send(self._token(), str(self._params().get("chat_id")), text)
            if not ok:
                logger.warning("Telegram: send failed — {}", detail)
            return ok
        except Exception as exc:                         # noqa: BLE001
            logger.warning("Telegram: notify error — {}", exc)
            return False

    def test(self) -> Tuple[bool, str]:
        """Send a test message regardless of the per-kind flags (but honours the
        token + chat id). Returns (ok, human message) for the UI."""
        if not self._token():
            return False, f"{TOKEN_ENV} is not set in the environment (.env)"
        p = self._params() or {}
        if not p.get("chat_id"):
            return False, "chat id is not set in Settings"
        ok, detail = self._send(self._token(), str(p.get("chat_id")),
                                "✅ Arrow StatArb — Telegram test message")
        return ok, ("test message sent" if ok else f"failed: {detail}")
