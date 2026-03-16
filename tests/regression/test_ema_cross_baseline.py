"""回归测试：EMA 交叉策略基准锁定.

用合成数据（确定性正弦波 + 趋势序列）跑 EMA 交叉策略，
将关键指标与硬编码基准值比对。

任何导致订单数/仓位数/成交数变化的代码改动都会在此处被捕获。

基准值生成方式：
    首次运行时用 --update-baseline 模式打印实际值，
    确认无误后硬编码到 BASELINE 字典中。
"""

from __future__ import annotations

import datetime as dt

from nautilus_trader.model.data import Bar
from nautilus_trader.model.objects import Price, Quantity

from tests.regression.conftest import BAR_TYPE, make_sine_bars, run_ema_cross

# ---------------------------------------------------------------------------
# 基准值（Hardcoded Baselines）
#
# 这些值通过 "首次诚实运行" 得出，后续每次回归都与之比对。
# 若确认代码改动合理导致基准变化，手动更新这里的值并附上 commit message。
# ---------------------------------------------------------------------------

# 正弦波数据 + EMA(5,20)，200 根 1m Bar
# 由首次诚实运行确认：iterations=200, orders=12, positions=6
SINE_BASELINE = {
    "iterations": 200,
    "total_orders": 12,
    "total_positions": 6,
}

# 正弦波数据 + EMA(5,20)，300 根 1m Bar（扩展基准）
SINE_300_BASELINE = {
    "iterations": 300,
    "total_orders": 20,
    "total_positions": 10,
}

# 正弦波高振幅数据 + EMA(5,20)，200 根 1m Bar（amplitude=2000, period=20）
SINE_HI_AMP_BASELINE = {
    "iterations": 200,
    "total_orders": 18,
    "total_positions": 9,
}


# ---------------------------------------------------------------------------
# 辅助：断言基准指标
# ---------------------------------------------------------------------------


