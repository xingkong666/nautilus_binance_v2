"""Telegram 通知器.

通过 Telegram Bot API 发送告警消息。
Bot Token 和 Chat ID 从环境变量读取（TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）。

依赖:
    httpx（已在 core dependencies 中）

注意:
    使用同步 httpx 调用，避免引入 asyncio 依赖。
    Telegram Bot API 限速约 30 msg/s（同一 chat 1 msg/s），告警场景下不会触发。
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from src.monitoring.notifier.base import AlertLevel, AlertMessage, BaseNotifier

logger = structlog.get_logger()

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier(BaseNotifier):
    """Telegram Bot 告警通知器.

    Usage:
        notifier = TelegramNotifier(
            bot_token="your_bot_token",
            chat_id="your_chat_id",
            min_level=AlertLevel.ERROR,
        )
        notifier.send(AlertMessage(
            level=AlertLevel.CRITICAL,
            rule_name="circuit_breaker",
            message="🚨 熔断触发：单日亏损超限",
        ))
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        min_level: AlertLevel = AlertLevel.ERROR,
        enabled: bool = True,
        timeout: float = 10.0,
    ) -> None:
        """初始化 Telegram 通知器.

        Args:
            bot_token: Telegram Bot Token（从 BotFather 获取）。
            chat_id: 目标 Chat ID（个人/群组/频道均可）。
            min_level: 最低发送级别，低于此级别静默丢弃，默认 ERROR。
            enabled: 是否启用，默认 True。
            timeout: HTTP 请求超时秒数，默认 10.0。
        """
        super().__init__(min_level=min_level, enabled=enabled)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout
        self._url = TELEGRAM_API_URL.format(token=bot_token)

    def _send(self, alert: AlertMessage) -> None:
        """通过 Telegram Bot API 发送消息.

        使用 MarkdownV2 格式，特殊字符已做转义处理。

        Args:
            alert: 告警消息实例。

        Raises:
            httpx.HTTPStatusError: API 返回非 2xx 状态码。
            httpx.TimeoutException: 请求超时。
        """
        text = self._escape_markdown(alert.format_text())

        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }

        resp = httpx.post(self._url, json=payload, timeout=self._timeout)
        resp.raise_for_status()

        logger.debug("telegram_sent", chat_id=self._chat_id, rule=alert.rule_name)

    @staticmethod
    def _escape_markdown(text: str) -> str:
        """转义 Telegram MarkdownV2 中的特殊字符.

        MarkdownV2 要求转义：_ * [ ] ( ) ~ ` > # + - = | { } . !

        Args:
            text: 原始文本。

        Returns:
            转义后的文本，可安全用于 MarkdownV2。
        """
        special_chars = r"\_*[]()~`>#+-=|{}.!"
        return "".join(f"\\{c}" if c in special_chars else c for c in text)

    @classmethod
    def from_env(
        cls,
        min_level: AlertLevel = AlertLevel.ERROR,
        enabled: bool = True,
    ) -> TelegramNotifier:
        """从环境变量创建实例（TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）.

        Args:
            min_level: 最低发送级别。
            enabled: 是否启用。

        Returns:
            TelegramNotifier 实例。

        Raises:
            ValueError: 环境变量未设置或为空。
        """
        import os
        from pathlib import Path

        # 尝试加载项目根目录的 .env 文件
        try:
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
        except ImportError:
            pass

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

        if not token or not chat_id:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in environment variables."
            )

        return cls(bot_token=token, chat_id=chat_id, min_level=min_level, enabled=enabled)
