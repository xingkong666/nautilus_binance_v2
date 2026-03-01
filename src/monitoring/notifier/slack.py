"""Slack 通知器.

通过 Slack Incoming Webhook 发送告警消息。
Webhook URL 从环境变量（SLACK_WEBHOOK_URL）或直接传参获取。

依赖:
    httpx（已在 core dependencies 中），无需 slack-sdk。

Webhook 创建:
    https://api.slack.com/messaging/webhooks
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from src.monitoring.notifier.base import AlertLevel, AlertMessage, BaseNotifier

logger = structlog.get_logger()

# 告警级别对应的 Slack attachment color
_LEVEL_COLORS = {
    AlertLevel.WARNING: "#FFA500",   # 橙色
    AlertLevel.ERROR: "#FF0000",     # 红色
    AlertLevel.CRITICAL: "#8B0000",  # 深红
}


class SlackNotifier(BaseNotifier):
    """Slack Incoming Webhook 告警通知器.

    Usage:
        notifier = SlackNotifier(
            webhook_url="https://hooks.slack.com/services/...",
            min_level=AlertLevel.CRITICAL,
        )
        notifier.send(AlertMessage(
            level=AlertLevel.CRITICAL,
            rule_name="circuit_breaker",
            message="🚨 熔断触发",
        ))
    """

    def __init__(
        self,
        webhook_url: str,
        channel: str = "",
        username: str = "TradingBot",
        min_level: AlertLevel = AlertLevel.CRITICAL,
        enabled: bool = True,
        timeout: float = 10.0,
    ) -> None:
        """初始化 Slack 通知器.

        Args:
            webhook_url: Slack Incoming Webhook URL。
            channel: 指定发送频道（如 "#alerts"），留空则使用 Webhook 默认频道。
            username: 消息显示的 Bot 名称，默认 "TradingBot"。
            min_level: 最低发送级别，默认 CRITICAL。
            enabled: 是否启用，默认 True。
            timeout: HTTP 请求超时秒数，默认 10.0。
        """
        super().__init__(min_level=min_level, enabled=enabled)
        self._webhook_url = webhook_url
        self._channel = channel
        self._username = username
        self._timeout = timeout

    def _send(self, alert: AlertMessage) -> None:
        """通过 Slack Webhook 发送 attachment 格式消息.

        使用 Slack attachment 以支持颜色标记（按告警级别区分）。

        Args:
            alert: 告警消息实例。

        Raises:
            httpx.HTTPStatusError: Webhook 返回非 2xx 状态码。
            httpx.TimeoutException: 请求超时。
        """
        color = _LEVEL_COLORS.get(alert.level, "#808080")

        attachment: dict[str, Any] = {
            "color": color,
            "title": f"[{alert.level.name}] {alert.rule_name}",
            "text": alert.message,
            "footer": f"Source: {alert.source}" if alert.source else "nautilus_binance_v2",
        }

        if alert.details:
            attachment["fields"] = [
                {"title": k, "value": str(v), "short": True}
                for k, v in alert.details.items()
            ]

        payload: dict[str, Any] = {
            "username": self._username,
            "attachments": [attachment],
        }
        if self._channel:
            payload["channel"] = self._channel

        resp = httpx.post(self._webhook_url, json=payload, timeout=self._timeout)
        resp.raise_for_status()

        logger.debug("slack_sent", rule=alert.rule_name)

    @classmethod
    def from_env(
        cls,
        min_level: AlertLevel = AlertLevel.CRITICAL,
        enabled: bool = True,
    ) -> SlackNotifier:
        """从环境变量创建实例（SLACK_WEBHOOK_URL）.

        Args:
            min_level: 最低发送级别。
            enabled: 是否启用。

        Returns:
            SlackNotifier 实例。

        Raises:
            ValueError: 环境变量未设置或为空。
        """
        import os

        url = os.environ.get("SLACK_WEBHOOK_URL", "")
        if not url:
            raise ValueError("SLACK_WEBHOOK_URL must be set in environment variables.")

        channel = os.environ.get("SLACK_CHANNEL", "")
        return cls(webhook_url=url, channel=channel, min_level=min_level, enabled=enabled)
