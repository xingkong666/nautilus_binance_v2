"""自定义 NautilusTrader 指标扩展.

提供 NautilusTrader 内置库未直接提供的技术指标，
作为正式的 ``Indicator`` 子类，与 NT 指标系统完全兼容。

Currently provided:
- ``WilderAdx``: Wilder 平滑 ADX 指标（NT ``DirectionalMovement``
  仅提供 +DI/-DI，不计算完整 Wilder-smoothed ADX）。
"""

from __future__ import annotations

from collections import deque

from nautilus_trader.indicators.base import Indicator
from nautilus_trader.model.data import Bar


class WilderAdx(Indicator):
    """Wilder 平滑 ADX 指标.

    NautilusTrader 内置的 ``DirectionalMovement`` 指标仅提供 +DI 和 -DI，
    不输出完整的 Wilder-smoothed ADX 值。本类补充该计算，
    算法与 J. Welles Wilder 原版一致：

    1. 初始化阶段：累积 ``period-1`` 根 bar 的 TR / +DM / -DM 总和，
       用于建立 smoothed 初始值。
    2. Wilder 平滑：``smoothed = smoothed - smoothed/n + current``
    3. ADX 初始化：再收集 ``period`` 个 DX 值取均值得到初始 ADX。
    4. ADX 更新：``adx = (adx * (period-1) + dx) / period``

    Attributes:
        period: ADX 计算周期（Wilder 原版为 14）。
        value: 当前 ADX 值（未初始化时为 ``None``）。
        plus_di: 当前 +DI 值（未初始化时为 ``None``）。
        minus_di: 当前 -DI 值（未初始化时为 ``None``）。

    """

    def __init__(self, period: int) -> None:
        """初始化 WilderAdx 指标.

        Args:
            period: ADX 计算周期，通常为 14。

        """
        super().__init__(params=[period])
        self.period: int = period

        # 公开输出
        self.value: float | None = None
        self.plus_di: float | None = None
        self.minus_di: float | None = None

        # 内部状态
        self._prev_high: float | None = None
        self._prev_low: float | None = None
        self._prev_close: float | None = None

        # 初始化阶段累加器
        self._tr_sum: float = 0.0
        self._plus_dm_sum: float = 0.0
        self._minus_dm_sum: float = 0.0

        # Wilder 平滑后的值
        self._smoothed_tr: float | None = None
        self._smoothed_plus_dm: float | None = None
        self._smoothed_minus_dm: float | None = None

        # ADX 初始化阶段的 DX 值缓冲
        self._dx_buffer: deque[float] = deque()

    # ------------------------------------------------------------------
    # NT Indicator 接口
    # ------------------------------------------------------------------

    def handle_bar(self, bar: Bar) -> None:
        """更新指标（NT 标准接口）.

        Args:
            bar: 新 Bar 数据。

        """
        self._set_has_inputs(True)
        self._update(float(bar.high), float(bar.low), float(bar.close))

    def handle_quote_tick(self, tick: object) -> None:  # type: ignore[override]
        """不支持 quote tick 更新（占位）."""

    def handle_trade_tick(self, tick: object) -> None:  # type: ignore[override]
        """不支持 trade tick 更新（占位）."""

    def _reset(self) -> None:
        """重置所有内部状态（NT Cython base 要求实现此方法）."""
        self.value = None
        self.plus_di = None
        self.minus_di = None

        self._prev_high = None
        self._prev_low = None
        self._prev_close = None

        self._tr_sum = 0.0
        self._plus_dm_sum = 0.0
        self._minus_dm_sum = 0.0

        self._smoothed_tr = None
        self._smoothed_plus_dm = None
        self._smoothed_minus_dm = None

        self._dx_buffer = deque()

    def reset(self) -> None:
        """重置指标到初始状态（公开接口）."""
        self._reset()  # clears our state
        super().reset()  # NT base clears initialized / has_inputs flags

    # ------------------------------------------------------------------
    # 兼容旧 _AdxState.update() 调用方式
    # ------------------------------------------------------------------

    def update(self, high: float, low: float, close: float) -> None:
        """手动更新（兼容旧 _AdxState.update() 调用签名）.

        Args:
            high: 当前 bar 最高价。
            low: 当前 bar 最低价。
            close: 当前 bar 收盘价。

        """
        self._set_has_inputs(True)
        self._update(high, low, close)

    # ------------------------------------------------------------------
    # 核心计算
    # ------------------------------------------------------------------

    def _update(self, high: float, low: float, close: float) -> None:
        """执行 Wilder ADX 更新逻辑."""
        # 第一根 bar：仅记录前值，不计算
        if self._prev_high is None or self._prev_low is None or self._prev_close is None:
            self._prev_high = high
            self._prev_low = low
            self._prev_close = close
            return

        # ---- 计算原始指标 ----
        up_move = high - self._prev_high
        down_move = self._prev_low - low

        plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0

        tr = max(
            high - low,
            abs(high - self._prev_close),
            abs(low - self._prev_close),
        )

        # ---- 阶段 1：初始化 smoothed 值 ----
        if self._smoothed_tr is None:
            self._tr_sum += tr
            self._plus_dm_sum += plus_dm
            self._minus_dm_sum += minus_dm

            if len(self._dx_buffer) < self.period - 1:
                self._dx_buffer.append(0.0)

            if len(self._dx_buffer) == self.period - 1:
                # 积累了 period-1 根 bar，建立初始 smoothed 值
                self._smoothed_tr = self._tr_sum
                self._smoothed_plus_dm = self._plus_dm_sum
                self._smoothed_minus_dm = self._minus_dm_sum
                self._dx_buffer.clear()

        # ---- 阶段 2：Wilder 平滑更新 ----
        else:
            n = float(self.period)
            self._smoothed_tr = self._smoothed_tr - (self._smoothed_tr / n) + tr
            self._smoothed_plus_dm = self._smoothed_plus_dm - (self._smoothed_plus_dm / n) + plus_dm
            self._smoothed_minus_dm = self._smoothed_minus_dm - (self._smoothed_minus_dm / n) + minus_dm

            if self._smoothed_tr > 0:
                plus_di = 100.0 * (self._smoothed_plus_dm / self._smoothed_tr)
                minus_di = 100.0 * (self._smoothed_minus_dm / self._smoothed_tr)
                di_sum = plus_di + minus_di
                dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
            else:
                plus_di = 0.0
                minus_di = 0.0
                dx = 0.0

            self.plus_di = plus_di
            self.minus_di = minus_di

            # ---- 阶段 3a：ADX 初始化（收集 period 个 DX 取均值）----
            if self.value is None:
                self._dx_buffer.append(dx)
                if len(self._dx_buffer) >= self.period:
                    self.value = sum(self._dx_buffer) / float(self.period)
                    self._dx_buffer.clear()
                    self._set_initialized(True)
            # ---- 阶段 3b：ADX 实时更新 ----
            else:
                self.value = ((self.value * (self.period - 1)) + dx) / float(self.period)
                if not self.initialized:
                    self._set_initialized(True)

        self._prev_high = high
        self._prev_low = low
        self._prev_close = close
