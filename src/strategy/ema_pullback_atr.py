"""EMA 趋势 + ATR 回撤反弹策略.

参考示例策略思路做项目适配：
- 使用 EMA 快慢线判定趋势方向；
- 在趋势方向上等待一次足够深的 ATR 回撤；
- 回撤后价格重新收回 EMA 快线时发出入场信号。

策略层仅产出信号，不直接管理下单与跟踪止损。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from nautilus_trader.config import PositiveFloat, PositiveInt
from nautilus_trader.indicators import AverageTrueRange, ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import EventBus, SignalDirection
from src.strategy.base import BaseStrategy, BaseStrategyConfig


@dataclass
class _AdxState:
    """Wilder ADX 状态机."""

    period: int
    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None
    _tr_sum: float = 0.0
    _plus_dm_sum: float = 0.0
    _minus_dm_sum: float = 0.0
    _smoothed_tr: float | None = None
    _smoothed_plus_dm: float | None = None
    _smoothed_minus_dm: float | None = None
    _dx_values: deque[float] = field(default_factory=deque)
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

        tr = max(
            high - low,
            abs(high - self.prev_close),
            abs(low - self.prev_close),
        )

        if self._smoothed_tr is None:
            self._tr_sum += tr
            self._plus_dm_sum += plus_dm
            self._minus_dm_sum += minus_dm

            if len(self._dx_values) < self.period - 1:
                self._dx_values.append(0.0)

            if len(self._dx_values) == self.period - 1:
                self._smoothed_tr = self._tr_sum
                self._smoothed_plus_dm = self._plus_dm_sum
                self._smoothed_minus_dm = self._minus_dm_sum
                self._dx_values.clear()
        else:
            n = float(self.period)
            assert self._smoothed_tr is not None
            assert self._smoothed_plus_dm is not None
            assert self._smoothed_minus_dm is not None
            self._smoothed_tr = self._smoothed_tr - (self._smoothed_tr / n) + tr
            self._smoothed_plus_dm = self._smoothed_plus_dm - (self._smoothed_plus_dm / n) + plus_dm
            self._smoothed_minus_dm = self._smoothed_minus_dm - (self._smoothed_minus_dm / n) + minus_dm

            if self._smoothed_tr > 0:
                plus_di = 100.0 * (self._smoothed_plus_dm / self._smoothed_tr)
                minus_di = 100.0 * (self._smoothed_minus_dm / self._smoothed_tr)
                di_sum = plus_di + minus_di
                dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
            else:
                dx = 0.0

            if self.adx is None:
                self._dx_values.append(dx)
                if len(self._dx_values) >= self.period:
                    self.adx = sum(self._dx_values) / float(self.period)
                    self.initialized = True
                    self._dx_values.clear()
            else:
                self.adx = ((self.adx * (self.period - 1)) + dx) / float(self.period)
                self.initialized = True

        self.prev_high = high
        self.prev_low = low
        self.prev_close = close


class EMAPullbackATRConfig(BaseStrategyConfig, frozen=True):
    """EMA 回撤策略配置."""

    instrument_id: InstrumentId
    bar_type: BarType
    fast_ema_period: PositiveInt = 20
    slow_ema_period: PositiveInt = 50
    pullback_atr_multiplier: PositiveFloat = 1.0
    min_trend_gap_ratio: float = 0.0005
    signal_cooldown_bars: int = 3
    adx_period: PositiveInt = 14
    adx_threshold: float = 20.0


class EMAPullbackATRStrategy(BaseStrategy):
    """EMA 趋势 + ATR 回撤反弹入场策略."""

    def __init__(self, config: EMAPullbackATRConfig, event_bus: EventBus | None = None) -> None:
        """Initialize the EMA pullback ATR strategy.

        Args:
            config: Configuration values for the component.
            event_bus: Event bus used for cross-module communication.
        """
        super().__init__(config, event_bus)
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)

        # BaseStrategy 只在 ATR 止盈止损开启时初始化 ATR；该策略信号本身也依赖 ATR。
        if self._atr_indicator is None:
            self._atr_indicator = AverageTrueRange(config.atr_period)

        self._prev_close: float | None = None
        self._bar_index = 0
        self._last_signal_bar_index: int | None = None
        self._long_pullback_armed = False
        self._short_pullback_armed = False
        self._adx = _AdxState(period=int(config.adx_period))

    def _register_indicators(self) -> None:
        """注册 EMA 指标."""
        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)

    def _history_warmup_bars(self) -> int:
        return (
            max(
                int(self.config.fast_ema_period),
                int(self.config.slow_ema_period),
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

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        """生成趋势回撤反弹信号.

        Args:
            bar: Incoming bar data for the strategy callback.
        """
        self._bar_index += 1

        if self._atr_indicator is None or not self._atr_indicator.initialized:
            self._prev_close = float(bar.close)
            return None

        close = float(bar.close)
        if close <= 0:
            self._prev_close = close
            return None

        self._adx.update(float(bar.high), float(bar.low), close)
        adx_threshold = float(self.config.adx_threshold)
        if adx_threshold > 0:
            if not self._adx.initialized or self._adx.adx is None:
                self._prev_close = close
                return None
            if self._adx.adx < adx_threshold:
                self._prev_close = close
                return None

        fast = float(self.fast_ema.value)
        slow = float(self.slow_ema.value)
        atr = float(self._atr_indicator.value)
        trend_gap_ratio = abs(fast - slow) / close

        min_gap = float(self.config.min_trend_gap_ratio)
        if trend_gap_ratio < min_gap:
            self._long_pullback_armed = False
            self._short_pullback_armed = False
            self._prev_close = close
            return None

        pullback_distance = atr * float(self.config.pullback_atr_multiplier)
        prev_close = self._prev_close
        signal: SignalDirection | None = None

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
                signal = SignalDirection.LONG
                self._long_pullback_armed = False

        else:
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
                signal = SignalDirection.SHORT
                self._short_pullback_armed = False

        if signal is not None:
            self._last_signal_bar_index = self._bar_index
            adx_text = f"{self._adx.adx:.2f}" if self._adx.adx is not None else "n/a"
            self.log.info(
                "EMA pullback accepted: "
                f"fast={fast:.2f} slow={slow:.2f} atr={atr:.2f} "
                f"gap={trend_gap_ratio:.6f} adx={adx_text} -> {signal.value}",
            )

        self._prev_close = close
        return signal

    def _cooldown_passed(self) -> bool:
        cooldown_bars = max(0, int(self.config.signal_cooldown_bars))
        if cooldown_bars <= 0:
            return True
        if self._last_signal_bar_index is None:
            return True

        bars_since_last_signal = self._bar_index - self._last_signal_bar_index
        return bars_since_last_signal >= cooldown_bars

    def on_reset(self) -> None:
        """重置指标与内部状态."""
        self.fast_ema.reset()
        self.slow_ema.reset()
        if self._atr_indicator is not None:
            self._atr_indicator.reset()

        self._prev_close = None
        self._bar_index = 0
        self._last_signal_bar_index = None
        self._long_pullback_armed = False
        self._short_pullback_armed = False
        self._adx = _AdxState(period=int(self.config.adx_period))
