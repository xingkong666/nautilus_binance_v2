"""AppFactory 策略加载测试."""

from __future__ import annotations

from src.app.factory import AppFactory
from src.core.enums import Interval
from src.strategy.micro_scalp import MicroScalpConfig, MicroScalpStrategy
from src.strategy.turtle import TurtleConfig, TurtleStrategy
from src.strategy.vegas_tunnel import VegasTunnelConfig, VegasTunnelStrategy


class _DummyContainer:
    def __init__(self) -> None:
        self.config = object()
        self.binance_adapter = None


def test_create_strategy_from_config_supports_turtle() -> None:
    """Verify that create strategy from config supports turtle."""
    factory = AppFactory(container=_DummyContainer())  # type: ignore[arg-type]
    strategy_cfg = {
        "name": "turtle",
        "params": {
            "entry_period": 20,
            "exit_period": 10,
            "atr_period": 20,
            "stop_atr_multiplier": 2.0,
            "unit_add_atr_step": 0.5,
            "max_units": 4,
            "trade_size": "0.02",
        },
    }

    strategy_cls, cfg = factory.create_strategy_from_config(
        strategy_cfg=strategy_cfg,
        symbol="BTCUSDT",
        interval=Interval.MINUTE_15,
    )

    assert strategy_cls is TurtleStrategy
    assert isinstance(cfg, TurtleConfig)
    assert cfg.entry_period == 20
    assert cfg.exit_period == 10
    assert str(cfg.trade_size) == "0.02"


def test_create_strategy_from_config_supports_micro_scalp() -> None:
    """Verify that create strategy from config supports micro scalp."""
    factory = AppFactory(container=_DummyContainer())  # type: ignore[arg-type]
    strategy_cfg = {
        "name": "micro_scalp",
        "params": {
            "fast_ema_period": 8,
            "slow_ema_period": 21,
            "rsi_period": 7,
            "trend_adx_threshold": 18.0,
            "entry_pullback_atr": 0.35,
            "maker_offset_ticks": 1,
            "limit_ttl_ms": 2500,
            "chase_ticks": 2,
            "post_only": True,
            "trade_size": "0.01",
        },
    }

    strategy_cls, cfg = factory.create_strategy_from_config(
        strategy_cfg=strategy_cfg,
        symbol="BTCUSDT",
        interval=Interval.MINUTE_1,
    )

    assert strategy_cls is MicroScalpStrategy
    assert isinstance(cfg, MicroScalpConfig)
    assert cfg.fast_ema_period == 8
    assert cfg.slow_ema_period == 21
    assert cfg.post_only is True


def test_create_strategy_from_config_supports_vegas_tunnel() -> None:
    """Verify that create strategy from config supports vegas tunnel."""
    factory = AppFactory(container=_DummyContainer())  # type: ignore[arg-type]
    strategy_cfg = {
        "name": "vegas_tunnel",
        "params": {
            "fast_ema_period": 12,
            "slow_ema_period": 36,
            "tunnel_ema_period_1": 144,
            "tunnel_ema_period_2": 169,
            "stop_atr_multiplier": 1.0,
            "tp_fib_1": 1.0,
            "tp_fib_2": 1.618,
            "tp_fib_3": 2.618,
            "tp_split_1": 0.4,
            "tp_split_2": 0.3,
            "tp_split_3": 0.3,
            "trade_size": "0.03",
        },
    }

    strategy_cls, cfg = factory.create_strategy_from_config(
        strategy_cfg=strategy_cfg,
        symbol="BTCUSDT",
        interval=Interval.HOUR_1,
    )

    assert strategy_cls is VegasTunnelStrategy
    assert isinstance(cfg, VegasTunnelConfig)
    assert cfg.fast_ema_period == 12
    assert cfg.slow_ema_period == 36
    assert cfg.tunnel_ema_period_1 == 144
    assert cfg.tunnel_ema_period_2 == 169
    assert str(cfg.trade_size) == "0.03"


def test_create_strategy_from_config_loads_leverage_aware_sizing() -> None:
    """Verify that create strategy from config loads leverage aware sizing."""
    factory = AppFactory(container=_DummyContainer())  # type: ignore[arg-type]
    strategy_cfg = {
        "name": "ema_pullback_atr",
        "params": {
            "fast_ema_period": 20,
            "slow_ema_period": 50,
            "margin_pct_per_trade": 8.0,
            "gross_exposure_pct_per_trade": 120.0,
            "capital_pct_per_trade": 15.0,
            "sizing_leverage": 10.0,
        },
    }

    _, cfg = factory.create_strategy_from_config(
        strategy_cfg=strategy_cfg,
        symbol="BTCUSDT",
        interval=Interval.HOUR_1,
    )

    assert cfg.margin_pct_per_trade == 8.0
    assert cfg.gross_exposure_pct_per_trade == 120.0
    assert cfg.capital_pct_per_trade == 15.0
    assert cfg.sizing_leverage == 10.0
