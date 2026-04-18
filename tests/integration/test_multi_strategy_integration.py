"""集成测试：多策略 + PortfolioAllocator 联调.

验证 EMACrossStrategy + RSIStrategy 两个策略与 PortfolioAllocator 协同工作：
- 两个策略可以同时运行在同一个 BacktestEngine 上
- PortfolioAllocator 能正确分配资金给两个策略
- 不同分配模式（equal / weight / risk_parity）行为正确
- 策略启停不影响另一个策略的分配
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from src.portfolio.allocator import PortfolioAllocator, PortfolioSnapshot
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy
from src.strategy.rsi_strategy import RSIStrategy, RSIStrategyConfig
from tests.regression.conftest import (
    BAR_TYPE,
    BTCUSDT,
    STARTING_BALANCE,
    build_engine,
    make_sine_bars,
)

# ---------------------------------------------------------------------------
# 多策略回测辅助
# ---------------------------------------------------------------------------


def run_multi_strategy(
    bars,
    ema_fast: int = 5,
    ema_slow: int = 20,
    rsi_period: int = 14,
    trade_size: str = "0.010",
    starting_balance: int = STARTING_BALANCE,
    entry_min_atr_ratio: float = 0.0,
    signal_cooldown_bars: int = 0,
) -> dict[str, Any]:
    """同时运行 EMA Cross + RSI 策略，返回联合指标.

    Args:
        bars: 合成 Bar 数据列表。
        ema_fast: EMA 快线周期。
        ema_slow: EMA 慢线周期。
        rsi_period: RSI 周期。
        trade_size: 每次下单量（币数字符串）。
        starting_balance: 初始余额（USDT）。
        entry_min_atr_ratio: EMA 入场最小 ATR 比例过滤，<=0 表示关闭。
        signal_cooldown_bars: EMA 信号冷却条数，<=0 表示关闭。

    Returns:
        包含 iterations / total_orders / total_positions 的字典。

    """
    engine = build_engine(starting_balance)
    engine.add_data(bars)

    ema_cfg = EMACrossConfig(
        instrument_id=BTCUSDT.id,
        bar_type=BAR_TYPE,
        fast_ema_period=ema_fast,
        slow_ema_period=ema_slow,
        trade_size=Decimal(trade_size),
        entry_min_atr_ratio=entry_min_atr_ratio,
        signal_cooldown_bars=signal_cooldown_bars,
    )
    rsi_cfg = RSIStrategyConfig(
        instrument_id=BTCUSDT.id,
        bar_type=BAR_TYPE,
        rsi_period=rsi_period,
        oversold_level=30.0,
        overbought_level=70.0,
        trade_size=Decimal(trade_size),
    )
    engine.add_strategy(EMACrossStrategy(config=ema_cfg))
    engine.add_strategy(RSIStrategy(config=rsi_cfg))
    engine.sort_data()
    engine.run()
    result = engine.get_result()

    metrics = {
        "iterations": result.iterations,
        "total_orders": result.total_orders,
        "total_positions": result.total_positions,
    }
    engine.dispose()
    return metrics


# ---------------------------------------------------------------------------
# 多策略回测测试
# ---------------------------------------------------------------------------


class TestMultiStrategyBacktest:
    """两个策略同时运行的回测集成测试。."""

    def test_both_strategies_run_on_same_engine(self):
        """两个策略同时加入引擎，能正常完成回测不抛出异常。."""
        bars = make_sine_bars(n=200)
        metrics = run_multi_strategy(bars)
        assert metrics["iterations"] == 200
        assert metrics["total_orders"] >= 0

    def test_multi_strategy_more_orders_than_single(self):
        """两个策略产生的订单总数 >= 单个策略（因为两个策略都会产生信号）。."""
        from tests.regression.conftest import run_ema_cross
        from tests.regression.test_rsi_baseline import run_rsi

        bars = make_sine_bars(n=200)
        m_ema = run_ema_cross(bars)
        m_rsi = run_rsi(bars)
        m_multi = run_multi_strategy(bars)

        # 两个策略合计订单数 = 两个单独运行的订单数之和
        # （回测引擎为每个策略独立管理订单）
        assert m_multi["total_orders"] == m_ema["total_orders"] + m_rsi["total_orders"], (
            f"多策略订单数 {m_multi['total_orders']} 应等于 EMA({m_ema['total_orders']}) + RSI({m_rsi['total_orders']})"
        )

    def test_multi_strategy_deterministic(self):
        """相同数据运行两次结果完全一致。."""
        bars = make_sine_bars(n=200)
        m1 = run_multi_strategy(bars)
        m2 = run_multi_strategy(bars)
        assert m1 == m2

    def test_multi_strategy_all_bars_processed(self):
        """引擎处理了所有 Bar。."""
        bars = make_sine_bars(n=150)
        metrics = run_multi_strategy(bars)
        assert metrics["iterations"] == 150

    def test_multi_strategy_large_dataset(self):
        """500 根 Bar 的多策略回测不超时、不报错。."""
        bars = make_sine_bars(n=500)
        metrics = run_multi_strategy(bars)
        assert metrics["iterations"] == 500
        assert metrics["total_orders"] >= 0


# ---------------------------------------------------------------------------
# 投资组合分配器 与多策略联调
# ---------------------------------------------------------------------------


class TestPortfolioAllocatorMultiStrategy:
    """PortfolioAllocator 管理两个策略的资金分配联调测试。."""

    # ------------------------------------------------------------------
    # 基础分配测试
    # ------------------------------------------------------------------

    def test_equal_mode_splits_capital_evenly(self):
        """Equal 模式：两个策略各分配一半（扣除储备）。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        results = allocator.allocate(Decimal("10000"))
        assert "ema_cross" in results
        assert "rsi_strategy" in results

        ema_alloc = results["ema_cross"].allocated_capital
        rsi_alloc = results["rsi_strategy"].allocated_capital
        # 各 50%，共 10000，各 5000
        assert ema_alloc == Decimal("5000.00")
        assert rsi_alloc == Decimal("5000.00")

    def test_weight_mode_respects_ratio(self):
        """Weight 模式：按权重比例分配（2:1）。."""
        allocator = PortfolioAllocator(
            {
                "mode": "weight",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross", "weight": 2.0},
                    {"strategy_id": "rsi_strategy", "weight": 1.0},
                ],
            }
        )
        results = allocator.allocate(Decimal("9000"))
        ema_alloc = results["ema_cross"].allocated_capital
        rsi_alloc = results["rsi_strategy"].allocated_capital
        # EMA: 相对强弱指数= 2 : 1 → 6000 : 3000
        # 分配器 使用 ROUND_DOWN 截断，实际值可能略低于精确值
        assert ema_alloc >= Decimal("5999.99")
        assert rsi_alloc >= Decimal("2999.99")
        # 验证比例关系：EMA分配量约为 相对强弱指数的两倍
        assert ema_alloc == rsi_alloc * 2 or abs(ema_alloc - rsi_alloc * 2) <= Decimal("0.02")

    def test_reserve_reduces_deployable_capital(self):
        """储备金正确从总资金中扣除。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 10.0,  # 10% 储备
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        results = allocator.allocate(Decimal("10000"))
        total_deployed = sum(r.allocated_capital for r in results.values())
        # 可部署 = 9000，各 4500
        assert total_deployed == Decimal("9000.00")

    def test_risk_parity_mode_with_volatility(self):
        """risk_parity 模式：波动率高的策略分得更少资金。."""
        allocator = PortfolioAllocator(
            {
                "mode": "risk_parity",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        # EMA波动率更高 → 应分配更少资金
        allocator.update_volatility("ema_cross", 0.4)  # 高波动率
        allocator.update_volatility("rsi_strategy", 0.2)  # 低波动率

        results = allocator.allocate(Decimal("10000"))
        ema_alloc = results["ema_cross"].allocated_capital
        rsi_alloc = results["rsi_strategy"].allocated_capital
        # 相对强弱指数波动率低 → 权重高 → 分配更多
        assert rsi_alloc > ema_alloc

    def test_risk_parity_fallback_equal_without_volatility(self):
        """risk_parity 模式无波动率数据时退化为等权重。."""
        allocator = PortfolioAllocator(
            {
                "mode": "risk_parity",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        # 不注入波动率 → 等权重
        results = allocator.allocate(Decimal("10000"))
        ema_alloc = results["ema_cross"].allocated_capital
        rsi_alloc = results["rsi_strategy"].allocated_capital
        assert ema_alloc == rsi_alloc

    # ------------------------------------------------------------------
    # 动态策略管理
    # ------------------------------------------------------------------

    def test_disable_one_strategy_all_capital_to_other(self):
        """禁用一个策略后，所有可部署资金分配给另一个。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        allocator.update_strategy_enabled("rsi_strategy", False)
        results = allocator.allocate(Decimal("10000"))

        assert "rsi_strategy" not in results
        assert results["ema_cross"].allocated_capital == Decimal("10000.00")

    def test_re_enable_strategy_restores_allocation(self):
        """重新启用策略后资金分配恢复正常。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        allocator.update_strategy_enabled("rsi_strategy", False)
        allocator.update_strategy_enabled("rsi_strategy", True)
        results = allocator.allocate(Decimal("10000"))

        assert "rsi_strategy" in results
        assert "ema_cross" in results
        assert results["ema_cross"].allocated_capital == Decimal("5000.00")
        assert results["rsi_strategy"].allocated_capital == Decimal("5000.00")

    def test_disable_all_strategies_raises(self):
        """禁用所有策略后调用 allocate 应抛出 ValueError。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        allocator.update_strategy_enabled("ema_cross", False)
        allocator.update_strategy_enabled("rsi_strategy", False)
        with pytest.raises(ValueError, match="没有已启用的策略"):
            allocator.allocate(Decimal("10000"))

    # ------------------------------------------------------------------
    # 再平衡测试
    # ------------------------------------------------------------------

    def test_rebalance_no_intents_within_threshold(self):
        """持仓价值在目标的 5% 偏差内，不触发再平衡。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        total_capital = Decimal("10000")
        # 两个策略各 5000，持仓价值与目标一致 → 不触发
        snapshots = [
            PortfolioSnapshot(
                strategy_id="ema_cross",
                instrument_id="BTCUSDT-PERP.BINANCE",
                current_quantity=Decimal("0.1"),
                current_price=Decimal("50000"),  # 持仓价值 5000 = 目标
                margin_used=Decimal("500"),
            ),
            PortfolioSnapshot(
                strategy_id="rsi_strategy",
                instrument_id="BTCUSDT-PERP.BINANCE",
                current_quantity=Decimal("0.1"),
                current_price=Decimal("50000"),
                margin_used=Decimal("500"),
            ),
        ]
        intents = allocator.rebalance(snapshots, total_capital)
        assert len(intents) == 0

    def test_rebalance_generates_intent_when_deviation_large(self):
        """持仓价值偏差超过 5% 时触发再平衡，产生 OrderIntent。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        total_capital = Decimal("10000")
        # EMA策略目标 5000，当前持仓价值仅 2000（偏差 60%）→ 应触发
        snapshots = [
            PortfolioSnapshot(
                strategy_id="ema_cross",
                instrument_id="BTCUSDT-PERP.BINANCE",
                current_quantity=Decimal("0.04"),  # 0.04 * 50000 = 2000
                current_price=Decimal("50000"),
                margin_used=Decimal("200"),
            ),
        ]
        intents = allocator.rebalance(snapshots, total_capital)
        assert len(intents) == 1
        assert intents[0].side == "BUY"  # 持仓不足 → 需要加仓

    def test_rebalance_close_disabled_strategy_position(self):
        """再平衡时，已禁用策略的持仓应全部平仓。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        allocator.update_strategy_enabled("rsi_strategy", False)

        snapshots = [
            PortfolioSnapshot(
                strategy_id="rsi_strategy",
                instrument_id="BTCUSDT-PERP.BINANCE",
                current_quantity=Decimal("0.1"),  # 有持仓
                current_price=Decimal("50000"),
                margin_used=Decimal("500"),
            ),
        ]
        intents = allocator.rebalance(snapshots, Decimal("10000"))
        # 禁用策略有持仓 → close_unknown=True→ 应产生平仓指令
        assert len(intents) == 1
        assert intents[0].reduce_only is True

    def test_rebalance_no_positions_no_intents(self):
        """没有持仓快照时，再平衡不产生任何指令。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        intents = allocator.rebalance([], Decimal("10000"))
        assert intents == []

    # ------------------------------------------------------------------
    # get_available_capital
    # ------------------------------------------------------------------

    def test_get_available_capital_subtracts_margin(self):
        """可用资金 = 分配资金 - 已用保证金。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        # 各 5000，EMA已用保证金 1000 → 可用 4000
        available = allocator.get_available_capital(
            "ema_cross",
            Decimal("10000"),
            margin_used=Decimal("1000"),
        )
        assert available == Decimal("4000.00")

    def test_get_available_capital_floor_at_zero(self):
        """可用资金不为负数（保证金超过分配时返回 0）。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 0.0,
                "strategies": [
                    {"strategy_id": "ema_cross"},
                    {"strategy_id": "rsi_strategy"},
                ],
            }
        )
        available = allocator.get_available_capital(
            "ema_cross",
            Decimal("10000"),
            margin_used=Decimal("9999"),  # 远超分配的 5000
        )
        assert available == Decimal("0")

    def test_get_available_capital_unknown_strategy_raises(self):
        """查询未知策略应抛出 KeyError。."""
        allocator = PortfolioAllocator(
            {
                "mode": "equal",
                "reserve_pct": 0.0,
                "strategies": [{"strategy_id": "ema_cross"}],
            }
        )
        with pytest.raises(KeyError, match="未知策略"):
            allocator.get_available_capital("nonexistent", Decimal("10000"))

    # ------------------------------------------------------------------
    # 汇总 工具
    # ------------------------------------------------------------------

    def test_summary_contains_both_strategies(self):
        """summary() 输出包含两个策略的名称和分配信息。."""
        allocator = PortfolioAllocator(
            {
                "mode": "weight",
                "reserve_pct": 5.0,
                "strategies": [
                    {"strategy_id": "ema_cross", "weight": 2.0},
                    {"strategy_id": "rsi_strategy", "weight": 1.0},
                ],
            }
        )
        text = allocator.summary(Decimal("10000"))
        assert "ema_cross" in text
        assert "rsi_strategy" in text
        assert "weight" in text
