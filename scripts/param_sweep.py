"""策略参数网格扫描.

支持多进程并行回测，并可配置样本内/样本外时间段。
扫描结果输出为 CSV 文件。
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import multiprocessing
import os
import time
from decimal import Decimal
from typing import Any

import pandas as pd
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.backtest.runner import BacktestConfig, BacktestRunner
from src.core.config import load_app_config
from src.core.enums import INTERVAL_TO_NAUTILUS, Interval
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy
from src.strategy.rsi_strategy import RSIStrategy, RSIStrategyConfig

# ---------- 扫描网格配置 ----------

EMA_GRID: dict[str, list[Any]] = {
    "fast_ema_period": [5, 8, 10, 15, 20],
    "slow_ema_period": [20, 30, 40, 50, 60],
    "atr_sl_multiplier": [None, 1.5, 2.0, 2.5],
    "atr_tp_multiplier": [None, 3.0, 4.0, 5.0],
    "entry_min_atr_ratio": [0.0, 0.0008, 0.0012, 0.0015, 0.0020, 0.0030],
    "signal_cooldown_bars": [0, 1, 2, 3, 5],
}

RSI_GRID: dict[str, list[Any]] = {
    "rsi_period": [7, 14, 21],
    "overbought_level": [70, 75, 80],
    "oversold_level": [20, 25, 30],
    "atr_sl_multiplier": [None, 1.5, 2.0, 2.5],
    "atr_tp_multiplier": [None, 3.0, 4.0, 5.0],
}

MICRO_SCALP_GRID: dict[str, list[Any]] = {  # ~4374 combos
    "fast_ema_period": [5, 8, 12],
    "slow_ema_period": [15, 21, 30],
    "rsi_period": [5, 7, 10],
    "trend_adx_threshold": [15.0, 18.0, 22.0],
    "atr_sl_multiplier": [0.3, 0.45, 0.6],
    "atr_tp_multiplier": [0.6, 0.8, 1.2],
    "maker_offset_ticks": [1, 2],
    "signal_cooldown_bars": [1, 2, 3],
}

VEGAS_TUNNEL_GRID: dict[str, list[Any]] = {  # ~6561 combos
    "fast_ema_period": [10, 12, 14],
    "slow_ema_period": [30, 36, 45],
    "signal_cooldown_bars": [2, 3, 5],
    "stop_atr_multiplier": [0.8, 1.0, 1.5],
    "tp_fib_1": [0.8, 1.0, 1.2],
    "tp_fib_2": [1.382, 1.618, 2.0],
    "tp_fib_3": [2.0, 2.618, 3.0],
    "atr_filter_min_ratio": [0.3, 0.5, 0.8],
}

TURTLE_GRID: dict[str, list[Any]] = {  # ~648 combos
    "entry_period": [15, 20, 25],
    "exit_period": [8, 10, 12],
    "atr_period": [14, 20],
    "stop_atr_multiplier": [1.5, 2.0, 2.5],
    "unit_add_atr_step": [0.3, 0.5, 0.8],
    "max_units": [2, 3, 4],
}

EMA_PULLBACK_GRID: dict[str, list[Any]] = {  # ~810 combos
    "fast_ema_period": [10, 20, 30],
    "slow_ema_period": [30, 50, 100],
    "pullback_atr_multiplier": [0.5, 1.0, 1.5],
    "adx_threshold": [25.0, 30.0, 35.0],
    "signal_cooldown_bars": [2, 3, 5],
}

# --------------------------------

# 固定基准配置
STARTING_BALANCE = 10_000
LEVERAGE = 10.0
TRADE_SIZE = Decimal("0.01")

# 手续费率（taker，用于成本调整计算）
_TAKER_FEE_RATE = 0.0004


def _generate_combinations(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """将参数字典转换为参数组合列表.

    Args:
        grid: Grid.
    """
    keys = list(grid.keys())
    values = [grid[k] for k in keys]
    combinations = itertools.product(*values)
    return [dict(zip(keys, combo, strict=True)) for combo in combinations]


def _grid_product(strategy_type: str, grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Generate task dicts for a strategy grid.

    Args:
        strategy_type: Strategy type identifier string.
        grid: Parameter grid to expand.

    Returns:
        List of task dicts with strategy_type set.
    """
    return [{"strategy_type": strategy_type, **combo} for combo in _generate_combinations(grid)]


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        if text.endswith("%"):
            text = text[:-1]
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _extract_metric(
    metric_spaces: list[dict[str, Any]],
    exact_keys: list[str],
    contains_any: list[str] | None = None,
) -> float | None:
    for space in metric_spaces:
        for key in exact_keys:
            if key in space:
                value = _to_float(space.get(key))
                if value is not None:
                    return value

    if contains_any is None:
        return None

    contains_any_lower = [needle.lower() for needle in contains_any]
    for space in metric_spaces:
        for key, raw_value in space.items():
            lower_key = str(key).lower()
            if any(needle in lower_key for needle in contains_any_lower):
                value = _to_float(raw_value)
                if value is not None:
                    return value

    return None


