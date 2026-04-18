"""Test that reset_daily() correctly clears daily state."""

from decimal import Decimal

from src.core.events import EventBus
from src.risk.real_time import RealTimeRiskMonitor


def test_reset_daily_clears_alerts():
    """After reset_daily(), alerts can fire again."""
    # 准备
    event_bus = EventBus()
    config = {
        "max_drawdown_pct": 50.0,  # 设置非常high以避免最大回撤警报
        "daily_loss_limit_usd": 1000,
        "trailing_drawdown_pct": 50.0,  # 设置非常high以避免尾随回撤警报
    }
    monitor = RealTimeRiskMonitor(
        event_bus=event_bus,
        config=config,
        redis_client=None,
    )

    # 使用 10,000 泰达币进行初始化
    initial_equity = Decimal("10000")
    monitor.initialize(initial_equity)

    # 通过将权益降至每日限额以下来触发每日损失警报
    # daily_loss_limit_usd = 1000，所以权益 < 9000 应该触发
    current_equity = Decimal("8500")  # 亏损 1500 > 1000 限额（仅回撤 15%）
    alerts = monitor.update(current_equity)

    # 验证每日损失警报是否已触发（现在应该只有 1 个警报）
    assert len(alerts) == 1
    assert "单日亏损超限" in alerts[0]
    assert "daily_loss" in monitor._alerts_fired

    # 以相同的权益再次更新 - 没有新警报（已触发）
    alerts = monitor.update(current_equity)
    assert len(alerts) == 0

    # 将每日状态重置为新的当前权益基准
    monitor.reset_daily(current_equity)

    # 验证每日状态已清除
    assert monitor._initial_equity == current_equity
    assert monitor._daily_pnl == Decimal("0")
    assert len(monitor._alerts_fired) == 0

    # 再次触发警报 - 应该触发，因为警报已清除
    # 再次跌破每日限额（新baseline 损失 1000USDT）
    new_equity = Decimal("7300")  # 8500 减少 1200 基准
    alerts = monitor.update(new_equity)

    # 验证重置后再次触发警报
    assert len(alerts) == 1
    assert "单日亏损超限" in alerts[0]


def test_reset_daily_updates_baseline():
    """reset_daily() updates the initial equity baseline."""
    # 准备
    event_bus = EventBus()
    config = {"daily_loss_limit_usd": 500}
    monitor = RealTimeRiskMonitor(
        event_bus=event_bus,
        config=config,
        redis_client=None,
    )

    # 使用 5,000 泰达币进行初始化
    initial_equity = Decimal("5000")
    monitor.initialize(initial_equity)
    assert monitor._initial_equity == initial_equity

    # 以新的股权价值重置
    new_equity = Decimal("7500")
    monitor.reset_daily(new_equity)

    # 验证新基准
    assert monitor._initial_equity == new_equity
    assert monitor._daily_pnl == Decimal("0")
