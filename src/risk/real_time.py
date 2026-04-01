"""实时风控.

持续监控仓位、PnL、回撤等指标.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from src.core.events import EventBus, RiskAlertEvent
from src.monitoring.metrics import DAILY_LOSS_UTILISATION

if TYPE_CHECKING:
    from src.cache.redis_client import RedisClient

logger = structlog.get_logger(__name__)

# Redis key for real-time risk metrics
_RISK_METRICS_KEY = "nautilus:risk:metrics"


class RealTimeRiskMonitor:
    """实时风控监控器.

    监控项:
    - 最大回撤
    - 单日最大亏损
    - 追踪回撤
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: dict[str, Any],
        redis_client: RedisClient | None = None,
    ) -> None:
        """Initialize the real time risk monitor.

        Args:
            event_bus: Event bus used for cross-module communication.
            config: Configuration values for the component.
            redis_client: Redis client.
        """
        self._event_bus = event_bus
        self._redis = redis_client
        self._max_drawdown_pct = config.get("max_drawdown_pct", 5.0)
        self._daily_loss_limit_usd = Decimal(str(config.get("daily_loss_limit_usd", 5000)))
        self._trailing_drawdown_pct = config.get("trailing_drawdown_pct", 3.0)

        # 状态
        self._peak_equity = Decimal(0)
        self._daily_pnl = Decimal(0)
        self._initial_equity = Decimal(0)
        self._alerts_fired: set[str] = set()

    def initialize(self, equity: Decimal) -> None:
        """初始化基准值.

        Args:
            equity: Current account equity value.
        """
        self._initial_equity = equity
        self._peak_equity = equity
        self._daily_pnl = Decimal(0)
        self._alerts_fired.clear()

    def update(self, current_equity: Decimal) -> list[str]:
        """更新权益并检查风控.

        Args:
            current_equity: 当前权益

        Returns:
            触发的告警列表

        """
        alerts: list[str] = []

        # 更新峰值
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity

        # 计算回撤
        if self._peak_equity > 0:
            drawdown_pct = float((self._peak_equity - current_equity) / self._peak_equity * 100)
        else:
            drawdown_pct = 0.0

        # 计算日PnL
        self._daily_pnl = current_equity - self._initial_equity

        # 更新日损失使用率指标
        if self._daily_loss_limit_usd > 0:
            loss_utilization = max(0.0, min(float(-self._daily_pnl / self._daily_loss_limit_usd), 1.0))
            DAILY_LOSS_UTILISATION.set(loss_utilization)

        # 1. 最大回撤检查
        if drawdown_pct >= self._max_drawdown_pct:
            alert_key = "max_drawdown"
            if alert_key not in self._alerts_fired:
                msg = f"最大回撤触发: {drawdown_pct:.1f}% >= {self._max_drawdown_pct}%"
                alerts.append(msg)
                self._fire_alert("CRITICAL", "max_drawdown", msg)
                self._alerts_fired.add(alert_key)

        # 2. 追踪回撤
        elif drawdown_pct >= self._trailing_drawdown_pct:
            alert_key = "trailing_drawdown"
            if alert_key not in self._alerts_fired:
                msg = f"追踪回撤预警: {drawdown_pct:.1f}% >= {self._trailing_drawdown_pct}%"
                alerts.append(msg)
                self._fire_alert("ERROR", "trailing_drawdown", msg)
                self._alerts_fired.add(alert_key)

        # 3. 单日亏损
        if self._daily_pnl < -self._daily_loss_limit_usd:
            alert_key = "daily_loss"
            if alert_key not in self._alerts_fired:
                msg = f"单日亏损超限: {self._daily_pnl:.0f} USDT"
                alerts.append(msg)
                self._fire_alert("CRITICAL", "daily_loss", msg)
                self._alerts_fired.add(alert_key)

        # 推送指标到 Redis（供 Grafana/外部进程消费）
        self._push_metrics_to_redis(current_equity, drawdown_pct)

        return alerts

    def _push_metrics_to_redis(self, current_equity: Decimal, drawdown_pct: float) -> None:
        """将实时风控指标推送到 Redis Hash.

        Args:
            current_equity: Current equity.
            drawdown_pct: Current drawdown percentage.
        """
        if self._redis is None or not self._redis.is_available:
            return
        try:
            self._redis.hset(
                _RISK_METRICS_KEY,
                {
                    "peak_equity": str(self._peak_equity),
                    "current_equity": str(current_equity),
                    "drawdown_pct": str(drawdown_pct),
                    "daily_pnl": str(self._daily_pnl),
                    "updated_at_ns": str(time.time_ns()),
                },
            )
        except (ConnectionError, OSError, AttributeError) as exc:
            logger.warning("risk_metrics_redis_push_failed", error=str(exc))

    def reset_daily(self, equity: Decimal) -> None:
        """每日重置.

        Args:
            equity: Current account equity value.
        """
        self._initial_equity = equity
        self._daily_pnl = Decimal(0)
        self._alerts_fired.clear()
        logger.info("risk_daily_reset", equity=str(equity))

    def _fire_alert(self, level: str, rule: str, msg: str) -> None:
        """发布风控告警事件.

        Args:
            level: Level.
            rule: Rule.
            msg: Msg.
        """
        logger.warning("risk_alert", level=level, rule=rule, message=msg)
        self._event_bus.publish(RiskAlertEvent(level=level, rule_name=rule, message=msg))
