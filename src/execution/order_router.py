"""订单路由.

接收经风控审核的 OrderIntent, 转化为实际订单并提交到交易所.

流程: Signal → OrderIntent → PreTradeRisk → OrderRouter → Exchange
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

import structlog
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy

from src.core.events import EventBus, OrderIntentEvent, RiskAlertEvent
from src.execution.order_intent import OrderIntent

logger = structlog.get_logger()


class OrderRouter:
    """订单路由器.

    负责将 OrderIntent 转化为 Nautilus 订单并通过 Strategy 提交.
    """

    def __init__(self, event_bus: EventBus) -> None:
        """Initialize the order router.

        Args:
            event_bus: Event bus used for cross-module communication.
        """
        self._event_bus = event_bus
        self._strategy: Strategy | None = None
        self._strategies: dict[str, Strategy] = {}

    def bind_strategy(self, strategy: Strategy) -> None:
        """绑定 Nautilus Strategy 实例 (用于下单).

        Args:
            strategy: Strategy instance to bind or inspect.
        """
        self._strategy = strategy
        instrument_id = getattr(getattr(strategy, "config", None), "instrument_id", None)
        if instrument_id is not None:
            self._strategies[str(instrument_id)] = strategy

    def route(self, intent: OrderIntent) -> bool:
        """路由订单.

        Args:
            intent: 订单意图

        Returns:
            是否成功提交

        """
        strategy = self._strategies.get(intent.instrument_id)
        if strategy is None and len(self._strategies) <= 1:
            strategy = self._strategy
        if strategy is None:
            logger.error("order_router_no_strategy")
            return False

        instrument = strategy.cache.instrument(InstrumentId.from_str(intent.instrument_id))
        if instrument is None:
            logger.error("instrument_not_found", instrument_id=intent.instrument_id)
            return False

        try:
            order = self._create_order(intent=intent, instrument=instrument, strategy=strategy)
            strategy.submit_order(order)

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
                    price=intent.price,
                    metadata=intent.metadata,
                    source="order_router",
                )
            )

            return True

        except Exception:
            logger.exception("order_submit_failed", instrument=intent.instrument_id)
            return False

    def _create_order(
        self,
        intent: OrderIntent,
        instrument: Instrument,
        strategy: Strategy,
    ) -> Any:
        """创建 Nautilus 订单.

        Args:
            intent: Intent.
            instrument: Instrument.
            strategy: Strategy instance to bind or inspect.
        """
        side = OrderSide.BUY if intent.side == "BUY" else OrderSide.SELL
        quantity = self._normalize_quantity_to_step(
            quantity=intent.quantity,
            instrument=instrument,
            instrument_id=intent.instrument_id,
        )
        if quantity is None:
            raise ValueError(f"Invalid quantity after step normalization: {intent.quantity}")
        try:
            tif = TimeInForce[intent.time_in_force.upper()]
        except KeyError as exc:
            raise ValueError(f"Unsupported time_in_force: {intent.time_in_force}") from exc

        order_type = intent.order_type.upper()
        if order_type == "MARKET":
            return strategy.order_factory.market(
                instrument_id=instrument.id,
                order_side=side,
                quantity=instrument.make_qty(quantity),
                time_in_force=tif,
                reduce_only=intent.reduce_only,
            )

        if order_type == "LIMIT":
            if intent.price is None:
                raise ValueError("LIMIT order requires price")
            post_only = bool(intent.metadata.get("post_only", False))
            chase_ticks = self._parse_non_negative_int(intent.metadata.get("chase_ticks", 0), default=0)
            limit_ttl_ms = self._parse_non_negative_int(intent.metadata.get("limit_ttl_ms", 0), default=0)

            if post_only and limit_ttl_ms > 0:
                logger.warning(
                    "limit_ttl_ignored_for_post_only",
                    instrument=intent.instrument_id,
                    limit_ttl_ms=limit_ttl_ms,
                )
                limit_ttl_ms = 0

            effective_tif = TimeInForce.IOC if limit_ttl_ms > 0 else tif
            effective_price = self._apply_chase_ticks(
                base_price=intent.price,
                side=intent.side,
                chase_ticks=chase_ticks,
                instrument=instrument,
            )

            return strategy.order_factory.limit(
                instrument_id=instrument.id,
                order_side=side,
                quantity=instrument.make_qty(quantity),
                price=instrument.make_price(effective_price),
                time_in_force=effective_tif,
                reduce_only=intent.reduce_only,
                post_only=post_only,
            )

        raise ValueError(f"Unsupported order_type: {intent.order_type}")

    @staticmethod
    def _parse_non_negative_int(raw: Any, default: int = 0) -> int:
        try:
            value = int(str(raw))
        except (ValueError, TypeError):
            return default
        return value if value >= 0 else default

    @staticmethod
    def _apply_chase_ticks(
        base_price: Decimal,
        side: str,
        chase_ticks: int,
        instrument: Instrument,
    ) -> Decimal:
        if chase_ticks <= 0:
            return base_price

        tick_size = Decimal("0.1")
        raw_tick = getattr(instrument, "price_increment", None)
        if raw_tick is not None:
            try:
                tick_size = Decimal(str(raw_tick))
            except (InvalidOperation, ValueError, TypeError):
                tick_size = Decimal("0.1")

        offset = tick_size * Decimal(str(chase_ticks))
        adjusted = base_price + offset if side.upper() == "BUY" else base_price - offset

        return adjusted if adjusted > 0 else base_price

    def _normalize_quantity_to_step(
        self,
        quantity: Decimal,
        instrument: Instrument,
        instrument_id: str,
    ) -> Decimal | None:
        if quantity <= 0:
            self._event_bus.publish(
                RiskAlertEvent(
                    level="ERROR",
                    rule_name="order_router_quantity_invalid",
                    message="Order quantity must be positive",
                    details={
                        "instrument_id": instrument_id,
                        "raw_quantity": str(quantity),
                    },
                    source="order_router",
                )
            )
            return None

        raw_step = getattr(instrument, "size_increment", None)
        if raw_step is None:
            return quantity

        try:
            step = Decimal(str(raw_step))
        except (InvalidOperation, ValueError, TypeError):
            return quantity

        if step <= 0:
            return quantity

        steps = (quantity / step).to_integral_value(rounding=ROUND_FLOOR)
        normalized = steps * step
        if normalized <= 0:
            logger.warning(
                "order_quantity_below_step_rejected",
                instrument=instrument_id,
                raw_quantity=str(quantity),
                size_increment=str(step),
            )
            self._event_bus.publish(
                RiskAlertEvent(
                    level="ERROR",
                    rule_name="order_router_quantity_below_step",
                    message="Order quantity below instrument size increment",
                    details={
                        "instrument_id": instrument_id,
                        "raw_quantity": str(quantity),
                        "size_increment": str(step),
                    },
                    source="order_router",
                )
            )
            return None

        if normalized != quantity:
            logger.warning(
                "order_quantity_normalized_to_step",
                instrument=instrument_id,
                raw_quantity=str(quantity),
                normalized_quantity=str(normalized),
                size_increment=str(step),
            )
            self._event_bus.publish(
                RiskAlertEvent(
                    level="WARNING",
                    rule_name="order_router_quantity_normalized",
                    message="Order quantity normalized to instrument size increment",
                    details={
                        "instrument_id": instrument_id,
                        "raw_quantity": str(quantity),
                        "normalized_quantity": str(normalized),
                        "size_increment": str(step),
                    },
                    source="order_router",
                )
            )
        return normalized
