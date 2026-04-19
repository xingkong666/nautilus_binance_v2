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
from src.core.nautilus_cache import build_nautilus_cache_settings
from src.exchange.binance_adapter import BinanceAdapter, build_binance_adapter
from src.strategy.base import BaseStrategy, BaseStrategyConfig
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy
from src.strategy.ema_pullback_atr import EMAPullbackATRConfig, EMAPullbackATRStrategy
from src.strategy.micro_scalp import MicroScalpConfig, MicroScalpStrategy
from src.strategy.turtle import TurtleConfig, TurtleStrategy
from src.strategy.vegas_tunnel import VegasTunnelConfig, VegasTunnelStrategy

logger = structlog.get_logger(__name__)


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
        margin_pct_per_trade: float | None = None,
        gross_exposure_pct_per_trade: float | None = None,
        capital_pct_per_trade: float | None = None,
        sizing_leverage: float = 1.0,
    ) -> tuple[type[EMACrossStrategy], EMACrossConfig]:
        """创建 EMA 交叉策略及其配置.

        Args:
            symbol: 交易对名称，如 "BTCUSDT"。
            interval: K 线周期，默认 1m。
            fast_ema: 快线 EMA 周期，默认 10。
            slow_ema: 慢线 EMA 周期，默认 20。
            trade_size: 每次交易数量（币数），默认 0.01。
            margin_pct_per_trade: 每笔按保证金占权益百分比进行 sizing，None 表示不用。
            gross_exposure_pct_per_trade: 每笔按名义敞口占权益百分比进行 sizing，None 表示不用。
            capital_pct_per_trade: 每笔使用账户总权益百分比（0-100），None 表示不用。
            sizing_leverage: 使用百分比 sizing 时应用的杠杆倍数。

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
            margin_pct_per_trade=margin_pct_per_trade,
            gross_exposure_pct_per_trade=gross_exposure_pct_per_trade,
            capital_pct_per_trade=capital_pct_per_trade,
            sizing_leverage=sizing_leverage,
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
        margin_pct_per_trade: float | None = None,
        gross_exposure_pct_per_trade: float | None = None,
        capital_pct_per_trade: float | None = None,
        sizing_leverage: float = 1.0,
        adx_period: int = 14,
        adx_threshold: float = 20.0,
    ) -> tuple[type[EMAPullbackATRStrategy], EMAPullbackATRConfig]:
        """创建 EMA 回撤策略及其配置.

        Args:
            symbol: Trading symbol to process.
            interval: Time interval used by the operation.
            fast_ema: Fast ema.
            slow_ema: Slow ema.
            pullback_atr_multiplier: Pullback atr multiplier.
            trade_size: Trade size.
            margin_pct_per_trade: Margin pct per trade.
            gross_exposure_pct_per_trade: Gross exposure pct per trade.
            capital_pct_per_trade: Capital pct per trade.
            sizing_leverage: Sizing leverage.
            adx_period: Adx period.
            adx_threshold: Adx threshold.
        """
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
            margin_pct_per_trade=margin_pct_per_trade,
            gross_exposure_pct_per_trade=gross_exposure_pct_per_trade,
            capital_pct_per_trade=capital_pct_per_trade,
            sizing_leverage=sizing_leverage,
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

    def create_turtle_strategy(
        self,
        symbol: str,
        interval: Interval = Interval.MINUTE_1,
        entry_period: int = 20,
        exit_period: int = 10,
        atr_period: int = 20,
        stop_atr_multiplier: float = 2.0,
        unit_add_atr_step: float = 0.5,
        max_units: int = 4,
        trade_size: Decimal = Decimal("0.01"),
        margin_pct_per_trade: float | None = None,
        gross_exposure_pct_per_trade: float | None = None,
        capital_pct_per_trade: float | None = None,
        sizing_leverage: float = 1.0,
    ) -> tuple[type[TurtleStrategy], TurtleConfig]:
        """创建海龟交易策略及其配置.

        Args:
            symbol: Trading symbol to process.
            interval: Time interval used by the operation.
            entry_period: Entry period.
            exit_period: Exit period.
            atr_period: Atr period.
            stop_atr_multiplier: Stop atr multiplier.
            unit_add_atr_step: Unit add atr step.
            max_units: Max units.
            trade_size: Trade size.
            margin_pct_per_trade: Margin pct per trade.
            gross_exposure_pct_per_trade: Gross exposure pct per trade.
            capital_pct_per_trade: Capital pct per trade.
            sizing_leverage: Sizing leverage.
        """
        instrument_id = InstrumentId.from_str(f"{symbol}-PERP.BINANCE")
        nautilus_interval = INTERVAL_TO_NAUTILUS[interval]
        if interval == Interval.MINUTE_1:
            bar_type = BarType.from_str(f"{instrument_id}-{nautilus_interval}-LAST-EXTERNAL")
        else:
            bar_type = BarType.from_str(
                f"{instrument_id}-{nautilus_interval}-LAST-INTERNAL@1-MINUTE-EXTERNAL",
            )

        config = TurtleConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            entry_period=entry_period,
            exit_period=exit_period,
            atr_period=atr_period,
            stop_atr_multiplier=stop_atr_multiplier,
            unit_add_atr_step=unit_add_atr_step,
            max_units=max_units,
            trade_size=trade_size,
            margin_pct_per_trade=margin_pct_per_trade,
            gross_exposure_pct_per_trade=gross_exposure_pct_per_trade,
            capital_pct_per_trade=capital_pct_per_trade,
            sizing_leverage=sizing_leverage,
        )
        logger.info(
            "strategy_created",
            strategy="Turtle",
            symbol=symbol,
            interval=interval.value,
            entry_period=entry_period,
            exit_period=exit_period,
            atr_period=atr_period,
            max_units=max_units,
        )
        return TurtleStrategy, config

    def create_micro_scalp_strategy(
        self,
        symbol: str,
        interval: Interval = Interval.MINUTE_1,
        trade_size: Decimal = Decimal("0.01"),
        margin_pct_per_trade: float | None = None,
        gross_exposure_pct_per_trade: float | None = None,
        capital_pct_per_trade: float | None = None,
        sizing_leverage: float = 1.0,
        fast_ema: int = 8,
        slow_ema: int = 21,
        rsi_period: int = 7,
        adx_period: int = 14,
        trend_adx_threshold: float = 18.0,
        entry_pullback_atr: float = 0.35,
        oversold_level: float = 24.0,
        overbought_level: float = 76.0,
        signal_cooldown_bars: int = 2,
        atr_sl_multiplier: float = 0.45,
        atr_tp_multiplier: float = 0.8,
        maker_offset_ticks: int = 1,
        limit_ttl_ms: int = 2500,
        chase_ticks: int = 2,
        post_only: bool = True,
    ) -> tuple[type[MicroScalpStrategy], MicroScalpConfig]:
        """创建 micro scalp 策略及其配置.

        Args:
            symbol: Trading symbol to process.
            interval: Time interval used by the operation.
            trade_size: Trade size.
            margin_pct_per_trade: Margin pct per trade.
            gross_exposure_pct_per_trade: Gross exposure pct per trade.
            capital_pct_per_trade: Capital pct per trade.
            sizing_leverage: Sizing leverage.
            fast_ema: Fast ema.
            slow_ema: Slow ema.
            rsi_period: Rsi period.
            adx_period: Adx period.
            trend_adx_threshold: Trend adx threshold.
            entry_pullback_atr: Entry pullback atr.
            oversold_level: Oversold level.
            overbought_level: Overbought level.
            signal_cooldown_bars: Signal cooldown bars.
            atr_sl_multiplier: Atr sl multiplier.
            atr_tp_multiplier: Atr tp multiplier.
            maker_offset_ticks: Maker offset ticks.
            limit_ttl_ms: Time value in milliseconds for limit ttl.
            chase_ticks: Chase ticks.
            post_only: Post only.
        """
        instrument_id = InstrumentId.from_str(f"{symbol}-PERP.BINANCE")
        nautilus_interval = INTERVAL_TO_NAUTILUS[interval]
        if interval == Interval.MINUTE_1:
            bar_type = BarType.from_str(f"{instrument_id}-{nautilus_interval}-LAST-EXTERNAL")
        else:
            bar_type = BarType.from_str(
                f"{instrument_id}-{nautilus_interval}-LAST-INTERNAL@1-MINUTE-EXTERNAL",
            )

        config = MicroScalpConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_size=trade_size,
            margin_pct_per_trade=margin_pct_per_trade,
            gross_exposure_pct_per_trade=gross_exposure_pct_per_trade,
            capital_pct_per_trade=capital_pct_per_trade,
            sizing_leverage=sizing_leverage,
            fast_ema_period=fast_ema,
            slow_ema_period=slow_ema,
            rsi_period=rsi_period,
            adx_period=adx_period,
            trend_adx_threshold=trend_adx_threshold,
            entry_pullback_atr=entry_pullback_atr,
            oversold_level=oversold_level,
            overbought_level=overbought_level,
            signal_cooldown_bars=signal_cooldown_bars,
            atr_sl_multiplier=atr_sl_multiplier,
            atr_tp_multiplier=atr_tp_multiplier,
            maker_offset_ticks=maker_offset_ticks,
            limit_ttl_ms=limit_ttl_ms,
            chase_ticks=chase_ticks,
            post_only=post_only,
        )
        logger.info(
            "strategy_created",
            strategy="MicroScalp",
            symbol=symbol,
            interval=interval.value,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
            rsi_period=rsi_period,
            adx_threshold=trend_adx_threshold,
        )
        return MicroScalpStrategy, config

    def create_vegas_tunnel_strategy(
        self,
        symbol: str,
        interval: Interval = Interval.HOUR_1,
        trade_size: Decimal = Decimal("0.01"),
        margin_pct_per_trade: float | None = None,
        gross_exposure_pct_per_trade: float | None = None,
        capital_pct_per_trade: float | None = None,
        sizing_leverage: float = 1.0,
        fast_ema: int = 12,
        slow_ema: int = 36,
        tunnel_ema_1: int = 144,
        tunnel_ema_2: int = 169,
        signal_cooldown_bars: int = 3,
        atr_filter_min_ratio: float = 0.0,
        stop_atr_multiplier: float = 1.0,
        tp_fib_1: float = 1.0,
        tp_fib_2: float = 1.618,
        tp_fib_3: float = 2.618,
        tp_split_1: float = 0.4,
        tp_split_2: float = 0.3,
        tp_split_3: float = 0.3,
    ) -> tuple[type[VegasTunnelStrategy], VegasTunnelConfig]:
        """创建 Vegas Tunnel 策略及其配置.

        Args:
            symbol: Trading symbol to process.
            interval: Time interval used by the operation.
            trade_size: Trade size.
            margin_pct_per_trade: Margin pct per trade.
            gross_exposure_pct_per_trade: Gross exposure pct per trade.
            capital_pct_per_trade: Capital pct per trade.
            sizing_leverage: Sizing leverage.
            fast_ema: Fast ema.
            slow_ema: Slow ema.
            tunnel_ema_1: Tunnel ema 1.
            tunnel_ema_2: Tunnel ema 2.
            signal_cooldown_bars: Signal cooldown bars.
            atr_filter_min_ratio: Atr filter min ratio.
            stop_atr_multiplier: Stop atr multiplier.
            tp_fib_1: Tp fib 1.
            tp_fib_2: Tp fib 2.
            tp_fib_3: Tp fib 3.
            tp_split_1: Tp split 1.
            tp_split_2: Tp split 2.
            tp_split_3: Tp split 3.
        """
        instrument_id = InstrumentId.from_str(f"{symbol}-PERP.BINANCE")
        nautilus_interval = INTERVAL_TO_NAUTILUS[interval]
        if interval == Interval.MINUTE_1:
            bar_type = BarType.from_str(f"{instrument_id}-{nautilus_interval}-LAST-EXTERNAL")
        else:
            bar_type = BarType.from_str(
                f"{instrument_id}-{nautilus_interval}-LAST-INTERNAL@1-MINUTE-EXTERNAL",
            )

        config = VegasTunnelConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_size=trade_size,
            margin_pct_per_trade=margin_pct_per_trade,
            gross_exposure_pct_per_trade=gross_exposure_pct_per_trade,
            capital_pct_per_trade=capital_pct_per_trade,
            sizing_leverage=sizing_leverage,
            fast_ema_period=fast_ema,
            slow_ema_period=slow_ema,
            tunnel_ema_period_1=tunnel_ema_1,
            tunnel_ema_period_2=tunnel_ema_2,
            signal_cooldown_bars=signal_cooldown_bars,
            atr_filter_min_ratio=atr_filter_min_ratio,
            stop_atr_multiplier=stop_atr_multiplier,
            tp_fib_1=tp_fib_1,
            tp_fib_2=tp_fib_2,
            tp_fib_3=tp_fib_3,
            tp_split_1=tp_split_1,
            tp_split_2=tp_split_2,
            tp_split_3=tp_split_3,
        )
        logger.info(
            "strategy_created",
            strategy="VegasTunnel",
            symbol=symbol,
            interval=interval.value,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
            tunnel_ema_1=tunnel_ema_1,
            tunnel_ema_2=tunnel_ema_2,
        )
        return VegasTunnelStrategy, config

    def create_strategy_from_config(
        self,
        strategy_cfg: dict[str, Any],
        symbol: str,
        interval: Interval,
    ) -> tuple[type[BaseStrategy], BaseStrategyConfig]:
        """根据 YAML 策略配置动态创建策略.

        从 configs/strategies/*.yaml 加载的配置字典创建对应策略。
        目前支持 "ema_cross"、"ema_pullback_atr"、"turtle"、"micro_scalp"、"vegas_tunnel"，后续可扩展。

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
            margin_pct = params.get("margin_pct_per_trade")
            gross_exposure_pct = params.get("gross_exposure_pct_per_trade")
            capital_pct = params.get("capital_pct_per_trade")
            return self.create_ema_cross_strategy(
                symbol=symbol,
                interval=interval,
                fast_ema=params.get("fast_ema_period", 10),
                slow_ema=params.get("slow_ema_period", 20),
                trade_size=Decimal(str(params.get("trade_size", "0.01"))),
                margin_pct_per_trade=float(margin_pct) if margin_pct is not None else None,
                gross_exposure_pct_per_trade=float(gross_exposure_pct) if gross_exposure_pct is not None else None,
                capital_pct_per_trade=float(capital_pct) if capital_pct is not None else None,
                sizing_leverage=float(params.get("sizing_leverage", 1.0)),
            )

        if name == "ema_pullback_atr":
            margin_pct = params.get("margin_pct_per_trade")
            gross_exposure_pct = params.get("gross_exposure_pct_per_trade")
            capital_pct = params.get("capital_pct_per_trade")
            return self.create_ema_pullback_atr_strategy(
                symbol=symbol,
                interval=interval,
                fast_ema=params.get("fast_ema_period", 20),
                slow_ema=params.get("slow_ema_period", 50),
                pullback_atr_multiplier=float(params.get("pullback_atr_multiplier", 1.0)),
                trade_size=Decimal(str(params.get("trade_size", "0.01"))),
                margin_pct_per_trade=float(margin_pct) if margin_pct is not None else None,
                gross_exposure_pct_per_trade=float(gross_exposure_pct) if gross_exposure_pct is not None else None,
                capital_pct_per_trade=float(capital_pct) if capital_pct is not None else None,
                sizing_leverage=float(params.get("sizing_leverage", 1.0)),
                adx_period=int(params.get("adx_period", 14)),
                adx_threshold=float(params.get("adx_threshold", 20.0)),
            )

        if name == "turtle":
            margin_pct = params.get("margin_pct_per_trade")
            gross_exposure_pct = params.get("gross_exposure_pct_per_trade")
            capital_pct = params.get("capital_pct_per_trade")
            return self.create_turtle_strategy(
                symbol=symbol,
                interval=interval,
                entry_period=int(params.get("entry_period", 20)),
                exit_period=int(params.get("exit_period", 10)),
                atr_period=int(params.get("atr_period", 20)),
                stop_atr_multiplier=float(params.get("stop_atr_multiplier", 2.0)),
                unit_add_atr_step=float(params.get("unit_add_atr_step", 0.5)),
                max_units=int(params.get("max_units", 4)),
                trade_size=Decimal(str(params.get("trade_size", "0.01"))),
                margin_pct_per_trade=float(margin_pct) if margin_pct is not None else None,
                gross_exposure_pct_per_trade=float(gross_exposure_pct) if gross_exposure_pct is not None else None,
                capital_pct_per_trade=float(capital_pct) if capital_pct is not None else None,
                sizing_leverage=float(params.get("sizing_leverage", 1.0)),
            )

        if name == "micro_scalp":
            margin_pct = params.get("margin_pct_per_trade")
            gross_exposure_pct = params.get("gross_exposure_pct_per_trade")
            capital_pct = params.get("capital_pct_per_trade")
            return self.create_micro_scalp_strategy(
                symbol=symbol,
                interval=interval,
                trade_size=Decimal(str(params.get("trade_size", "0.01"))),
                margin_pct_per_trade=float(margin_pct) if margin_pct is not None else None,
                gross_exposure_pct_per_trade=float(gross_exposure_pct) if gross_exposure_pct is not None else None,
                capital_pct_per_trade=float(capital_pct) if capital_pct is not None else None,
                sizing_leverage=float(params.get("sizing_leverage", 1.0)),
                fast_ema=int(params.get("fast_ema_period", 8)),
                slow_ema=int(params.get("slow_ema_period", 21)),
                rsi_period=int(params.get("rsi_period", 7)),
                adx_period=int(params.get("adx_period", 14)),
                trend_adx_threshold=float(params.get("trend_adx_threshold", 18.0)),
                entry_pullback_atr=float(params.get("entry_pullback_atr", 0.35)),
                oversold_level=float(params.get("oversold_level", 24.0)),
                overbought_level=float(params.get("overbought_level", 76.0)),
                signal_cooldown_bars=int(params.get("signal_cooldown_bars", 2)),
                atr_sl_multiplier=float(params.get("atr_sl_multiplier", 0.45)),
                atr_tp_multiplier=float(params.get("atr_tp_multiplier", 0.8)),
                maker_offset_ticks=int(params.get("maker_offset_ticks", 1)),
                limit_ttl_ms=int(params.get("limit_ttl_ms", 2500)),
                chase_ticks=int(params.get("chase_ticks", 2)),
                post_only=bool(params.get("post_only", True)),
            )

        if name == "vegas_tunnel":
            margin_pct = params.get("margin_pct_per_trade")
            gross_exposure_pct = params.get("gross_exposure_pct_per_trade")
            capital_pct = params.get("capital_pct_per_trade")
            return self.create_vegas_tunnel_strategy(
                symbol=symbol,
                interval=interval,
                trade_size=Decimal(str(params.get("trade_size", "0.01"))),
                margin_pct_per_trade=float(margin_pct) if margin_pct is not None else None,
                gross_exposure_pct_per_trade=float(gross_exposure_pct) if gross_exposure_pct is not None else None,
                capital_pct_per_trade=float(capital_pct) if capital_pct is not None else None,
                sizing_leverage=float(params.get("sizing_leverage", 1.0)),
                fast_ema=int(params.get("fast_ema_period", 12)),
                slow_ema=int(params.get("slow_ema_period", 36)),
                tunnel_ema_1=int(params.get("tunnel_ema_period_1", 144)),
                tunnel_ema_2=int(params.get("tunnel_ema_period_2", 169)),
                signal_cooldown_bars=int(params.get("signal_cooldown_bars", 3)),
                atr_filter_min_ratio=float(params.get("atr_filter_min_ratio", 0.0)),
                stop_atr_multiplier=float(params.get("stop_atr_multiplier", 1.0)),
                tp_fib_1=float(params.get("tp_fib_1", 1.0)),
                tp_fib_2=float(params.get("tp_fib_2", 1.618)),
                tp_fib_3=float(params.get("tp_fib_3", 2.618)),
                tp_split_1=float(params.get("tp_split_1", 0.4)),
                tp_split_2=float(params.get("tp_split_2", 0.3)),
                tp_split_3=float(params.get("tp_split_3", 0.3)),
            )

        raise ValueError(f"Unsupported strategy: '{name}'. Available: ema_cross, ema_pullback_atr, turtle, micro_scalp, vegas_tunnel")

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
        # 优先复用容器内已按配置初始化的适配器
        if self._container.binance_adapter is not None:
            logger.info("binance_adapter_reused_from_container")
            return self._container.binance_adapter

        # 回退：按参数临时构建（开发调试 / 单元测试场景）
        if environment is None:
            environment = BinanceEnvironment.LIVE if self._config.env == "prod" else BinanceEnvironment.TESTNET
        cache_settings = build_nautilus_cache_settings(self._config, mode="live")
        adapter = build_binance_adapter(
            environment=environment,
            symbols=symbols,
            leverages=leverages,
            proxy_url=proxy_url,
            cache=cache_settings.cache,
            instance_id=cache_settings.instance_id,
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
