"""成交处理.

处理订单成交事件, 更新状态、记录交易、触发后续流程.
"""

from __future__ import annotations

import structlog

from src.core.events import Event, EventBus, EventType
from src.state.persistence import TradePersistence

logger = structlog.get_logger()


class FillHandler:
    """成交处理器.

    订单成交后:
    1. 记录交易到持久化层
    2. 更新仓位状态
    3. 触发事后风控
    4. 发布成交事件
    """

    def __init__(self, event_bus: EventBus, persistence: TradePersistence) -> None:
        self._event_bus = event_bus
        self._persistence = persistence

    def on_fill(
        self,
        instrument_id: str,
        side: str,
        quantity: str,
        price: str,
        order_id: str = "",
        strategy_id: str = "",
        fees: str = "0",
    ) -> None:
        """处理成交.

        Args:
            instrument_id: 交易对
            side: BUY / SELL
            quantity: 成交数量
            price: 成交价格
            order_id: 订单ID
            strategy_id: 策略ID
            fees: 手续费
        """
        # 1. 持久化
        self._persistence.record_trade(
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            price=price,
            order_id=order_id,
            strategy_id=strategy_id,
            fees=fees,
        )

        # 2. 发布成交事件
        self._event_bus.publish(
            Event(
                event_type=EventType.ORDER_FILLED,
                source="fill_handler",
                payload={
                    "instrument_id": instrument_id,
                    "side": side,
                    "quantity": quantity,
                    "price": price,
                    "order_id": order_id,
                    "fees": fees,
                },
            )
        )

        logger.info(
            "fill_processed",
            instrument=instrument_id,
            side=side,
            quantity=quantity,
            price=price,
            fees=fees,
        )
