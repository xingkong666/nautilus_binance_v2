"""PortfolioAllocator 单元测试."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.portfolio.allocator import (
    PortfolioAllocator,
    PortfolioSnapshot,
)

# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------

TWO_STRATEGIES = [
    {"strategy_id": "ema_cross", "weight": 2.0},
    {"strategy_id": "mean_revert", "weight": 1.0},
]

THREE_EQUAL = [
    {"strategy_id": "s1"},
    {"strategy_id": "s2"},
    {"strategy_id": "s3"},
]


def make_allocator(mode="equal", strategies=None, reserve_pct=5.0, **kwargs):
    """Build allocator.

    Args:
        mode: Mode.
        strategies: Strategies.
        reserve_pct: Percent value for reserve.
        **kwargs: Additional keyword arguments forwarded to the callable.
    """
    if strategies is None:
        strategies = TWO_STRATEGIES
    return PortfolioAllocator({"mode": mode, "reserve_pct": reserve_pct, "strategies": strategies, **kwargs})


# ---------------------------------------------------------------------------
# 初始化校验
# ---------------------------------------------------------------------------


class TestInit:
    """Test cases for init."""

    def test_invalid_mode_raises(self):
        """Verify that invalid mode raises."""
        with pytest.raises(ValueError, match="无效分配模式"):
            PortfolioAllocator({"mode": "unknown", "strategies": TWO_STRATEGIES})

    def test_empty_strategies_raises(self):
        """Verify that empty strategies raises."""
        with pytest.raises(ValueError, match="strategies 列表不能为空"):
            PortfolioAllocator({"strategies": []})

    def test_valid_modes(self):
        """Verify that valid modes."""
        for mode in ("equal", "weight", "risk_parity"):
            a = make_allocator(mode=mode)
            assert a._mode == mode


# ---------------------------------------------------------------------------
# 平等的 模式
# ---------------------------------------------------------------------------


class TestEqualMode:
    """Test cases for equal mode."""

    def test_equal_split(self):
        """Verify that equal split."""
        allocator = make_allocator(mode="equal", strategies=THREE_EQUAL, reserve_pct=0.0)
        results = allocator.allocate(Decimal("3000"))
        totals = sum(r.allocated_capital for r in results.values())
        # ROUND_DOWN 导致三等分有微小舍入误差，允许 ±0.05
        assert abs(totals - Decimal("3000")) < Decimal("0.05")
        for r in results.values():
            assert abs(r.allocated_capital - Decimal("1000.00")) < Decimal("0.05")

    def test_reserve_pct_reduces_deployable(self):
        """Verify that reserve percent reduces deployable."""
        allocator = make_allocator(mode="equal", strategies=THREE_EQUAL, reserve_pct=10.0)
        results = allocator.allocate(Decimal("3000"))
        # 可部署 = 2700，每个约 900，允许舍入误差
        for r in results.values():
            assert abs(r.allocated_capital - Decimal("900.00")) < Decimal("0.05")

    def test_returns_all_enabled_strategies(self):
        """Verify that returns all enabled strategies."""
        allocator = make_allocator(mode="equal")
        results = allocator.allocate(Decimal("10000"))
        assert set(results.keys()) == {"ema_cross", "mean_revert"}


# ---------------------------------------------------------------------------
# 重量 模式
# ---------------------------------------------------------------------------


class TestWeightMode:
    """Test cases for weight mode."""

    def test_weight_proportional(self):
        # 重量 2:1 → ema_cross 应拿 2/3，mean_revert 拿 1/3
        """Verify that weight proportional."""
        allocator = make_allocator(mode="weight", reserve_pct=0.0)
        results = allocator.allocate(Decimal("3000"))
        ema = results["ema_cross"].allocated_capital
        mr = results["mean_revert"].allocated_capital
        # 允许 1 分钱的舍入误差
        assert abs(ema - Decimal("2000")) < Decimal("1")
        assert abs(mr - Decimal("1000")) < Decimal("1")

    def test_max_allocation_pct_cap(self):
        """Verify that max allocation percent cap."""
        strategies = [
            {"strategy_id": "s1", "weight": 10.0, "max_allocation_pct": 30.0},
            {"strategy_id": "s2", "weight": 1.0},
        ]
        allocator = make_allocator(mode="weight", strategies=strategies, reserve_pct=0.0)
        results = allocator.allocate(Decimal("10000"))
        # s1 被上限到 30% = 3000
        assert results["s1"].allocated_capital <= Decimal("3000.01")


# ---------------------------------------------------------------------------
# risk_parity 模式
# ---------------------------------------------------------------------------


class TestRiskParityMode:
    """Test cases for risk parity mode."""

    def test_no_vol_fallback_to_equal(self):
        """Verify that no vol fallback to equal."""
        allocator = make_allocator(mode="risk_parity", strategies=THREE_EQUAL, reserve_pct=0.0)
        results = allocator.allocate(Decimal("3000"))
        for r in results.values():
            assert abs(r.allocated_capital - Decimal("1000.00")) < Decimal("0.05")

    def test_higher_vol_gets_less_allocation(self):
        """Verify that higher vol gets less allocation."""
        strategies = [
            {"strategy_id": "low_vol"},
            {"strategy_id": "high_vol"},
        ]
        allocator = make_allocator(mode="risk_parity", strategies=strategies, reserve_pct=0.0)
        allocator.update_volatility("low_vol", 0.1)  # 10% 波动率 → 权重 10
        allocator.update_volatility("high_vol", 0.5)  # 50% 波动率 → 权重 2
        results = allocator.allocate(Decimal("12000"))
        # low_vol 权重 10/(10+2) ≈ 83%，high_vol ≈ 17%
        assert results["low_vol"].allocated_capital > results["high_vol"].allocated_capital

    def test_update_volatility_invalid_raises(self):
        """Verify that update volatility invalid raises."""
        allocator = make_allocator(mode="risk_parity")
        with pytest.raises(ValueError, match="波动率必须为正数"):
            allocator.update_volatility("ema_cross", 0.0)


# ---------------------------------------------------------------------------
# get_available_capital
# ---------------------------------------------------------------------------


class TestGetAvailableCapital:
    """Test cases for get available capital."""

    def test_basic(self):
        """Verify that basic."""
        allocator = make_allocator(mode="equal", strategies=TWO_STRATEGIES, reserve_pct=0.0)
        # 两个策略等分 10000 → 各 5000
        avail = allocator.get_available_capital("ema_cross", Decimal("10000"))
        assert avail == Decimal("5000.00")

    def test_margin_used_reduces_available(self):
        """Verify that margin used reduces available."""
        allocator = make_allocator(mode="equal", strategies=TWO_STRATEGIES, reserve_pct=0.0)
        avail = allocator.get_available_capital("ema_cross", Decimal("10000"), margin_used=Decimal("2000"))
        assert avail == Decimal("3000.00")

    def test_margin_exceeds_allocation_clamps_to_zero(self):
        """Verify that margin exceeds allocation clamps to zero."""
        allocator = make_allocator(mode="equal", strategies=TWO_STRATEGIES, reserve_pct=0.0)
        avail = allocator.get_available_capital("ema_cross", Decimal("10000"), margin_used=Decimal("9999"))
        assert avail == Decimal("0")

    def test_unknown_strategy_raises(self):
        """Verify that unknown strategy raises."""
        allocator = make_allocator()
        with pytest.raises(KeyError, match="未知策略"):
            allocator.get_available_capital("nonexistent", Decimal("10000"))


# ---------------------------------------------------------------------------
# 重新平衡
# ---------------------------------------------------------------------------


class TestRebalance:
    """Test cases for rebalance."""

    def test_no_rebalance_within_threshold(self):
        """偏差 < 5% 不产生订单."""
        allocator = make_allocator(mode="equal", strategies=TWO_STRATEGIES, reserve_pct=0.0)
        # ema_cross 分配 5000，当前持仓价值 4900（偏差 2%）
        snaps = [
            PortfolioSnapshot(
                strategy_id="ema_cross",
                instrument_id="BTCUSDT-PERP",
                current_quantity=Decimal("0.1"),
                current_price=Decimal("49000"),  # 值 = 4900
                margin_used=Decimal("490"),
            )
        ]
        intents = allocator.rebalance(snaps, Decimal("10000"))
        assert intents == []

    def test_rebalance_triggers_buy(self):
        """偏差 > 5%，持仓不足 → 生成 BUY 订单."""
        allocator = make_allocator(mode="equal", strategies=TWO_STRATEGIES, reserve_pct=0.0)
        # ema_cross 分配 5000，当前持仓价值 4000（偏差 20%）
        snaps = [
            PortfolioSnapshot(
                strategy_id="ema_cross",
                instrument_id="BTCUSDT-PERP",
                current_quantity=Decimal("0.08"),
                current_price=Decimal("50000"),  # 值 = 4000
            )
        ]
        intents = allocator.rebalance(snaps, Decimal("10000"))
        assert len(intents) == 1
        assert intents[0].side == "BUY"
        assert intents[0].strategy_id == "ema_cross"

    def test_rebalance_zero_target_generates_close(self):
        """目标分配为 0 且有持仓 → 生成平仓 SELL 订单."""
        strategies = [
            {"strategy_id": "active"},
            {"strategy_id": "disabled", "enabled": False},
        ]
        allocator = make_allocator(mode="equal", strategies=strategies, reserve_pct=0.0)
        snaps = [
            PortfolioSnapshot(
                strategy_id="disabled",
                instrument_id="ETHUSDT-PERP",
                current_quantity=Decimal("1.0"),
                current_price=Decimal("3000"),
            )
        ]
        intents = allocator.rebalance(snaps, Decimal("10000"))
        assert len(intents) == 1
        assert intents[0].reduce_only is True

    def test_unknown_strategy_close_unknown_true(self):
        """未知策略有持仓且 close_unknown=True（默认）→ 生成平仓单."""
        allocator = make_allocator(mode="equal")
        snaps = [
            PortfolioSnapshot(
                strategy_id="ghost_strategy",
                instrument_id="BTCUSDT-PERP",
                current_quantity=Decimal("1.0"),
                current_price=Decimal("50000"),
            )
        ]
        intents = allocator.rebalance(snaps, Decimal("10000"))
        assert len(intents) == 1
        assert intents[0].reduce_only is True

    def test_unknown_strategy_close_unknown_false(self):
        """未知策略 close_unknown=False → 跳过，不产生订单."""
        allocator = make_allocator(mode="equal")
        snaps = [
            PortfolioSnapshot(
                strategy_id="ghost_strategy",
                instrument_id="BTCUSDT-PERP",
                current_quantity=Decimal("1.0"),
                current_price=Decimal("50000"),
            )
        ]
        intents = allocator.rebalance(snaps, Decimal("10000"), close_unknown=False)
        assert intents == []


# ---------------------------------------------------------------------------
# update_strategy_enabled
# ---------------------------------------------------------------------------


class TestDynamicEnable:
    """Test cases for dynamic enable."""

    def test_disable_strategy_excludes_from_allocation(self):
        """Verify that disable strategy excludes from allocation."""
        allocator = make_allocator(mode="equal", strategies=TWO_STRATEGIES)
        allocator.update_strategy_enabled("mean_revert", False)
        results = allocator.allocate(Decimal("10000"))
        assert "mean_revert" not in results
        assert "ema_cross" in results

    def test_unknown_strategy_raises(self):
        """Verify that unknown strategy raises."""
        allocator = make_allocator()
        with pytest.raises(KeyError, match="未知策略"):
            allocator.update_strategy_enabled("ghost", True)


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


class TestSummary:
    """Test cases for summary."""

    def test_summary_contains_strategy_ids(self):
        """Verify that summary contains strategy IDs."""
        allocator = make_allocator(mode="weight")
        s = allocator.summary(Decimal("10000"))
        assert "ema_cross" in s
        assert "mean_revert" in s
        assert "weight" in s
