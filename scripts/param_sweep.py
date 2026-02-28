"""策略参数网格扫描.

支持多进程并行回测, 并可配置样本内/样本外时间段.
扫描结果输出为 CSV 文件.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import multiprocessing
import os
import shutil
import time
from decimal import Decimal
from typing import Any

import pandas as pd
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Money

from src.core.config import load_app_config
from src.backtest.runner import BacktestRunner, BacktestConfig
from src.core.enums import INTERVAL_TO_NAUTILUS, Interval
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy
from src.strategy.rsi_strategy import RSIStrategyConfig, RSIStrategy


# ---------- 扫描网格配置 ----------

EMA_GRID: dict[str, list[Any]] = {
    "fast_ema_period": [5, 8, 10, 15, 20],
    "slow_ema_period": [20, 30, 40, 50, 60],
    "atr_sl_multiplier": [None, 1.5, 2.0, 2.5],
    "atr_tp_multiplier": [None, 3.0, 4.0, 5.0],
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
SYMBOL = "BTCUSDT"
INTERVAL = Interval.MINUTE_15
STARTING_BALANCE = 10_000
LEVERAGE = 10.0
TRADE_SIZE = Decimal("0.01")


def _generate_combinations(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """将参数字典转换为参数组合列表."""
    keys = grid.keys()
    values = grid.values()
    combinations = list(itertools.product(*values))
    return [dict(zip(keys, combo)) for combo in combinations]


def run_single_backtest(params: dict[str, Any]) -> dict[str, Any]:
    """多进程执行单一参数回测.

    这里不使用 Logging 配置，以免终端日志交叉混乱。
    返回一个包含参数和 PnL 统计的一维字典。
    """
    start_date = dt.datetime.strptime(params.pop("start_date"), "%Y-%m-%d").date()
    end_date = dt.datetime.strptime(params.pop("end_date"), "%Y-%m-%d").date()

    runner_config = BacktestConfig(
        start=start_date,
        end=end_date,
        symbols=[SYMBOL],
        interval=INTERVAL,
        starting_balance_usdt=STARTING_BALANCE,
        leverage=LEVERAGE,
        bypass_logging=True,  # 屏蔽大量单次回测的日志输出
    )
    app_config = load_app_config(env=os.getenv("ENV", "dev"))
    runner = BacktestRunner(app_config=app_config, backtest_config=runner_config)

    instrument_id = InstrumentId.from_str(f"{SYMBOL}-PERP.BINANCE")
    nautilus_interval = INTERVAL_TO_NAUTILUS[INTERVAL]
    bar_type = BarType.from_str(f"{instrument_id}-{nautilus_interval}-LAST-EXTERNAL")

    strategy_type = params.pop("strategy_type")

    try:
        if strategy_type == "ema":
            strategy_cls = EMACrossStrategy
            strategy_config = EMACrossConfig(
                instrument_id=instrument_id,
                bar_type=bar_type,
                trade_size=TRADE_SIZE,
                fast_ema_period=params["fast_ema_period"],
                slow_ema_period=params["slow_ema_period"],
                atr_sl_multiplier=params.get("atr_sl_multiplier"),
                atr_tp_multiplier=params.get("atr_tp_multiplier"),
            )
        elif strategy_type == "rsi":
            strategy_cls = RSIStrategy
            strategy_config = RSIStrategyConfig(
                instrument_id=instrument_id,
                bar_type=bar_type,
                trade_size=TRADE_SIZE,
                rsi_period=params["rsi_period"],
                overbought_level=params["overbought_level"],
                oversold_level=params["oversold_level"],
                atr_sl_multiplier=params.get("atr_sl_multiplier"),
                atr_tp_multiplier=params.get("atr_tp_multiplier"),
            )
        else:
            raise ValueError(f"Unknown strategy type: {strategy_type}")

        run_result = runner.run(strategy_cls, strategy_config)

    # run_result.result is of type nautilus_trader.backtest.results.BacktestResult
        print("Extracting stats for run:", params)
        res = params.copy()
        try:
             pnls = run_result.result.stats_pnls or {}
             returns = run_result.result.stats_returns or {}
             stats = {**pnls, **returns}
        except Exception as e:
             stats = {}
             
        # Normalize keys formatting when printing (or converting to string)
        res["Sharpe"] = round(float(stats.get("Sharpe Ratio (252 days)", 0.0)), 3)
        res["Sortino"] = round(float(stats.get("Sortino Ratio (252 days)", 0.0)), 3)
        
        # PnL% can be formatted string
        pnl = stats.get("PnL (total)", 0.0)
        pnl_pct = stats.get("PnL% (total)", pnl) # Fallback to absolute if % is missing
        if isinstance(pnl_pct, str):
             pnl_pct = float(pnl_pct.replace('%', ''))
        res["PnL%"] = round(float(pnl_pct), 2)
        
        wr = stats.get("Win Rate", 0.0)
        if isinstance(wr, str):
             wr = float(wr.replace('%', '')) / 100.0  # Just in case
        res["WinRate%"] = round(float(wr) * 100, 1)
        
        res["PF"] = round(float(stats.get("Profit Factor", 0.0)), 3)
        
        # From raw nautilus orders/positions lists on the result
        res["Orders"] = len(run_result.reports["orders"]) if "orders" in run_result.reports else 0
        res["Positions"] = len(run_result.reports["positions"]) if "positions" in run_result.reports else 0

        return res

    except Exception as e:
        # 有些组合可能没有开出一单，或者是极端的指标报错，记录异常但不崩溃
        res = params.copy()
        res["Sharpe"] = 0.0
        res["PnL%"] = 0.0
        res["Orders"] = 0
        res["Error"] = str(e)
        return res


def save_and_print_results(
    results: list[dict[str, Any]], output_dir: str, file_name: str, top_n: int = 10
) -> None:
    """保存 CSV 并打印 TopN."""
    df = pd.DataFrame(results)
    
    # 根据跑出的结果，按夏普比率倒序
    if "Sharpe" in df.columns:
        df = df.sort_values(by="Sharpe", ascending=False).reset_index(drop=True)

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, file_name)
    df.to_csv(out_path, index=False)

    print("\n" + "=" * 80)
    print(f"  🏆 Top {top_n}  (sorted by Sharpe)")
    print("=" * 80)
    
    # 打印前 N 名（处理控制台对齐）
    display_cols = [c for c in df.columns if c not in ["Error"]]
    print(df[display_cols].head(top_n).to_string(index=True))

    print("-" * 80)
    print(f"  💾 Saved: {out_path} ({len(df)} rows)\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="策略参数网格扫描")
    parser.add_argument("--strategy", choices=["all", "ema", "rsi"], default="ema")
    parser.add_argument("--is-start", default="2024-01-01", help="In-sample 起始日期")
    parser.add_argument("--is-end", default="2024-03-31", help="In-sample 结束日期")
    parser.add_argument("--oos-start", default="2024-04-01", help="OOS 起始日期")
    parser.add_argument("--oos-end", default="2024-05-31", help="OOS 结束日期")
    parser.add_argument("--no-oos", action="store_true", help="跳过 OOS 验证")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4, help="并行进程数")
    parser.add_argument("--output-dir", default="experiments/sweep", help="结果输出目录")
    parser.add_argument("--top-n", type=int, default=20, help="排行表展示前 N 名")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    sweep_tasks = []
    
    if args.strategy in ["all", "ema"]:
        combos = _generate_combinations(EMA_GRID)
        for c in combos:
            c_copy = c.copy()
            c_copy["strategy_type"] = "ema"
            sweep_tasks.append(c_copy)
            
    if args.strategy in ["all", "rsi"]:
        combos = _generate_combinations(RSI_GRID)
        for c in combos:
            c_copy = c.copy()
            c_copy["strategy_type"] = "rsi"
            sweep_tasks.append(c_copy)

    # ---------- In-Sample 扫描 ----------
    is_tasks = []
    for t in sweep_tasks:
        task = t.copy()
        task["start_date"] = args.is_start
        task["end_date"] = args.is_end
        is_tasks.append(task)

    print(f"\n======================================================================")
    print(f"🔍 参数网格扫描 (IS)")
    print(f"======================================================================")
    print(f"  In-sample  : {args.is_start} ~ {args.is_end}")
    print(f"  Interval   : {INTERVAL.value}")
    print(f"  Total jobs : {len(is_tasks)}")
    print(f"  Workers    : {args.workers}")
    print(f"======================================================================")

    t0 = time.time()
    is_results = []
    with multiprocessing.Pool(processes=args.workers) as pool:
        for i, res in enumerate(pool.imap_unordered(run_single_backtest, is_tasks), 1):
            is_results.append(res)
            
            # Simple progress reporting
            if i % 10 == 0 or i == len(is_tasks):
                elapsed = time.time() - t0
                eta = elapsed / i * (len(is_tasks) - i)
                print(f"  [{i:>4}/{len(is_tasks)}] done={i}  elapsed={elapsed:.0f}s  ETA={eta:.0f}s")
                
    elapsed_is = time.time() - t0
    print(f"\n✅ IS 完成！耗时 {elapsed_is:.1f}s")

    # 分离 ema 和 rsi 结果
    is_ema = [r for r in is_results if r.get("strategy_type") == "ema"]
    is_rsi = [r for r in is_results if r.get("strategy_type") == "rsi"]

    if is_ema:
        save_and_print_results(is_ema, args.output_dir, "ema_sweep_is.csv", args.top_n)
    if is_rsi:
        save_and_print_results(is_rsi, args.output_dir, "rsi_sweep_is.csv", args.top_n)

    if args.no_oos:
        return

    # ---------- Out-of-Sample 验证 ----------
    print(f"\n======================================================================")
    print(f"🎯 样本外验证 (OOS)")
    print(f"======================================================================")
    print(f"  OOS        : {args.oos_start} ~ {args.oos_end}")
    
    # Picking top 10 from EMA IS
    is_ema.sort(key=lambda x: x.get("Sharpe", -999.0), reverse=True)
    top_is_ema = is_ema[:10]
    
    oos_tasks = []
    for res in top_is_ema:
        t = {
            "strategy_type": "ema",
            "start_date": args.oos_start,
            "end_date": args.oos_end,
            "fast_ema_period": res["fast_ema_period"],
            "slow_ema_period": res["slow_ema_period"],
            "atr_sl_multiplier": res["atr_sl_multiplier"],
            "atr_tp_multiplier": res["atr_tp_multiplier"],
        }
        oos_tasks.append(t)

    t0 = time.time()
    oos_results = []
    with multiprocessing.Pool(processes=args.workers) as pool:
        for res in pool.imap_unordered(run_single_backtest, oos_tasks):
            oos_results.append(res)
            
    elapsed_oos = time.time() - t0
    print(f"✅ OOS 完成！耗时 {elapsed_oos:.1f}s")
    
    if oos_results:
        save_and_print_results(oos_results, args.output_dir, "ema_sweep_oos.csv", args.top_n)


if __name__ == "__main__":
    main()
