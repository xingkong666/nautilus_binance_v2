"""海龟交易法策略.

规则:
- 入场: 20 周期 Donchian 突破。
- 出场: 10 周期反向 Donchian 突破。
- 风控: 2N 止损 (N = ATR)。
- 加仓: 每盈利 0.5N 加 1 单位，最多 4 单位。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from nautilus_trader.config import PositiveFloat, PositiveInt
from nautilus_trader.indicators import DonchianChannel
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import EventBus, SignalDirection, SignalEvent
from src.strategy.base import BaseStrategy, BaseStrategyConfig, _PendingOrder


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
        """Initialize the turtle strategy.

        Args:
            config: Configuration values for the component.
            event_bus: Event bus used for cross-module communication.
        """
        super().__init__(config, event_bus)
        self._ensure_atr_indicator()

        # NT 内置唐奇安通道（替代手动 deque[float] 追踪）
        self._entry_channel = DonchianChannel(int(config.entry_period))
        self._exit_channel = DonchianChannel(int(config.exit_period))

        self._position_side: str = "flat"  # 空仓 / 多头 / 空头
        self._units_held: int = 0
        self._unit_qty: Decimal | None = None
        self._last_add_price: float | None = None
        self._stop_price: float | None = None

        self._pending_order: _PendingOrder | None = None

        # 前一根 K 线的唐奇安值，用于排除当前 K 线语义
        self._prev_entry_high: float | None = None
        self._prev_entry_low: float | None = None
        self._prev_exit_high: float | None = None
        self._prev_exit_low: float | None = None

    def _register_indicators(self) -> None:
        """注册 Donchian 通道指标（ATR 由 BaseStrategy 注册）."""
        self.register_indicator_for_bars(self.config.bar_type, self._entry_channel)
        self.register_indicator_for_bars(self.config.bar_type, self._exit_channel)

    def _history_warmup_bars(self) -> int:
        return (
            max(
                int(self.config.entry_period),
                int(self.config.exit_period),
                int(self.config.atr_period),
            )
            + 2
        )

    def _on_historical_bar(self, bar: Bar) -> None:
        pass  # DonchianChannel 和 ATR 通过 register_indicator_for_bars 更新

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        """Generate signal.

        Args:
            bar: Bar data for the current evaluation.

        Returns:
            SignalDirection: Result of generate signal.
        """
        self._pending_order = None

        if self._atr_indicator is None or not self._atr_indicator.initialized:
            return None

        close = float(bar.close)
        if close <= 0:
            return None

        if not self._entry_channel.initialized or not self._exit_channel.initialized:
            return None

        cur_entry_high = float(self._entry_channel.upper)
        cur_entry_low = float(self._entry_channel.lower)
        cur_exit_high = float(self._exit_channel.upper)
        cur_exit_low = float(self._exit_channel.lower)

        # exclude_current 为 True 时，将收盘价与前一根 K 线的
        # 通道值比较，避免当前 K 线价格抬高通道。
        if self.config.breakout_lookback_exclude_current and self._prev_entry_high is not None:
            entry_high = self._prev_entry_high
            entry_low = self._prev_entry_low  # type: ignore[assignment]
            exit_high = self._prev_exit_high  # type: ignore[assignment]
            exit_low = self._prev_exit_low  # type: ignore[assignment]
        else:
            entry_high = cur_entry_high
            entry_low = cur_entry_low
            exit_high = cur_exit_high
            exit_low = cur_exit_low

        atr = float(self._atr_indicator.value)
        result = self._decide_signal(
            bar=bar,
            close=close,
            atr=atr,
            entry_high=entry_high,
            entry_low=entry_low,
            exit_high=exit_high,
            exit_low=exit_low,
        )

        # 保存当前值快照，供下一根 K 线比较
        self._prev_entry_high = cur_entry_high
        self._prev_entry_low = cur_entry_low
        self._prev_exit_high = cur_exit_high
        self._prev_exit_low = cur_exit_low

        return result

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
        """Run on reset."""
        super().on_reset()
        if self._atr_indicator is not None:
            self._atr_indicator.reset()

        self._entry_channel.reset()
        self._exit_channel.reset()
        self._pending_order = None
        self._prev_entry_high = None
        self._prev_entry_low = None
        self._prev_exit_high = None
        self._prev_exit_low = None
        self._reset_position_state()
