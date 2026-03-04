"""AppFactory 策略加载测试."""

from __future__ import annotations

from src.app.factory import AppFactory
from src.core.enums import Interval
from src.strategy.turtle import TurtleConfig, TurtleStrategy


class _DummyContainer:
    def __init__(self) -> None:
        self.config = object()
        self.binance_adapter = None


def test_create_strategy_from_config_supports_turtle() -> None:
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
