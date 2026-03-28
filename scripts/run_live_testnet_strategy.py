#!/usr/bin/env python3
"""在 Binance Futures Testnet 运行真实策略.

用法示例:
    uv run python scripts/run_live_testnet_strategy.py \
      --strategy-config configs/strategies/vegas_tunnel.yaml \
      --symbol BTCUSDT
"""
# ruff: noqa: E402,I001

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from threading import Timer
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.config import (
    BinanceAccountType,
    BinanceDataClientConfig,
    BinanceExecClientConfig,
    BinanceInstrumentProviderConfig,
)
from nautilus_trader.adapters.binance.factories import (
    BinanceLiveDataClientFactory,
    BinanceLiveExecClientFactory,
)
from nautilus_trader.config import (
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.config import load_app_config, load_yaml
from src.core.nautilus_cache import build_nautilus_cache_settings
from src.strategy.base import BaseStrategy, BaseStrategyConfig
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy
from src.strategy.ema_pullback_atr import EMAPullbackATRConfig, EMAPullbackATRStrategy
from src.strategy.micro_scalp import MicroScalpConfig, MicroScalpStrategy
from src.strategy.turtle import TurtleConfig, TurtleStrategy
from src.strategy.vegas_tunnel import VegasTunnelConfig, VegasTunnelStrategy

_REQUEST_STOP: callable | None = None

_STRATEGY_REGISTRY: dict[str, tuple[type[BaseStrategy], type[BaseStrategyConfig]]] = {
    "ema_cross": (EMACrossStrategy, EMACrossConfig),
    "ema_pullback_atr": (EMAPullbackATRStrategy, EMAPullbackATRConfig),
    "turtle": (TurtleStrategy, TurtleConfig),
    "micro_scalp": (MicroScalpStrategy, MicroScalpConfig),
    "vegas_tunnel": (VegasTunnelStrategy, VegasTunnelConfig),
}


def _load_env_file() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return

    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
    print(f"✅ 已加载 .env: {env_file}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在 Binance Testnet 运行真实策略")
    parser.add_argument(
        "--strategy-config",
        default="configs/strategies/vegas_tunnel.yaml",
        help="策略 YAML 配置路径（默认：configs/strategies/vegas_tunnel.yaml）",
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对（默认：BTCUSDT）")
    parser.add_argument("--timeout-seconds", type=float, default=0.0, help="自动停止秒数，0 表示不自动停止")
    parser.add_argument("--log-level", default="INFO", help="Nautilus 日志级别（默认：INFO）")
    return parser.parse_args()


def _normalize_instrument_id(symbol: str) -> InstrumentId:
    value = symbol if "." in symbol else f"{symbol}-PERP.BINANCE"
    return InstrumentId.from_str(value)


def _resolve_testnet_credentials() -> tuple[str | None, str | None]:
    api_key = os.environ.get("BINANCE_TESTNET_API_KEY") or os.environ.get("BINANCE_FUTURES_TESTNET_API_KEY")
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET") or os.environ.get("BINANCE_FUTURES_TESTNET_API_SECRET")
    return api_key, api_secret


def _build_strategy(strategy_config_path: Path, instrument_id: InstrumentId) -> BaseStrategy:
    raw = load_yaml(strategy_config_path)
    cfg = raw.get("strategy", {})
    strategy_name = str(cfg.get("name", "")).strip()
    params: dict[str, Any] = dict(cfg.get("params", {}))
    bar_type_template = str(cfg.get("bar_type", "")).strip()

    if strategy_name not in _STRATEGY_REGISTRY:
        raise ValueError(f"不支持的策略: {strategy_name}")
    if not bar_type_template:
        raise ValueError("策略配置缺少 bar_type")

    strategy_cls, strategy_config_cls = _STRATEGY_REGISTRY[strategy_name]
    bar_type = BarType.from_str(bar_type_template.format(instrument_id=str(instrument_id)))

    strategy_cfg = strategy_config_cls(
        instrument_id=instrument_id,
        bar_type=bar_type,
        **params,
    )
    return strategy_cls(config=strategy_cfg)


def _build_node(
    instrument_id: InstrumentId,
    api_key: str | None,
    api_secret: str | None,
    strategy: BaseStrategy,
    log_level: str,
) -> TradingNode:
    app_config = load_app_config(env=os.environ.get("ENV", "dev"))
    cache_settings = build_nautilus_cache_settings(app_config, mode="live")
    instrument_provider = BinanceInstrumentProviderConfig(load_ids=frozenset([str(instrument_id)]))

    data_cfg = BinanceDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=BinanceEnvironment.TESTNET,
        instrument_provider=instrument_provider,
    )

    exec_cfg = BinanceExecClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=BinanceEnvironment.TESTNET,
        use_reduce_only=True,
        use_position_ids=True,
        instrument_provider=instrument_provider,
    )

    trader_id = f"LIVE-{strategy.__class__.__name__.upper()}-TESTNET"
    node_config = TradingNodeConfig(
        trader_id=trader_id,
        instance_id=cache_settings.instance_id,
        cache=cache_settings.cache,
        data_clients={"BINANCE": data_cfg},
        exec_clients={"BINANCE": exec_cfg},
        data_engine=LiveDataEngineConfig(time_bars_timestamp_on_close=True),
        exec_engine=LiveExecEngineConfig(reconciliation=True, reconciliation_lookback_mins=60),
        risk_engine=LiveRiskEngineConfig(
            bypass=False,
            max_order_submit_rate="100/00:00:01",
            max_order_modify_rate="100/00:00:01",
        ),
        logging=LoggingConfig(log_level=log_level),
    )

    node = TradingNode(config=node_config)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)
    node.trader.add_strategy(strategy)
    node.build()
    return node


