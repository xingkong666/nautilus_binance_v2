"""应用启动引导.

提供统一的应用初始化入口，按环境加载配置、构建容器、启动服务。
所有模式（回测、模拟盘、实盘）都通过 bootstrap 获取就绪的 Container。

典型用法:
    # 回测
    container = bootstrap(env="dev")
    factory = AppFactory(container)
    runner = factory.create_backtest_runner(start, end)

    # 关闭
    container.teardown()

    # 或使用上下文管理器（推荐）:
    with bootstrap_context(env="prod") as ctx:
        ctx.factory.create_backtest_runner(...)
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Timer
from types import FrameType
from typing import Any

import structlog
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.app.container import Container
from src.app.factory import AppFactory
from src.core.config import AppConfig, load_app_config, load_yaml
from src.core.logging import setup_logging
from src.state.reconciliation import ReconciliationEngine
from src.state.recovery import RecoveryManager
from src.strategy.base import BaseStrategy, BaseStrategyConfig
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy
from src.strategy.ema_pullback_atr import EMAPullbackATRConfig, EMAPullbackATRStrategy
from src.strategy.micro_scalp import MicroScalpConfig, MicroScalpStrategy
from src.strategy.turtle import TurtleConfig, TurtleStrategy
from src.strategy.vegas_tunnel import VegasTunnelConfig, VegasTunnelStrategy

logger = structlog.get_logger()


_STRATEGY_REGISTRY: dict[str, tuple[type[BaseStrategy], type[BaseStrategyConfig]]] = {
    "ema_cross": (EMACrossStrategy, EMACrossConfig),
    "ema_pullback_atr": (EMAPullbackATRStrategy, EMAPullbackATRConfig),
    "turtle": (TurtleStrategy, TurtleConfig),
    "micro_scalp": (MicroScalpStrategy, MicroScalpConfig),
    "vegas_tunnel": (VegasTunnelStrategy, VegasTunnelConfig),
}


# ---------------------------------------------------------------------------
# 启动上下文
# ---------------------------------------------------------------------------


@dataclass
class AppContext:
    """应用运行上下文，持有 container 和 factory 的引用.

    Attributes:
        config: 已加载的应用配置。
        container: 已 build 的依赖容器。
        factory: 对象工厂，基于 container 创建业务对象。
    """

    config: AppConfig
    container: Container
    factory: AppFactory


# ---------------------------------------------------------------------------
# 核心启动函数
# ---------------------------------------------------------------------------


def bootstrap(env: str | None = None, log_level: str = "INFO") -> Container:
    """加载配置、初始化日志、构建并返回 Container.

    这是最轻量的启动方式，返回裸 Container，适合脚本场景。

    Args:
        env: 运行环境标识（dev/stage/prod），None 时从 .env 文件读取。
        log_level: 日志级别，默认 INFO。

    Returns:
        已 build 的 Container 实例。
    """
    config = load_app_config(env=env)
    setup_logging(level=log_level)

    logger.info("app_bootstrap", env=config.env)

    container = Container(config)
    container.build()

    return container


def bootstrap_app(env: str | None = None, log_level: str = "INFO") -> AppContext:
    """完整启动应用，返回包含 container + factory 的 AppContext.

    适合需要同时使用容器和工厂的场景（大多数情况推荐此函数）。

    Args:
        env: 运行环境标识。
        log_level: 日志级别。

    Returns:
        AppContext，含 config / container / factory。
    """
    container = bootstrap(env=env, log_level=log_level)
    factory = AppFactory(container)

    return AppContext(
        config=container.config,
        container=container,
        factory=factory,
    )


@contextmanager
def bootstrap_context(
    env: str | None = None,
    log_level: str = "INFO",
) -> Generator[AppContext]:
    """上下文管理器形式的启动，退出时自动 teardown.

    推荐在脚本和测试中使用，确保资源（DB 连接等）正确释放。

    Args:
        env: 运行环境标识。
        log_level: 日志级别。

    Yields:
        AppContext，含 config / container / factory。

    Example:
        with bootstrap_context(env="dev") as ctx:
            runner = ctx.factory.create_backtest_runner(start, end)
            result = runner.run(strategy_cls, strategy_cfg)
    """
    ctx = bootstrap_app(env=env, log_level=log_level)
    try:
        yield ctx
    finally:
        ctx.container.teardown()


# ---------------------------------------------------------------------------
# 信号处理（实盘用）
# ---------------------------------------------------------------------------


def register_shutdown_handler(container: Container) -> None:
    """注册 SIGINT / SIGTERM 信号处理器，优雅关闭应用.

    收到信号后执行 container.teardown()，然后退出进程。
    适合实盘长驻进程场景。

    Args:
        container: 需要在退出时 teardown 的 Container 实例。
    """

    def _handler(signum: int, frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        logger.warning("shutdown_signal_received", signal=sig_name)
        container.teardown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    logger.info("shutdown_handler_registered")


def _normalize_instrument_id(symbol_or_instrument: str) -> InstrumentId:
    value = symbol_or_instrument.strip()
    if not value:
        raise ValueError("Live strategy symbol/instrument_id cannot be empty.")
    if "." not in value:
        value = f"{value}-PERP.BINANCE"
    return InstrumentId.from_str(value)


def _build_live_strategy(
    strategy_config_path: Path,
    container: Container,
    symbol: str | None = None,
) -> BaseStrategy:
    raw = load_yaml(strategy_config_path)
    strategy_cfg = raw.get("strategy", {})
    strategy_name = str(strategy_cfg.get("name", "")).strip()
    if strategy_name not in _STRATEGY_REGISTRY:
        raise ValueError(f"Unsupported strategy: {strategy_name}")

    raw_instruments = strategy_cfg.get("instruments", [])
    instrument_source = symbol or (raw_instruments[0] if raw_instruments else "")
    instrument_id = _normalize_instrument_id(str(instrument_source))
    bar_type_template = str(strategy_cfg.get("bar_type", "")).strip()
    if not bar_type_template:
        raise ValueError("Strategy config missing bar_type")

    params: dict[str, Any] = dict(strategy_cfg.get("params", {}))
    strategy_cls, strategy_config_cls = _STRATEGY_REGISTRY[strategy_name]
    config = strategy_config_cls(
        instrument_id=instrument_id,
        bar_type=BarType.from_str(bar_type_template.format(instrument_id=str(instrument_id))),
        close_positions_on_stop=bool(strategy_cfg.get("close_positions_on_stop", True)),
        **params,
    )
    return strategy_cls(config=config, event_bus=container.event_bus)


def _extract_account_balance(balances: list[dict[str, Any]]) -> str:
    preferred_assets = ("USDT", "USDC")
    for asset in preferred_assets:
        match = next((balance for balance in balances if str(balance.get("asset")) == asset), None)
        if match is not None:
            return str(match.get("walletBalance", "0"))
    if balances:
        return str(balances[0].get("walletBalance", "0"))
    return "0"


def _normalize_exchange_positions(raw_positions: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for position in raw_positions:
        symbol = str(position.get("symbol", "")).strip()
        if not symbol:
            continue
        normalized.append(
            {
                "instrument_id": f"{symbol}-PERP.BINANCE",
                "side": str(position.get("positionSide", "BOTH")),
                "quantity": str(position.get("positionAmt", "0")).lstrip("+").lstrip("-") or "0",
                "entry_price": str(position.get("entryPrice", "0")),
                "unrealized_pnl": str(position.get("unrealizedProfit", "0")),
                "leverage": str(position.get("leverage", "1")),
            }
        )
    return normalized


def _bootstrap_live_state(container: Container, adapter: Any) -> None:
    raw_balances, raw_positions = adapter.fetch_account_snapshot()
    raw_open_orders = adapter.fetch_open_orders()
    normalized_positions = _normalize_exchange_positions(raw_positions)
    for position in normalized_positions:
        container.ignored_instruments.ignore(
            instrument_id=position["instrument_id"],
            reason="existing_exchange_position_on_startup",
            source="bootstrap",
            details={"side": position.get("side", "BOTH")},
        )
    for order in raw_open_orders:
        symbol = str(order.get("symbol", "")).strip()
        if not symbol:
            continue
        container.ignored_instruments.ignore(
            instrument_id=f"{symbol}-PERP.BINANCE",
            reason="existing_exchange_open_order_on_startup",
            source="bootstrap",
            details={"client_order_id": str(order.get("clientOrderId", ""))},
        )
    recovery = RecoveryManager(
        snapshot_mgr=container.snapshot_manager,
        reconciler=ReconciliationEngine(container.event_bus),
    )
    snapshot = recovery.recover(
        exchange_positions=normalized_positions,
        account_balance=_extract_account_balance(raw_balances),
    )
    if snapshot is None:
        return
    snapshot.open_orders = raw_open_orders
    container.snapshot_manager.save(snapshot)
    logger.info(
        "live_state_bootstrapped",
        positions=len(snapshot.positions),
        recovery_source=snapshot.metadata.get("recovery_source", ""),
        recovery_action=snapshot.metadata.get("recovery_action", ""),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap live trading application")
    parser.add_argument("--env", default=None, help="运行环境（dev/stage/prod）")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    parser.add_argument("--strategy-config", default="", help="策略 YAML 路径")
    parser.add_argument("--symbol", default="", help="交易对，如 BTCUSDT")
    parser.add_argument("--timeout-seconds", type=float, default=0.0, help="自动停止秒数，0 表示不自动停止")
    return parser.parse_args()


def run_live(
    env: str | None = None,
    log_level: str = "INFO",
    strategy_config: str = "",
    symbol: str = "",
    timeout_seconds: float = 0.0,
) -> None:
    """运行单策略 live/testnet 入口."""
    ctx = bootstrap_app(env=env, log_level=log_level)
    supervisor = None
    stop_timer: Timer | None = None

    try:
        strategy_config_path = Path(strategy_config or ctx.config.live.strategy_config).expanduser()
        if not strategy_config_path.is_absolute():
            strategy_config_path = Path.cwd() / strategy_config_path
        if not strategy_config_path.exists():
            raise FileNotFoundError(f"Strategy config not found: {strategy_config_path}")

        live_symbol = symbol or ctx.config.live.symbol
        effective_timeout = timeout_seconds if timeout_seconds > 0 else ctx.config.live.timeout_seconds

        strategy = _build_live_strategy(strategy_config_path, ctx.container, symbol=live_symbol or None)
        ctx.container.order_router.bind_strategy(strategy)

        adapter = ctx.factory.create_binance_adapter(
            symbols=[str(strategy.config.instrument_id)],
        )
        adapter.prepare_runtime_config()
        _bootstrap_live_state(ctx.container, adapter)
        adapter.register_strategy(strategy)
        adapter.build_node()

        from src.live.supervisor import LiveSupervisor

        supervisor = LiveSupervisor(ctx.container)
        supervisor.start()

        if effective_timeout > 0:
            stop_timer = Timer(effective_timeout, adapter.request_stop)
            stop_timer.daemon = True
            stop_timer.start()

        logger.info(
            "live_run_starting",
            env=ctx.config.env,
            strategy_config=str(strategy_config_path),
            instrument_id=str(strategy.config.instrument_id),
            timeout_seconds=effective_timeout,
        )
        adapter.run()
    except KeyboardInterrupt:
        logger.warning("live_run_interrupted")
    finally:
        if stop_timer is not None:
            stop_timer.cancel()
        if supervisor is not None:
            supervisor.stop(timeout=10.0)
        if ctx.container.binance_adapter is not None:
            try:
                import asyncio

                asyncio.run(ctx.container.binance_adapter.stop())
            except RuntimeError:
                ctx.container.binance_adapter.request_stop()
                ctx.container.binance_adapter.dispose()
        ctx.container.teardown()


def main() -> None:
    args = _parse_args()
    config = load_app_config(env=args.env)
    live_strategy_config = args.strategy_config or config.live.strategy_config
    if live_strategy_config:
        run_live(
            env=args.env,
            log_level=args.log_level,
            strategy_config=live_strategy_config,
            symbol=args.symbol,
            timeout_seconds=args.timeout_seconds,
        )
        return

    raise SystemExit("Missing live strategy config. Pass --strategy-config or set live.strategy_config in env YAML.")


if __name__ == "__main__":
    main()