def _extract_stats(run_result: Any) -> dict[str, float]:
    result = run_result.result
    returns = result.stats_returns or {}

    pnls = result.stats_pnls or {}
    usdt_pnls: dict[str, Any] = {}
    if isinstance(pnls, dict):
        usdt_raw = pnls.get("USDT")
        if isinstance(usdt_raw, dict):
            usdt_pnls = usdt_raw

    spaces = [returns, usdt_pnls]

    sharpe = _extract_metric(
        spaces,
        exact_keys=["Sharpe Ratio (252 days)"],
        contains_any=["sharpe"],
    )
    sortino = _extract_metric(
        spaces,
        exact_keys=["Sortino Ratio (252 days)"],
        contains_any=["sortino"],
    )
    pnl_pct = _extract_metric(
        spaces,
        exact_keys=["PnL% (total)"],
        contains_any=["pnl%"],
    )
    win_rate = _extract_metric(
        spaces,
        exact_keys=["Win Rate"],
        contains_any=["win rate"],
    )
    profit_factor = _extract_metric(
        spaces,
        exact_keys=["Profit Factor"],
        contains_any=["profit factor"],
    )
    calmar = _extract_metric(
        spaces,
        exact_keys=["Calmar Ratio (252 days)"],
        contains_any=["calmar"],
    )

    volatility = _extract_metric(
        spaces,
        exact_keys=["Returns Volatility (252 days)", "Volatility (252 days)"],
        contains_any=["volatility"],
    )
    max_drawdown = _extract_metric(
        spaces,
        exact_keys=["Max Drawdown", "Max Drawdown (%)", "Maximum Drawdown"],
        contains_any=["max drawdown", "maximum drawdown"],
    )

    # Cost-adjusted PnL: try native fields first, then compute from fees
    pnl_after_costs = _extract_metric(
        spaces,
        exact_keys=["PnL (after costs)", "PnL After Costs", "Net PnL"],
        contains_any=["after costs", "after_costs", "net pnl"],
    )
    pnl_pct_after_costs_native = _extract_metric(
        spaces,
        exact_keys=["PnL% (after costs)", "PnL% After Costs", "Net PnL%"],
        contains_any=["pnl% after", "pnl_pct_after"],
    )

    # Compute cost-adjusted metrics if not available natively
    total_orders = int(getattr(result, "total_orders", 0) or 0)
    raw_pnl = _extract_metric(
        spaces,
        exact_keys=["PnL (total)"],
        contains_any=["pnl (total)", "total pnl"],
    )
    if raw_pnl is None:
        raw_pnl = (pnl_pct or 0.0) / 100.0 * STARTING_BALANCE

    estimated_fee_cost = total_orders * 2 * _TAKER_FEE_RATE * STARTING_BALANCE * (LEVERAGE / 100.0)

    if pnl_after_costs is None:
        pnl_after_costs = raw_pnl - estimated_fee_cost

    if pnl_pct_after_costs_native is None:
        pnl_pct_after_costs = round(pnl_after_costs / STARTING_BALANCE * 100.0, 4)
    else:
        pnl_pct_after_costs = round(pnl_pct_after_costs_native, 4)

    return {
        "Sharpe": round(sharpe or 0.0, 3),
        "Sortino": round(sortino or 0.0, 3),
        "PnL%": round(pnl_pct or 0.0, 2),
        "WinRate%": round(win_rate or 0.0, 2),
        "PF": round(profit_factor or 0.0, 3),
        "Calmar": round(calmar or 0.0, 3),
        "Volatility": round(abs(volatility or 0.0), 4),
        "MaxDrawdown": round(abs(max_drawdown or 0.0), 4),
        "PnLAfterCosts": round(pnl_after_costs, 2),
        "PnLPctAfterCosts": pnl_pct_after_costs,
    }


