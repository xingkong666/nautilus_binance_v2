"""Unit tests for WalkForwardEngine data classes and stability scoring.

Tests focus on _compute_stability() and data class construction —
no BacktestEngine, no file I/O, no network calls.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.backtest.walk_forward_engine import (
    StabilityReport,
    WalkForwardEngine,
    WalkForwardResult,
    WindowResult,
)
from src.backtest.walkforward import WalkforwardWindow

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_window(index: int = 1) -> WalkforwardWindow:
    return WalkforwardWindow(
        index=index,
        train_start=dt.date(2024, 1, 1),
        train_end=dt.date(2024, 6, 30),
        test_start=dt.date(2024, 7, 1),
        test_end=dt.date(2024, 9, 30),
    )


def _make_summary(pnl_pct: float) -> dict:
    return {
        "pnl": {"USDT": {"PnL (total)": pnl_pct * 1000, "PnL% (total)": pnl_pct, "Win Rate": 0.5}},
        "returns": {},
        "analysis": {"costs": {"pnl_pct_after_costs": pnl_pct, "pnl_after_costs": pnl_pct * 1000}},
    }


def _make_window_result(
    index: int,
    is_pnl_pct: float,
    oos_pnl_pct: float,
) -> WindowResult:
    return WindowResult(
        window=_make_window(index),
        train_summary=_make_summary(is_pnl_pct),
        test_summary=_make_summary(oos_pnl_pct),
        train_equity_curve=pd.DataFrame(),
        test_equity_curve=pd.DataFrame(),
        active_strategy_count=2,
        allocation_map={"strategy_A": 0.5, "strategy_B": 0.5},
    )


def _make_engine() -> WalkForwardEngine:
    """Return a WalkForwardEngine with mocked app_config / factory (no real I/O)."""
    app_config = MagicMock()
    factory = MagicMock()
    portfolio_config = {
        "name": "test_portfolio",
        "backtest": {"interval": "1h", "starting_balance_usdt": 10_000, "leverage": 10.0},
        "allocation": {"mode": "risk_parity"},
        "strategies": [{"name": "EMA", "symbol": "BTCUSDT", "enabled": True}],
        "walkforward": {
            "start": "2024-01-01",
            "end": "2024-12-31",
            "train_months": 6,
            "test_months": 3,
            "step_months": 3,
            "selection_metric": "pnl_pct_after_costs",
        },
    }
    return WalkForwardEngine(
        app_config=app_config,
        factory=factory,
        portfolio_config=portfolio_config,
    )


# ---------------------------------------------------------------------------
# 测试 1 —— 完全 一致性 (全部 OOS > 0)
# ---------------------------------------------------------------------------


def test_stability_perfect_consistency() -> None:
    """All OOS windows profitable → consistency_rate=1.0, passed=True."""
    engine = _make_engine()
    windows = [
        _make_window_result(1, is_pnl_pct=5.0, oos_pnl_pct=3.0),
        _make_window_result(2, is_pnl_pct=4.0, oos_pnl_pct=2.0),
        _make_window_result(3, is_pnl_pct=6.0, oos_pnl_pct=4.0),
    ]
    report = engine._compute_stability(windows)

    assert report.consistency_rate == 1.0
    assert report.overfitting_score == 0.0
    assert report.passed is True
    assert report.window_count == 3


# ---------------------------------------------------------------------------
# 测试 2 —— 零 一致性 (全部 OOS < 0)
# ---------------------------------------------------------------------------


def test_stability_zero_consistency() -> None:
    """All OOS windows losing → passed=False, overfitting_score=1.0."""
    engine = _make_engine()
    windows = [
        _make_window_result(1, is_pnl_pct=5.0, oos_pnl_pct=-2.0),
        _make_window_result(2, is_pnl_pct=3.0, oos_pnl_pct=-1.0),
        _make_window_result(3, is_pnl_pct=4.0, oos_pnl_pct=-3.0),
    ]
    report = engine._compute_stability(windows)

    assert report.consistency_rate == 0.0
    assert report.overfitting_score == 1.0
    assert report.passed is False


# ---------------------------------------------------------------------------
# 测试 3 —— 正 IS/OOS 相关
# ---------------------------------------------------------------------------


def test_stability_correlation_positive() -> None:
    """IS↑ OOS↑ → Pearson correlation > 0."""
    engine = _make_engine()
    # 单调递增IS & OOS→ 完美正相关
    windows = [
        _make_window_result(1, is_pnl_pct=1.0, oos_pnl_pct=0.5),
        _make_window_result(2, is_pnl_pct=3.0, oos_pnl_pct=1.5),
        _make_window_result(3, is_pnl_pct=5.0, oos_pnl_pct=2.5),
    ]
    report = engine._compute_stability(windows)

    assert report.is_oos_correlation > 0
    assert report.is_oos_correlation == pytest.approx(1.0, abs=1e-4)


# ---------------------------------------------------------------------------
# 测试 4 —— 单个 窗口 (no crash, correlation=0.0)
# ---------------------------------------------------------------------------


def test_stability_single_window() -> None:
    """window_count=1 → no crash, correlation defaults to 0.0."""
    engine = _make_engine()
    windows = [_make_window_result(1, is_pnl_pct=5.0, oos_pnl_pct=2.0)]
    report = engine._compute_stability(windows)

    assert report.window_count == 1
    assert report.is_oos_correlation == 0.0
    # consistency_rate 仍应计算（1 OOS> 0 个窗口，共 1 个）
    assert report.consistency_rate == 1.0
    assert report.passed is True


# ---------------------------------------------------------------------------
# 测试 5 —— degradation_ratio (IS=5%, OOS=2% → ratio≈0.4)
# ---------------------------------------------------------------------------


def test_stability_degradation_ratio() -> None:
    """IS mean=5%, OOS mean=2% → degradation_ratio ≈ 0.4."""
    engine = _make_engine()
    windows = [
        _make_window_result(1, is_pnl_pct=5.0, oos_pnl_pct=2.0),
        _make_window_result(2, is_pnl_pct=5.0, oos_pnl_pct=2.0),
    ]
    report = engine._compute_stability(windows)

    assert report.mean_is_pnl_pct == pytest.approx(5.0, abs=1e-3)
    assert report.mean_oos_pnl_pct == pytest.approx(2.0, abs=1e-3)
    assert report.degradation_ratio == pytest.approx(0.4, abs=1e-3)


# ---------------------------------------------------------------------------
# 测试 6 —— 零 IS PNL→ degradation_ratio=0.0, no 零除法错误
# ---------------------------------------------------------------------------


def test_stability_zero_is_pnl() -> None:
    """IS PnL = 0 → degradation_ratio=0.0, no ZeroDivisionError."""
    engine = _make_engine()
    windows = [
        _make_window_result(1, is_pnl_pct=0.0, oos_pnl_pct=1.0),
        _make_window_result(2, is_pnl_pct=0.0, oos_pnl_pct=-1.0),
    ]
    report = engine._compute_stability(windows)

    assert report.degradation_ratio == 0.0
    # 没有引发异常（重要的断言是我们到达这里）
    assert isinstance(report, StabilityReport)


# ---------------------------------------------------------------------------
# 测试7——WindowResult数据类字段完整
# ---------------------------------------------------------------------------


def test_window_result_dataclass() -> None:
    """WindowResult can be constructed with all required fields."""
    window = _make_window(index=3)
    train_curve = pd.DataFrame({"equity": [10000.0, 10500.0]})
    test_curve = pd.DataFrame({"equity": [10000.0, 9800.0]})

    wr = WindowResult(
        window=window,
        train_summary=_make_summary(5.0),
        test_summary=_make_summary(-2.0),
        train_equity_curve=train_curve,
        test_equity_curve=test_curve,
        active_strategy_count=3,
        allocation_map={"strat_A": 0.33, "strat_B": 0.33, "strat_C": 0.34},
        selection_rows=[{"strategy_id": "strat_A", "score": 5.0}],
        allocation_rows=[{"strategy_id": "strat_A", "allocation_pct": 0.33}],
        regime_rows=[{"symbol": "BTCUSDT", "regime_pass": True}],
    )

    assert wr.window.index == 3
    assert wr.active_strategy_count == 3
    assert len(wr.allocation_map) == 3
    assert len(wr.selection_rows) == 1
    assert len(wr.allocation_rows) == 1
    assert len(wr.regime_rows) == 1
    assert wr.train_equity_curve.shape == (2, 1)
    assert wr.test_equity_curve.shape == (2, 1)


# ---------------------------------------------------------------------------
# 测试 8 —— 前进结果 总计的 包含 test_mean_pnl_pct
# ---------------------------------------------------------------------------


def test_walk_forward_result_aggregate() -> None:
    """WalkForwardResult.aggregate dict must contain test_mean_pnl_pct field."""
    engine = _make_engine()
    windows = [
        _make_window_result(1, is_pnl_pct=4.0, oos_pnl_pct=2.0),
        _make_window_result(2, is_pnl_pct=6.0, oos_pnl_pct=3.0),
    ]
    stability = engine._compute_stability(windows)
    aggregate = engine._build_aggregate(windows, selection_min_score=None)

    result = WalkForwardResult(
        portfolio_name="test_portfolio",
        windows=windows,
        stability=stability,
        aggregate=aggregate,
        stitched_test_equity=pd.DataFrame(),
    )

    assert "test_mean_pnl_pct" in result.aggregate
    assert result.aggregate["test_mean_pnl_pct"] == pytest.approx(2.5, abs=1e-3)
    assert result.portfolio_name == "test_portfolio"
    assert len(result.windows) == 2


# ---------------------------------------------------------------------------
# 测试9——空窗口→安全的StabilityReport，passed=False
# ---------------------------------------------------------------------------


def test_stability_empty_windows() -> None:
    """Empty window list → StabilityReport with window_count=0 and passed=False."""
    engine = _make_engine()
    report = engine._compute_stability([])

    assert report.window_count == 0
    assert report.passed is False
    assert report.consistency_rate == 0.0
    assert report.overfitting_score == 1.0
    assert report.is_oos_correlation == 0.0
    assert report.degradation_ratio == 0.0


# ---------------------------------------------------------------------------
# 测试 10 —— generate_windows 产生 正确的 数量
# ---------------------------------------------------------------------------


def test_generate_windows_count() -> None:
    """generate_windows() uses walkforward config and returns correct window list."""
    engine = _make_engine()
    windows = engine.generate_windows()

    # 2024-01-01 至 2024-12-31，train=6m，test=3m，step=3m → 2 个窗口
    assert len(windows) == 2
    assert windows[0].train_start == dt.date(2024, 1, 1)
    assert windows[0].test_start == dt.date(2024, 7, 1)
