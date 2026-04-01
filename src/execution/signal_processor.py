"""信号处理器.

订阅策略信号并转换为标准 OrderIntent, 交由 OrderRouter 下单。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from src.core.events import EventType, OrderIntentEvent, RiskAlertEvent, SignalEvent
from src.execution.ignored_instruments import IgnoredInstrumentRegistry
from src.execution.order_intent import OrderIntent
from src.execution.order_router import OrderRouter
from src.execution.rate_limiter import RateLimiter
from src.risk.position_sizer import PositionSizer
from src.risk.pre_trade import PreTradeRiskManager

logger = structlog.get_logger(__name__)


class SignalProcessor:
    """将 SignalEvent 转换为 OrderIntent 的桥接器."""

    def __init__(
        self,
        event_bus: Any,
        order_router: OrderRouter,
        pre_trade_risk: PreTradeRiskManager | None = None,
        rate_limiter: RateLimiter | None = None,
        ignored_instruments: IgnoredInstrumentRegistry | None = None,
        position_sizer: PositionSizer | None = None,
    ) -> None:
        """Initialize the signal processor.

        Args:
            event_bus: Event bus used for cross-module communication.
            order_router: Order router.
            pre_trade_risk: Pre trade risk.
            rate_limiter: Rate limiter.
            ignored_instruments: Ignored instruments.
            position_sizer: Position sizer for calculating trade quantities.
        """
        self._event_bus = event_bus
        self._order_router = order_router
        self._pre_trade_risk = pre_trade_risk
        self._rate_limiter = rate_limiter
        self._ignored_instruments = ignored_instruments
        self._position_sizer = position_sizer
        self._event_bus.subscribe(EventType.SIGNAL, self._on_signal)

    def _on_signal(self, event: Any) -> None:
        if not isinstance(event, SignalEvent):
            return

        intent = self._to_intent(event)
        if intent is None:
            return
        if self._is_ignored(intent):
            return

        if not self._check_rate_limit(intent):
            return
        if not self._check_pre_trade_risk(intent):
            return

        if self._order_router.route(intent) and self._rate_limiter is not None:
            self._rate_limiter.record()

    def _is_ignored(self, intent: OrderIntent) -> bool:
        if self._ignored_instruments is None or not self._ignored_instruments.is_ignored(intent.instrument_id):
            return False

        ignored = self._ignored_instruments.get(intent.instrument_id) or {}
        logger.warning(
            "signal_rejected_ignored_instrument",
            instrument=intent.instrument_id,
            strategy=intent.strategy_id,
            reason=ignored.get("reason", ""),
        )
        self._event_bus.publish(
            RiskAlertEvent(
                level="WARNING",
                rule_name="ignored_instrument",
                message="Signal rejected because instrument is ignored",
                details={
                    "instrument_id": intent.instrument_id,
                    "strategy_id": intent.strategy_id,
                    "reason": ignored.get("reason", ""),
                },
                source="signal_processor",
            )
        )
        return True

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
                quantity=self._apply_position_sizing(qty, signal, metadata, raw_price or price),
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
            quantity=self._apply_position_sizing(default_qty, signal, metadata, price),
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

    def _apply_position_sizing(
        self,
        base_quantity: Decimal,
        signal: SignalEvent,
        metadata: dict[str, Any],
        current_price: Decimal | None,
    ) -> Decimal:
        """Apply position sizing if PositionSizer is available.

        Args:
            base_quantity: The base quantity before sizing
            signal: The signal event containing strength
            metadata: Signal metadata for account info
            current_price: Current market price

        Returns:
            Sized quantity
        """
        if self._position_sizer is None:
            return base_quantity

        # Extract signal strength
        signal_strength = getattr(signal, "strength", 1.0) or 1.0

        # Get current price from various sources
        if current_price is None:
            current_price = (
                self._parse_qty(metadata.get("bar_close")) or self._parse_qty(metadata.get("price")) or Decimal("0")
            )

        # Get account equity - for now use a default, this should be injected properly
        # TODO: Account equity should come from portfolio/account state
        account_equity = self._parse_qty(metadata.get("account_equity")) or Decimal("10000")

        if current_price <= 0 or account_equity <= 0:
            logger.warning(
                "position_sizing_skipped_invalid_params",
                current_price=str(current_price),
                account_equity=str(account_equity),
                base_quantity=str(base_quantity),
            )
            return base_quantity

        try:
            sized_qty = self._position_sizer.calculate(
                account_equity=account_equity,
                current_price=current_price,
                signal_strength=float(signal_strength),
            )

            if sized_qty > Decimal("0"):
                logger.info(
                    "position_size_calculated",
                    signal_strength=signal_strength,
                    base_quantity=str(base_quantity),
                    sized_quantity=str(sized_qty),
                    current_price=str(current_price),
                    account_equity=str(account_equity),
                )
                return sized_qty
            else:
                logger.warning(
                    "position_sizing_returned_zero",
                    signal_strength=signal_strength,
                    current_price=str(current_price),
                    account_equity=str(account_equity),
                )
                return base_quantity

        except (ValueError, TypeError, ZeroDivisionError) as exc:
            logger.warning(
                "position_sizing_error",
                error=str(exc),
                base_quantity=str(base_quantity),
            )
            return base_quantity

    def _check_rate_limit(self, intent: OrderIntent) -> bool:
        if self._rate_limiter is None:
            return True
        if self._rate_limiter.can_proceed():
            return True

        logger.warning("signal_rejected_rate_limit", instrument=intent.instrument_id, strategy=intent.strategy_id)
        self._event_bus.publish(
            RiskAlertEvent(
                level="ERROR",
                rule_name="rate_limit",
                message="Order rejected by rate limiter",
                details={
                    "instrument_id": intent.instrument_id,
                    "strategy_id": intent.strategy_id,
                },
                source="signal_processor",
            )
        )
        return False

    def _check_pre_trade_risk(self, intent: OrderIntent) -> bool:
        if self._pre_trade_risk is None:
            return True

        risk_event = OrderIntentEvent(
            instrument_id=intent.instrument_id,
            side=intent.side,
            quantity=intent.quantity,
            order_type=intent.order_type,
            price=intent.price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            metadata=intent.metadata,
            source=intent.strategy_id,
        )
        metadata = intent.metadata if isinstance(intent.metadata, dict) else {}
        current_price = (
            intent.price
            or self._parse_qty(metadata.get("order_price"))
            or self._parse_qty(metadata.get("price"))
            or self._parse_qty(metadata.get("bar_close"))
            or Decimal("0")
        )
        current_position_usd = self._parse_qty(metadata.get("current_position_usd")) or Decimal("0")
        current_open_orders_raw = metadata.get("current_open_orders", 0)
        try:
            current_open_orders = int(current_open_orders_raw)
        except (TypeError, ValueError):
            current_open_orders = 0

        result = self._pre_trade_risk.check(
            intent=risk_event,
            current_position_usd=current_position_usd,
            current_open_orders=current_open_orders,
            current_price=current_price,
        )
        if not result.passed:
            logger.warning(
                "signal_rejected_pre_trade_risk",
                instrument=intent.instrument_id,
                strategy=intent.strategy_id,
                reason=result.reason,
            )
        return result.passed
