"""对象工厂.

负责创建策略、NautilusTrader 引擎等需要复杂参数的对象。
Factory 只负责"生产"，不持有状态；有状态的单例放在 Container 中。

使用方式:
    factory = AppFactory(container)
    engine = factory.create_backtest_engine(bt_config)
    strategy = factory.create_ema_cross_strategy(symbol="BTCUSDT")
"""
# ruff: noqa: TC001,TC003

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import structlog
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.app.container import Container
from src.backtest.runner import BacktestConfig, BacktestRunner
from src.core.enums import INTERVAL_TO_NAUTILUS, Interval
from src.exchange.binance_adapter import BinanceAdapter, build_binance_adapter
from src.strategy.base import BaseStrategy, BaseStrategyConfig
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy
from src.strategy.ema_pullback_atr import EMAPullbackATRConfig, EMAPullbackATRStrategy

logger = structlog.get_logger()


class AppFactory:
    """应用对象工厂.

    依赖 Container 提供基础设施，负责根据配置创建策略/引擎等业务对象。
    """

    def __init__(self, container: Container) -> None:
        """初始化工厂.

        Args:
            container: 已 build() 的依赖容器，工厂从中获取 config 等依赖。
        """
        self._container = container
        self._config = container.config

    # ------ 策略工厂 ------

    def create_ema_cross_strategy(
        self,
        symbol: str,
        interval: Interval = Interval.MINUTE_1,
        fast_ema: int = 10,
        slow_ema: int = 20,
        trade_size: Decimal = Decimal("0.01"),
        capital_pct_per_trade: float | None = None,
    ) -> tuple[type[EMACrossStrategy], EMACrossConfig]:
        """创建 EMA 交叉策略及其配置.

        Args:
            symbol: 交易对名称，如 "BTCUSDT"。
            interval: K 线周期，默认 1m。
            fast_ema: 快线 EMA 周期，默认 10。
            slow_ema: 慢线 EMA 周期，默认 20。
            trade_size: 每次交易数量（币数），默认 0.01。
            capital_pct_per_trade: 每笔使用账户总权益百分比（0-100），None 表示不用。

        Returns:
            (策略类, 策略配置实例) 元组，可直接传入 BacktestRunner.run()。
        """
        instrument_id = InstrumentId.from_str(f"{symbol}-PERP.BINANCE")
        nautilus_interval = INTERVAL_TO_NAUTILUS[interval]
        if interval == Interval.MINUTE_1:
            bar_type = BarType.from_str(f"{instrument_id}-{nautilus_interval}-LAST-EXTERNAL")
        else:
            bar_type = BarType.from_str(
                f"{instrument_id}-{nautilus_interval}-LAST-INTERNAL@1-MINUTE-EXTERNAL",
            )

        config = EMACrossConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            fast_ema_period=fast_ema,
            slow_ema_period=slow_ema,
            trade_size=trade_size,
            capital_pct_per_trade=capital_pct_per_trade,
        )

        logger.info(
            "strategy_created",
            strategy="EMACross",
            symbol=symbol,
            interval=interval.value,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
        )
        return EMACrossStrategy, config

    def create_ema_pullback_atr_strategy(
        self,
        symbol: str,
        interval: Interval = Interval.MINUTE_1,
        fast_ema: int = 20,
        slow_ema: int = 50,
        pullback_atr_multiplier: float = 1.0,
        trade_size: Decimal = Decimal("0.01"),
        capital_pct_per_trade: float | None = None,
        adx_period: int = 14,
        adx_threshold: float = 20.0,
    ) -> tuple[type[EMAPullbackATRStrategy], EMAPullbackATRConfig]:
        """创建 EMA 回撤策略及其配置."""
        instrument_id = InstrumentId.from_str(f"{symbol}-PERP.BINANCE")
        nautilus_interval = INTERVAL_TO_NAUTILUS[interval]
        if interval == Interval.MINUTE_1:
            bar_type = BarType.from_str(f"{instrument_id}-{nautilus_interval}-LAST-EXTERNAL")
        else:
            bar_type = BarType.from_str(
                f"{instrument_id}-{nautilus_interval}-LAST-INTERNAL@1-MINUTE-EXTERNAL",
            )

        config = EMAPullbackATRConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            fast_ema_period=fast_ema,
            slow_ema_period=slow_ema,
            pullback_atr_multiplier=pullback_atr_multiplier,
            trade_size=trade_size,
            capital_pct_per_trade=capital_pct_per_trade,
            adx_period=adx_period,
            adx_threshold=adx_threshold,
        )

        logger.info(
            "strategy_created",
            strategy="EMAPullbackATR",
            symbol=symbol,
            interval=interval.value,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
            pullback_atr_multiplier=pullback_atr_multiplier,
            adx_period=adx_period,
            adx_threshold=adx_threshold,
        )
        return EMAPullbackATRStrategy, config

    def create_strategy_from_config(
        self,
        strategy_cfg: dict[str, Any],
        symbol: str,
        interval: Interval,
    ) -> tuple[type[BaseStrategy], BaseStrategyConfig]:
        """根据 YAML 策略配置动态创建策略.

        从 configs/strategies/*.yaml 加载的配置字典创建对应策略。
        目前支持 "ema_cross"、"ema_pullback_atr"，后续可扩展。

        Args:
            strategy_cfg: 策略配置字典（来自 YAML configs/strategies/）。
            symbol: 交易对名称。
            interval: K 线周期。

        Returns:
            (策略类, 策略配置实例) 元组。

        Raises:
            ValueError: 不支持的策略名称。
        """
        name = strategy_cfg.get("name", "")
        params = strategy_cfg.get("params", {})

        if name == "ema_cross":
            capital_pct = params.get("capital_pct_per_trade")
            return self.create_ema_cross_strategy(
                symbol=symbol,
                interval=interval,
                fast_ema=params.get("fast_ema_period", 10),
                slow_ema=params.get("slow_ema_period", 20),
                trade_size=Decimal(str(params.get("trade_size", "0.01"))),
                capital_pct_per_trade=float(capital_pct) if capital_pct is not None else None,
            )

        if name == "ema_pullback_atr":
            capital_pct = params.get("capital_pct_per_trade")
            return self.create_ema_pullback_atr_strategy(
                symbol=symbol,
                interval=interval,
                fast_ema=params.get("fast_ema_period", 20),
                slow_ema=params.get("slow_ema_period", 50),
                pullback_atr_multiplier=float(params.get("pullback_atr_multiplier", 1.0)),
                trade_size=Decimal(str(params.get("trade_size", "0.01"))),
                capital_pct_per_trade=float(capital_pct) if capital_pct is not None else None,
                adx_period=int(params.get("adx_period", 14)),
                adx_threshold=float(params.get("adx_threshold", 20.0)),
            )

        raise ValueError(f"Unsupported strategy: '{name}'. Available: ema_cross, ema_pullback_atr")

    # ------ 交易所适配器工厂 ------

    def create_binance_adapter(
        self,
        symbols: list[str] | None = None,
        leverages: dict[str, int] | None = None,
        environment: BinanceEnvironment | None = None,
        proxy_url: str | None = None,
    ) -> BinanceAdapter:
        """创建 BinanceAdapter，优先使用 Container 内已注册的实例.

        若 Container 在 build() 时已根据配置创建了 adapter，直接返回该实例；
        否则使用参数临时构建一个新实例（适用于脚本/测试场景）。

        Args:
            symbols: 要预加载的合约符号列表，如 ["BTCUSDT", "ETHUSDT"]。
                Container 已有 adapter 时此参数忽略。
            leverages: 各合约杠杆倍数，如 {"BTCUSDT": 10}。
                Container 已有 adapter 时此参数忽略。
            environment: Binance 环境（LIVE / TESTNET / DEMO）。
                None 时根据 AppConfig.env 推断：prod → LIVE，其余 → TESTNET。
                Container 已有 adapter 时此参数忽略。
            proxy_url: HTTP 代理 URL。Container 已有 adapter 时此参数忽略。

        Returns:
            未启动的 BinanceAdapter 实例（需调用 `await adapter.start()` 后使用）。
        """
        # 优先复用 Container 内已按配置初始化的 adapter
        if self._container.binance_adapter is not None:
            logger.info("binance_adapter_reused_from_container")
            return self._container.binance_adapter

        # 回退：按参数临时构建（dev 调试 / 单元测试场景）
        if environment is None:
            environment = (
                BinanceEnvironment.LIVE
                if self._config.env == "prod"
                else BinanceEnvironment.TESTNET
            )
        adapter = build_binance_adapter(
            environment=environment,
            symbols=symbols,
            leverages=leverages,
            proxy_url=proxy_url,
        )
        logger.info(
            "binance_adapter_created",
            environment=environment.value,
            symbols=symbols,
        )
        return adapter

    # ------ 回测工厂 ------

    def create_backtest_runner(
        self,
        start: dt.date,
        end: dt.date,
        symbols: list[str] | None = None,
        interval: Interval = Interval.MINUTE_1,
        starting_balance: int | None = None,
        leverage: float | None = None,
    ) -> BacktestRunner:
        """创建并配置 BacktestRunner.

        Args:
            start: 回测起始日期（含）。
            end: 回测结束日期（含）。
            symbols: 交易对列表；None 时从 AppConfig.account 推断（默认 BTCUSDT）。
            interval: K 线周期，默认 1m。
            starting_balance: 初始余额（USDT）；None 时使用 AppConfig.account.starting_balance。
            leverage: 杠杆倍数；None 时使用 AppConfig.account.max_leverage。

        Returns:
            配置好的 BacktestRunner 实例。
        """
        account_cfg = self._config.account

        bt_config = BacktestConfig(
            start=start,
            end=end,
            symbols=symbols or ["BTCUSDT"],
            interval=interval,
            starting_balance_usdt=starting_balance or account_cfg.starting_balance,
            leverage=leverage or float(account_cfg.max_leverage),
        )

        logger.info(
            "backtest_runner_created",
            start=str(start),
            end=str(end),
            symbols=bt_config.symbols,
            interval=interval.value,
        )
        return BacktestRunner(self._config, bt_config)
