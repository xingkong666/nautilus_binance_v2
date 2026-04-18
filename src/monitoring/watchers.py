"""事件监视器.

订阅 EventBus 中的各类事件，根据 alerts.yaml 中的规则条件触发告警。
Watchers 是「告警规则引擎」：它们监听系统事件，决定何时该发出告警。

已实现的 Watcher:
    - RiskAlertWatcher：直接转发 RiskAlertEvent（熔断、风控拦截等）
    - DrawdownWatcher：持续跟踪回撤，超阈值时发告警
    - FillLatencyWatcher：检测成交延迟异常

扩展方式:
    1. 继承 BaseWatcher
    2. 在 __init__ 中订阅关心的 EventType
    3. 实现 handler，满足条件时调用 self._alert_manager.send_direct()
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from contextlib import suppress
from decimal import Decimal
from typing import Any

import structlog

from src.core.events import Event, EventBus, EventType, RiskAlertEvent
from src.monitoring.alerting import AlertManager
from src.monitoring.notifier.base import AlertLevel

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------


class BaseWatcher(ABC):
    """监视器基类.

    子类在 __init__ 中完成 EventBus 订阅，在 handler 中实现规则判断。
    """

    def __init__(self, event_bus: EventBus, alert_manager: AlertManager) -> None:
        """初始化监视器.

        Args:
            event_bus: 应用事件总线，用于订阅事件。
            alert_manager: 告警管理器，用于发送告警。

        """
        self._event_bus = event_bus
        self._alert_manager = alert_manager
        self._register()

    @abstractmethod
    def _register(self) -> None:
        """在 EventBus 上注册事件订阅（子类实现）."""


# ---------------------------------------------------------------------------
# 具体 Watcher
# ---------------------------------------------------------------------------


class RiskAlertWatcher(BaseWatcher):
    """直接转发 RiskAlertEvent 的监视器.

    将 EventBus 上的 RiskAlertEvent 转发到 AlertManager，
    不做额外规则判断（熔断、风控拦截等已在来源模块中格式化好）。
    """

    def __init__(
        self,
        event_bus: EventBus,
        alert_manager: AlertManager,
        cooldown_seconds: float = 60.0,
    ) -> None:
        """Initialize the risk alert watcher.

        Args:
            event_bus: Event bus used for cross-module communication.
            alert_manager: Alert manager.
            cooldown_seconds: Cooldown seconds.
        """
        self._cooldown_seconds = max(0.0, cooldown_seconds)
        self._last_alert_ts: dict[str, float] = {}
        super().__init__(event_bus, alert_manager)

    def _register(self) -> None:
        """订阅 RISK_ALERT 事件."""
        self._event_bus.subscribe(EventType.RISK_ALERT, self._on_risk_alert)
        logger.info(
            "watcher_registered",
            watcher="RiskAlertWatcher",
            cooldown_seconds=self._cooldown_seconds,
        )

    def _on_risk_alert(self, event: Event) -> None:
        """处理风控告警事件，直接转发给 AlertManager.

        Args:
            event: RiskAlertEvent 实例。

        """
        if not isinstance(event, RiskAlertEvent):
            return

        now = time.time()
        instrument_id = ""
        if isinstance(event.details, dict):
            instrument_id = str(event.details.get("instrument_id", ""))

        dedupe_key = f"{event.rule_name}:{instrument_id}"
        last = self._last_alert_ts.get(dedupe_key, 0.0)
        if now - last < self._cooldown_seconds:
            logger.debug(
                "risk_alert_suppressed_by_cooldown",
                rule_name=event.rule_name,
                instrument_id=instrument_id,
                cooldown_seconds=self._cooldown_seconds,
            )
            return
        self._last_alert_ts[dedupe_key] = now

        self._alert_manager.send_direct(
            level=event.level,
            rule_name=event.rule_name,
            message=event.message,
            details=event.details,
            source=event.source or "risk",
        )


class DrawdownWatcher(BaseWatcher):
    """回撤监视器.

    持续跟踪账户回撤，当回撤超过配置阈值时发出告警。
    在实盘中需由账户更新逻辑定期调用 update_equity()。
    """

    def __init__(
        self,
        event_bus: EventBus,
        alert_manager: AlertManager,
        warning_pct: float = 3.0,
        critical_pct: float = 5.0,
        cooldown_seconds: float = 300.0,
    ) -> None:
        """初始化回撤监视器.

        Args:
            event_bus: 事件总线。
            alert_manager: 告警管理器。
            warning_pct: 触发 ERROR 级别告警的回撤阈值（%），默认 3.0。
            critical_pct: 触发 CRITICAL 级别告警的回撤阈值（%），默认 5.0。
            cooldown_seconds: 同级别告警的最短发送间隔（秒），避免告警风暴，默认 300。

        """
        self._warning_pct = warning_pct
        self._critical_pct = critical_pct
        self._cooldown_seconds = cooldown_seconds
        self._peak_equity: Decimal = Decimal(0)
        self._last_alert_ts: dict[str, float] = {}  # 级别名 -> 时间戳
        super().__init__(event_bus, alert_manager)

    def _register(self) -> None:
        """DrawdownWatcher 不订阅特定事件，通过外部调用 update_equity()."""
        logger.info("watcher_registered", watcher="DrawdownWatcher")

    def update_equity(self, current_equity: Decimal) -> None:
        """更新账户权益并检查回撤告警.

        应在每次账户权益变化时调用（如每次成交后）。

        Args:
            current_equity: 当前账户净值（USDT）。

        """
        if current_equity <= 0:
            return

        # 更新峰值
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
            return  # 新高，无回撤

        if self._peak_equity == 0:
            return

        drawdown_pct = float((self._peak_equity - current_equity) / self._peak_equity * 100)

        if drawdown_pct >= self._critical_pct:
            self._try_send_alert(
                level=AlertLevel.CRITICAL,
                drawdown_pct=drawdown_pct,
                threshold=self._critical_pct,
            )
        elif drawdown_pct >= self._warning_pct:
            self._try_send_alert(
                level=AlertLevel.ERROR,
                drawdown_pct=drawdown_pct,
                threshold=self._warning_pct,
            )

    def _try_send_alert(self, level: AlertLevel, drawdown_pct: float, threshold: float) -> None:
        """冷却检查后发送回撤告警.

        Args:
            level: 告警级别。
            drawdown_pct: 当前回撤百分比。
            threshold: 触发的阈值百分比。

        """
        now = time.time()
        last = self._last_alert_ts.get(level.name, 0.0)

        if now - last < self._cooldown_seconds:
            return  # 冷却中，跳过

        self._last_alert_ts[level.name] = now
        severity = "预警" if level == AlertLevel.ERROR else "严重"
        emoji = "⚠️" if level == AlertLevel.ERROR else "🚨"
        self._alert_manager.send_direct(
            level=level,
            rule_name="drawdown_warning",
            message=f"{emoji} 回撤{severity}: {drawdown_pct:.1f}% (阈值 {threshold:.1f}%)",
            details={
                "drawdown_pct": f"{drawdown_pct:.2f}%",
                "peak_equity": str(self._peak_equity),
                "threshold": f"{threshold:.1f}%",
            },
            source="DrawdownWatcher",
        )


class FillLatencyWatcher(BaseWatcher):
    """成交延迟监视器.

    监听 ORDER_SUBMITTED 和 ORDER_FILLED 事件，
    计算从提交到成交的延迟，超过阈值时发告警。
    """

    def __init__(
        self,
        event_bus: EventBus,
        alert_manager: AlertManager,
        latency_threshold_ms: float = 1000.0,
    ) -> None:
        """初始化成交延迟监视器.

        Args:
            event_bus: 事件总线。
            alert_manager: 告警管理器。
            latency_threshold_ms: 成交延迟告警阈值（毫秒），默认 1000ms。

        """
        self._latency_threshold_ms = latency_threshold_ms
        self._submit_ts: dict[str, int] = {}  # 订单 ID -> 提交时间戳（ns）
        super().__init__(event_bus, alert_manager)

    def _register(self) -> None:
        """订阅订单提交和成交事件."""
        self._event_bus.subscribe(EventType.ORDER_SUBMITTED, self._on_submitted)
        self._event_bus.subscribe(EventType.ORDER_FILLED, self._on_filled)
        logger.info("watcher_registered", watcher="FillLatencyWatcher")

    def _on_submitted(self, event: Event) -> None:
        """记录订单提交时间戳.

        Args:
            event: ORDER_SUBMITTED 事件，payload 中需包含 order_id。

        """
        order_id = event.payload.get("order_id", "")
        if order_id:
            self._submit_ts[order_id] = event.timestamp_ns

    def _on_filled(self, event: Event) -> None:
        """计算成交延迟，超阈值时发告警.

        Args:
            event: ORDER_FILLED 事件，payload 中需包含 order_id。

        """
        order_id = event.payload.get("order_id", "")
        if not order_id or order_id not in self._submit_ts:
            return

        submit_ns = self._submit_ts.pop(order_id)
        latency_ms = (event.timestamp_ns - submit_ns) / 1_000_000

        if latency_ms > self._latency_threshold_ms:
            self._alert_manager.send_direct(
                level=AlertLevel.WARNING,
                rule_name="order_fill_latency",
                message=f"⏱️ 成交延迟过高: {latency_ms:.0f}ms (阈值 {self._latency_threshold_ms:.0f}ms)",
                details={
                    "order_id": order_id,
                    "latency_ms": f"{latency_ms:.0f}",
                    "instrument": event.payload.get("instrument_id", ""),
                },
                source="FillLatencyWatcher",
            )


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def build_watchers(
    event_bus: EventBus,
    alert_manager: AlertManager,
    alerting_config: dict[str, Any],
) -> list[BaseWatcher]:
    """根据配置批量创建并注册所有 Watcher.

    从 alerts.yaml 的 rules 列表中推断需要启用哪些 Watcher。
    目前固定启用 RiskAlertWatcher，其余按规则名按需启用。

    Args:
        event_bus: 事件总线。
        alert_manager: 告警管理器。
        alerting_config: alerts.yaml 中 alerting 字段的内容字典。

    Returns:
        已注册完毕的 Watcher 实例列表。

    """
    watchers: list[BaseWatcher] = []

    if not alerting_config.get("enabled", False):
        return watchers

    rules = {r["name"] for r in alerting_config.get("rules", [])}

    # RiskAlertWatcher 始终启用（对接熔断/风控模块）
    risk_alert_cooldown = float(alerting_config.get("risk_alert_cooldown_seconds", 60.0))
    watchers.append(RiskAlertWatcher(event_bus, alert_manager, cooldown_seconds=risk_alert_cooldown))

    # 回撤监视器
    if "drawdown_warning" in rules:
        drawdown_threshold = 10.0
        for rule in alerting_config.get("rules", []):
            if rule.get("name") == "drawdown_warning":
                cond = rule.get("condition", "")
                with suppress(ValueError, IndexError):
                    drawdown_threshold = float(cond.split(">")[-1].strip())
        watchers.append(
            DrawdownWatcher(
                event_bus,
                alert_manager,
                warning_pct=drawdown_threshold,
                critical_pct=drawdown_threshold + 3.0,
            )
        )

    # 成交延迟监视器
    if "order_fill_latency" in rules:
        latency_threshold = 1000.0
        for rule in alerting_config.get("rules", []):
            if rule.get("name") == "order_fill_latency":
                cond = rule.get("condition", "")
                # 简单解析 "fill_latency_ms > 1000" 中的数值
                with suppress(ValueError, IndexError):
                    latency_threshold = float(cond.split(">")[-1].strip())
        watchers.append(FillLatencyWatcher(event_bus, alert_manager, latency_threshold))

    logger.info("watchers_built", count=len(watchers))
    return watchers
