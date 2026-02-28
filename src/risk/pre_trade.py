"""事前风控.

在订单提交到交易所之前进行检查.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog

from src.core.events import EventBus, OrderIntentEvent, RiskAlertEvent

logger = structlog.get_logger()


@dataclass
class PreTradeCheckResult:
    """事前风控检查结果."""

    passed: bool
    reason: str = ""
    details: dict[str, Any] | None = None


class PreTradeRiskManager:
    """事前风控管理器.

    检查项:
    - 单笔订单大小限制
    - 总仓位大小限制
    - 最大杠杆限制
    - 最小下单间隔
    - 最大挂单数量
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: dict[str, Any],
    ) -> None:
        self._event_bus = event_bus
        self._max_order_size_usd = Decimal(str(config.get("max_order_size_usd", 50000)))
        self._max_position_size_usd = Decimal(str(config.get("max_position_size_usd", 200000)))
        self._max_leverage = config.get("max_leverage", 10)
        self._min_order_interval_ms = config.get("min_order_interval_ms", 500)
        self._max_open_orders = config.get("max_open_orders", 20)
        self._last_order_ts_ns: int = 0

    def check(
        self,
        intent: OrderIntentEvent,
        current_position_usd: Decimal = Decimal(0),
        current_open_orders: int = 0,
        current_price: Decimal = Decimal(0),
    ) -> PreTradeCheckResult:
        """执行事前风控检查.

        Args:
            intent: 订单意图
            current_position_usd: 当前仓位价值 (USDT)
            current_open_orders: 当前挂单数量
            current_price: 当前价格

        Returns:
            检查结果
        """
        import time

        # 1. 单笔订单大小
        order_value = intent.quantity * current_price if current_price else Decimal(0)
        if order_value > self._max_order_size_usd:
            return self._fail(
                f"单笔订单超限: {order_value:.0f} > {self._max_order_size_usd:.0f} USDT",
                intent,
            )

        # 2. 总仓位大小
        new_position = current_position_usd + order_value
        if new_position > self._max_position_size_usd:
            return self._fail(
                f"总仓位超限: {new_position:.0f} > {self._max_position_size_usd:.0f} USDT",
                intent,
            )

        # 3. 挂单数量
        if current_open_orders >= self._max_open_orders:
            return self._fail(
                f"挂单数超限: {current_open_orders} >= {self._max_open_orders}",
                intent,
            )

        # 4. 下单间隔
        now_ns = time.time_ns()
        interval_ms = (now_ns - self._last_order_ts_ns) / 1e6
        if self._last_order_ts_ns > 0 and interval_ms < self._min_order_interval_ms:
            return self._fail(
                f"下单过于频繁: {interval_ms:.0f}ms < {self._min_order_interval_ms}ms",
                intent,
            )

        self._last_order_ts_ns = now_ns
        logger.info("pre_trade_check_passed", instrument=intent.instrument_id, side=intent.side)
        return PreTradeCheckResult(passed=True)

    def _fail(self, reason: str, intent: OrderIntentEvent) -> PreTradeCheckResult:
        """风控检查失败."""
        logger.warning("pre_trade_check_failed", reason=reason, instrument=intent.instrument_id)
        self._event_bus.publish(
            RiskAlertEvent(
                level="ERROR",
                rule_name="pre_trade",
                message=reason,
                details={"instrument_id": intent.instrument_id, "side": intent.side},
            )
        )
        return PreTradeCheckResult(passed=False, reason=reason)