def _smooth_score(stats: dict[str, float], orders: int) -> float:
    """综合评分: 越高越好（高 Sharpe + 低波动 + 低回撤 + 适度交易频率）.

    Args:
        stats: Stats.
        orders: Orders.
    """
    return round(
        stats["Sharpe"]
        + (0.4 * stats["Calmar"])
        - (0.6 * stats["Volatility"])
        - (0.4 * stats["MaxDrawdown"])
        - (0.01 * orders),
        4,
    )


def run_single_backtest(task: dict[str, Any]) -> dict[str, Any]:
    """多进程执行单一参数回测.

    Args:
        task: Backtest task definition to execute.
    """
    symbol = str(task["symbol"])
    strategy_type = str(task["strategy_type"])
    start_date = dt.date.fromisoformat(str(task["start_date"]))
    end_date = dt.date.fromisoformat(str(task["end_date"]))
    interval_str = str(task.get("interval", "15m"))
    interval = Interval(interval_str)

    strategy_params = {
        k: v for k, v in task.items() if k not in {"symbol", "strategy_type", "start_date", "end_date", "interval"}
    }

    runner_config = BacktestConfig(
        start=start_date,
        end=end_date,
        symbols=[symbol],
        interval=interval,
        starting_balance_usdt=STARTING_BALANCE,
        leverage=LEVERAGE,
        bypass_logging=True,
    )

    app_config = load_app_config(env=os.getenv("ENV", "dev"))
    runner = BacktestRunner(app_config=app_config, backtest_config=runner_config)

    instrument_id = InstrumentId.from_str(f"{symbol}-PERP.BINANCE")
    nautilus_interval = INTERVAL_TO_NAUTILUS[interval]
    if interval == Interval.MINUTE_1:
        bar_type = BarType.from_str(f"{instrument_id}-{nautilus_interval}-LAST-EXTERNAL")
    else:
        bar_type = BarType.from_str(
            f"{instrument_id}-{nautilus_interval}-LAST-INTERNAL@1-MINUTE-EXTERNAL",
        )

    result_row: dict[str, Any] = {
        "symbol": symbol,
        "strategy_type": strategy_type,
        **strategy_params,
        "start_date": str(start_date),
        "end_date": str(end_date),
    }

    try:
        if strategy_type == "ema":
            strategy_cls = EMACrossStrategy
            strategy_config = EMACrossConfig(
                instrument_id=instrument_id,
                bar_type=bar_type,
                trade_size=TRADE_SIZE,
                fast_ema_period=int(strategy_params["fast_ema_period"]),
                slow_ema_period=int(strategy_params["slow_ema_period"]),
                atr_sl_multiplier=strategy_params.get("atr_sl_multiplier"),
                atr_tp_multiplier=strategy_params.get("atr_tp_multiplier"),
                entry_min_atr_ratio=float(strategy_params.get("entry_min_atr_ratio", 0.0)),
                signal_cooldown_bars=int(strategy_params.get("signal_cooldown_bars", 0)),
            )
        elif strategy_type == "rsi":
            strategy_cls = RSIStrategy
            strategy_config = RSIStrategyConfig(
                instrument_id=instrument_id,
                bar_type=bar_type,
                trade_size=TRADE_SIZE,
                rsi_period=int(strategy_params["rsi_period"]),
                overbought_level=float(strategy_params["overbought_level"]),
                oversold_level=float(strategy_params["oversold_level"]),
                atr_sl_multiplier=strategy_params.get("atr_sl_multiplier"),
                atr_tp_multiplier=strategy_params.get("atr_tp_multiplier"),
            )
        elif strategy_type == "micro_scalp":
            from src.strategy.micro_scalp import MicroScalpConfig, MicroScalpStrategy

            strategy_cls = MicroScalpStrategy
            strategy_config = MicroScalpConfig(
                instrument_id=instrument_id,
                bar_type=bar_type,
                trade_size=TRADE_SIZE,
                fast_ema_period=int(strategy_params["fast_ema_period"]),
                slow_ema_period=int(strategy_params["slow_ema_period"]),
                rsi_period=int(strategy_params["rsi_period"]),
                trend_adx_threshold=float(strategy_params["trend_adx_threshold"]),
                atr_sl_multiplier=float(strategy_params["atr_sl_multiplier"]),
                atr_tp_multiplier=float(strategy_params["atr_tp_multiplier"]),
                maker_offset_ticks=int(strategy_params["maker_offset_ticks"]),
                signal_cooldown_bars=int(strategy_params["signal_cooldown_bars"]),
            )
        elif strategy_type == "vegas_tunnel":
            from src.strategy.vegas_tunnel import VegasTunnelConfig, VegasTunnelStrategy

            strategy_cls = VegasTunnelStrategy
            strategy_config = VegasTunnelConfig(
                instrument_id=instrument_id,
                bar_type=bar_type,
                trade_size=TRADE_SIZE,
                fast_ema_period=int(strategy_params["fast_ema_period"]),
                slow_ema_period=int(strategy_params["slow_ema_period"]),
                signal_cooldown_bars=int(strategy_params["signal_cooldown_bars"]),
                stop_atr_multiplier=float(strategy_params["stop_atr_multiplier"]),
                tp_fib_1=float(strategy_params["tp_fib_1"]),
                tp_fib_2=float(strategy_params["tp_fib_2"]),
                tp_fib_3=float(strategy_params["tp_fib_3"]),
                atr_filter_min_ratio=float(strategy_params["atr_filter_min_ratio"]),
            )
        elif strategy_type == "turtle":
            from src.strategy.turtle import TurtleConfig, TurtleStrategy

            strategy_cls = TurtleStrategy
            strategy_config = TurtleConfig(
                instrument_id=instrument_id,
                bar_type=bar_type,
                trade_size=TRADE_SIZE,
                entry_period=int(strategy_params["entry_period"]),
                exit_period=int(strategy_params["exit_period"]),
                atr_period=int(strategy_params["atr_period"]),
                stop_atr_multiplier=float(strategy_params["stop_atr_multiplier"]),
                unit_add_atr_step=float(strategy_params["unit_add_atr_step"]),
                max_units=int(strategy_params["max_units"]),
            )
        elif strategy_type == "ema_pullback_atr":
            from src.strategy.ema_pullback_atr import EMAPullbackATRConfig, EMAPullbackATRStrategy

            strategy_cls = EMAPullbackATRStrategy
            strategy_config = EMAPullbackATRConfig(
                instrument_id=instrument_id,
                bar_type=bar_type,
                trade_size=TRADE_SIZE,
                fast_ema_period=int(strategy_params["fast_ema_period"]),
                slow_ema_period=int(strategy_params["slow_ema_period"]),
                pullback_atr_multiplier=float(strategy_params["pullback_atr_multiplier"]),
                adx_threshold=float(strategy_params["adx_threshold"]),
                signal_cooldown_bars=int(strategy_params["signal_cooldown_bars"]),
            )
        else:
            raise ValueError(f"Unknown strategy type: {strategy_type}")

        run_result = runner.run(strategy_cls, strategy_config)
        stats = _extract_stats(run_result)

        orders = int(run_result.result.total_orders)
        positions = int(run_result.result.total_positions)

        result_row.update(stats)
        result_row["Orders"] = orders
        result_row["Positions"] = positions
        result_row["SmoothScore"] = _smooth_score(stats, orders=orders)
        return result_row

    except Exception as exc:  # noqa: BLE001
        result_row.update(
            {
                "Sharpe": 0.0,
                "Sortino": 0.0,
                "PnL%": 0.0,
                "WinRate%": 0.0,
                "PF": 0.0,
                "Calmar": 0.0,
                "Volatility": 0.0,
                "MaxDrawdown": 0.0,
                "PnLAfterCosts": 0.0,
                "PnLPctAfterCosts": 0.0,
                "Orders": 0,
                "Positions": 0,
                "SmoothScore": -999.0,
                "Error": str(exc),
            }
        )
        return result_row


