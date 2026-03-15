"""Walk-forward 验证辅助工具."""

from __future__ import annotations

import calendar
import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class WalkforwardWindow:
    """滚动验证窗口."""

    index: int
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date


def add_months(value: dt.date, months: int) -> dt.date:
    """按月平移日期，超出天数时夹到月底."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(value.day, last_day))


def generate_walkforward_windows(
    start: dt.date,
    end: dt.date,
    train_months: int,
    test_months: int,
    step_months: int,
) -> list[WalkforwardWindow]:
    """生成滚动样本内/样本外窗口."""
    if train_months <= 0 or test_months <= 0 or step_months <= 0:
        raise ValueError("train_months/test_months/step_months 必须 > 0")
    if start > end:
        raise ValueError("start 不能大于 end")

    windows: list[WalkforwardWindow] = []
    cursor = start
    index = 1
    while True:
        train_end = add_months(cursor, train_months) - dt.timedelta(days=1)
        test_start = train_end + dt.timedelta(days=1)
        test_end = add_months(test_start, test_months) - dt.timedelta(days=1)
        if test_end > end:
            break

        windows.append(
            WalkforwardWindow(
                index=index,
                train_start=cursor,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        index += 1
        cursor = add_months(cursor, step_months)

    return windows


def flatten_summary(summary: dict[str, Any], phase: str, window_index: int) -> dict[str, Any]:
    """将回测 summary 扁平化为单行记录."""
    pnl = summary.get("pnl", {}).get("USDT", {})
    returns = summary.get("returns", {})
    costs = summary.get("analysis", {}).get("costs", {})
    metadata = summary.get("metadata", {})

    return {
        "phase": phase,
        "window_index": window_index,
        "period": summary.get("period"),
        "symbols": ",".join(summary.get("symbols", [])),
        "interval": summary.get("interval"),
        "strategy_names": ",".join(metadata.get("strategy_names", [])),
        "total_orders": summary.get("total_orders"),
        "total_positions": summary.get("total_positions"),
        "pnl_total": pnl.get("PnL (total)"),
        "pnl_pct": pnl.get("PnL% (total)"),
        "win_rate": pnl.get("Win Rate"),
        "profit_factor": returns.get("Profit Factor"),
        "sharpe": returns.get("Sharpe Ratio (252 days)"),
        "sortino": returns.get("Sortino Ratio (252 days)"),
        "pnl_after_costs": costs.get("pnl_after_costs"),
        "pnl_pct_after_costs": costs.get("pnl_pct_after_costs"),
        "modeled_slippage_cost": costs.get("modeled_slippage_cost"),
        "funding_cost": costs.get("funding_cost"),
    }


def scale_sizing_params(params: dict[str, Any], allocation_pct: float) -> dict[str, Any]:
    """按分配比例缩放 sizing 参数."""
    scaled = dict(params)
    ratio = max(0.0, allocation_pct)

    if "margin_pct_per_trade" in scaled and scaled["margin_pct_per_trade"] is not None:
        scaled["margin_pct_per_trade"] = float(scaled["margin_pct_per_trade"]) * ratio
    if "gross_exposure_pct_per_trade" in scaled and scaled["gross_exposure_pct_per_trade"] is not None:
        scaled["gross_exposure_pct_per_trade"] = float(scaled["gross_exposure_pct_per_trade"]) * ratio
    if "capital_pct_per_trade" in scaled and scaled["capital_pct_per_trade"] is not None:
        scaled["capital_pct_per_trade"] = float(scaled["capital_pct_per_trade"]) * ratio
    if ratio > 0 and all(
        scaled.get(key) in (None, 0) for key in (
            "margin_pct_per_trade",
            "gross_exposure_pct_per_trade",
            "capital_pct_per_trade",
        )
    ) and scaled.get("trade_size") is not None:
        scaled["trade_size"] = float(scaled["trade_size"]) * ratio

    return scaled


def selection_passes(score: float, min_score: float | None) -> bool:
    """判断候选分数是否通过窗口级别的 regime filter."""
    if min_score is None:
        return True
    return score >= min_score


def meets_min_active_strategies(active_count: int, min_active_strategies: int | None) -> bool:
    """判断组合级别是否满足最少激活腿数要求."""
    if min_active_strategies is None or min_active_strategies <= 0:
        return True
    return active_count >= min_active_strategies


def resolve_min_active_strategies(
    *,
    min_active_strategies: int | None,
    min_active_strategies_on_regime_veto: int | None,
    regime_veto_count: int,
) -> int | None:
    """当窗口存在 regime veto 时，允许使用更低的组合级最少激活腿数."""
    if regime_veto_count <= 0 or min_active_strategies_on_regime_veto is None:
        return min_active_strategies
    if min_active_strategies is None:
        return min_active_strategies_on_regime_veto
    return min(min_active_strategies, min_active_strategies_on_regime_veto)


def score_weight(score: float, method: str = "none") -> float:
    """将训练期分数映射为分配权重系数."""
    normalized = max(0.0, score)
    if method == "none":
        return 1.0
    if normalized <= 0:
        return 0.0
    if method == "linear":
        return normalized
    if method == "sqrt":
        return math.sqrt(normalized)
    if method == "log1p":
        return math.log1p(normalized)
    raise ValueError(f"unsupported score weighting method: {method}")


def combine_risk_score_weight(
    *,
    volatility: float,
    score: float,
    score_weighting_method: str = "none",
) -> float:
    """将风险平价和训练分数组合为单一权重."""
    vol = volatility if volatility > 0 else 1.0
    return (1.0 / vol) * score_weight(score, method=score_weighting_method)


def stitch_equity_curves(curves: list[pd.DataFrame], starting_balance: int) -> pd.DataFrame:
    """将多段样本外权益曲线按资金续接拼成一条总曲线."""
    stitched_parts: list[pd.DataFrame] = []
    capital = float(starting_balance)

    for curve in curves:
        if curve.empty:
            continue

        part = curve.copy()
        base = float(part["equity"].iloc[0])
        if base <= 0:
            continue

        part["stitched_equity"] = capital * (part["equity"] / base)
        capital = float(part["stitched_equity"].iloc[-1])
        stitched_parts.append(part)

    if not stitched_parts:
        return pd.DataFrame(columns=["phase", "window_index", "step", "timestamp", "equity", "stitched_equity"])

    return pd.concat(stitched_parts, ignore_index=True)
