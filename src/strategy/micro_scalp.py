"""Micro scalp strategy.

1m 高频策略：
- ADX 高于阈值时执行趋势回撤入场；
- ADX 低于阈值时执行 RSI 极值回归入场；
- 信号仅发布到 EventBus，由执行层以 LIMIT 订单执行。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from nautilus_trader.config import PositiveFloat, PositiveInt
from nautilus_trader.indicators import AverageTrueRange, ExponentialMovingAverage
from nautilus_trader.indicators.momentum import RelativeStrengthIndex
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import EventBus, SignalDirection, SignalEvent
from src.strategy.base import BaseStrategy, BaseStrategyConfig


@dataclass
class _AdxState:
    """Wilder ADX state."""

    period: int
    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None
    tr_sum: float = 0.0
    plus_dm_sum: float = 0.0
    minus_dm_sum: float = 0.0
    smoothed_tr: float | None = None
    smoothed_plus_dm: float | None = None
    smoothed_minus_dm: float | None = None
    dx_values: deque[float] = field(default_factory=deque)
    adx: float | None = None
    initialized: bool = False

    def update(self, high: float, low: float, close: float) -> None:
        if self.prev_high is None or self.prev_low is None or self.prev_close is None:
            self.prev_high = high
            self.prev_low = low
            self.prev_close = close
            return

        up_move = high - self.prev_high
        down_move = self.prev_low - low
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0
        tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))

        if self.smoothed_tr is None:
            self.tr_sum += tr
            self.plus_dm_sum += plus_dm
            self.minus_dm_sum += minus_dm
            if len(self.dx_values) < self.period - 1:
                self.dx_values.append(0.0)
            if len(self.dx_values) == self.period - 1:
                self.smoothed_tr = self.tr_sum
                self.smoothed_plus_dm = self.plus_dm_sum
                self.smoothed_minus_dm = self.minus_dm_sum
                self.dx_values.clear()
        else:
            n = float(self.period)
            assert self.smoothed_tr is not None
            assert self.smoothed_plus_dm is not None
            assert self.smoothed_minus_dm is not None
            self.smoothed_tr = self.smoothed_tr - (self.smoothed_tr / n) + tr
            self.smoothed_plus_dm = self.smoothed_plus_dm - (self.smoothed_plus_dm / n) + plus_dm
            self.smoothed_minus_dm = self.smoothed_minus_dm - (self.smoothed_minus_dm / n) + minus_dm

            if self.smoothed_tr > 0:
                plus_di = 100.0 * (self.smoothed_plus_dm / self.smoothed_tr)
                minus_di = 100.0 * (self.smoothed_minus_dm / self.smoothed_tr)
                di_sum = plus_di + minus_di
                dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
            else:
                dx = 0.0

            if self.adx is None:
                self.dx_values.append(dx)
                if len(self.dx_values) >= self.period:
                    self.adx = sum(self.dx_values) / float(self.period)
                    self.initialized = True
                    self.dx_values.clear()
            else:
                self.adx = ((self.adx * (self.period - 1)) + dx) / float(self.period)
                self.initialized = True

        self.prev_high = high
        self.prev_low = low
        self.prev_close = close


class MicroScalpConfig(BaseStrategyConfig, frozen=True):
    """Micro scalp 配置."""

    instrument_id: InstrumentId
    bar_type: BarType

    fast_ema_period: PositiveInt = 8
    slow_ema_period: PositiveInt = 21
    rsi_period: PositiveInt = 7
    adx_period: PositiveInt = 14
    trend_adx_threshold: PositiveFloat = 18.0
    entry_pullback_atr: PositiveFloat = 0.35
    oversold_level: PositiveFloat = 24.0
    overbought_level: PositiveFloat = 76.0
    signal_cooldown_bars: int = 2

    atr_sl_multiplier: float | None = 0.45
    atr_tp_multiplier: float | None = 0.8

    maker_offset_ticks: PositiveInt = 1
    limit_ttl_ms: PositiveInt = 2500
    chase_ticks: PositiveInt = 2
    post_only: bool = True


class MicroScalpStrategy(BaseStrategy):
    """趋势回撤 + 震荡反转的 1m 高频策略."""

    def __init__(self, config: MicroScalpConfig, event_bus: EventBus | None = None) -> None:
        """Initialize the micro scalp strategy.

        Args:
            config: Configuration values for the component.
            event_bus: Event bus used for cross-module communication.
        """
        super().__init__(config, event_bus)
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)
        self.rsi = RelativeStrengthIndex(config.rsi_period)

        if self._atr_indicator is None:
            self._atr_indicator = AverageTrueRange(config.atr_period)

        self._adx = _AdxState(period=int(config.adx_period))
        self._prev_close: float | None = None
        self._prev_rsi: float | None = None
        self._bar_index = 0
        self._last_signal_bar_index: int | None = None
        self._long_pullback_armed = False
        self._short_pullback_armed = False
        self._last_mode = "range"
        self._last_order_side = "BUY"

    def _register_indicators(self) -> None:
        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.rsi)

    def _history_warmup_bars(self) -> int:
        return (
            max(
                int(self.config.fast_ema_period),
                int(self.config.slow_ema_period),
                int(self.config.rsi_period),
                int(self.config.atr_period),
                int(self.config.adx_period) * 2,
            )
            + 2
        )

    def _on_historical_bar(self, bar: Bar) -> None:
        self._bar_index += 1
        close = float(bar.close)
        self._adx.update(float(bar.high), float(bar.low), close)
        self._prev_close = close
        self._prev_rsi = float(self.rsi.value)

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        """Generate signal.

        Args:
            bar: Bar data for the current evaluation.

        Returns:
            SignalDirection: Result of generate signal.
        """
        self._bar_index += 1
        close = float(bar.close)
        if close <= 0 or self._atr_indicator is None:
            return None
        if not self._atr_indicator.initialized:
            self._prev_close = close
            self._prev_rsi = float(self.rsi.value)
            return None

        self._adx.update(float(bar.high), float(bar.low), close)
        adx_value = self._adx.adx
        trend_mode = bool(
            self._adx.initialized and adx_value is not None and adx_value >= self.config.trend_adx_threshold
        )
        self._last_mode = "trend" if trend_mode else "range"

        signal = self._trend_signal(bar) if trend_mode else self._range_signal()
        self._prev_close = close
        self._prev_rsi = float(self.rsi.value)
        if signal is not None:
            self._last_signal_bar_index = self._bar_index
        return signal

    def _trend_signal(self, bar: Bar) -> SignalDirection | None:
        if self._atr_indicator is None:
            return None

        fast = float(self.fast_ema.value)
        slow = float(self.slow_ema.value)
        close = float(bar.close)
        prev_close = self._prev_close
        atr = float(self._atr_indicator.value)
        pullback_distance = atr * float(self.config.entry_pullback_atr)

        if fast >= slow:
            self._short_pullback_armed = False
            if float(bar.low) <= fast - pullback_distance:
                self._long_pullback_armed = True
            if (
                self._long_pullback_armed
                and prev_close is not None
                and prev_close < fast
                and close >= fast
                and self._cooldown_passed()
            ):
                self._long_pullback_armed = False
                self._last_order_side = "BUY"
                return SignalDirection.LONG
            return None

        self._long_pullback_armed = False
        if float(bar.high) >= fast + pullback_distance:
            self._short_pullback_armed = True
        if (
            self._short_pullback_armed
            and prev_close is not None
            and prev_close > fast
            and close <= fast
            and self._cooldown_passed()
        ):
            self._short_pullback_armed = False
            self._last_order_side = "SELL"
            return SignalDirection.SHORT
        return None

    def _range_signal(self) -> SignalDirection | None:
        current_rsi = float(self.rsi.value)
        prev_rsi = self._prev_rsi
        if prev_rsi is None or not self._cooldown_passed():
            return None

        if prev_rsi <= self.config.oversold_level < current_rsi:
            self._last_order_side = "BUY"
            return SignalDirection.LONG
        if prev_rsi >= self.config.overbought_level > current_rsi:
            self._last_order_side = "SELL"
            return SignalDirection.SHORT
        return None

    def _cooldown_passed(self) -> bool:
        cooldown_bars = max(0, int(self.config.signal_cooldown_bars))
        if cooldown_bars <= 0 or self._last_signal_bar_index is None:
            return True
        return (self._bar_index - self._last_signal_bar_index) >= cooldown_bars

    def _calc_limit_price(self, close: Decimal, side: str) -> Decimal:
        tick_size = Decimal("0.1")
        if self.instrument is not None and hasattr(self.instrument, "price_increment"):
            tick_size = Decimal(str(self.instrument.price_increment))

        offset = tick_size * Decimal(str(int(self.config.maker_offset_ticks)))
        price = close - offset if side == "BUY" else close + offset
        return max(price, Decimal("0.00000001"))

    def _publish_signal(self, direction: SignalDirection, bar: Bar) -> None:
        side = "BUY" if direction == SignalDirection.LONG else "SELL"
        self._last_order_side = side

        qty_decimal = self._resolve_order_quantity_decimal(bar, fallback_trade_size=True)
        if qty_decimal is None or qty_decimal <= 0:
            return

        close = Decimal(str(bar.close))
        limit_price = self._calc_limit_price(close=close, side=side)
        metadata: dict[str, Any] = {
            "bar_close": str(bar.close),
            "bar_type": str(self.config.bar_type),
            "mode": self._last_mode,
            "order_side": side,
            "order_qty": format(qty_decimal.normalize(), "f"),
            "order_type": "LIMIT",
            "order_price": str(limit_price),
            "time_in_force": "GTC",
            "post_only": bool(self.config.post_only),
            "limit_ttl_ms": int(self.config.limit_ttl_ms),
            "chase_ticks": int(self.config.chase_ticks),
            "reduce_only": False,
        }

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

    def on_reset(self) -> None:
        """Run on reset."""
        self.fast_ema.reset()
        self.slow_ema.reset()
        self.rsi.reset()
        if self._atr_indicator is not None:
            self._atr_indicator.reset()

        self._adx = _AdxState(period=int(self.config.adx_period))
        self._prev_close = None
        self._prev_rsi = None
        self._bar_index = 0
        self._last_signal_bar_index = None
        self._long_pullback_armed = False
        self._short_pullback_armed = False
        self._last_mode = "range"
        self._last_order_side = "BUY"
