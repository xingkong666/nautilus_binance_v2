"""EMA 快慢线交叉策略.

只产出信号, 不直接下单. 下单由执行引擎负责.
"""
# ruff: noqa: TC002

from __future__ import annotations

from nautilus_trader.config import PositiveInt
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import EventBus, SignalDirection
from src.strategy.base import BaseStrategy, BaseStrategyConfig


class EMACrossConfig(BaseStrategyConfig, frozen=True):
    """EMA 交叉策略配置.

    Attributes:
        instrument_id: 交易对（继承自 BaseStrategyConfig）。
        bar_type: K 线周期（继承自 BaseStrategyConfig）。
        trade_size: 每次交易量（币数，继承自 BaseStrategyConfig，默认 0.01）。
        fast_ema_period: 快线 EMA 周期，默认 10。
        slow_ema_period: 慢线 EMA 周期，默认 20。
        entry_min_atr_ratio: 入场最小波动阈值（ATR / close），默认 0.0015。
        signal_cooldown_bars: 信号冷却条数，默认 3。

    """

    instrument_id: InstrumentId
    bar_type: BarType
    fast_ema_period: PositiveInt = 10
    slow_ema_period: PositiveInt = 20
    entry_min_atr_ratio: float = 0.0015
    signal_cooldown_bars: int = 3


class EMACrossStrategy(BaseStrategy):
    """EMA 快慢线交叉策略.

    - 快线上穿慢线 → LONG 信号
    - 快线下穿慢线 → SHORT 信号

    信号发布到 EventBus, 由执行引擎处理.
    """

    def __init__(self, config: EMACrossConfig, event_bus: EventBus | None = None) -> None:
        """Initialize the EMA cross strategy.

        Args:
            config: Configuration values for the component.
            event_bus: Event bus used for cross-module communication.
        """
        super().__init__(config, event_bus)
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)
        self._prev_fast_above: bool | None = None

        # BaseStrategy 仅在启用 ATR 止盈止损时构建 ATR 指标；
        # EMA 的波动过滤也需要 ATR，因此在此补充初始化。
        if config.entry_min_atr_ratio > 0:
            self._ensure_atr_indicator()

    def _register_indicators(self) -> None:
        """注册 EMA 指标."""
        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)

    def _history_warmup_bars(self) -> int:
        periods = [int(self.config.fast_ema_period), int(self.config.slow_ema_period)]
        if float(self.config.entry_min_atr_ratio) > 0:
            periods.append(int(self.config.atr_period))
        return max(periods) + 2

    def _on_historical_bar(self, bar: Bar) -> None:
        self._bar_index += 1
        self._prev_fast_above = self.fast_ema.value >= self.slow_ema.value

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        """生成 EMA 交叉信号.

        只在交叉发生时产出信号, 避免重复信号.

        Args:
            bar: Incoming bar data for the strategy callback.
        """
        self._bar_index += 1

        fast_above = self.fast_ema.value >= self.slow_ema.value

        # 第一根 bar, 记录状态但不产出信号
        if self._prev_fast_above is None:
            self._prev_fast_above = fast_above
            return None

        signal: SignalDirection | None = None

        # 金叉: 快线从下方穿越到上方
        if fast_above and not self._prev_fast_above:
            signal = SignalDirection.LONG

        # 死叉: 快线从上方穿越到下方
        elif not fast_above and self._prev_fast_above:
            signal = SignalDirection.SHORT

        self._prev_fast_above = fast_above

        if signal is None:
            return None

        atr_ratio: float | None = None
        min_atr_ratio = float(self.config.entry_min_atr_ratio)
        if min_atr_ratio > 0:
            if self._atr_indicator is None or not self._atr_indicator.initialized:
                self.log.info("EMA cross filtered: ATR not initialized")
                return None

            close = float(bar.close)
            if close <= 0:
                self.log.warning(f"EMA cross filtered: invalid close={close}")
                return None

            atr_ratio = float(self._atr_indicator.value) / close
            if atr_ratio < min_atr_ratio:
                self.log.info(
                    f"EMA cross filtered: atr_ratio={atr_ratio:.6f} < min_atr_ratio={min_atr_ratio:.6f}",
                )
                return None

        if not self._cooldown_passed():
            self.log.info("EMA cross filtered: cooldown active")
            return None

        self._last_signal_bar_index = self._bar_index
        atr_ratio_text = f"{atr_ratio:.6f}" if atr_ratio is not None else "n/a"
        cooldown_bars = max(0, int(self.config.signal_cooldown_bars))
        self.log.info(
            "EMA Cross accepted: "
            f"fast={self.fast_ema.value:.2f} slow={self.slow_ema.value:.2f} "
            f"atr_ratio={atr_ratio_text} cooldown={cooldown_bars} → {signal.value}",
        )

        return signal

    def on_reset(self) -> None:
        """重置指标."""
        super().on_reset()
        self.fast_ema.reset()
        self.slow_ema.reset()
        self._prev_fast_above = None
