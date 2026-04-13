"""Vegas Tunnel strategy.

经典 Vegas 隧道法（项目适配版）：
- 趋势框架：EMA144/EMA169 作为隧道；
- 触发信号：EMA12/EMA36 穿越，且同向位于隧道外侧；
- 风控：入场后按 ATR 初始止损；
- 止盈：基于ATR Fib 三档分批止盈（默认 40/30/30）。

策略层仅产出信号与订单意图元数据，不直接调用交易所 API。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from nautilus_trader.config import PositiveFloat, PositiveInt
from nautilus_trader.indicators import ExponentialMovingAverage, RelativeStrengthIndex
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import EventBus, SignalDirection, SignalEvent
from src.strategy.base import BaseStrategy, BaseStrategyConfig, _PendingOrder


class VegasTunnelConfig(BaseStrategyConfig, frozen=True):
    """Vegas 隧道策略配置."""

    instrument_id: InstrumentId
    bar_type: BarType

    fast_ema_period: PositiveInt = 12
    slow_ema_period: PositiveInt = 36
    tunnel_ema_period_1: PositiveInt = 144
    tunnel_ema_period_2: PositiveInt = 169

    signal_cooldown_bars: int = 3
    atr_filter_min_ratio: float = 0.0
    min_tunnel_width_pct: float = 0.0

    stop_atr_multiplier: PositiveFloat = 1.0
    tp_fib_1: PositiveFloat = 1.0
    tp_fib_2: PositiveFloat = 1.618
    tp_fib_3: PositiveFloat = 2.618

    tp_split_1: PositiveFloat = 0.4
    tp_split_2: PositiveFloat = 0.3
    tp_split_3: PositiveFloat = 0.3

    trail_stop_after_tp2: bool = False

    rsi_filter_period: PositiveInt = 14
    rsi_long_max: float = 100.0
    rsi_short_min: float = 0.0


class VegasTunnelStrategy(BaseStrategy):
    """EMA12/36 + Vegas tunnel + Fib ladder TP."""

    def __init__(self, config: VegasTunnelConfig, event_bus: EventBus | None = None) -> None:
        """Initialize the vegas tunnel strategy.

        Args:
            config: Configuration values for the component.
            event_bus: Event bus used for cross-module communication.
        """
        super().__init__(config, event_bus)

        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)
        self.tunnel_ema_1 = ExponentialMovingAverage(config.tunnel_ema_period_1)
        self.tunnel_ema_2 = ExponentialMovingAverage(config.tunnel_ema_period_2)
        self.rsi = RelativeStrengthIndex(config.rsi_filter_period)

        self._ensure_atr_indicator()

        self._prev_fast_above_slow: bool | None = None
        # 当前 bar 的隧道边界，供 _maybe_exit 引用（trail_stop_after_tp2）
        self._tunnel_upper: float = 0.0
        self._tunnel_lower: float = 0.0

        self._is_long: bool | None = None  # None = flat, True = long, False = short
        self._entry_price: float | None = None
        self._remaining_qty = Decimal("0")
        self._stop_price: float | None = None
        self._tp_prices: list[float] = []
        self._tp_qtys: list[Decimal] = []
        self._tp_filled: list[bool] = [False, False, False]

        self._pending_order: _PendingOrder | None = None

    def _register_indicators(self) -> None:
        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.tunnel_ema_1)
        self.register_indicator_for_bars(self.config.bar_type, self.tunnel_ema_2)
        self.register_indicator_for_bars(self.config.bar_type, self.rsi)

    def _history_warmup_bars(self) -> int:
        return (
            max(
                int(self.config.fast_ema_period),
                int(self.config.slow_ema_period),
                int(self.config.tunnel_ema_period_1),
                int(self.config.tunnel_ema_period_2),
                int(self.config.atr_period),
            )
            + 2
        )

    def _on_historical_bar(self, bar: Bar) -> None:
        self._bar_index += 1
        self._prev_fast_above_slow = float(self.fast_ema.value) > float(self.slow_ema.value)

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        """Generate signal.

        Args:
            bar: Bar data for the current evaluation.

        Returns:
            SignalDirection: Result of generate signal.
        """
        self._pending_order = None
        self._bar_index += 1

        if self._atr_indicator is None or not self._atr_indicator.initialized:
            return None

        close = float(bar.close)
        if close <= 0:
            return None

        fast = float(self.fast_ema.value)
        slow = float(self.slow_ema.value)
        tunnel_a = float(self.tunnel_ema_1.value)
        tunnel_b = float(self.tunnel_ema_2.value)
        tunnel_upper = max(tunnel_a, tunnel_b)
        tunnel_lower = min(tunnel_a, tunnel_b)

        # 保存当前隧道边界（供 _maybe_exit 的追踪止损使用）
        self._tunnel_upper = tunnel_upper
        self._tunnel_lower = tunnel_lower

        # 先处理已有仓位的止损/分批止盈
        is_long = self._is_long is True
        is_short = self._is_long is False
        has_position = self._is_long is not None
        if has_position:
            exit_signal = self._maybe_exit(close)
            if exit_signal is not None:
                return exit_signal

        fast_above_slow = fast > slow
        if self._prev_fast_above_slow is None:
            self._prev_fast_above_slow = fast_above_slow
            return None

        long_cross = fast_above_slow and not self._prev_fast_above_slow
        short_cross = (not fast_above_slow) and self._prev_fast_above_slow
        self._prev_fast_above_slow = fast_above_slow

        long_ready = long_cross and fast > tunnel_upper and slow > tunnel_upper
        short_ready = short_cross and fast < tunnel_lower and slow < tunnel_lower

        # 与持仓反向信号，先平再说，避免同 bar 反手冲突
        if is_long and short_ready:
            return self._close_full(reason="reverse_signal_short")
        if is_short and long_ready:
            return self._close_full(reason="reverse_signal_long")

        if has_position:
            return None

        atr_ratio_min = float(self.config.atr_filter_min_ratio)
        if atr_ratio_min > 0:
            atr_ratio = float(self._atr_indicator.value) / close
            if atr_ratio < atr_ratio_min:
                return None

        min_tunnel_width_pct = float(self.config.min_tunnel_width_pct)
        if min_tunnel_width_pct > 0 and close > 0:
            tunnel_width_pct = (tunnel_upper - tunnel_lower) / close
            if tunnel_width_pct < min_tunnel_width_pct:
                return None

        if not self._cooldown_passed():
            return None

        rsi_val = float(self.rsi.value) if self.rsi.initialized else None

        if long_ready:
            if rsi_val is not None and rsi_val >= float(self.config.rsi_long_max):
                return None
            return self._open_position(
                side="long",
                close=close,
                bar=bar,
            )

        if short_ready:
            if rsi_val is not None and rsi_val <= float(self.config.rsi_short_min):
                return None
            return self._open_position(
                side="short",
                close=close,
                bar=bar,
            )

        return None

    def _open_position(self, side: str, close: float, bar: Bar) -> SignalDirection | None:
        total_qty = self._resolve_order_quantity_decimal(bar, fallback_trade_size=False)
        if total_qty is None or total_qty <= 0:
            return None

        split_qtys = self._split_quantities(total_qty)
        atr = float(self._atr_indicator.value)

        if side == "long":
            stop_price = close - atr * float(self.config.stop_atr_multiplier)
            tp_prices = [
                close + atr * float(self.config.tp_fib_1),
                close + atr * float(self.config.tp_fib_2),
                close + atr * float(self.config.tp_fib_3),
            ]
            order_side = "BUY"
            signal = SignalDirection.LONG
        else:
            stop_price = close + atr * float(self.config.stop_atr_multiplier)
            tp_prices = [
                close - atr * float(self.config.tp_fib_1),
                close - atr * float(self.config.tp_fib_2),
                close - atr * float(self.config.tp_fib_3),
            ]
            order_side = "SELL"
            signal = SignalDirection.SHORT

        self._is_long = side == "long"
        self._entry_price = close
        self._remaining_qty = total_qty
        self._stop_price = stop_price
        self._tp_prices = tp_prices
        self._tp_qtys = split_qtys
        self._tp_filled = [False, False, False]

        self._pending_order = _PendingOrder(
            action="entry",
            side=order_side,
            qty=total_qty,
            reduce_only=False,
            reason=f"vegas_entry_{side}",
        )
        self._last_signal_bar_index = self._bar_index
        return signal

    def _maybe_exit(self, close: float) -> SignalDirection | None:
        entry = self._entry_price
        stop = self._stop_price
        if entry is None or stop is None or self._remaining_qty <= 0:
            self._reset_position_state()
            return None

        # 硬止损
        if self._is_long is True and close <= stop:
            return self._close_full(reason="stop_loss")
        if self._is_long is False and close >= stop:
            return self._close_full(reason="stop_loss")

        # 分批止盈（每根 bar 最多触发一档）
        for idx in range(3):
            if self._tp_filled[idx]:
                continue
            tp_price = self._tp_prices[idx]
            hit = close >= tp_price if self._is_long is True else close <= tp_price
            if not hit:
                continue

            qty = self._tp_qtys[idx]
            if qty <= 0:
                self._tp_filled[idx] = True
                continue

            qty = min(qty, self._remaining_qty)
            self._remaining_qty -= qty
            self._tp_filled[idx] = True

            if idx == 0:
                # TP1 后上移到保本
                self._stop_price = entry

            if idx == 1 and self.config.trail_stop_after_tp2:
                # TP2 后将止损追踪至隧道边（锁定更多利润）
                if self._is_long is True:
                    self._stop_price = self._tunnel_lower
                else:
                    self._stop_price = self._tunnel_upper

            is_last = self._remaining_qty <= Decimal("0") or idx == 2
            if is_last:
                qty += max(self._remaining_qty, Decimal("0"))
                self._remaining_qty = Decimal("0")

            close_side = "SELL" if self._is_long is True else "BUY"
            self._pending_order = _PendingOrder(
                action=f"tp{idx + 1}",
                side=close_side,
                qty=qty,
                reduce_only=True,
                reason=f"take_profit_{idx + 1}",
            )

            if is_last:
                self._reset_position_state()

            self._last_signal_bar_index = self._bar_index
            return SignalDirection.FLAT

        return None

    def _close_full(self, reason: str) -> SignalDirection:
        if self._remaining_qty <= 0:
            self._reset_position_state()
            return SignalDirection.FLAT

        qty = self._remaining_qty
        close_side = "SELL" if self._is_long is True else "BUY"
        self._pending_order = _PendingOrder(
            action="exit",
            side=close_side,
            qty=qty,
            reduce_only=True,
            reason=reason,
        )
        self._reset_position_state()
        self._last_signal_bar_index = self._bar_index
        return SignalDirection.FLAT

    def _split_quantities(self, total_qty: Decimal) -> list[Decimal]:
        return self._split_quantity_by_ratios_strict_step(
            total_qty=total_qty,
            ratios=[
                Decimal(str(self.config.tp_split_1)),
                Decimal(str(self.config.tp_split_2)),
                Decimal(str(self.config.tp_split_3)),
            ],
        )

    def _publish_signal(self, direction: SignalDirection, bar: Bar) -> None:
        pending = self._pending_order
        if pending is None:
            return

        metadata: dict[str, Any] = {
            "bar_close": str(bar.close),
            "bar_type": str(self.config.bar_type),
            "signal_action": pending.action,
            "order_side": pending.side,
            "order_qty": str(pending.qty),
            "order_type": "MARKET",
            "time_in_force": "GTC",
            "reduce_only": pending.reduce_only,
            "reason": pending.reason,
            "vegas_side": ("long" if self._is_long is True else "short" if self._is_long is False else "flat"),
            "vegas_entry_price": str(self._entry_price) if self._entry_price is not None else "",
            "vegas_stop_price": str(self._stop_price) if self._stop_price is not None else "",
        }

        if self._tp_prices:
            metadata["vegas_tp1"] = str(self._tp_prices[0])
            metadata["vegas_tp2"] = str(self._tp_prices[1])
            metadata["vegas_tp3"] = str(self._tp_prices[2])

        if self._event_bus:
            self._event_bus.publish(
                SignalEvent(
                    source=self.__class__.__name__,
                    instrument_id=str(self.config.instrument_id),
                    direction=direction,
                    strength=1.0,
                    metadata=metadata,
                )
            )
            return

        self._submit_market_order(direction, bar)

    def _submit_market_order(self, direction: SignalDirection, bar: Bar) -> None:
        pending = self._pending_order
        if pending is None or self.instrument is None:
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

    def _reset_position_state(self) -> None:
        self._is_long = None
        self._entry_price = None
        self._remaining_qty = Decimal("0")
        self._stop_price = None
        self._tp_prices = []
        self._tp_qtys = []
        self._tp_filled = [False, False, False]

    def on_reset(self) -> None:
        """Run on reset."""
        super().on_reset()
        self.fast_ema.reset()
        self.slow_ema.reset()
        self.tunnel_ema_1.reset()
        self.tunnel_ema_2.reset()
        self.rsi.reset()
        if self._atr_indicator is not None:
            self._atr_indicator.reset()

        self._prev_fast_above_slow = None
        self._tunnel_upper = 0.0
        self._tunnel_lower = 0.0
        self._pending_order = None
        self._reset_position_state()
