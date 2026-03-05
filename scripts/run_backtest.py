#!/usr/bin/env python3
"""回测运行脚本.

从 ParquetDataCatalog 加载历史数据，运行指定策略的回测，并输出报告。

Usage:
    # 使用默认参数（BTCUSDT，1m，2024-01-01 ~ 2024-03-31）
    python scripts/run_backtest.py

    # 自定义时间段和 symbol
    python scripts/run_backtest.py \\
        --symbols BTCUSDT ETHUSDT \\
        --start 2023-01-01 \\
        --end 2023-12-31 \\
        --interval 1m

    # 指定初始资金和杠杆
    python scripts/run_backtest.py \\
        --balance 50000 \\
        --leverage 5

    # 保存报告到指定目录
    python scripts/run_backtest.py --save --output-dir experiments/reports/my_run
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.backtest.report import BacktestReporter
from src.backtest.runner import BacktestConfig, BacktestRunner
from src.core.config import load_app_config
from src.core.enums import INTERVAL_TO_NAUTILUS, Interval
from src.core.logging import setup_logging
from src.strategy.base import BaseStrategy, BaseStrategyConfig
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy
from src.strategy.ema_pullback_atr import EMAPullbackATRConfig, EMAPullbackATRStrategy
from src.strategy.micro_scalp import MicroScalpConfig, MicroScalpStrategy
from src.strategy.turtle import TurtleConfig, TurtleStrategy
from src.strategy.vegas_tunnel import VegasTunnelConfig, VegasTunnelStrategy


def parse_args() -> argparse.Namespace:
    """解析命令行参数.

    Returns:
        argparse.Namespace: 包含以下字段：
            - symbols (list[str]): 交易对列表，默认 ["BTCUSDT"]。
            - start (str): 回测起始日期，格式 YYYY-MM-DD。
            - end (str): 回测结束日期，格式 YYYY-MM-DD。
            - interval (str): K 线周期，默认 "1m"。
            - balance (int): 初始账户余额（USDT），默认 10000。
            - leverage (float): 账户杠杆，默认 10.0。
            - fast_ema (int): 快线 EMA 周期，默认 10。
            - slow_ema (int): 慢线 EMA 周期，默认 20。
            - save (bool): 是否保存报告到文件。
            - output_dir (str | None): 报告输出目录。
            - env (str | None): 配置环境标识。
    """
    parser = argparse.ArgumentParser(description="NautilusTrader 回测运行脚本")

    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT"], help="交易对列表（默认：BTCUSDT）")
    parser.add_argument(
        "--strategy",
        choices=["ema_cross", "ema_pullback_atr", "turtle", "micro_scalp", "vegas_tunnel"],
        default="ema_cross",
        help="策略类型（默认：ema_cross）",
    )
    parser.add_argument("--start", default="2024-01-01", help="回测起始日期 YYYY-MM-DD（默认：2024-01-01）")
    parser.add_argument("--end", default="2024-03-31", help="回测结束日期 YYYY-MM-DD（默认：2024-03-31）")
    parser.add_argument(
        "--interval",
        default=None,
        choices=[i.value for i in Interval],
        help="K 线周期（默认：除 Vegas 外为 1m，Vegas 为 1h）",
    )
    parser.add_argument("--balance", type=int, default=10_000, help="初始余额 USDT（默认：10000）")
    parser.add_argument("--leverage", type=float, default=10.0, help="账户杠杆（默认：10.0）")
    parser.add_argument(
        "--trade-size",
        type=str,
        default="0.01",
        help="固定下单数量（币数，默认：0.01；当设置 capital_pct_per_trade 时作为回退值）",
    )
    parser.add_argument(
        "--capital-pct-per-trade",
        type=float,
        default=None,
        help="按账户总权益百分比下单（0-100），设置后优先于 trade_size",
    )
    parser.add_argument("--fast-ema", type=int, default=10, help="快线 EMA 周期（默认：10）")
    parser.add_argument("--slow-ema", type=int, default=20, help="慢线 EMA 周期（默认：20）")
    parser.add_argument(
        "--pullback-atr-multiplier",
        type=float,
        default=1.0,
        help="EMA 回撤策略的 ATR 回撤倍数（默认：1.0）",
    )
    parser.add_argument(
        "--adx-period",
        type=int,
        default=14,
        help="EMA 回撤策略 ADX 周期（默认：14）",
    )
    parser.add_argument(
        "--adx-threshold",
        type=float,
        default=20.0,
        help="EMA 回撤策略 ADX 阈值（默认：20.0，<=0 表示关闭）",
    )
    parser.add_argument(
        "--min-trend-gap-ratio",
        type=float,
        default=0.0005,
        help="EMA 回撤策略最小趋势间距比例 |fast-slow|/close（默认：0.0005）",
    )
    parser.add_argument(
        "--entry-min-atr-ratio",
        type=float,
        default=0.0015,
        help="信号过滤最小 ATR/Close 比例（默认：0.0015，<=0 表示关闭）",
    )
    parser.add_argument(
        "--signal-cooldown-bars",
        type=int,
        default=3,
        help="信号冷却 Bar 数（默认：3，<=0 表示关闭；两个 EMA 策略均生效）",
    )
    parser.add_argument("--entry-period", type=int, default=20, help="海龟策略入场通道周期（默认：20）")
    parser.add_argument("--exit-period", type=int, default=10, help="海龟策略出场通道周期（默认：10）")
    parser.add_argument("--turtle-atr-period", type=int, default=20, help="海龟策略 ATR 周期（默认：20）")
    parser.add_argument(
        "--stop-atr-multiplier",
        type=float,
        default=2.0,
        help="海龟策略止损 ATR 乘数（默认：2.0）",
    )
    parser.add_argument(
        "--unit-add-atr-step",
        type=float,
        default=0.5,
        help="海龟策略加仓阶梯（ATR 倍数，默认：0.5）",
    )
    parser.add_argument("--max-units", type=int, default=4, help="海龟策略最大分批单位数（默认：4）")
    parser.add_argument("--atr-sl", type=float, default=None, help="ATR 止损乘数 (如: 2.0，默认: None)")
    parser.add_argument("--atr-tp", type=float, default=None, help="ATR 止盈乘数 (如: 4.0，默认: None)")
    parser.add_argument("--rsi-period", type=int, default=7, help="MicroScalp RSI 周期（默认：7）")
    parser.add_argument("--oversold-level", type=float, default=24.0, help="MicroScalp RSI 超卖阈值（默认：24）")
    parser.add_argument(
        "--overbought-level",
        type=float,
        default=76.0,
        help="MicroScalp RSI 超买阈值（默认：76）",
    )
    parser.add_argument("--trend-adx-threshold", type=float, default=18.0, help="MicroScalp ADX 阈值（默认：18）")
    parser.add_argument(
        "--entry-pullback-atr",
        type=float,
        default=0.35,
        help="MicroScalp 趋势回撤 ATR 倍数（默认：0.35）",
    )
    parser.add_argument("--maker-offset-ticks", type=int, default=1, help="MicroScalp 挂单偏移 tick（默认：1）")
    parser.add_argument("--limit-ttl-ms", type=int, default=2500, help="MicroScalp 限价单超时毫秒（默认：2500）")
    parser.add_argument("--chase-ticks", type=int, default=2, help="MicroScalp 追价 tick（默认：2）")
    parser.add_argument(
        "--post-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="MicroScalp 是否启用 post-only（默认：True）",
    )
    parser.add_argument("--vegas-fast-ema", type=int, default=12, help="Vegas 快线 EMA 周期（默认：12）")
    parser.add_argument("--vegas-slow-ema", type=int, default=36, help="Vegas 慢线 EMA 周期（默认：36）")
    parser.add_argument("--tunnel-ema-1", type=int, default=144, help="Vegas 隧道 EMA1 周期（默认：144）")
    parser.add_argument("--tunnel-ema-2", type=int, default=169, help="Vegas 隧道 EMA2 周期（默认：169）")
    parser.add_argument(
        "--vegas-stop-atr-multiplier",
        type=float,
        default=1.0,
        help="Vegas 初始止损 ATR 乘数（默认：1.0）",
    )
    parser.add_argument("--vegas-fib-1", type=float, default=1.0, help="Vegas TP1 Fib 倍数（默认：1.0）")
    parser.add_argument("--vegas-fib-2", type=float, default=1.618, help="Vegas TP2 Fib 倍数（默认：1.618）")
    parser.add_argument("--vegas-fib-3", type=float, default=2.618, help="Vegas TP3 Fib 倍数（默认：2.618）")
    parser.add_argument("--vegas-tp-split-1", type=float, default=0.4, help="Vegas TP1 分仓比例（默认：0.4）")
    parser.add_argument("--vegas-tp-split-2", type=float, default=0.3, help="Vegas TP2 分仓比例（默认：0.3）")
    parser.add_argument("--vegas-tp-split-3", type=float, default=0.3, help="Vegas TP3 分仓比例（默认：0.3）")
    parser.add_argument(
        "--vegas-atr-filter-min-ratio",
        type=float,
        default=0.0,
        help="Vegas 最小 ATR/Close 过滤阈值（默认：0，<=0 表示关闭）",
    )
    parser.add_argument("--save", action="store_true", help="保存报告到文件")
    parser.add_argument("--output-dir", default=None, help="报告输出目录（默认：experiments/reports/<run_id>）")
    parser.add_argument("--env", default=None, help="配置环境（默认：dev）")

    return parser.parse_args()


def build_bar_type(symbol: str, interval: Interval) -> tuple[InstrumentId, BarType]:
    """按交易对和周期构建 instrument_id 与 bar_type."""
    instrument_id = InstrumentId.from_str(f"{symbol}-PERP.BINANCE")
    nautilus_interval = INTERVAL_TO_NAUTILUS[interval]
    if interval == Interval.MINUTE_1:
        bar_type = BarType.from_str(f"{instrument_id}-{nautilus_interval}-LAST-EXTERNAL")
    else:
        bar_type = BarType.from_str(
            f"{instrument_id}-{nautilus_interval}-LAST-INTERNAL@1-MINUTE-EXTERNAL",
        )
    return instrument_id, bar_type


def build_strategy(
    args: argparse.Namespace,
    symbol: str,
    interval: Interval,
) -> tuple[type[BaseStrategy], BaseStrategyConfig]:
    """根据参数构建策略类与策略配置."""
    instrument_id, bar_type = build_bar_type(symbol, interval)

    if args.strategy == "ema_cross":
        config: BaseStrategyConfig = EMACrossConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            fast_ema_period=args.fast_ema,
            slow_ema_period=args.slow_ema,
            entry_min_atr_ratio=args.entry_min_atr_ratio,
            signal_cooldown_bars=args.signal_cooldown_bars,
            trade_size=Decimal(args.trade_size),
            capital_pct_per_trade=args.capital_pct_per_trade,
            atr_sl_multiplier=args.atr_sl,
            atr_tp_multiplier=args.atr_tp,
        )
        return EMACrossStrategy, config

    if args.strategy == "ema_pullback_atr":
        config = EMAPullbackATRConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            fast_ema_period=args.fast_ema,
            slow_ema_period=args.slow_ema,
            pullback_atr_multiplier=args.pullback_atr_multiplier,
            min_trend_gap_ratio=args.min_trend_gap_ratio,
            signal_cooldown_bars=args.signal_cooldown_bars,
            trade_size=Decimal(args.trade_size),
            capital_pct_per_trade=args.capital_pct_per_trade,
            atr_period=14,
            atr_sl_multiplier=args.atr_sl,
            atr_tp_multiplier=args.atr_tp,
            adx_period=args.adx_period,
            adx_threshold=args.adx_threshold,
        )
        return EMAPullbackATRStrategy, config

    if args.strategy == "turtle":
        config = TurtleConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            entry_period=args.entry_period,
            exit_period=args.exit_period,
            atr_period=args.turtle_atr_period,
            stop_atr_multiplier=args.stop_atr_multiplier,
            unit_add_atr_step=args.unit_add_atr_step,
            max_units=args.max_units,
            trade_size=Decimal(args.trade_size),
            capital_pct_per_trade=args.capital_pct_per_trade,
        )
        return TurtleStrategy, config

    if args.strategy == "micro_scalp":
        config = MicroScalpConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_size=Decimal(args.trade_size),
            capital_pct_per_trade=args.capital_pct_per_trade,
            fast_ema_period=args.fast_ema,
            slow_ema_period=args.slow_ema,
            rsi_period=args.rsi_period,
            adx_period=args.adx_period,
            trend_adx_threshold=args.trend_adx_threshold,
            entry_pullback_atr=args.entry_pullback_atr,
            oversold_level=args.oversold_level,
            overbought_level=args.overbought_level,
            signal_cooldown_bars=args.signal_cooldown_bars,
            atr_sl_multiplier=args.atr_sl if args.atr_sl is not None else 0.45,
            atr_tp_multiplier=args.atr_tp if args.atr_tp is not None else 0.8,
            maker_offset_ticks=args.maker_offset_ticks,
            limit_ttl_ms=args.limit_ttl_ms,
            chase_ticks=args.chase_ticks,
            post_only=args.post_only,
        )
        return MicroScalpStrategy, config

    if args.strategy == "vegas_tunnel":
        config = VegasTunnelConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_size=Decimal(args.trade_size),
            capital_pct_per_trade=args.capital_pct_per_trade,
            fast_ema_period=args.vegas_fast_ema,
            slow_ema_period=args.vegas_slow_ema,
            tunnel_ema_period_1=args.tunnel_ema_1,
            tunnel_ema_period_2=args.tunnel_ema_2,
            signal_cooldown_bars=args.signal_cooldown_bars,
            atr_filter_min_ratio=args.vegas_atr_filter_min_ratio,
            stop_atr_multiplier=args.vegas_stop_atr_multiplier,
            tp_fib_1=args.vegas_fib_1,
            tp_fib_2=args.vegas_fib_2,
            tp_fib_3=args.vegas_fib_3,
            tp_split_1=args.vegas_tp_split_1,
            tp_split_2=args.vegas_tp_split_2,
            tp_split_3=args.vegas_tp_split_3,
        )
        return VegasTunnelStrategy, config

    raise ValueError(f"Unsupported strategy: {args.strategy}")


def main() -> None:
    """回测主入口.

    Raises:
        ValueError: 日期参数不合法，或 catalog 中无对应数据。
        SystemExit: argparse 参数错误时自动触发。
    """
    args = parse_args()
    setup_logging(level="WARNING")  # 回测时减少日志噪音

    app_config = load_app_config(env=args.env)

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)

    if start > end:
        print(f"❌ start ({start}) 不能大于 end ({end})")
        sys.exit(1)

    interval_raw = args.interval
    if interval_raw is None:
        interval_raw = "1h" if args.strategy == "vegas_tunnel" else "1m"
    interval = Interval(interval_raw)

    # 目前只支持单 symbol 策略绑定一个 instrument
    # 多 symbol 可扩展为多策略实例
    symbol = args.symbols[0]

    bt_config = BacktestConfig(
        start=start,
        end=end,
        symbols=args.symbols,
        interval=interval,
        starting_balance_usdt=args.balance,
        leverage=args.leverage,
    )

    strategy_cls, strategy_config = build_strategy(args, symbol, interval)

    print("=" * 70)
    print("🚀 启动回测")
    print("=" * 70)
    print(f"  Symbol   : {', '.join(args.symbols)}")
    print(f"  Strategy : {args.strategy}")
    print(f"  Period   : {start} ~ {end}")
    print(f"  Interval : {interval.value}")
    print(f"  Balance  : {args.balance:,} USDT  Leverage: {args.leverage}x")
    if args.capital_pct_per_trade is not None and args.capital_pct_per_trade > 0:
        print(f"  Sizing   : capital_pct_per_trade={args.capital_pct_per_trade}%")
    else:
        print(f"  Sizing   : fixed trade_size={args.trade_size}")
    if args.strategy == "ema_cross":
        print(f"  EMA      : fast={args.fast_ema}  slow={args.slow_ema}")
        print(
            "  Filter   : "
            f"min_atr_ratio={args.entry_min_atr_ratio} "
            f"cooldown_bars={args.signal_cooldown_bars}"
        )
    elif args.strategy == "ema_pullback_atr":
        print(f"  EMA      : fast={args.fast_ema}  slow={args.slow_ema}")
        print(
            "  Pullback : "
            f"atr_multiplier={args.pullback_atr_multiplier} "
            f"min_trend_gap_ratio={args.min_trend_gap_ratio} "
            f"cooldown_bars={args.signal_cooldown_bars} "
            f"adx_period={args.adx_period} "
            f"adx_threshold={args.adx_threshold}"
        )
    elif args.strategy == "turtle":
        print(
            "  Turtle   : "
            f"entry={args.entry_period} exit={args.exit_period} "
            f"atr_period={args.turtle_atr_period} stop={args.stop_atr_multiplier}N "
            f"add_step={args.unit_add_atr_step}N max_units={args.max_units}"
        )
    elif args.strategy == "micro_scalp":
        print(
            "  Micro    : "
            f"EMA={args.fast_ema}/{args.slow_ema} RSI={args.rsi_period} "
            f"ADX={args.trend_adx_threshold} pullback_atr={args.entry_pullback_atr} "
            f"cooldown_bars={args.signal_cooldown_bars} "
            f"maker_ticks={args.maker_offset_ticks} ttl_ms={args.limit_ttl_ms} "
            f"chase_ticks={args.chase_ticks} post_only={args.post_only}"
        )
    else:
        print(
            "  Vegas    : "
            f"EMA={args.vegas_fast_ema}/{args.vegas_slow_ema} "
            f"Tunnel={args.tunnel_ema_1}/{args.tunnel_ema_2} "
            f"SL={args.vegas_stop_atr_multiplier}ATR "
            f"Fib=({args.vegas_fib_1},{args.vegas_fib_2},{args.vegas_fib_3}) "
            f"Split=({args.vegas_tp_split_1},{args.vegas_tp_split_2},{args.vegas_tp_split_3}) "
            f"cooldown_bars={args.signal_cooldown_bars}"
        )
    print("=" * 70)

    runner = BacktestRunner(app_config, bt_config)
    run_result = runner.run(strategy_cls, strategy_config)

    # 输出报告
    reporter = BacktestReporter(run_result)
    reporter.print_summary()

    # 保存报告
    if args.save:
        output_dir = Path(args.output_dir) if args.output_dir else None
        saved_path = reporter.save(output_dir)
        print(f"\n💾 报告已保存: {saved_path}")


if __name__ == "__main__":
    main()
