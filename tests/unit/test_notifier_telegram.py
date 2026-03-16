"""Tests for test notifier telegram."""

from __future__ import annotations

from types import SimpleNamespace

from src.monitoring.notifier.base import AlertLevel, AlertMessage
from src.monitoring.notifier.telegram import TelegramNotifier


def test_telegram_notifier_retries_without_env_proxy_on_missing_socksio(monkeypatch) -> None:
    """Verify that telegram notifier retries without env proxy on missing socksio.

    Args:
        monkeypatch: Monkeypatch.
    """
    calls: list[dict[str, object]] = []

    def _fake_post(url: str, json: dict[str, object], timeout: float, trust_env: bool = True):
        calls.append(
            {
                "url": url,
                "json": json,
                "timeout": timeout,
                "trust_env": trust_env,
            }
        )
        if trust_env:
            raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr("src.monitoring.notifier.telegram.httpx.post", _fake_post)

    notifier = TelegramNotifier(
        bot_token="bot-token",
        chat_id="chat-id",
        min_level=AlertLevel.WARNING,
    )

    sent = notifier.send(
        AlertMessage(
            level=AlertLevel.ERROR,
            rule_name="reconciliation_mismatch",
            message="startup mismatch",
        )
    )

    assert sent is True
    assert len(calls) == 2
    assert calls[0]["trust_env"] is True
    assert calls[1]["trust_env"] is False