def _request_node_stop() -> None:
    global _REQUEST_STOP
    stop_fn = _REQUEST_STOP
    if stop_fn is None:
        return
    _REQUEST_STOP = None
    stop_fn()


def main() -> None:
    """Run the script entrypoint."""
    global _REQUEST_STOP

    _load_env_file()
    args = _parse_args()
    strategy_config_path = (ROOT / args.strategy_config).resolve()
    if not strategy_config_path.exists():
        raise FileNotFoundError(f"策略配置不存在: {strategy_config_path}")

    instrument_id = _normalize_instrument_id(args.symbol)
    api_key, api_secret = _resolve_testnet_credentials()
    if not api_key or not api_secret:
        raise RuntimeError("缺少 Testnet 凭证，请设置 BINANCE_TESTNET_API_KEY/BINANCE_TESTNET_API_SECRET")

    strategy = _build_strategy(strategy_config_path, instrument_id)
    node = _build_node(
        instrument_id=instrument_id,
        api_key=api_key,
        api_secret=api_secret,
        strategy=strategy,
        log_level=args.log_level,
    )

    print("=" * 64)
    print("🚀 模拟盘策略已启动（Binance Futures Testnet）")
    print(f"   策略配置: {strategy_config_path}")
    print(f"   策略类:   {strategy.__class__.__name__}")
    print(f"   合约:     {instrument_id}")
    if args.timeout_seconds > 0:
        print(f"   自动停止: {args.timeout_seconds:.0f} 秒")
    else:
        print("   自动停止: 关闭（Ctrl+C 停止）")
    print("=" * 64)

    _REQUEST_STOP = node.stop
    timeout_timer: Timer | None = None
    if args.timeout_seconds > 0:
        timeout_timer = Timer(
            args.timeout_seconds,
            lambda: (print(f"\n⚠️ 达到超时 {args.timeout_seconds:.0f}s，正在停止节点"), _request_node_stop()),
        )
        timeout_timer.daemon = True
        timeout_timer.start()

    try:
        node.run()
    except KeyboardInterrupt:
        print("\n⚠️ 用户中断，正在停止节点...")
        _request_node_stop()
    finally:
        if timeout_timer is not None:
            timeout_timer.cancel()
        _REQUEST_STOP = None
        node.dispose()
        print("\n✅ 模拟盘策略已退出")


if __name__ == "__main__":
    main()
