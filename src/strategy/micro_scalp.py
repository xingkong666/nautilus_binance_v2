"""Micro scalp strategy.

1m 高频策略：
- ADX 高于阈值时执行趋势回撤入场；
- ADX 低于阈值时执行 RSI 极值回归入场；
- 信号仅发布到 EventBus，由执行层以 LIMIT 订单执行。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from nautilus_trader.config import PositiveFloat, PositiveInt
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.indicators.momentum import RelativeStrengthIndex
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import EventBus, SignalDirection, SignalEvent
from src.core.indicators import WilderAdx
from src.strategy.base import BaseStrategy, BaseStrategyConfig


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

        self._ensure_atr_indicator()

        self._adx = WilderAdx(period=int(config.adx_period))
        self._prev_close: float | None = None
        self._prev_rsi: float | None = None
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
        close = float(bar.close)
        if close <= 0 or self._atr_indicator is None:
            return None
        if not self._atr_indicator.initialized:
            self._prev_close = close
            self._prev_rsi = float(self.rsi.value)
            return None

        self._adx.update(float(bar.high), float(bar.low), close)
        adx_value = self._adx.value
        trend_mode = bool(self._adx.initialized and adx_value is not None and adx_value >= self.config.trend_adx_threshold)
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
            if self._long_pullback_armed and prev_close is not None and prev_close < fast and close >= fast and self._cooldown_passed():
                self._long_pullback_armed = False
                self._last_order_side = "BUY"
                return SignalDirection.LONG
            return None

        self._long_pullback_armed = False
        if float(bar.high) >= fast + pullback_distance:
            self._short_pullback_armed = True
        if self._short_pullback_armed and prev_close is not None and prev_close > fast and close <= fast and self._cooldown_passed():
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

    def _calc_limit_price(self, close: Decimal, side: str) -> Decimal:
        tick_size = Decimal("0.1")
        if self.instrument is not None and hasattr(self.instrument, "price_increment"):
            tick_size = Decimal(str(self.instrument.price_increment))

        offset = tick_size * Decimal(str(int(self.config.maker_offset_ticks)))
        raw_price = close - offset if side == "BUY" else close + offset
        raw_price = max(raw_price, Decimal("0.00000001"))

        # 可用时使用 make_price() 对齐到交易品种最小变动价位
        if self.instrument is not None:
            try:
                return Decimal(str(self.instrument.make_price(float(raw_price))))
            except (ValueError, OverflowError):
                pass
        return raw_price

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
        super().on_reset()
        self.fast_ema.reset()
        self.slow_ema.reset()
        self.rsi.reset()
        if self._atr_indicator is not None:
            self._atr_indicator.reset()

        self._adx = WilderAdx(period=int(self.config.adx_period))
        self._prev_close = None
        self._prev_rsi = None
        self._long_pullback_armed = False
        self._short_pullback_armed = False
        self._last_mode = "range"
        self._last_order_side = "BUY"
