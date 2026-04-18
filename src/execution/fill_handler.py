"""成交处理.

处理订单成交事件, 更新状态、记录交易、触发后续流程.
"""

from __future__ import annotations

import structlog

from src.core.events import Event, EventBus, EventType
from src.risk.post_trade import PostTradeAnalyzer, TradeAnalysis
from src.state.persistence import TradePersistence

logger = structlog.get_logger(__name__)


class FillHandler:
    """成交处理器.

    订单成交后:
    1. 记录交易到持久化层
    2. 更新仓位状态
    3. 触发事后风控
    4. 发布成交事件
    """

    def __init__(
        self,
        event_bus: EventBus,
        persistence: TradePersistence,
        post_trade_analyzer: PostTradeAnalyzer | None = None,
    ) -> None:
        """Initialize the fill handler.

        Args:
            event_bus: Event bus used for cross-module communication.
            persistence: Persistence.
            post_trade_analyzer: Post-trade analyzer for PnL/slippage attribution.
        """
        self._event_bus = event_bus
        self._persistence = persistence
        self._post_trade_analyzer = post_trade_analyzer

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

        # 2. 事后风控分析
        if self._post_trade_analyzer is not None:
            try:
                from decimal import Decimal

                # 根据成交数据创建 贸易分析
                # 注意：单笔成交使用同一价格作为入场价和出场价
                # 真实 PNL计算需要跨多笔成交追踪仓位
                analysis = TradeAnalysis(
                    instrument_id=instrument_id,
                    side=side,
                    quantity=Decimal(quantity),
                    entry_price=Decimal(price),
                    exit_price=Decimal(price),  # 单笔成交时与入场价相同
                    pnl=Decimal("0"),  # 未追踪仓位的单笔成交按零 PNL处理
                    fees=Decimal(fees),
                    slippage_bps=0.0,  # 计算滑点需要预期价格
                    duration_seconds=0.0,  # 计算需要订单提交时间
                    strategy_id=strategy_id or "unknown",
                )
                self._post_trade_analyzer.record_trade(analysis)
            except (AttributeError, ValueError, TypeError) as exc:
                logger.warning("post_trade_record_failed", error=str(exc))

        # 3. 发布成交事件
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
