"""回归测试：Turtle 策略基准锁定."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.strategy.turtle import TurtleConfig, TurtleStrategy
from tests.regression.conftest import BAR_TYPE, BTCUSDT, STARTING_BALANCE, build_engine, make_sine_bars

SINE_TURTLE_BASELINE = {
    "iterations": 200,
    "total_orders": 44,
    "total_positions": 22,
}


def run_turtle(
    bars,
    entry_period: int = 20,
    exit_period: int = 10,
    max_units: int = 4,
    starting_balance: int = STARTING_BALANCE,
) -> dict[str, Any]:
    engine = build_engine(starting_balance)
    engine.add_data(bars)

    cfg = TurtleConfig(
        instrument_id=BTCUSDT.id,
        bar_type=BAR_TYPE,
        trade_size=Decimal("0.010"),
        entry_period=entry_period,
        exit_period=exit_period,
        atr_period=20,
        stop_atr_multiplier=2.0,
        unit_add_atr_step=0.5,
        max_units=max_units,
    )
    engine.add_strategy(TurtleStrategy(config=cfg))
    engine.sort_data()
    engine.run()
    result = engine.get_result()

    metrics = {
        "iterations": result.iterations,
        "total_orders": result.total_orders,
        "total_positions": result.total_positions,
    }
    engine.dispose()
    return metrics


def test_sine_baseline_locked() -> None:
    bars = make_sine_bars(n=200, base_price=50_000.0, amplitude=500.0, period=30.0)
    metrics = run_turtle(bars)

    assert metrics == SINE_TURTLE_BASELINE


def test_disable_pyramid_reduces_orders() -> None:
    bars = make_sine_bars(n=200, base_price=50_000.0, amplitude=500.0, period=30.0)
    base = run_turtle(bars, max_units=4)
    no_pyramid = run_turtle(bars, max_units=1)

    assert no_pyramid["total_orders"] <= base["total_orders"]