def save_and_print_results(
    results: list[dict[str, Any]],
    output_dir: str,
    file_name: str,
    top_n: int = 10,
) -> None:
    """保存 CSV 并打印 TopN.

    Args:
        results: Computed results to persist or display.
        output_dir: Output directory for generated artifacts.
        file_name: Output file name.
        top_n: Maximum number of top rows to include.
    """
    df = pd.DataFrame(results)
    if df.empty:
        print(f"\n⚠️ No rows for {file_name}")
        return

    sort_col = "SmoothScore" if "SmoothScore" in df.columns else "Sharpe"
    df = df.sort_values(by=sort_col, ascending=False).reset_index(drop=True)

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, file_name)
    df.to_csv(out_path, index=False)

    print("\n" + "=" * 80)
    print(f"  🏆 Top {top_n} (sorted by {sort_col})")
    print("=" * 80)

    display_cols = [
        c
        for c in (
            "symbol",
            "strategy_type",
            "Sharpe",
            "Calmar",
            "Volatility",
            "MaxDrawdown",
            "PnL%",
            "WinRate%",
            "PnLPctAfterCosts",
            "Orders",
            "SmoothScore",
        )
        if c in df.columns
    ]
    print(df[display_cols].head(top_n).to_string(index=True))

    print("-" * 80)
    print(f"  💾 Saved: {out_path} ({len(df)} rows)\n")


