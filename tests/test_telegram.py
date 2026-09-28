"""Telegram notifier — enable/kind gating, token-from-env, no-network sending."""

from arrow_statarb.core.telegram import TelegramNotifier


def _notifier(params, token="T", sent=None):
    sent = sent if sent is not None else []
    def send(tok, chat, text):
        sent.append({"token": tok, "chat": chat, "text": text})
        return True, "ok"
    n = TelegramNotifier(params_provider=lambda: params,
                         token_provider=lambda: token, send_fn=send)
    return n, sent


def test_disabled_is_silent_noop():
    n, sent = _notifier({"enabled": False, "chat_id": "123", "notify_trades": True})
    assert n.notify("hi", "trade") is False
    assert sent == []


def test_unconfigured_without_token_or_chat():
    n, sent = _notifier({"enabled": True, "chat_id": "123", "notify_trades": True}, token="")
    assert n.notify("hi") is False and sent == []            # no token
    n2, sent2 = _notifier({"enabled": True, "chat_id": "", "notify_trades": True})
    assert n2.notify("hi") is False and sent2 == []          # no chat id


def test_kind_gating():
    p = {"enabled": True, "chat_id": "123",
         "notify_trades": True, "notify_health": False}
    n, sent = _notifier(p)
    assert n.notify("entry", "trade") is True
    assert n.notify("frozen", "health") is False            # health muted
    assert len(sent) == 1 and sent[0]["chat"] == "123"


def test_send_carries_token_and_text():
    n, sent = _notifier({"enabled": True, "chat_id": "999", "notify_trades": True}, token="BOT")
    n.notify("ENTRY LONG z=-2.5", "trade")
    assert sent[0]["token"] == "BOT" and "ENTRY LONG" in sent[0]["text"]


def test_test_message_reports_missing_token():
    n, _ = _notifier({"enabled": True, "chat_id": "1"}, token="")
    ok, msg = n.test()
    assert ok is False and "ARROW_TELEGRAM_BOT_TOKEN" in msg


def test_test_message_sends_when_configured():
    n, sent = _notifier({"enabled": False, "chat_id": "1"})   # test ignores enabled
    ok, msg = n.test()
    assert ok is True and len(sent) == 1
