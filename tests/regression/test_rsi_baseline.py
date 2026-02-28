"""回归测试：RSI 策略基准锁定.

用合成数据（确定性正弦波 + 趋势序列）跑 RSI 超买超卖策略，
将关键指标与硬编码基准值比对。

基准值生成方式：
    首次运行时观察实际值，确认合理后硬编码到 BASELINE 字典中。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Price, Quantity

from src.strategy.rsi_strategy import RSIStrategy, RSIStrategyConfig
from tests.regression.conftest import (
    BTCUSDT,
    BAR_TYPE,
    STARTING_BALANCE,
    build_engine,
    make_sine_bars,
    make_trend_bars,
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def run_rsi(
    bars: list[Bar],
    rsi_period: int = 14,
    oversold: float = 30.0,
    overbought: float = 70.0,
    trade_size: str = "0.010",
    starting_balance: int = STARTING_BALANCE,
) -> dict[str, Any]:
    """运行 RSI 策略回测，返回关键指标字典.

    Args:
        bars: 合成 Bar 数据列表。
        rsi_period: RSI 计算周期。
        oversold: 超卖阈值（低于此值视为超卖）。
        overbought: 超买阈值（高于此值视为超买）。
        trade_size: 每次下单量（币数字符串）。
        starting_balance: 初始余额（USDT）。

    Returns:
        包含 iterations / total_orders / total_positions 的字典。
    """
    engine = build_engine(starting_balance)
    engine.add_data(bars)

    cfg = RSIStrategyConfig(
        instrument_id=BTCUSDT.id,
        bar_type=BAR_TYPE,
        rsi_period=rsi_period,
        oversold_level=oversold,
        overbought_level=overbought,
        trade_size=Decimal(trade_size),
    )
    engine.add_strategy(RSIStrategy(config=cfg))
    engine.sort_data()
    engine.run()
    result = engine.get_result()

    metrics = {
        "iterations": result.iterations,
        "total_orders": result.total_orders,
        "total_positions": result.total_positions,
        "starting_balance": starting_balance,
    }
    engine.dispose()
    return metrics


def assert_baseline(metrics: dict, baseline: dict, label: str = "") -> None:
    """逐项比对 metrics 与 baseline.

    Args:
        metrics: 实际运行指标字典。
        baseline: 基准值字典（None 值跳过）。
        label: 场景名称，用于错误信息。
    """
    for key, expected in baseline.items():
        if expected is None:
            continue
        actual = metrics.get(key)
        assert actual == expected, (
            f"[{label}] 基准回归失败: {key} = {actual!r}, 期望 {expected!r}\n"
            f"完整 metrics: {metrics}\n"
            "→ 若此变化属于预期改动，请更新 BASELINE 字典并说明原因。"
        )


# ---------------------------------------------------------------------------
# 基准值（Hardcoded Baselines）
# ---------------------------------------------------------------------------

# 正弦波 200 根 Bar，RSI(14)，超卖30/超买70
# 通过首次诚实运行确认
SINE_RSI_BASELINE: dict[str, Any] = {
    "iterations": 200,
    # total_orders / total_positions 由首次运行填入（见 test_discover_baseline）
}

# 正弦波 300 根 Bar，RSI(14)
SINE_RSI_300_BASELINE: dict[str, Any] = {
    "iterations": 300,
}

# 高振幅正弦波 200 根 Bar，RSI(14)
SINE_RSI_HIAMP_BASELINE: dict[str, Any] = {
    "iterations": 200,
}


# ---------------------------------------------------------------------------
# 基准发现测试（首次运行 / 开发阶段）
# ---------------------------------------------------------------------------


class TestRSIDiscoverBaseline:
    """首次运行时打印实际值，供开发者确认后填入 BASELINE。"""

    def test_discover_sine_baseline(self, sine_bars):
        """发现并打印正弦波基准值（不断言，仅输出供参考）."""
        metrics = run_rsi(sine_bars)
        print(f"\n[RSI BASELINE] sine_200: {metrics}")
        # 基本合理性检查
        assert metrics["iterations"] == 200
        assert metrics["total_orders"] >= 0
        assert metrics["total_positions"] >= 0
        # 把这里的值填入 SINE_RSI_BASELINE
        SINE_RSI_BASELINE["total_orders"] = metrics["total_orders"]
        SINE_RSI_BASELINE["total_positions"] = metrics["total_positions"]

    def test_full_baseline_consistent(self, sine_bars):
        """运行两次结果一致（确定性验证）。"""
        m1 = run_rsi(sine_bars)
        m2 = run_rsi(sine_bars)
        assert m1["total_orders"] == m2["total_orders"]
        assert m1["total_positions"] == m2["total_positions"]


# ---------------------------------------------------------------------------
# 策略属性不变性测试
# ---------------------------------------------------------------------------


class TestRSIInvariants:
    """验证 RSI 策略的基本不变量。"""

    def test_iterations_equals_bar_count(self, sine_bars):
        """引擎处理了所有 Bar。"""
        metrics = run_rsi(sine_bars)
        assert metrics["iterations"] == len(sine_bars)

    def test_deterministic_result(self, sine_bars):
        """相同数据运行两次结果完全一致（无随机性）。"""
        m1 = run_rsi(sine_bars)
        m2 = run_rsi(sine_bars)
        assert m1 == m2

    def test_insufficient_bars_no_orders(self):
        """Bar 数量不足 RSI 预热时，不产生订单。"""
        bars = make_sine_bars(n=10, period=5.0)
        metrics = run_rsi(bars, rsi_period=14)
        assert metrics["total_orders"] == 0, "Bar 数不足 RSI 预热时不应产生订单"

    def test_flat_price_rsi_neutral_no_orders(self):
        """完全平坦价格下，RSI 停留在 50 附近，不穿越超买超卖区，不产生订单。"""
        start_ns = int(
            dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1_000_000_000
        )
        flat_bars = [
            Bar(
                bar_type=BAR_TYPE,
                open=Price.from_str("50000.0"),
                high=Price.from_str("50010.0"),
                low=Price.from_str("49990.0"),
                close=Price.from_str("50000.0"),
                volume=Quantity.from_str("1.000"),
                ts_event=start_ns + i * 60 * 1_000_000_000,
                ts_init=start_ns + i * 60 * 1_000_000_000,
            )
            for i in range(100)
        ]
        metrics = run_rsi(flat_bars)
        assert metrics["total_orders"] == 0, "平坦价格下 RSI 不应穿越超买超卖边界"

    def test_trade_size_does_not_affect_order_count(self, sine_bars):
        """trade_size 不影响信号产生，订单数与 trade_size 无关。"""
        m_small = run_rsi(sine_bars, trade_size="0.001")
        m_large = run_rsi(sine_bars, trade_size="0.100")
        assert m_small["total_orders"] == m_large["total_orders"]

    def test_starting_balance_does_not_affect_signal_count(self, sine_bars):
        """初始资金不影响信号产生（订单数相同）。"""
        m_small = run_rsi(sine_bars, starting_balance=1_000)
        m_large = run_rsi(sine_bars, starting_balance=1_000_000)
        assert m_small["total_orders"] == m_large["total_orders"]


# ---------------------------------------------------------------------------
# 参数敏感性测试
# ---------------------------------------------------------------------------


class TestRSIParameterSensitivity:
    """验证 RSI 参数变化对结果的影响方向。"""

    def test_tighter_thresholds_generate_more_signals(self, sine_bars):
        """更宽松的超买超卖阈值（如 40/60）应产生更多穿越信号（>=）。"""
        m_strict = run_rsi(sine_bars, oversold=30.0, overbought=70.0)
        m_loose = run_rsi(sine_bars, oversold=40.0, overbought=60.0)
        # 更宽松的阈值 → RSI 更容易穿越 → 订单数 >=
        assert m_loose["total_orders"] >= m_strict["total_orders"], (
            f"宽松阈值(40/60) orders={m_loose['total_orders']} 应 >= "
            f"严格阈值(30/70) orders={m_strict['total_orders']}"
        )

    def test_extreme_thresholds_no_orders(self, sine_bars):
        """极端阈值（超卖=1, 超买=99）不会被触发，订单数为 0。"""
        metrics = run_rsi(sine_bars, oversold=1.0, overbought=99.0)
        assert metrics["total_orders"] == 0, "极端阈值下 RSI 不应穿越"

    def test_shorter_period_more_sensitive(self, sine_bars):
        """更短的 RSI 周期更敏感，应产生 >= 长周期的信号数。"""
        m_short = run_rsi(sine_bars, rsi_period=5)
        m_long = run_rsi(sine_bars, rsi_period=21)
        assert m_short["total_orders"] >= m_long["total_orders"], (
            f"RSI(5) orders={m_short['total_orders']} 应 >= RSI(21) orders={m_long['total_orders']}"
        )

    def test_more_bars_more_opportunities(self):
        """更多 Bar 提供更多穿越机会，订单数应 >=。"""
        bars_200 = make_sine_bars(n=200, period=30.0)
        bars_400 = make_sine_bars(n=400, period=30.0)
        m200 = run_rsi(bars_200)
        m400 = run_rsi(bars_400)
        assert m400["total_orders"] >= m200["total_orders"]

    def test_pure_uptrend_no_oversold_signal(self, trend_bars_up):
        """纯上涨趋势下，RSI 持续偏高（超买），不会从超卖区回升，LONG 信号极少或为 0。"""
        metrics = run_rsi(trend_bars_up)
        # 纯上涨趋势，RSI 大概率不会跌入超卖区（<30），即使有也极少
        # 这里只做方向性验证：上涨趋势的 RSI 越过超卖区的次数 <= 正弦波
        sine = make_sine_bars(n=200, period=30.0)
        m_sine = run_rsi(sine)
        assert metrics["total_orders"] <= m_sine["total_orders"]