def _save_best_json(
    oos_results: list[dict[str, Any]],
    is_results_by_symbol: dict[str, list[dict[str, Any]]],
    output_dir: str,
    strategy_type: str,
    symbol: str,
    top_n: int = 10,
) -> None:
    """将最优 OOS 结果保存为 JSON 文件.

    Args:
        oos_results: OOS backtest results.
        is_results_by_symbol: IS results grouped by symbol.
        output_dir: Output directory for JSON artifacts.
        strategy_type: Strategy type identifier.
        symbol: Trading symbol.
        top_n: How many top results to save.
    """
    symbol_oos = [r for r in oos_results if r.get("symbol") == symbol and not r.get("Error")]
    if not symbol_oos:
        return

    symbol_oos_sorted = sorted(
        symbol_oos,
        key=lambda r: float(r.get("PnLPctAfterCosts", -999.0)),
        reverse=True,
    )

    is_lookup: dict[str, dict[str, Any]] = {}
    for row in is_results_by_symbol.get(symbol, []):
        key_params = {
            k: v
            for k, v in row.items()
            if k
            not in {
                "symbol",
                "strategy_type",
                "start_date",
                "end_date",
                "interval",
                "Sharpe",
                "Sortino",
                "PnL%",
                "WinRate%",
                "PF",
                "Calmar",
                "Volatility",
                "MaxDrawdown",
                "PnLAfterCosts",
                "PnLPctAfterCosts",
                "Orders",
                "Positions",
                "SmoothScore",
                "Error",
            }
        }
        key = json.dumps(key_params, sort_keys=True)
        is_lookup[key] = row

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"{strategy_type}_{symbol}_best.json")

    records: list[dict[str, Any]] = []
    for rank, oos_row in enumerate(symbol_oos_sorted[:top_n], 1):
        params = {
            k: v
            for k, v in oos_row.items()
            if k
            not in {
                "symbol",
                "strategy_type",
                "start_date",
                "end_date",
                "interval",
                "Sharpe",
                "Sortino",
                "PnL%",
                "WinRate%",
                "PF",
                "Calmar",
                "Volatility",
                "MaxDrawdown",
                "PnLAfterCosts",
                "PnLPctAfterCosts",
                "Orders",
                "Positions",
                "SmoothScore",
                "Error",
            }
        }
        key = json.dumps(params, sort_keys=True)
        is_row = is_lookup.get(key, {})

        oos_metrics = {
            k: oos_row.get(k, 0.0)
            for k in (
                "Sharpe",
                "Sortino",
                "PnL%",
                "WinRate%",
                "PF",
                "Calmar",
                "Volatility",
                "MaxDrawdown",
                "PnLAfterCosts",
                "PnLPctAfterCosts",
            )
        }
        is_metrics = {
            k: is_row.get(k, 0.0)
            for k in (
                "Sharpe",
                "Sortino",
                "PnL%",
                "WinRate%",
                "PF",
                "Calmar",
                "Volatility",
                "MaxDrawdown",
                "PnLAfterCosts",
                "PnLPctAfterCosts",
            )
        }

        records.append({"params": params, "is_metrics": is_metrics, "oos_metrics": oos_metrics, "rank": rank})

    with open(out_path, "w") as fh:
        json.dump(records, fh, indent=2)

    print(f"  💾 Saved JSON: {out_path} ({len(records)} entries)\n")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        argparse.Namespace: Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(description="策略参数网格扫描")
    parser.add_argument(
        "--strategy",
        choices=["all", "ema", "rsi", "micro_scalp", "vegas_tunnel", "turtle", "ema_pullback_atr"],
        default="ema",
    )
    parser.add_argument(
        "--interval",
        choices=["1m", "5m", "15m", "1h", "4h"],
        default="15m",
        help="K 线间隔（默认：15m）",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTCUSDT", "ETHUSDT"],
        help="扫描标的列表（默认：BTCUSDT ETHUSDT）",
    )
    parser.add_argument("--is-start", default="2024-07-01", help="In-sample 起始日期")
    parser.add_argument("--is-end", default="2024-10-31", help="In-sample 结束日期")
    parser.add_argument("--oos-start", default="2024-11-01", help="OOS 起始日期")
    parser.add_argument("--oos-end", default="2024-12-31", help="OOS 结束日期")
    parser.add_argument("--oos-top-k", type=int, default=10, help="每个 symbol 的 OOS 候选数量")
    parser.add_argument("--no-oos", action="store_true", help="跳过 OOS 验证")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4, help="并行进程数")
    parser.add_argument("--output-dir", default="experiments/sweep", help="结果输出目录")
    parser.add_argument("--top-n", type=int, default=20, help="排行表展示前 N 名")
    parser.add_argument(
        "--save",
        action="store_true",
        help="保存最优结果到 experiments/param_sweep/<strategy>_<symbol>_best.json",
    )

    return parser.parse_args()


