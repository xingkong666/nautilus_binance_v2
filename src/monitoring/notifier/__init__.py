"""告警通知器.

提供多渠道告警推送：Telegram、Slack。

Quick start:
    from src.monitoring.notifier import AlertLevel, AlertMessage, TelegramNotifier

    notifier = TelegramNotifier(bot_token="...", chat_id="...")
    notifier.send(AlertMessage(
        level=AlertLevel.CRITICAL,
        rule_name="circuit_breaker",
        message="🚨 熔断触发",
    ))
"""

from src.monitoring.notifier.base import AlertLevel, AlertMessage, BaseNotifier
from src.monitoring.notifier.slack import SlackNotifier
from src.monitoring.notifier.telegram import TelegramNotifier

__all__ = [
    "AlertLevel",
    "AlertMessage",
    "BaseNotifier",
    "TelegramNotifier",
    "SlackNotifier",
]
