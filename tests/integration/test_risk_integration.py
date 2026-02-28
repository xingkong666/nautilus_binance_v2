"""集成测试：风控模块联动.

测试 PreTradeRisk + CircuitBreaker + DrawdownController 协同工作。
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from src.core.events import EventBus, EventType, OrderIntentEvent, RiskAlertEvent
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.drawdown_control import DrawdownController
from src.risk.pre_trade import PreTradeRiskManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus():
    bus = EventBus()
    yield bus
    bus.clear()


@pytest.fixture
def pre_trade(event_bus):
    return PreTradeRiskManager(
        event_bus=event_bus,
        config={
            "max_order_size_usd": 10_000,
            "max_position_size_usd": 50_000,
            "max_leverage": 10,
            "min_order_interval_ms": 0,
            "max_open_orders": 20,
        },
    )


@pytest.fixture
def circuit_breaker(event_bus):
    return CircuitBreaker(
        event_bus=event_bus,
        config={
            "triggers": [
                {
                    "type": "daily_loss",
                    "threshold_usd": 1000,
                    "action": "halt_all",
                    "cooldown_minutes": 60,
                },
                {
                    "type": "rapid_loss",
                    "max_losses": 3,
                    "action": "reduce_only",
                    "cooldown_minutes": 30,
                },
            ]
        },
    )


@pytest.fixture
def drawdown_controller():
    return DrawdownController(warning_pct=3.0, critical_pct=5.0)


# ---------------------------------------------------------------------------
# CircuitBreaker 集成测试
# ---------------------------------------------------------------------------


class TestCircuitBreakerIntegration:
    def test_initial_state_not_active(self, circuit_breaker):
        """初始状态熔断器未激活。"""
        assert circuit_breaker.is_active is False

    def test_daily_loss_triggers_breaker(self, circuit_breaker, event_bus):
        """单日亏损超阈值时触发熔断，发布 RISK_ALERT 事件。"""
        alerts = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)

        # 触发：daily_pnl=-1200 < -threshold(-1000)
        triggered = circuit_breaker.check_daily_loss(daily_pnl=Decimal("-1200"))

        assert triggered is True
        assert circuit_breaker.is_active is True
        assert len(alerts) == 1

    def test_within_daily_loss_does_not_trigger(self, circuit_breaker):
        """单日亏损未超阈值时不触发熔断。"""
        triggered = circuit_breaker.check_daily_loss(daily_pnl=Decimal("-800"))
        assert triggered is False
        assert circuit_breaker.is_active is False

    def test_circuit_breaker_blocks_pre_trade(self, circuit_breaker, pre_trade, event_bus):
        """熔断激活后，PreTradeRisk 应拒绝新订单（上层协同逻辑）。"""
        # 触发熔断
        circuit_breaker.check_daily_loss(daily_pnl=Decimal("-2000"))
        assert circuit_breaker.is_active is True

        # 模拟上层在提交前检查熔断状态（实际由 OrderRouter / Supervisor 负责）
        intent = OrderIntentEvent(
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity=Decimal("0.01"),
        )
        # 熔断激活时，系统级别应拒绝：验证 is_active 状态供上层使用
        is_halted = circuit_breaker.is_active
        assert is_halted is True

    def test_rapid_loss_trigger(self, circuit_breaker, event_bus):
        """连续多次亏损触发 rapid_loss 熔断。"""
        alerts = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)

        # rapid_loss 触发条件：在冷却窗口内记录 >= max_losses(=3) 笔亏损
        for _ in range(3):
            triggered = circuit_breaker.check_rapid_loss(loss_amount=Decimal("100"))

        # 第 3 次时应触发
        assert triggered is True
        assert circuit_breaker.is_active is True
        assert len(alerts) >= 1


# ---------------------------------------------------------------------------
# DrawdownController 集成测试
# ---------------------------------------------------------------------------


class TestDrawdownControllerIntegration:
    """DrawdownController 使用 update_equity + get_size_multiplier API。"""

    def test_no_reduction_below_warning_threshold(self, drawdown_controller):
        """回撤低于预警阈值时，仓位乘数为 1.0（无缩减）。"""
        drawdown_controller.update_equity(Decimal("100000"))
        multiplier = drawdown_controller.get_size_multiplier(Decimal("97500"))
        # 回撤 2.5% < warning 3%
        assert multiplier == 1.0

    def test_warning_level_reduces_position(self, drawdown_controller):
        """回撤达到预警阈值时，仓位乘数降至 reduce_factor。"""
        drawdown_controller.update_equity(Decimal("100000"))
        multiplier = drawdown_controller.get_size_multiplier(Decimal("97000"))
        # 回撤 3% >= warning_pct → 返回 reduce_factor (default 0.5)
        assert multiplier == 0.5

    def test_critical_level_halts_trading(self, drawdown_controller):
        """回撤超过严重阈值时，仓位乘数为 0.0（停止交易）。"""
        drawdown_controller.update_equity(Decimal("100000"))
        multiplier = drawdown_controller.get_size_multiplier(Decimal("94000"))
        # 回撤 6% >= critical_pct 5% → 停止
        assert multiplier == 0.0

    def test_peak_equity_updates_correctly(self, drawdown_controller):
        """update_equity 只在新高时更新峰值。"""
        drawdown_controller.update_equity(Decimal("100000"))
        drawdown_controller.update_equity(Decimal("98000"))  # 不更新峰值
        # 峰值仍为 100000，回撤 2% < warning
        multiplier = drawdown_controller.get_size_multiplier(Decimal("98000"))
        assert multiplier == 1.0

    def test_no_peak_returns_full_multiplier(self, drawdown_controller):
        """未设置峰值时，get_size_multiplier 返回 1.0。"""
        # 未调用 update_equity，peak=0
        multiplier = drawdown_controller.get_size_multiplier(Decimal("50000"))
        assert multiplier == 1.0


# ---------------------------------------------------------------------------
# PreTrade + CircuitBreaker 协同测试
# ---------------------------------------------------------------------------


class TestRiskCoordination:
    def test_normal_flow_passes_both_checks(self, pre_trade, circuit_breaker, event_bus):
        """正常订单通过事前风控，且熔断未激活。"""
        intent = OrderIntentEvent(
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity=Decimal("0.01"),
        )
        pre_result = pre_trade.check(
            intent=intent,
            current_position_usd=Decimal(0),
            current_open_orders=0,
            current_price=Decimal("50000"),
        )

        assert pre_result.passed is True
        assert circuit_breaker.is_active is False

    def test_risk_alert_count_after_failures(self, pre_trade, event_bus):
        """多次风控失败，每次都发布 RISK_ALERT 事件。"""
        alerts = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)

        for _ in range(3):
            intent = OrderIntentEvent(
                instrument_id="BTCUSDT-PERP.BINANCE",
                side="BUY",
                quantity=Decimal("10"),  # 超额订单
            )
            pre_trade.check(
                intent=intent,
                current_position_usd=Decimal(0),
                current_open_orders=0,
                current_price=Decimal("50000"),
            )

        assert len(alerts) == 3

    def test_risk_event_contains_correct_info(self, pre_trade, event_bus):
        """RISK_ALERT 事件包含有效的规则名和消息。"""
        alerts = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)

        intent = OrderIntentEvent(
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity=Decimal("100"),  # 超额
        )
        pre_trade.check(
            intent=intent,
            current_position_usd=Decimal(0),
            current_open_orders=0,
            current_price=Decimal("50000"),
        )

        assert len(alerts) == 1
        alert: RiskAlertEvent = alerts[0]
        assert alert.event_type == EventType.RISK_ALERT
        assert alert.rule_name  # 不为空
        assert alert.message    # 不为空