def _build_sweep_tasks(args: argparse.Namespace) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []

    for symbol in args.symbols:
        if args.strategy in ("all", "ema"):
            for combo in _generate_combinations(EMA_GRID):
                tasks.append({"symbol": symbol, "strategy_type": "ema", **combo})

        if args.strategy in ("all", "rsi"):
            for combo in _generate_combinations(RSI_GRID):
                tasks.append({"symbol": symbol, "strategy_type": "rsi", **combo})

        if args.strategy in ("all", "micro_scalp"):
            tasks.extend([{"symbol": symbol, **t} for t in _grid_product("micro_scalp", MICRO_SCALP_GRID)])

        if args.strategy in ("all", "vegas_tunnel"):
            tasks.extend([{"symbol": symbol, **t} for t in _grid_product("vegas_tunnel", VEGAS_TUNNEL_GRID)])

        if args.strategy in ("all", "turtle"):
            tasks.extend([{"symbol": symbol, **t} for t in _grid_product("turtle", TURTLE_GRID)])

        if args.strategy in ("all", "ema_pullback_atr"):
            tasks.extend([{"symbol": symbol, **t} for t in _grid_product("ema_pullback_atr", EMA_PULLBACK_GRID)])

    return tasks


def _run_pool(
    tasks: list[dict[str, Any]],
    workers: int,
    label: str,
    interval: Interval,
) -> list[dict[str, Any]]:
    print("\n======================================================================")
    print(f"🔍 {label}")
    print("======================================================================")
    print(f"  Interval   : {interval.value}")
    print(f"  Total jobs : {len(tasks)}")
    print(f"  Workers    : {workers}")
    print("======================================================================")

    t0 = time.time()
    results: list[dict[str, Any]] = []

    with multiprocessing.Pool(processes=workers) as pool:
        for idx, res in enumerate(pool.imap_unordered(run_single_backtest, tasks), 1):
            results.append(res)
            if idx % 10 == 0 or idx == len(tasks):
                elapsed = time.time() - t0
                eta = elapsed / idx * (len(tasks) - idx)
                print(f"  [{idx:>4}/{len(tasks)}] done={idx}  elapsed={elapsed:.0f}s  ETA={eta:.0f}s")

    elapsed_total = time.time() - t0
    print(f"\n✅ {label} 完成！耗时 {elapsed_total:.1f}s")
    return results


