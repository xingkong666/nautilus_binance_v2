"""告警调度器.

订阅 EventBus 中的 RiskAlertEvent，将告警路由到已注册的通知渠道。
支持从 configs/monitoring/alerts.yaml 配置自动构建通知器列表。

架构:
    EventBus (RiskAlertEvent)
        → AlertManager.on_alert()
            → [TelegramNotifier, SlackNotifier, ...]
                → 各渠道发送（按 min_level 过滤）

Usage:
    manager = AlertManager(event_bus)
    manager.add_notifier(TelegramNotifier(...))
    manager.start()       # 挂载 EventBus 订阅
"""

from __future__ import annotations

from typing import Any

import structlog

from src.core.events import Event, EventBus, EventType, RiskAlertEvent
from src.monitoring.notifier.base import AlertLevel, AlertMessage, BaseNotifier

logger = structlog.get_logger(__name__)


class AlertManager:
    """告警调度管理器.

    统一管理多个通知渠道，接收 RiskAlertEvent 并广播到所有已注册的通知器。
    通知器内部按自身 min_level 过滤，AlertManager 本身不做级别过滤。
    """

    def __init__(self, event_bus: EventBus) -> None:
        """初始化告警管理器.

        Args:
            event_bus: 应用事件总线，用于订阅 RiskAlertEvent。

        """
        self._event_bus = event_bus
        self._notifiers: list[BaseNotifier] = []
        self._started = False

    def add_notifier(self, notifier: BaseNotifier) -> AlertManager:
        """注册一个通知渠道.

        Args:
            notifier: BaseNotifier 子类实例（TelegramNotifier / SlackNotifier 等）。

        Returns:
            self，支持链式调用。

        """
        self._notifiers.append(notifier)
        logger.info("notifier_registered", channel=notifier.__class__.__name__)
        return self

    def start(self) -> None:
        """挂载 EventBus 订阅，开始监听 RiskAlertEvent.

        重复调用无副作用（已 started 时直接返回）。
        """
        if self._started:
            return

        self._event_bus.subscribe(EventType.RISK_ALERT, self._on_event)
        self._started = True
        logger.info("alert_manager_started", notifiers=len(self._notifiers))

    def stop(self) -> None:
        """停止接收告警（清空 EventBus 订阅由 EventBus.clear() 负责）."""
        self._started = False
        logger.info("alert_manager_stopped")

    def send_direct(
        self,
        level: AlertLevel | str,
        rule_name: str,
        message: str,
        details: dict[str, Any] | None = None,
        source: str = "",
    ) -> int:
        """直接发送告警，不经过 EventBus（适合外部代码主动触发）.

        Args:
            level: 告警级别。
            rule_name: 规则名称。
            message: 告警消息正文。
            details: 附加上下文字典，可选。
            source: 来源模块名，可选。

        Returns:
            成功发送到的渠道数量。

        """
        alert = AlertMessage(
            level=level,
            rule_name=rule_name,
            message=message,
            details=details,
            source=source,
        )
        return self._dispatch(alert)

    def _on_event(self, event: Event) -> None:
        """EventBus 回调：将 RiskAlertEvent 转换为 AlertMessage 并分发.

        Args:
            event: 从 EventBus 接收到的事件，预期为 RiskAlertEvent 类型。

        """
        if not isinstance(event, RiskAlertEvent):
            return

        alert = AlertMessage(
            level=event.level,
            rule_name=event.rule_name,
            message=event.message,
            details=event.details,
            source=event.source,
        )
        self._dispatch(alert)

    def _dispatch(self, alert: AlertMessage) -> int:
        """将告警广播到所有已注册通知器.

        Args:
            alert: 待发送的告警消息。

        Returns:
            成功发送的渠道数量。

        """
        if not self._notifiers:
            logger.debug("no_notifiers_registered", rule=alert.rule_name)
            return 0

        sent = 0
        for notifier in self._notifiers:
            if notifier.send(alert):
                sent += 1

        logger.info(
            "alert_dispatched",
            rule=alert.rule_name,
            level=alert.level.name,
            sent=sent,
            total=len(self._notifiers),
        )
        return sent


# ---------------------------------------------------------------------------
# 工厂函数：从配置自动构建告警管理器
# ---------------------------------------------------------------------------


def build_alert_manager(
    event_bus: EventBus,
    alerting_config: dict[str, Any],
    telegram_token: str = "",
    telegram_chat_id: str = "",
) -> AlertManager:
    """根据 alerts.yaml 配置自动构建并注册通知器.

    解析 alerting.channels 列表，按 type 创建对应的通知器实例并注册到 AlertManager。
    不支持的渠道类型会记录 warning 并跳过。

    Args:
        event_bus: 应用事件总线。
        alerting_config: alerts.yaml 中 alerting 字段的内容字典。
        telegram_token: Telegram Bot Token；空字符串时从环境变量读取。
        telegram_chat_id: Telegram Chat ID；空字符串时从环境变量读取。

    Returns:
        已注册好所有通知器、但尚未 start() 的 AlertManager 实例。
        调用方需手动调用 manager.start() 挂载订阅。

    """
    from src.monitoring.notifier.slack import SlackNotifier
    from src.monitoring.notifier.telegram import TelegramNotifier

    manager = AlertManager(event_bus)

    if not alerting_config.get("enabled", False):
        logger.info("alerting_disabled")
        return manager

    channels = alerting_config.get("channels", [])

    for ch in channels:
        ch_type = ch.get("type", "")
        ch_enabled = ch.get("enabled", True)
        levels = ch.get("levels", ["CRITICAL", "ERROR"])
        # 取级别列表中最低的作为最低告警级别
        min_level = min((AlertLevel.from_str(lv) for lv in levels), key=lambda x: x.value)

        try:
            if ch_type == "telegram":
                if not ch_enabled:
                    continue
                if telegram_token and telegram_chat_id:
                    telegram_notifier = TelegramNotifier(
                        bot_token=telegram_token,
                        chat_id=telegram_chat_id,
                        min_level=min_level,
                        enabled=True,
                    )
                else:
                    telegram_notifier = TelegramNotifier.from_env(min_level=min_level, enabled=True)
                manager.add_notifier(telegram_notifier)

            elif ch_type == "slack":
                if not ch_enabled:
                    continue
                webhook_url = ch.get("webhook_url", "")
                if webhook_url:
                    slack_notifier = SlackNotifier(
                        webhook_url=webhook_url,
                        min_level=min_level,
                        enabled=True,
                    )
                else:
                    slack_notifier = SlackNotifier.from_env(min_level=min_level, enabled=True)
                manager.add_notifier(slack_notifier)

            else:
                logger.warning("unknown_alert_channel_type", type=ch_type)

        except (ValueError, Exception):
            logger.warning(
                "alert_channel_init_failed",
                channel=ch_type,
                reason="missing credentials or config error",
            )

    return manager
