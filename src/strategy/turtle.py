"""海龟交易法策略.

规则:
- 入场: 20 周期 Donchian 突破。
- 出场: 10 周期反向 Donchian 突破。
- 风控: 2N 止损 (N = ATR)。
- 加仓: 每盈利 0.5N 加 1 单位，最多 4 单位。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from nautilus_trader.config import PositiveFloat, PositiveInt
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import EventBus, SignalDirection, SignalEvent
from src.strategy.base import BaseStrategy, BaseStrategyConfig


@dataclass
class _PendingOrder:
    action: str
    side: str
    qty: Decimal
    reduce_only: bool
    reason: str


class TurtleConfig(BaseStrategyConfig, frozen=True):
    """海龟交易策略配置."""

    instrument_id: InstrumentId
    bar_type: BarType
    entry_period: PositiveInt = 20
    exit_period: PositiveInt = 10
    atr_period: PositiveInt = 20
    stop_atr_multiplier: PositiveFloat = 2.0
    unit_add_atr_step: PositiveFloat = 0.5
    max_units: PositiveInt = 4
    breakout_lookback_exclude_current: bool = True


class TurtleStrategy(BaseStrategy):
    """Donchian 突破 + ATR 风控的海龟策略."""

    def __init__(self, config: TurtleConfig, event_bus: EventBus | None = None) -> None:
        super().__init__(config, event_bus)
        if self._atr_indicator is None:
            self._atr_indicator = AverageTrueRange(config.atr_period)

        self._highs: deque[float] = deque(maxlen=int(config.entry_period))
        self._lows: deque[float] = deque(maxlen=int(config.entry_period))

        self._position_side: str = "flat"  # flat / long / short
        self._units_held: int = 0
        self._unit_qty: Decimal | None = None
        self._last_add_price: float | None = None
        self._stop_price: float | None = None

        self._pending_order: _PendingOrder | None = None

    def _register_indicators(self) -> None:
        """仅依赖 ATR 指标."""

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        self._pending_order = None

        if self._atr_indicator is None or not self._atr_indicator.initialized:
            self._append_bar(bar)
            return None

        close = float(bar.close)
        if close <= 0:
            self._append_bar(bar)
            return None

        entry_period = int(self.config.entry_period)
        exit_period = int(self.config.exit_period)

        entry_ready = len(self._highs) >= entry_period and len(self._lows) >= entry_period
        exit_ready = len(self._highs) >= exit_period and len(self._lows) >= exit_period

        if not entry_ready:
            self._append_bar(bar)
            return None

        entry_high = max(list(self._highs)[-entry_period:])
        entry_low = min(list(self._lows)[-entry_period:])
        exit_high = max(list(self._highs)[-exit_period:]) if exit_ready else entry_high
        exit_low = min(list(self._lows)[-exit_period:]) if exit_ready else entry_low

        atr = float(self._atr_indicator.value)
        signal = self._decide_signal(
            bar=bar,
            close=close,
            atr=atr,
            entry_high=entry_high,
            entry_low=entry_low,
            exit_high=exit_high,
            exit_low=exit_low,
        )

        self._append_bar(bar)
        return signal

    def _decide_signal(
        self,
        bar: Bar,
        close: float,
        atr: float,
        entry_high: float,
        entry_low: float,
        exit_high: float,
        exit_low: float,
    ) -> SignalDirection | None:
        stop_distance = float(self.config.stop_atr_multiplier) * atr

        if self._position_side == "long":
            if self._stop_price is not None and close <= self._stop_price:
                return self._emit_exit("stop_2N")
            if close < exit_low:
                return self._emit_exit("exit_breakout")

            add_price = self._last_add_price
            if (
                add_price is not None
                and self._unit_qty is not None
                and self._units_held < int(self.config.max_units)
                and close >= add_price + float(self.config.unit_add_atr_step) * atr
            ):
                self._units_held += 1
                self._last_add_price = close
                self._stop_price = close - stop_distance
                return self._set_pending(
                    action="add",
                    side="BUY",
                    qty=self._unit_qty,
                    reduce_only=False,
                    reason="pyramid_0.5N",
                    direction=SignalDirection.LONG,
                )
            return None

        if self._position_side == "short":
            if self._stop_price is not None and close >= self._stop_price:
                return self._emit_exit("stop_2N")
            if close > exit_high:
                return self._emit_exit("exit_breakout")

            add_price = self._last_add_price
            if (
                add_price is not None
                and self._unit_qty is not None
                and self._units_held < int(self.config.max_units)
                and close <= add_price - float(self.config.unit_add_atr_step) * atr
            ):
                self._units_held += 1
                self._last_add_price = close
                self._stop_price = close + stop_distance
                return self._set_pending(
                    action="add",
                    side="SELL",
                    qty=self._unit_qty,
                    reduce_only=False,
                    reason="pyramid_0.5N",
                    direction=SignalDirection.SHORT,
                )
            return None

        unit_qty = self._build_unit_quantity(bar)
        if unit_qty is None:
            return None

        if close > entry_high:
            self._position_side = "long"
            self._units_held = 1
            self._unit_qty = unit_qty
            self._last_add_price = close
            self._stop_price = close - stop_distance
            return self._set_pending(
                action="entry",
                side="BUY",
                qty=unit_qty,
                reduce_only=False,
                reason="entry_breakout",
                direction=SignalDirection.LONG,
            )

        if close < entry_low:
            self._position_side = "short"
            self._units_held = 1
            self._unit_qty = unit_qty
            self._last_add_price = close
            self._stop_price = close + stop_distance
            return self._set_pending(
                action="entry",
                side="SELL",
                qty=unit_qty,
                reduce_only=False,
                reason="entry_breakout",
                direction=SignalDirection.SHORT,
            )

        return None

    def _build_unit_quantity(self, bar: Bar) -> Decimal | None:
        value = self._resolve_order_quantity_decimal(bar, fallback_trade_size=False)
        if value is None:
            return None
        if value <= 0:
            return None
        return value

    def _emit_exit(self, reason: str) -> SignalDirection:
        if self._units_held <= 0 or self._unit_qty is None:
            self._reset_position_state()
            return self._set_pending(
                action="exit",
                side="SELL",
                qty=Decimal("0"),
                reduce_only=True,
                reason=reason,
                direction=SignalDirection.FLAT,
            )

        qty = self._split_quantity_by_ratios_strict_step(
            total_qty=self._unit_qty * Decimal(str(self._units_held)),
            ratios=[Decimal("1")],
        )[0]
        side = "SELL" if self._position_side == "long" else "BUY"
        self._reset_position_state()
        return self._set_pending(
            action="exit",
            side=side,
            qty=qty,
            reduce_only=True,
            reason=reason,
            direction=SignalDirection.FLAT,
        )

    def _set_pending(
        self,
        action: str,
        side: str,
        qty: Decimal,
        reduce_only: bool,
        reason: str,
        direction: SignalDirection,
    ) -> SignalDirection:
        self._pending_order = _PendingOrder(
            action=action,
            side=side,
            qty=qty,
            reduce_only=reduce_only,
            reason=reason,
        )
        return direction

    def _append_bar(self, bar: Bar) -> None:
        self._highs.append(float(bar.high))
        self._lows.append(float(bar.low))

    def _reset_position_state(self) -> None:
        self._position_side = "flat"
        self._units_held = 0
        self._unit_qty = None
        self._last_add_price = None
        self._stop_price = None

    def _publish_signal(self, direction: SignalDirection, bar: Bar) -> None:
        pending = self._pending_order
        if pending is None:
            return

        self.log.info(
            f"Signal: {pending.action} {pending.side} qty={pending.qty} @ {bar.close}",
        )

        metadata: dict[str, Any] = {
            "bar_close": str(bar.close),
            "bar_type": str(self.config.bar_type),
            "signal_action": pending.action,
            "order_side": pending.side,
            "order_qty": str(pending.qty),
            "reduce_only": pending.reduce_only,
            "reason": pending.reason,
            "units_held": self._units_held,
        }

        if self._event_bus:
            signal = SignalEvent(
                source=self.__class__.__name__,
                instrument_id=str(self.config.instrument_id),
                direction=direction,
                strength=1.0,
                metadata=metadata,
            )
            self._event_bus.publish(signal)
        else:
            self._submit_market_order(direction, bar)

    def _submit_market_order(self, direction: SignalDirection, bar: Bar) -> None:
        pending = self._pending_order
        if pending is None or self.instrument is None:
            return

        if pending.action == "exit":
            self.close_all_positions(self.config.instrument_id)
            return

        side = OrderSide.BUY if pending.side == "BUY" else OrderSide.SELL
        qty = self.instrument.make_qty(pending.qty)
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=qty,
            time_in_force=TimeInForce.GTC,
            reduce_only=pending.reduce_only,
        )
        self.submit_order(order)

    def on_reset(self) -> None:
        if self._atr_indicator is not None:
            self._atr_indicator.reset()

        self._highs.clear()
        self._lows.clear()
        self._pending_order = None
        self._reset_position_state()