def _build_oos_tasks_for_strategy(
    is_results: list[dict[str, Any]],
    strategy_type: str,
    param_keys: list[str],
    symbols: list[str],
    oos_start: str,
    oos_end: str,
    oos_top_k: int,
    interval_str: str,
) -> list[dict[str, Any]]:
    """Build OOS tasks for a specific strategy from IS results.

    Args:
        is_results: All IS results.
        strategy_type: Strategy type to filter by.
        param_keys: Parameter keys to carry forward.
        symbols: List of symbols to process.
        oos_start: OOS start date string.
        oos_end: OOS end date string.
        oos_top_k: Number of top IS candidates per symbol.
        interval_str: Interval string.

    Returns:
        List of OOS task dicts.
    """
    candidates = [r for r in is_results if r.get("strategy_type") == strategy_type and not r.get("Error")]
    if not candidates:
        return []

    oos_tasks: list[dict[str, Any]] = []
    for symbol in symbols:
        symbol_rows = [r for r in candidates if r.get("symbol") == symbol]
        symbol_rows.sort(
            key=lambda row: (float(row.get("SmoothScore", -999.0)), float(row.get("Sharpe", -999.0))),
            reverse=True,
        )
        for row in symbol_rows[:oos_top_k]:
            task: dict[str, Any] = {
                "symbol": symbol,
                "strategy_type": strategy_type,
                "start_date": oos_start,
                "end_date": oos_end,
                "interval": interval_str,
            }
            for key in param_keys:
                if key in row:
                    task[key] = row[key]
            oos_tasks.append(task)

    return oos_tasks


