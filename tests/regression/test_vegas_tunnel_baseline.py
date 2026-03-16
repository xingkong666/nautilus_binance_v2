"""回归测试：Vegas Tunnel 策略基准锁定."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.strategy.vegas_tunnel import VegasTunnelConfig, VegasTunnelStrategy
from tests.regression.conftest import BAR_TYPE, BTCUSDT, STARTING_BALANCE, build_engine, make_sine_bars

CLASSIC_SINE_BASELINE = {
    "iterations": 200,
    "total_orders": 0,
    "total_positions": 0,
}

FAST_TUNNEL_SINE_BASELINE = {
    "iterations": 400,
    "total_orders": 75,
    "total_positions": 15,
}


def run_vegas(
    bars,
    tunnel_ema_1: int = 144,
    tunnel_ema_2: int = 169,
    trade_size: str = "0.010",
    starting_balance: int = STARTING_BALANCE,
) -> dict[str, Any]:
    """Run run vegas.

    Args:
        bars: Bar data used by the operation.
        tunnel_ema_1: Tunnel EMA 1.
        tunnel_ema_2: Tunnel EMA 2.
        trade_size: Trade size.
        starting_balance: Starting balance.

    Returns:
        dict[str, Any]: Dictionary representation of the result.
    """
    engine = build_engine(starting_balance)
    engine.add_data(bars)

    cfg = VegasTunnelConfig(
        instrument_id=BTCUSDT.id,
        bar_type=BAR_TYPE,
        trade_size=Decimal(trade_size),
        fast_ema_period=12,
        slow_ema_period=36,
        tunnel_ema_period_1=tunnel_ema_1,
        tunnel_ema_period_2=tunnel_ema_2,
        signal_cooldown_bars=3,
        atr_filter_min_ratio=0.0,
        stop_atr_multiplier=1.0,
        tp_fib_1=1.0,
        tp_fib_2=1.618,
        tp_fib_3=2.618,
        tp_split_1=0.4,
        tp_split_2=0.3,
        tp_split_3=0.3,
    )
    engine.add_strategy(VegasTunnelStrategy(config=cfg))
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


def test_classic_tunnel_sine_baseline_locked() -> None:
    """Verify that classic tunnel sine baseline locked."""
    bars = make_sine_bars(n=200, base_price=50_000.0, amplitude=500.0, period=30.0)
    metrics = run_vegas(bars)

    assert metrics == CLASSIC_SINE_BASELINE


def test_fast_tunnel_sine_baseline_locked() -> None:
    """Verify that fast tunnel sine baseline locked."""
    bars = make_sine_bars(n=400, base_price=50_000.0, amplitude=1200.0, period=24.0)
    metrics = run_vegas(
        bars,
        tunnel_ema_1=21,
        tunnel_ema_2=34,
    )

    assert metrics == FAST_TUNNEL_SINE_BASELINE


def test_fast_tunnel_baseline_is_deterministic() -> None:
    """Verify that fast tunnel baseline is deterministic."""
    bars = make_sine_bars(n=400, base_price=50_000.0, amplitude=1200.0, period=24.0)
    m1 = run_vegas(bars, tunnel_ema_1=21, tunnel_ema_2=34)
    m2 = run_vegas(bars, tunnel_ema_1=21, tunnel_ema_2=34)

    assert m1 == m2


def test_trade_size_does_not_change_signal_count() -> None:
    """Verify that trade size does not change signal count."""
    bars = make_sine_bars(n=400, base_price=50_000.0, amplitude=1200.0, period=24.0)
    m_small = run_vegas(bars, tunnel_ema_1=21, tunnel_ema_2=34, trade_size="0.001")
    m_large = run_vegas(bars, tunnel_ema_1=21, tunnel_ema_2=34, trade_size="0.100")

    assert m_small["total_orders"] > 0
    assert m_large["total_orders"] > 0
    assert m_small["total_orders"] <= m_large["total_orders"]
    assert m_small["total_positions"] <= m_large["total_positions"]
