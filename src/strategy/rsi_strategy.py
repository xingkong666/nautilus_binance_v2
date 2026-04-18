"""RSI 超买超卖策略.

只产出信号, 不直接下单. 下单由执行引擎负责.

策略逻辑:
- RSI 从超卖区 (< oversold_level) 回升至上方 → LONG 信号
- RSI 从超买区 (> overbought_level) 回落至下方 → SHORT 信号
- 其他状态不产生信号 (持仓不变)
"""

from __future__ import annotations

from nautilus_trader.config import PositiveFloat, PositiveInt
from nautilus_trader.indicators.momentum import RelativeStrengthIndex
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import EventBus, SignalDirection
from src.strategy.base import BaseStrategy, BaseStrategyConfig


class RSIStrategyConfig(BaseStrategyConfig, frozen=True):
    """RSI 策略配置.

    Attributes:
        instrument_id: 交易对（继承自 BaseStrategyConfig）。
        bar_type: K 线周期（继承自 BaseStrategyConfig）。
        trade_size: 每次交易量（币数，继承自 BaseStrategyConfig，默认 0.01）。
        rsi_period: RSI 计算周期，默认 14。
        oversold_level: 超卖阈值，RSI 低于此值视为超卖，默认 30.0。
        overbought_level: 超买阈值，RSI 高于此值视为超买，默认 70.0。

    """

    instrument_id: InstrumentId
    bar_type: BarType
    rsi_period: PositiveInt = 14
    oversold_level: PositiveFloat = 30.0
    overbought_level: PositiveFloat = 70.0


class RSIStrategy(BaseStrategy):
    """RSI 超买超卖反转策略.

    通过检测 RSI 穿越超买/超卖边界来产出信号:
    - RSI 上穿 oversold_level → LONG（超卖区回归，做多）
    - RSI 下穿 overbought_level → SHORT（超买区回落，做空）

    信号发布到 EventBus, 由执行引擎处理.
    """

    def __init__(self, config: RSIStrategyConfig, event_bus: EventBus | None = None) -> None:
        """初始化 RSI 策略.

        Args:
            config: RSI 策略配置。
            event_bus: 事件总线，实盘模式下必传，回测模式下可为 None。

        """
        super().__init__(config, event_bus)
        self.rsi = RelativeStrengthIndex(config.rsi_period)
        self._prev_rsi: float | None = None

    def _register_indicators(self) -> None:
        """注册 RSI 指标."""
        self.register_indicator_for_bars(self.config.bar_type, self.rsi)

    def _history_warmup_bars(self) -> int:
        return int(self.config.rsi_period) + 2

    def _on_historical_bar(self, bar: Bar) -> None:
        self._prev_rsi = float(self.rsi.value)

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        """生成 RSI 超买超卖信号.

        只在 RSI 穿越超买/超卖边界时产出信号，避免区间内重复触发。

        Args:
            bar: 当前 Bar。

        Returns:
            信号方向，若不满足条件返回 None。

        """
        current_rsi = self.rsi.value

        # 第一根 K 线，记录状态但不产出信号
        if self._prev_rsi is None:
            self._prev_rsi = current_rsi
            return None

        signal: SignalDirection | None = None

        oversold = self.config.oversold_level
        overbought = self.config.overbought_level

        # 超卖回升：相对强弱指数从下方穿越 oversold_level → 做多
        if self._prev_rsi <= oversold < current_rsi:
            signal = SignalDirection.LONG
            self.log.info(
                f"RSI oversold cross: prev={self._prev_rsi:.2f} → curr={current_rsi:.2f} (threshold={oversold}) → LONG",
            )

        # 超买回落：相对强弱指数从上方穿越 overbought_level → 做空
        elif self._prev_rsi >= overbought > current_rsi:
            signal = SignalDirection.SHORT
            self.log.info(
                "RSI overbought cross: "
                f"prev={self._prev_rsi:.2f} → curr={current_rsi:.2f} "
                f"(threshold={overbought}) → SHORT",
            )

        self._prev_rsi = current_rsi
        return signal

    def on_reset(self) -> None:
        """重置指标."""
        super().on_reset()
        self.rsi.reset()
        self._prev_rsi = None
