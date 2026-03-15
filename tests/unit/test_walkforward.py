from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from src.backtest.walkforward import (
    add_months,
    combine_risk_score_weight,
    flatten_summary,
    generate_walkforward_windows,
    meets_min_active_strategies,
    resolve_min_active_strategies,
    scale_sizing_params,
    score_weight,
    selection_passes,
    stitch_equity_curves,
)


def test_add_months_clamps_to_month_end() -> None:
    assert add_months(dt.date(2024, 1, 31), 1) == dt.date(2024, 2, 29)


def test_generate_walkforward_windows_rolls_forward() -> None:
    windows = generate_walkforward_windows(
        start=dt.date(2024, 1, 1),
        end=dt.date(2024, 12, 31),
        train_months=6,
        test_months=3,
        step_months=3,
    )

    assert len(windows) == 2
    assert windows[0].train_start == dt.date(2024, 1, 1)
    assert windows[0].test_start == dt.date(2024, 7, 1)
    assert windows[1].train_start == dt.date(2024, 4, 1)
    assert windows[1].test_end == dt.date(2024, 12, 31)


def test_flatten_summary_extracts_cost_adjusted_metrics() -> None:
    row = flatten_summary(
        summary={
            "period": "2024-01-01 ~ 2024-03-31",
            "symbols": ["BTCUSDT"],
            "interval": "1h",
            "total_orders": 10,
            "total_positions": 5,
            "pnl": {"USDT": {"PnL (total)": 100.0, "PnL% (total)": 1.0, "Win Rate": 0.5}},
            "returns": {"Profit Factor": 1.2, "Sharpe Ratio (252 days)": 1.5, "Sortino Ratio (252 days)": 2.0},
            "analysis": {"costs": {"pnl_after_costs": 90.0, "pnl_pct_after_costs": 0.9, "funding_cost": 2.0}},
            "metadata": {"strategy_names": ["VegasTunnelStrategy", "EMAPullbackATRStrategy"]},
        },
        phase="test",
        window_index=1,
    )

    assert row["phase"] == "test"
    assert row["window_index"] == 1
    assert row["strategy_names"] == "VegasTunnelStrategy,EMAPullbackATRStrategy"
    assert row["pnl_pct_after_costs"] == 0.9


def test_scale_sizing_params_scales_margin_pct() -> None:
    scaled = scale_sizing_params(
        {"margin_pct_per_trade": 10.0, "trade_size": 0.01},
        allocation_pct=0.25,
    )

    assert scaled["margin_pct_per_trade"] == 2.5


def test_scale_sizing_params_scales_trade_size_when_no_pct_fields() -> None:
    scaled = scale_sizing_params(
        {"trade_size": 0.02},
        allocation_pct=0.5,
    )

    assert scaled["trade_size"] == 0.01


def test_selection_passes_respects_min_score() -> None:
    assert selection_passes(1.0, 0.0) is True
    assert selection_passes(-0.1, 0.0) is False
    assert selection_passes(-5.0, None) is True


def test_meets_min_active_strategies_respects_threshold() -> None:
    assert meets_min_active_strategies(2, 2) is True
    assert meets_min_active_strategies(1, 2) is False
    assert meets_min_active_strategies(0, None) is True


def test_resolve_min_active_strategies_uses_relaxed_gate_only_when_veto_exists() -> None:
    assert resolve_min_active_strategies(
        min_active_strategies=2,
        min_active_strategies_on_regime_veto=1,
        regime_veto_count=1,
    ) == 1
    assert resolve_min_active_strategies(
        min_active_strategies=2,
        min_active_strategies_on_regime_veto=1,
        regime_veto_count=0,
    ) == 2
    assert resolve_min_active_strategies(
        min_active_strategies=None,
        min_active_strategies_on_regime_veto=1,
        regime_veto_count=2,
    ) == 1


def test_score_weight_supports_multiple_methods() -> None:
    assert score_weight(9.0, "none") == 1.0
    assert score_weight(9.0, "linear") == 9.0
    assert score_weight(9.0, "sqrt") == 3.0
    assert round(score_weight(9.0, "log1p"), 6) == round(math.log1p(9.0), 6)
    assert score_weight(-1.0, "sqrt") == 0.0


def test_combine_risk_score_weight_multiplies_inverse_vol_and_score_weight() -> None:
    weight = combine_risk_score_weight(volatility=2.0, score=9.0, score_weighting_method="sqrt")
    assert weight == 1.5


def test_stitch_equity_curves_rolls_capital_forward() -> None:
    first = pd.DataFrame({"equity": [10000.0, 11000.0], "phase": ["test", "test"], "window_index": [1, 1]})
    second = pd.DataFrame({"equity": [10000.0, 9000.0], "phase": ["test", "test"], "window_index": [2, 2]})

    stitched = stitch_equity_curves([first, second], starting_balance=10_000)

    assert float(stitched["stitched_equity"].iloc[0]) == 10_000.0
    assert float(stitched["stitched_equity"].iloc[1]) == 11_000.0
    assert float(stitched["stitched_equity"].iloc[-1]) == 9_900.0