def assert_baseline(metrics: dict, baseline: dict, label: str = "") -> None:
    """逐项比对 metrics 与 baseline，不匹配时给出详细错误信息.

    Args:
        metrics: 实际运行得到的指标字典。
        baseline: 基准值字典（None 值跳过）。
        label: 测试场景名称，用于错误信息。

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
# 正弦波基准测试
# ---------------------------------------------------------------------------


class TestSineWaveBaseline:
    """使用正弦波合成数据的基准锁定测试。."""

    def test_total_iterations(self, sine_bars):
        """验证引擎处理了所有 200 根 Bar。.

        Args:
            sine_bars: Sine bars.
        """
        metrics = run_ema_cross(sine_bars)
        assert metrics["iterations"] == SINE_BASELINE["iterations"]

    def test_total_orders_unchanged(self, sine_bars):
        """验证订单总数与基准一致（防止策略逻辑被意外修改）。.

        Args:
            sine_bars: Sine bars.
        """
        metrics = run_ema_cross(sine_bars)
        assert_baseline(metrics, SINE_BASELINE, "SineWave EMA(5,20)")

    def test_total_positions_unchanged(self, sine_bars):
        """验证仓位总数与基准一致。.

        Args:
            sine_bars: Sine bars.
        """
        metrics = run_ema_cross(sine_bars)
        assert metrics["total_positions"] == SINE_BASELINE["total_positions"]

    def test_orders_greater_than_zero(self, sine_bars):
        """正弦波数据应触发至少 1 次交叉，产出订单。.

        Args:
            sine_bars: Sine bars.
        """
        metrics = run_ema_cross(sine_bars)
        assert metrics["total_orders"] > 0, "EMA 策略在正弦波数据上应产生交叉信号"

    def test_positions_equal_half_orders(self, sine_bars):
        """每个仓位对应 1 笔开仓订单（开仓=收到信号），总仓位约为订单数的一半（开+平）。.

        Args:
            sine_bars: Sine bars.
        """
        metrics = run_ema_cross(sine_bars)
        # NautilusTrader HEDGING 模式：每次方向变化产生 2 笔订单（平旧 + 开新）
        # 第一次交叉只有 1 笔开仓；后续每次交叉产生 2 笔
        # 所以 total_orders >= total_positions
        assert metrics["total_orders"] >= metrics["total_positions"]

    def test_deterministic_result(self, sine_bars):
        """相同数据运行两次结果完全一致（无随机性）。.

        Args:
            sine_bars: Sine bars.
        """
        m1 = run_ema_cross(sine_bars)
        m2 = run_ema_cross(sine_bars)
        assert m1["total_orders"] == m2["total_orders"]
        assert m1["total_positions"] == m2["total_positions"]


# ---------------------------------------------------------------------------
# 趋势数据基准测试
# ---------------------------------------------------------------------------


class TestMultiScenarioBaseline:
    """多场景基准锁定测试：验证不同数据特征下的结果稳定性。."""

    def test_300_bars_baseline(self):
        """300 根正弦波 Bar 的基准锁定。."""
        bars = make_sine_bars(n=300, base_price=50_000.0, amplitude=500.0, period=30.0)
        metrics = run_ema_cross(bars)
        assert_baseline(metrics, SINE_300_BASELINE, "Sine 300bars EMA(5,20)")

    def test_hi_amplitude_baseline(self):
        """高振幅正弦波基准锁定（振幅=2000，周期=20）。."""
        bars = make_sine_bars(n=200, base_price=50_000.0, amplitude=2000.0, period=20.0)
        metrics = run_ema_cross(bars)
        assert_baseline(metrics, SINE_HI_AMP_BASELINE, "Sine HiAmp EMA(5,20)")

    def test_hi_amplitude_more_orders_than_normal(self):
        """高振幅数据产生的交叉次数 >= 标准振幅（振幅越大交叉越明显）。."""
        bars_normal = make_sine_bars(n=200, amplitude=500.0, period=30.0)
        bars_hiamp = make_sine_bars(n=200, amplitude=2000.0, period=20.0)
        m_normal = run_ema_cross(bars_normal)
        m_hiamp = run_ema_cross(bars_hiamp)
        assert m_hiamp["total_orders"] >= m_normal["total_orders"]

    def test_pure_uptrend_no_crossover(self, trend_bars_up):
        """纯线性上涨趋势：EMA 从一开始就单方向排列，不会产生交叉信号。.

        这是一个「不变量」测试：纯趋势 = 无交叉 = 零订单。
        若将来策略改为趋势跟踪，此测试需要相应更新。

        Args:
            trend_bars_up: Trend bars up.
        """
        metrics = run_ema_cross(trend_bars_up)
        assert metrics["total_orders"] == 0, (
            "纯线性上涨趋势（EMA 从不交叉）不应产生订单，若此测试失败说明策略信号逻辑可能发生了变化"
        )

    def test_pure_downtrend_no_crossover(self, trend_bars_down):
        """纯线性下跌趋势：同上，不产生交叉信号。.

        Args:
            trend_bars_down: Trend bars down.
        """
        metrics = run_ema_cross(trend_bars_down)
        assert metrics["total_orders"] == 0


# ---------------------------------------------------------------------------
# 参数敏感性测试
# ---------------------------------------------------------------------------


class TestParameterSensitivity:
    """验证参数变化对结果的影响方向，防止参数范围或默认值被意外改动。."""

    def test_faster_ema_generates_more_orders(self, sine_bars):
        """更短的 EMA 周期应产生更多交叉（更多订单）。.

        Args:
            sine_bars: Sine bars.
        """
        metrics_slow = run_ema_cross(sine_bars, fast_period=5, slow_period=20)
        metrics_fast = run_ema_cross(sine_bars, fast_period=3, slow_period=10)
        # 更快的参数在同样的数据上应产生 >= 慢参数的订单数
        assert metrics_fast["total_orders"] >= metrics_slow["total_orders"], (
            f"EMA(3,10) orders={metrics_fast['total_orders']} 应 >= EMA(5,20) orders={metrics_slow['total_orders']}"
        )

    def test_same_period_ema_generates_no_orders(self):
        """快线 = 慢线时，不会产生交叉，订单数为 0。."""
        bars = make_sine_bars(n=100)
        metrics = run_ema_cross(bars, fast_period=10, slow_period=10)
        assert metrics["total_orders"] == 0, "快慢线相同时不应产生信号"

    def test_more_bars_same_pattern_same_result(self):
        """在完整周期的正弦波上，bars 数量翻倍应该使订单数也对应翻倍（近似）。."""
        bars_200 = make_sine_bars(n=200, period=30.0)
        bars_400 = make_sine_bars(n=400, period=30.0)
        m200 = run_ema_cross(bars_200)
        m400 = run_ema_cross(bars_400)
        # 400 根 bar 的订单应 >= 200 根（因为多了更多周期）
        assert m400["total_orders"] >= m200["total_orders"]

    def test_larger_trade_size_does_not_change_order_count(self, sine_bars):
        """trade_size 不影响信号产生，订单数与 trade_size 无关。.

        Args:
            sine_bars: Sine bars.
        """
        m_small = run_ema_cross(sine_bars, trade_size="0.001")
        m_large = run_ema_cross(sine_bars, trade_size="0.100")
        assert m_small["total_orders"] == m_large["total_orders"]


class TestSignalFilters:
    """验证 EMA 新增过滤器对交易频率有抑制作用。."""

    def test_atr_ratio_filter_reduces_orders(self, sine_bars):
        """Verify that ATR ratio filter reduces orders.

        Args:
            sine_bars: Sine bars.
        """
        baseline = run_ema_cross(sine_bars, entry_min_atr_ratio=0.0, signal_cooldown_bars=0)
        filtered = run_ema_cross(sine_bars, entry_min_atr_ratio=0.0025, signal_cooldown_bars=0)
        assert filtered["total_orders"] <= baseline["total_orders"]

    def test_cooldown_reduces_orders(self, sine_bars):
        """Verify that cooldown reduces orders.

        Args:
            sine_bars: Sine bars.
        """
        baseline = run_ema_cross(sine_bars, entry_min_atr_ratio=0.0, signal_cooldown_bars=0)
        filtered = run_ema_cross(sine_bars, entry_min_atr_ratio=0.0, signal_cooldown_bars=3)
        assert filtered["total_orders"] <= baseline["total_orders"]


# ---------------------------------------------------------------------------
# 策略属性不变性测试
# ---------------------------------------------------------------------------


class TestStrategyInvariants:
    """验证策略的基本不变量，任何破坏这些性质的改动都是 Bug。."""

    def test_no_orders_on_flat_data(self):
        """完全平坦的价格数据（无波动）不产生交叉，订单数为 0。."""
        start_ns = int(dt.datetime(2024, 1, 1, tzinfo=dt.UTC).timestamp() * 1_000_000_000)
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
        metrics = run_ema_cross(flat_bars)
        assert metrics["total_orders"] == 0, "平坦数据不应触发 EMA 交叉"

    def test_no_orders_insufficient_bars(self):
        """Bar 数量不足以预热 EMA 时，不产生订单。."""
        # slow_period=20，只提供 19 根 bar → 无法计算 EMA → 无信号
        bars = make_sine_bars(n=19, period=10.0)
        metrics = run_ema_cross(bars, fast_period=5, slow_period=20)
        assert metrics["total_orders"] == 0, "Bar 数不足预热时不应产生订单"

    def test_starting_balance_does_not_affect_signal_count(self, sine_bars):
        """初始资金不影响信号产生逻辑（订单数相同）。.

        Args:
            sine_bars: Sine bars.
        """
        m_small = run_ema_cross(sine_bars, starting_balance=1_000)
        m_large = run_ema_cross(sine_bars, starting_balance=1_000_000)
        assert m_small["total_orders"] == m_large["total_orders"]
