"""订单路由.

接收经风控审核的 OrderIntent, 转化为实际订单并提交到交易所.

流程: Signal → OrderIntent → PreTradeRisk → OrderRouter → Exchange
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.trading.strategy import Strategy

from src.core.events import EventBus, EventType, OrderIntentEvent
from src.execution.order_intent import OrderIntent

logger = structlog.get_logger()


class OrderRouter:
    """订单路由器.

    负责将 OrderIntent 转化为 Nautilus 订单并通过 Strategy 提交.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._strategy: Strategy | None = None

    def bind_strategy(self, strategy: Strategy) -> None:
        """绑定 Nautilus Strategy 实例 (用于下单)."""
        self._strategy = strategy

    def route(self, intent: OrderIntent) -> bool:
        """路由订单.

        Args:
            intent: 订单意图

        Returns:
            是否成功提交
        """
        if self._strategy is None:
            logger.error("order_router_no_strategy")
            return False

        instrument = self._strategy.cache.instrument(InstrumentId.from_str(intent.instrument_id))
        if instrument is None:
            logger.error("instrument_not_found", instrument_id=intent.instrument_id)
            return False

        try:
            order = self._create_order(intent, instrument)
            self._strategy.submit_order(order)

            logger.info(
                "order_submitted",
                instrument=intent.instrument_id,
                side=intent.side,
                quantity=str(intent.quantity),
                order_type=intent.order_type,
            )

            # 发布提交事件
            self._event_bus.publish(
                OrderIntentEvent(
                    instrument_id=intent.instrument_id,
                    side=intent.side,
                    quantity=intent.quantity,
                    order_type=intent.order_type,
                    source="order_router",
                )
            )

            return True

        except Exception:
            logger.exception("order_submit_failed", instrument=intent.instrument_id)
            return False

    def _create_order(self, intent: OrderIntent, instrument: Instrument) -> MarketOrder:
        """创建 Nautilus 订单."""
        side = OrderSide.BUY if intent.side == "BUY" else OrderSide.SELL
        tif = TimeInForce[intent.time_in_force]

        if intent.order_type == "MARKET":
            return self._strategy.order_factory.market(
                instrument_id=instrument.id,
                order_side=side,
                quantity=instrument.make_qty(intent.quantity),
                time_in_force=tif,
                reduce_only=intent.reduce_only,
            )
        else:
            # 默认用市价单, 其他类型后续扩展
            return self._strategy.order_factory.market(
                instrument_id=instrument.id,
                order_side=side,
                quantity=instrument.make_qty(intent.quantity),
                time_in_force=tif,
                reduce_only=intent.reduce_only,
            )
