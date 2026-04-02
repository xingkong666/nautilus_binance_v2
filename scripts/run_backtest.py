#!/usr/bin/env python3
r"""回测运行脚本.

策略参数全部从 --config YAML 文件读取，命令行只控制时间窗口与运行时参数。

Usage:
    # 使用默认参数（BTCUSDT，2024-01-01 ~ 2024-03-31）
    python scripts/run_backtest.py --config configs/strategies/ema_cross.yaml

    # 自定义时间段
    python scripts/run_backtest.py \
        --config configs/strategies/turtle.yaml \
        --start 2023-01-01 \
        --end   2023-12-31

    # 覆盖初始资金和杠杆
    python scripts/run_backtest.py \
        --config configs/strategies/vegas_tunnel.yaml \
        --start 2024-06-01 \
        --end   2024-12-31 \
        --balance 50000 \
        --leverage 5

    # 不保存报告
    python scripts/run_backtest.py \
        --config configs/strategies/ema_cross.yaml \
        --no-save

    # 保存报告到指定目录
    python scripts/run_backtest.py \
        --config configs/strategies/ema_cross.yaml \
        --output-dir experiments/reports/my_run
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.backtest.report import BacktestReporter
from src.backtest.runner import BacktestConfig, BacktestRunner
from src.core.config import load_app_config
from src.core.enums import Interval
from src.core.logging import setup_logging
from src.strategy.base import BaseStrategy, BaseStrategyConfig

# ---------------------------------------------------------------------------
# bar_type 解析
# ---------------------------------------------------------------------------

_BAR_TYPE_INTERVAL_MAP: dict[str, Interval] = {
    "1-MINUTE": Interval.MINUTE_1,
    "3-MINUTE": Interval.MINUTE_3,
    "5-MINUTE": Interval.MINUTE_5,
    "15-MINUTE": Interval.MINUTE_15,
    "30-MINUTE": Interval.MINUTE_30,
    "1-HOUR": Interval.HOUR_1,
    "4-HOUR": Interval.HOUR_4,
    "1-DAY": Interval.DAY_1,
}


def _interval_from_bar_type(bar_type_tpl: str, instrument_id: InstrumentId) -> Interval:
    """从 bar_type 模板推断策略所使用的目标 Interval（用于 BacktestConfig.interval）.

    - ``INTERNAL@`` 格式（内部聚合）：解析目标周期，BacktestRunner 会自动从 1m 数据聚合。
    - 纯 ``EXTERNAL`` 格式（1m）：直接返回 MINUTE_1，数据源即为 1m。
    """
    resolved = bar_type_tpl.replace("{instrument_id}", str(instrument_id))
    # 格式: BTCUSDT-PERP.BINANCE-{step}-{agg}-LAST-{INTERNAL@1-MINUTE-EXTERNAL | EXTERNAL}
    # instrument_id 含一个 "-"，split 后 parts[2]=step, parts[3]=aggregation
    parts = resolved.split("-")
    if len(parts) >= 4:
        key = f"{parts[2]}-{parts[3]}"
        return _BAR_TYPE_INTERVAL_MAP.get(key, Interval.MINUTE_1)
    return Interval.MINUTE_1


def _is_internally_aggregated(bar_type_tpl: str) -> bool:
    """判断 bar_type 模板是否为内部聚合（INTERNAL@）格式."""
    return "INTERNAL@" in bar_type_tpl


def _resolve_bar_type(bar_type_tpl: str, instrument_id: InstrumentId) -> BarType:
    """将 bar_type 模板中的 {instrument_id} 替换为实际值并解析."""
    resolved = bar_type_tpl.replace("{instrument_id}", str(instrument_id))
    return BarType.from_str(resolved)


# ---------------------------------------------------------------------------
# 策略加载
# ---------------------------------------------------------------------------

_STRATEGY_REGISTRY: dict[str, tuple[str, str]] = {
    "ema_cross": ("src.strategy.ema_cross", "EMACrossStrategy"),
    "ema_pullback_atr": ("src.strategy.ema_pullback_atr", "EMAPullbackATRStrategy"),
    "turtle": ("src.strategy.turtle", "TurtleStrategy"),
    "micro_scalp": ("src.strategy.micro_scalp", "MicroScalpStrategy"),
    "vegas_tunnel": ("src.strategy.vegas_tunnel", "VegasTunnelStrategy"),
}

_CONFIG_REGISTRY: dict[str, tuple[str, str]] = {
    "ema_cross": ("src.strategy.ema_cross", "EMACrossConfig"),
    "ema_pullback_atr": ("src.strategy.ema_pullback_atr", "EMAPullbackATRConfig"),
    "turtle": ("src.strategy.turtle", "TurtleConfig"),
    "micro_scalp": ("src.strategy.micro_scalp", "MicroScalpConfig"),
    "vegas_tunnel": ("src.strategy.vegas_tunnel", "VegasTunnelConfig"),
}


def _import(module_path: str, class_name: str) -> Any:
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


def _class_path_to_module(class_path: str) -> tuple[str, str]:
    """将 YAML 中 'strategy.ema_cross:EMACrossStrategy' 转为 (module, class)."""
    module_rel, cls = class_path.split(":")
    module_abs = f"src.{module_rel}"
    return module_abs, cls


def load_strategy_from_yaml(
    cfg: dict[str, Any],
) -> tuple[type[BaseStrategy], BaseStrategyConfig]:
    """从 YAML 配置字典构建策略类与策略配置.

    Args:
        cfg: YAML 根节点下的 strategy 字典。

    Returns:
        (StrategyClass, strategy_config_instance)

    """
    strategy_name: str = cfg["name"]
    class_path: str = cfg["class_path"]
    params: dict[str, Any] = cfg.get("params", {})
    instruments: list[str] = cfg.get("instruments", ["BTCUSDT-PERP.BINANCE"])
    bar_type_tpl: str = cfg.get("bar_type", "")

    # 策略类
    strat_module, strat_cls_name = _class_path_to_module(class_path)
    strategy_cls: type[BaseStrategy] = _import(strat_module, strat_cls_name)

    # 配置类（名称约定：XxxStrategy → XxxConfig）
    if strategy_name in _CONFIG_REGISTRY:
        cfg_module, cfg_cls_name = _CONFIG_REGISTRY[strategy_name]
    else:
        # fallback：根据策略类名推断
        cfg_cls_name = strat_cls_name.replace("Strategy", "Config")
        cfg_module = strat_module
    config_cls = _import(cfg_module, cfg_cls_name)

    instrument_id = InstrumentId.from_str(instruments[0])
    bar_type = _resolve_bar_type(bar_type_tpl, instrument_id)

    # trade_size 需要 Decimal
    if "trade_size" in params:
        params["trade_size"] = Decimal(str(params["trade_size"]))

    strategy_config: BaseStrategyConfig = config_cls(
        instrument_id=instrument_id,
        bar_type=bar_type,
        **params,
    )
    return strategy_cls, strategy_config


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """解析命令行参数."""
    parser = argparse.ArgumentParser(
        description="NautilusTrader 回测运行脚本（策略参数由 --config YAML 提供）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="YAML",
        help="策略配置文件路径，例如 configs/strategies/ema_cross.yaml",
    )
    parser.add_argument("--start", default="2024-01-01", help="回测起始日期 YYYY-MM-DD（默认：2024-01-01）")
    parser.add_argument("--end", default="2024-03-31", help="回测结束日期 YYYY-MM-DD（默认：2024-03-31）")
    parser.add_argument("--balance", type=int, default=10_000, help="初始余额 USDT（默认：10000）")
    parser.add_argument("--leverage", type=float, default=10.0, help="账户杠杆（默认：10.0）")
    parser.add_argument("--no-save", action="store_true", help="禁止保存报告到文件（默认：保存）")
    parser.add_argument("--output-dir", default=None, help="报告输出目录（默认：experiments/reports/<run_id>）")
    parser.add_argument("--env", default=None, help="配置环境（默认：dev）")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def main() -> None:
    """回测主入口."""
    args = parse_args()
    setup_logging(level="WARNING")

    # 加载策略 YAML
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    if not config_path.exists():
        print(f"❌ 找不到配置文件: {config_path}")
        sys.exit(1)

    with config_path.open() as f:
        raw: dict[str, Any] = yaml.safe_load(f)
    strategy_cfg_raw: dict[str, Any] = raw["strategy"]

    # 日期校验
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    if start > end:
        print(f"❌ start ({start}) 不能大于 end ({end})")
        sys.exit(1)

    # 构建策略
    strategy_cls, strategy_config = load_strategy_from_yaml(strategy_cfg_raw)

    # 从 bar_type 推断 interval（供 BacktestConfig 使用）
    instrument_id = InstrumentId.from_str(strategy_cfg_raw.get("instruments", ["BTCUSDT-PERP.BINANCE"])[0])
    bar_type_tpl = strategy_cfg_raw.get("bar_type", "")
    interval = _interval_from_bar_type(bar_type_tpl, instrument_id)
    use_aggregation = _is_internally_aggregated(bar_type_tpl)

    # 从 instrument_id 取 symbol（去掉 "-PERP.BINANCE" 后缀）
    symbol = instrument_id.symbol.value.replace("-PERP", "")

    app_config = load_app_config(env=args.env)

    bt_config = BacktestConfig(
        start=start,
        end=end,
        symbols=[symbol],
        interval=interval,
        starting_balance_usdt=args.balance,
        leverage=args.leverage,
    )

    # 打印摘要
    strategy_name = strategy_cfg_raw["name"]
    params = strategy_cfg_raw.get("params", {})
    print("=" * 70)
    print("🚀 启动回测")
    print("=" * 70)
    print(f"  Config   : {config_path.relative_to(ROOT)}")
    print(f"  Strategy : {strategy_name}")
    print(f"  Symbol   : {symbol}")
    print(f"  Period   : {start} ~ {end}")
    data_src = f"1m → {interval.value} (内部聚合)" if use_aggregation else "1m (直接使用)"
    print(f"  Interval : {interval.value}  DataSrc: {data_src}")
    print(f"  Balance  : {args.balance:,} USDT  Leverage: {args.leverage}x")
    if params:
        print("  Params   :")
        for k, v in params.items():
            print(f"    {k}: {v}")
    print("=" * 70)

    runner = BacktestRunner(app_config, bt_config)
    run_result = runner.run(strategy_cls, strategy_config)

    reporter = BacktestReporter(run_result)
    reporter.print_summary()

    if not args.no_save:
        output_dir = Path(args.output_dir) if args.output_dir else None
        saved_path = reporter.save(output_dir)
        print(f"\n💾 报告已保存: {saved_path}")


if __name__ == "__main__":
    main()
