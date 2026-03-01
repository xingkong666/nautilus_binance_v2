"""通知器基类.

定义统一的通知接口，所有渠道（Telegram、Slack 等）都继承此基类。
支持按告警级别过滤，避免低级别消息污染高优先级渠道。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum, unique
from typing import Any

import structlog

logger = structlog.get_logger()


@unique
class AlertLevel(Enum):
    """告警级别，数值越大优先级越高."""

    WARNING = 1
    ERROR = 2
    CRITICAL = 3

    @classmethod
    def from_str(cls, s: str) -> AlertLevel:
        """从字符串解析告警级别（不区分大小写）.

        Args:
            s: 级别字符串，如 "WARNING"、"ERROR"、"CRITICAL"。

        Returns:
            对应的 AlertLevel 枚举值。

        Raises:
            ValueError: 无法识别的级别字符串。
        """
        try:
            return cls[s.upper()]
        except KeyError as err:
            raise ValueError(f"Unknown alert level: '{s}'. Valid: WARNING, ERROR, CRITICAL") from err


class AlertMessage:
    """告警消息封装.

    Attributes:
        level: 告警级别。
        rule_name: 触发告警的规则名，如 "circuit_breaker_triggered"。
        message: 消息正文（已格式化）。
        details: 附加上下文字典，可选。
        source: 产生告警的模块/组件名，可选。
    """

    def __init__(
        self,
        level: AlertLevel | str,
        rule_name: str,
        message: str,
        details: dict[str, Any] | None = None,
        source: str = "",
    ) -> None:
        """初始化告警消息.

        Args:
            level: 告警级别，可传 AlertLevel 枚举或字符串。
            rule_name: 规则名称。
            message: 消息正文。
            details: 附加上下文字典，可选。
            source: 来源模块名，可选。
        """
        self.level = AlertLevel.from_str(level) if isinstance(level, str) else level
        self.rule_name = rule_name
        self.message = message
        self.details = details or {}
        self.source = source

    def format_text(self) -> str:
        """格式化为可读文本（用于 Telegram/Slack 消息正文）.

        Returns:
            多行格式化字符串，包含级别、规则名、消息和附加细节。
        """
        level_emoji = {
            AlertLevel.WARNING: "⚠️",
            AlertLevel.ERROR: "🔴",
            AlertLevel.CRITICAL: "🚨",
        }.get(self.level, "ℹ️")

        lines = [
            f"{level_emoji} [{self.level.name}] {self.rule_name}",
            f"{self.message}",
        ]

        if self.source:
            lines.append(f"Source: {self.source}")

        if self.details:
            for k, v in self.details.items():
                lines.append(f"  {k}: {v}")

        return "\n".join(lines)


class BaseNotifier(ABC):
    """通知器基类.

    子类实现 _send() 方法，基类负责级别过滤和异常处理。
    """

    def __init__(self, min_level: AlertLevel = AlertLevel.WARNING, enabled: bool = True) -> None:
        """初始化通知器.

        Args:
            min_level: 最低发送级别，低于此级别的消息会被静默丢弃。
            enabled: 是否启用此通知器，False 时所有 send() 调用均为空操作。
        """
        self._min_level = min_level
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """返回通知器是否启用."""
        return self._enabled

    def send(self, alert: AlertMessage) -> bool:
        """发送告警消息，自动过滤低级别消息.

        Args:
            alert: 待发送的告警消息。

        Returns:
            True 表示发送成功；False 表示未发送（disabled / 级别不够 / 发送失败）。
        """
        if not self._enabled:
            return False

        if alert.level.value < self._min_level.value:
            logger.debug(
                "alert_filtered",
                level=alert.level.name,
                min_level=self._min_level.name,
                rule=alert.rule_name,
            )
            return False

        try:
            self._send(alert)
            logger.info(
                "alert_sent",
                channel=self.__class__.__name__,
                level=alert.level.name,
                rule=alert.rule_name,
            )
            return True
        except Exception:
            logger.exception(
                "alert_send_failed",
                channel=self.__class__.__name__,
                level=alert.level.name,
                rule=alert.rule_name,
            )
            return False

    @abstractmethod
    def _send(self, alert: AlertMessage) -> None:
        """实际发送逻辑，由子类实现.

        Args:
            alert: 已通过级别过滤的告警消息。

        Raises:
            Exception: 发送失败时抛出，由基类 send() 捕获并记录。
        """
