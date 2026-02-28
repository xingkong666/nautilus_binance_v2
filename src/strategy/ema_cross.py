"""EMA 快慢线交叉策略.

只产出信号, 不直接下单. 下单由执行引擎负责.
"""

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
    """

    instrument_id: InstrumentId
    bar_type: BarType
    fast_ema_period: PositiveInt = 10
    slow_ema_period: PositiveInt = 20


class EMACrossStrategy(BaseStrategy):
    """EMA 快慢线交叉策略.

    - 快线上穿慢线 → LONG 信号
    - 快线下穿慢线 → SHORT 信号

    信号发布到 EventBus, 由执行引擎处理.
    """

    def __init__(self, config: EMACrossConfig, event_bus: EventBus | None = None) -> None:
        super().__init__(config, event_bus)
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)
        self._prev_fast_above: bool | None = None

    def _register_indicators(self) -> None:
        """注册 EMA 指标."""
        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        """生成 EMA 交叉信号.

        只在交叉发生时产出信号, 避免重复信号.
        """
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

        if signal:
            self.log.info(
                f"EMA Cross: fast={self.fast_ema.value:.2f} slow={self.slow_ema.value:.2f} → {signal.value}",
            )

        return signal

    def on_reset(self) -> None:
        """重置指标."""
        self.fast_ema.reset()
        self.slow_ema.reset()
        self._prev_fast_above = None
