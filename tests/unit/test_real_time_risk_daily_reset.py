"""Test that reset_daily() correctly clears daily state."""

from decimal import Decimal

from src.core.events import EventBus
from src.risk.real_time import RealTimeRiskMonitor


def test_reset_daily_clears_alerts():
    """After reset_daily(), alerts can fire again."""
    # Setup
    event_bus = EventBus()
    config = {
        "max_drawdown_pct": 50.0,  # Set very high to avoid max drawdown alerts
        "daily_loss_limit_usd": 1000,
        "trailing_drawdown_pct": 50.0,  # Set very high to avoid trailing drawdown alerts
    }
    monitor = RealTimeRiskMonitor(
        event_bus=event_bus,
        config=config,
        redis_client=None,
    )

    # Initialize with 10,000 USDT
    initial_equity = Decimal("10000")
    monitor.initialize(initial_equity)

    # Trigger daily loss alert by dropping equity below daily limit
    # daily_loss_limit_usd = 1000, so equity < 9000 should trigger
    current_equity = Decimal("8500")  # Loss of 1500 > 1000 limit (only 15% drawdown)
    alerts = monitor.update(current_equity)

    # Verify daily loss alert was fired (should be only 1 alert now)
    assert len(alerts) == 1
    assert "单日亏损超限" in alerts[0]
    assert "daily_loss" in monitor._alerts_fired

    # Update again with same equity - no new alerts (already fired)
    alerts = monitor.update(current_equity)
    assert len(alerts) == 0

    # Reset daily state with current equity as new baseline
    monitor.reset_daily(current_equity)

    # Verify daily state was cleared
    assert monitor._initial_equity == current_equity
    assert monitor._daily_pnl == Decimal("0")
    assert len(monitor._alerts_fired) == 0

    # Trigger alert again - should fire because alerts were cleared
    # Drop below daily limit again (1000 USDT loss from new baseline)
    new_equity = Decimal("7300")  # Loss of 1200 from 8500 baseline
    alerts = monitor.update(new_equity)

    # Verify alert fires again after reset
    assert len(alerts) == 1
    assert "单日亏损超限" in alerts[0]


def test_reset_daily_updates_baseline():
    """reset_daily() updates the initial equity baseline."""
    # Setup
    event_bus = EventBus()
    config = {"daily_loss_limit_usd": 500}
    monitor = RealTimeRiskMonitor(
        event_bus=event_bus,
        config=config,
        redis_client=None,
    )

    # Initialize with 5,000 USDT
    initial_equity = Decimal("5000")
    monitor.initialize(initial_equity)
    assert monitor._initial_equity == initial_equity

    # Reset with new equity value
    new_equity = Decimal("7500")
    monitor.reset_daily(new_equity)

    # Verify new baseline
    assert monitor._initial_equity == new_equity
    assert monitor._daily_pnl == Decimal("0")
