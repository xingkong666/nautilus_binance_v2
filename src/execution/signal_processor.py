"""信号处理器.

订阅策略信号并转换为标准 OrderIntent, 交由 OrderRouter 下单。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from src.core.events import EventType, SignalEvent
from src.execution.order_intent import OrderIntent
from src.execution.order_router import OrderRouter

logger = structlog.get_logger()


class SignalProcessor:
    """将 SignalEvent 转换为 OrderIntent 的桥接器."""

    def __init__(self, event_bus: Any, order_router: OrderRouter) -> None:
        self._event_bus = event_bus
        self._order_router = order_router
        self._event_bus.subscribe(EventType.SIGNAL, self._on_signal)

    def _on_signal(self, event: Any) -> None:
        if not isinstance(event, SignalEvent):
            return

        intent = self._to_intent(event)
        if intent is None:
            return

        self._order_router.route(intent)

    def _to_intent(self, signal: SignalEvent) -> OrderIntent | None:
        metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
        raw_qty = metadata.get("order_qty")
        raw_side = metadata.get("order_side")
        raw_order_type = str(metadata.get("order_type", "MARKET")).upper()
        raw_tif = str(metadata.get("time_in_force", "GTC")).upper()
        raw_price = metadata.get("order_price", metadata.get("price"))
        price = self._parse_qty(raw_price) if raw_price is not None else None

        if raw_qty is not None and raw_side is not None:
            qty = self._parse_qty(raw_qty)
            side = str(raw_side).upper()
            if qty is None or qty <= 0:
                logger.warning("signal_ignored_invalid_qty", source=signal.source, raw_qty=raw_qty)
                return None
            if side not in {"BUY", "SELL"}:
                logger.warning("signal_ignored_invalid_side", source=signal.source, raw_side=raw_side)
                return None
            if raw_order_type not in {"MARKET", "LIMIT"}:
                logger.warning("signal_ignored_invalid_order_type", source=signal.source, raw_order_type=raw_order_type)
                return None
            if raw_order_type == "LIMIT" and (price is None or price <= 0):
                logger.warning(
                    "signal_ignored_invalid_limit_price",
                    source=signal.source,
                    raw_price=raw_price,
                )
                return None

            return OrderIntent(
                instrument_id=signal.instrument_id,
                side=side,
                quantity=qty,
                order_type=raw_order_type,
                price=price,
                time_in_force=raw_tif,
                reduce_only=bool(metadata.get("reduce_only", False)),
                strategy_id=signal.source,
                metadata=metadata,
            )

        # 兼容旧策略: 缺省数量使用 0.01
        default_qty = self._parse_qty(metadata.get("quantity", "0.01"))
        if default_qty is None or default_qty <= 0:
            logger.warning("signal_ignored_default_qty_invalid", source=signal.source)
            return None
        if raw_order_type not in {"MARKET", "LIMIT"}:
            logger.warning("signal_ignored_invalid_order_type", source=signal.source, raw_order_type=raw_order_type)
            return None
        if raw_order_type == "LIMIT" and (price is None or price <= 0):
            logger.warning(
                "signal_ignored_invalid_limit_price",
                source=signal.source,
                raw_price=raw_price,
            )
            return None

        return OrderIntent.from_signal(
            instrument_id=signal.instrument_id,
            direction=signal.direction,
            quantity=default_qty,
            strategy_id=signal.source,
            order_type=raw_order_type,
            price=price,
            time_in_force=raw_tif,
            metadata=metadata,
        )

    @staticmethod
    def _parse_qty(raw: Any) -> Decimal | None:
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError):
            return None
