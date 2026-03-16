"""策略参数网格扫描.

支持多进程并行回测，并可配置样本内/样本外时间段。
扫描结果输出为 CSV 文件。
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
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

# --------------------------------

# 固定基准配置
INTERVAL = Interval.MINUTE_15
STARTING_BALANCE = 10_000
LEVERAGE = 10.0
TRADE_SIZE = Decimal("0.01")


def _generate_combinations(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """将参数字典转换为参数组合列表.

    Args:
        grid: Grid.
    """
    keys = list(grid.keys())
    values = [grid[k] for k in keys]
    combinations = itertools.product(*values)
    return [dict(zip(keys, combo, strict=True)) for combo in combinations]


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

    return {
        "Sharpe": round(sharpe or 0.0, 3),
        "Sortino": round(sortino or 0.0, 3),
        "PnL%": round(pnl_pct or 0.0, 2),
        "WinRate%": round(win_rate or 0.0, 2),
        "PF": round(profit_factor or 0.0, 3),
        "Calmar": round(calmar or 0.0, 3),
        "Volatility": round(abs(volatility or 0.0), 4),
        "MaxDrawdown": round(abs(max_drawdown or 0.0), 4),
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

    strategy_params = {k: v for k, v in task.items() if k not in {"symbol", "strategy_type", "start_date", "end_date"}}

    runner_config = BacktestConfig(
        start=start_date,
        end=end_date,
        symbols=[symbol],
        interval=INTERVAL,
        starting_balance_usdt=STARTING_BALANCE,
        leverage=LEVERAGE,
        bypass_logging=True,
    )

    app_config = load_app_config(env=os.getenv("ENV", "dev"))
    runner = BacktestRunner(app_config=app_config, backtest_config=runner_config)

    instrument_id = InstrumentId.from_str(f"{symbol}-PERP.BINANCE")
    nautilus_interval = INTERVAL_TO_NAUTILUS[INTERVAL]
    if INTERVAL == Interval.MINUTE_1:
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
            "Orders",
            "SmoothScore",
        )
        if c in df.columns
    ]
    print(df[display_cols].head(top_n).to_string(index=True))

    print("-" * 80)
    print(f"  💾 Saved: {out_path} ({len(df)} rows)\n")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        argparse.Namespace: Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(description="策略参数网格扫描")
    parser.add_argument("--strategy", choices=["all", "ema", "rsi"], default="ema")
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

    return parser.parse_args()


def _build_sweep_tasks(args: argparse.Namespace) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []

    for symbol in args.symbols:
        if args.strategy in ["all", "ema"]:
            for combo in _generate_combinations(EMA_GRID):
                tasks.append({"symbol": symbol, "strategy_type": "ema", **combo})

        if args.strategy in ["all", "rsi"]:
            for combo in _generate_combinations(RSI_GRID):
                tasks.append({"symbol": symbol, "strategy_type": "rsi", **combo})

    return tasks


def _run_pool(tasks: list[dict[str, Any]], workers: int, label: str) -> list[dict[str, Any]]:
    print("\n======================================================================")
    print(f"🔍 {label}")
    print("======================================================================")
    print(f"  Interval   : {INTERVAL.value}")
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


def main() -> None:
    """Run the script entrypoint."""
    args = parse_args()

    sweep_tasks = _build_sweep_tasks(args)
    is_tasks = [
        {
            **task,
            "start_date": args.is_start,
            "end_date": args.is_end,
        }
        for task in sweep_tasks
    ]

    print(f"  In-sample  : {args.is_start} ~ {args.is_end}")
    is_results = _run_pool(is_tasks, workers=args.workers, label="参数网格扫描 (IS)")

    is_ema = [r for r in is_results if r.get("strategy_type") == "ema"]
    is_rsi = [r for r in is_results if r.get("strategy_type") == "rsi"]

    if is_ema:
        save_and_print_results(is_ema, args.output_dir, "ema_sweep_is.csv", args.top_n)
    if is_rsi:
        save_and_print_results(is_rsi, args.output_dir, "rsi_sweep_is.csv", args.top_n)

    if args.no_oos:
        return

    ema_candidates = [row for row in is_ema if not row.get("Error")]
    if not ema_candidates:
        print("\n⚠️ 无可用 EMA IS 候选，跳过 OOS。")
        return

    oos_tasks: list[dict[str, Any]] = []
    for symbol in args.symbols:
        symbol_rows = [row for row in ema_candidates if row.get("symbol") == symbol]
        symbol_rows.sort(
            key=lambda row: (float(row.get("SmoothScore", -999.0)), float(row.get("Sharpe", -999.0))),
            reverse=True,
        )

        top_rows = symbol_rows[: args.oos_top_k]
        for row in top_rows:
            oos_tasks.append(
                {
                    "symbol": symbol,
                    "strategy_type": "ema",
                    "start_date": args.oos_start,
                    "end_date": args.oos_end,
                    "fast_ema_period": row["fast_ema_period"],
                    "slow_ema_period": row["slow_ema_period"],
                    "atr_sl_multiplier": row["atr_sl_multiplier"],
                    "atr_tp_multiplier": row["atr_tp_multiplier"],
                    "entry_min_atr_ratio": row["entry_min_atr_ratio"],
                    "signal_cooldown_bars": row["signal_cooldown_bars"],
                }
            )

    if not oos_tasks:
        print("\n⚠️ OOS 任务为空，跳过 OOS。")
        return

    print(f"  OOS        : {args.oos_start} ~ {args.oos_end}")
    oos_results = _run_pool(oos_tasks, workers=args.workers, label="样本外验证 (OOS)")
    save_and_print_results(oos_results, args.output_dir, "ema_sweep_oos.csv", args.top_n)


if __name__ == "__main__":
    main()
