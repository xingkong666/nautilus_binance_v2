"""Test PreTradeRiskManager leverage validation."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.events import EventBus, OrderIntentEvent
from src.risk.pre_trade import PreTradeRiskManager


@pytest.fixture
def event_bus():
    """Mock event bus."""
    return MagicMock(spec=EventBus)


@pytest.fixture
def config():
    """Risk manager config."""
    return {
        "max_order_size_usd": 10000,
        "max_position_size_usd": 50000,
        "max_leverage": 10,
        "min_order_interval_ms": 500,
        "max_open_orders": 5,
    }


@pytest.fixture
def risk_manager(event_bus, config):
    """PreTradeRiskManager instance."""
    return PreTradeRiskManager(event_bus, config)


@pytest.fixture
def order_intent():
    """Sample order intent."""
    return OrderIntentEvent(
        instrument_id="BTCUSDT",
        side="BUY",
        quantity=Decimal("1.0"),
        order_type="MARKET",
    )


@pytest.mark.parametrize(
    "leverage,max_lev,should_pass",
    [
        (5.0, 10, True),  # 低于限制
        (10.0, 10, True),  # 处于极限
        (15.0, 10, False),  # 超过限制
        (0.0, 10, True),  # unknown杠杆 - 跳过检查
        (-1.0, 10, True),  # 无效杠杆 - 跳过检查
    ],
)
def test_leverage_check(leverage, max_lev, should_pass, event_bus, order_intent):
    """Test leverage validation."""
    config = {
        "max_order_size_usd": 100000,  # 增加以避免订单size 失败
        "max_position_size_usd": 500000,  # 增加以避免位置size 失败
        "max_leverage": max_lev,
        "min_order_interval_ms": 0,  # 禁用间隔检查
        "max_open_orders": 50,  # 高数量以避免未结订单失败
    }

    risk_manager = PreTradeRiskManager(event_bus, config)

    result = risk_manager.check(
        intent=order_intent,
        current_position_usd=Decimal("1000"),
        current_open_orders=1,
        current_price=Decimal("50000"),
        current_leverage=leverage,
    )

    if should_pass:
        assert result.passed, f"Expected pass but got: {result.reason}"
    else:
        assert not result.passed, "Expected failure but check passed"
        assert "杠杆超限" in result.reason, f"Expected leverage error message but got: {result.reason}"
        assert f"{leverage:.1f}x" in result.reason
        assert f"{max_lev}x" in result.reason


def test_leverage_check_with_zero_leverage_skips_validation(event_bus, order_intent):
    """Test that zero leverage skips validation."""
    config = {
        "max_order_size_usd": 100000,
        "max_position_size_usd": 500000,
        "max_leverage": 5,
        "min_order_interval_ms": 0,
        "max_open_orders": 50,
    }

    risk_manager = PreTradeRiskManager(event_bus, config)

    result = risk_manager.check(
        intent=order_intent,
        current_position_usd=Decimal("1000"),
        current_open_orders=1,
        current_price=Decimal("50000"),
        current_leverage=0.0,
    )

    assert result.passed, f"Zero leverage should pass but got: {result.reason}"


def test_leverage_check_prometheus_counter_incremented(event_bus, order_intent):
    """Test that Prometheus counter is incremented on leverage failure."""
    from unittest.mock import patch

    config = {
        "max_order_size_usd": 100000,
        "max_position_size_usd": 500000,
        "max_leverage": 5,
        "min_order_interval_ms": 0,
        "max_open_orders": 50,
    }

    risk_manager = PreTradeRiskManager(event_bus, config)

    with patch("src.risk.pre_trade.RISK_CHECKS_TOTAL") as mock_counter:
        result = risk_manager.check(
            intent=order_intent,
            current_position_usd=Decimal("1000"),
            current_open_orders=1,
            current_price=Decimal("50000"),
            current_leverage=10.0,  # 超过最大值 5
        )

        assert not result.passed
        mock_counter.labels.assert_called_once_with(check_type="leverage", result="fail")
        mock_counter.labels.return_value.inc.assert_called_once()


def test_leverage_check_event_bus_alert_published(event_bus, order_intent):
    """Test that RiskAlertEvent is published on leverage failure."""
    config = {
        "max_order_size_usd": 100000,
        "max_position_size_usd": 500000,
        "max_leverage": 5,
        "min_order_interval_ms": 0,
        "max_open_orders": 50,
    }

    risk_manager = PreTradeRiskManager(event_bus, config)

    result = risk_manager.check(
        intent=order_intent,
        current_position_usd=Decimal("1000"),
        current_open_orders=1,
        current_price=Decimal("50000"),
        current_leverage=10.0,  # 超过最大值 5
    )

    assert not result.passed

    # 验证RiskAlertEvent已发布
    event_bus.publish.assert_called_once()
    published_event = event_bus.publish.call_args[0][0]
    assert published_event.level == "ERROR"
    assert published_event.rule_name == "pre_trade"
    assert "杠杆超限" in published_event.message
    assert published_event.details["instrument_id"] == "BTCUSDT"
    assert published_event.details["side"] == "BUY"