def main() -> None:
    """Run the script entrypoint."""
    args = parse_args()
    interval = Interval(args.interval)

    sweep_tasks = _build_sweep_tasks(args)

    # Inject interval into every task so run_single_backtest can read it
    is_tasks = [
        {
            **task,
            "start_date": args.is_start,
            "end_date": args.is_end,
            "interval": args.interval,
        }
        for task in sweep_tasks
    ]

    print(f"  In-sample  : {args.is_start} ~ {args.is_end}")
    is_results = _run_pool(is_tasks, workers=args.workers, label="参数网格扫描 (IS)", interval=interval)

    is_ema = [r for r in is_results if r.get("strategy_type") == "ema"]
    is_rsi = [r for r in is_results if r.get("strategy_type") == "rsi"]
    is_micro_scalp = [r for r in is_results if r.get("strategy_type") == "micro_scalp"]
    is_vegas_tunnel = [r for r in is_results if r.get("strategy_type") == "vegas_tunnel"]
    is_turtle = [r for r in is_results if r.get("strategy_type") == "turtle"]
    is_ema_pullback = [r for r in is_results if r.get("strategy_type") == "ema_pullback_atr"]

    if is_ema:
        save_and_print_results(is_ema, args.output_dir, "ema_sweep_is.csv", args.top_n)
    if is_rsi:
        save_and_print_results(is_rsi, args.output_dir, "rsi_sweep_is.csv", args.top_n)
    if is_micro_scalp:
        save_and_print_results(is_micro_scalp, args.output_dir, "micro_scalp_sweep_is.csv", args.top_n)
    if is_vegas_tunnel:
        save_and_print_results(is_vegas_tunnel, args.output_dir, "vegas_tunnel_sweep_is.csv", args.top_n)
    if is_turtle:
        save_and_print_results(is_turtle, args.output_dir, "turtle_sweep_is.csv", args.top_n)
    if is_ema_pullback:
        save_and_print_results(is_ema_pullback, args.output_dir, "ema_pullback_atr_sweep_is.csv", args.top_n)

    if args.no_oos:
        return

    # ---- OOS Phase ----
    oos_tasks: list[dict[str, Any]] = []

    oos_tasks.extend(
        _build_oos_tasks_for_strategy(
            is_results,
            "ema",
            [
                "fast_ema_period",
                "slow_ema_period",
                "atr_sl_multiplier",
                "atr_tp_multiplier",
                "entry_min_atr_ratio",
                "signal_cooldown_bars",
            ],
            args.symbols,
            args.oos_start,
            args.oos_end,
            args.oos_top_k,
            args.interval,
        )
    )
    oos_tasks.extend(
        _build_oos_tasks_for_strategy(
            is_results,
            "rsi",
            ["rsi_period", "overbought_level", "oversold_level", "atr_sl_multiplier", "atr_tp_multiplier"],
            args.symbols,
            args.oos_start,
            args.oos_end,
            args.oos_top_k,
            args.interval,
        )
    )
    oos_tasks.extend(
        _build_oos_tasks_for_strategy(
            is_results,
            "micro_scalp",
            [
                "fast_ema_period",
                "slow_ema_period",
                "rsi_period",
                "trend_adx_threshold",
                "atr_sl_multiplier",
                "atr_tp_multiplier",
                "maker_offset_ticks",
                "signal_cooldown_bars",
            ],
            args.symbols,
            args.oos_start,
            args.oos_end,
            args.oos_top_k,
            args.interval,
        )
    )
    oos_tasks.extend(
        _build_oos_tasks_for_strategy(
            is_results,
            "vegas_tunnel",
            [
                "fast_ema_period",
                "slow_ema_period",
                "signal_cooldown_bars",
                "stop_atr_multiplier",
                "tp_fib_1",
                "tp_fib_2",
                "tp_fib_3",
                "atr_filter_min_ratio",
            ],
            args.symbols,
            args.oos_start,
            args.oos_end,
            args.oos_top_k,
            args.interval,
        )
    )
    oos_tasks.extend(
        _build_oos_tasks_for_strategy(
            is_results,
            "turtle",
            ["entry_period", "exit_period", "atr_period", "stop_atr_multiplier", "unit_add_atr_step", "max_units"],
            args.symbols,
            args.oos_start,
            args.oos_end,
            args.oos_top_k,
            args.interval,
        )
    )
    oos_tasks.extend(
        _build_oos_tasks_for_strategy(
            is_results,
            "ema_pullback_atr",
            ["fast_ema_period", "slow_ema_period", "pullback_atr_multiplier", "adx_threshold", "signal_cooldown_bars"],
            args.symbols,
            args.oos_start,
            args.oos_end,
            args.oos_top_k,
            args.interval,
        )
    )

    if not oos_tasks:
        print("\n⚠️ OOS 任务为空，跳过 OOS。")
        return

    print(f"  OOS        : {args.oos_start} ~ {args.oos_end}")
    oos_results = _run_pool(oos_tasks, workers=args.workers, label="样本外验证 (OOS)", interval=interval)

    oos_ema = [r for r in oos_results if r.get("strategy_type") == "ema"]
    oos_rsi = [r for r in oos_results if r.get("strategy_type") == "rsi"]
    oos_micro_scalp = [r for r in oos_results if r.get("strategy_type") == "micro_scalp"]
    oos_vegas_tunnel = [r for r in oos_results if r.get("strategy_type") == "vegas_tunnel"]
    oos_turtle = [r for r in oos_results if r.get("strategy_type") == "turtle"]
    oos_ema_pullback = [r for r in oos_results if r.get("strategy_type") == "ema_pullback_atr"]

    if oos_ema:
        save_and_print_results(oos_ema, args.output_dir, "ema_sweep_oos.csv", args.top_n)
    if oos_rsi:
        save_and_print_results(oos_rsi, args.output_dir, "rsi_sweep_oos.csv", args.top_n)
    if oos_micro_scalp:
        save_and_print_results(oos_micro_scalp, args.output_dir, "micro_scalp_sweep_oos.csv", args.top_n)
    if oos_vegas_tunnel:
        save_and_print_results(oos_vegas_tunnel, args.output_dir, "vegas_tunnel_sweep_oos.csv", args.top_n)
    if oos_turtle:
        save_and_print_results(oos_turtle, args.output_dir, "turtle_sweep_oos.csv", args.top_n)
    if oos_ema_pullback:
        save_and_print_results(oos_ema_pullback, args.output_dir, "ema_pullback_atr_sweep_oos.csv", args.top_n)

    if args.save:
        save_dir = "experiments/param_sweep"
        is_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in is_results:
            sym = str(row.get("symbol", ""))
            is_by_symbol.setdefault(sym, []).append(row)

        for strategy_type, oos_rows in [
            ("ema", oos_ema),
            ("rsi", oos_rsi),
            ("micro_scalp", oos_micro_scalp),
            ("vegas_tunnel", oos_vegas_tunnel),
            ("turtle", oos_turtle),
            ("ema_pullback_atr", oos_ema_pullback),
        ]:
            if not oos_rows:
                continue
            for sym in args.symbols:
                _save_best_json(
                    oos_rows,
                    is_by_symbol,
                    save_dir,
                    strategy_type,
                    sym,
                    top_n=args.top_n,
                )


if __name__ == "__main__":
    main()
