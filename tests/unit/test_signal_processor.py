"""SignalProcessor 单元测试."""

from __future__ import annotations

from src.core.events import EventBus, EventType, SignalDirection, SignalEvent
from src.execution.ignored_instruments import IgnoredInstrumentRegistry
from src.execution.signal_processor import SignalProcessor


class _RouterSpy:
    def __init__(self) -> None:
        self.intents = []

    def route(self, intent):
        self.intents.append(intent)
        return True


class _RouterRejectSpy(_RouterSpy):
    def route(self, intent):
        self.intents.append(intent)
        return False


class _RateLimiterStub:
    def __init__(self, can_proceed: bool = True) -> None:
        self._can_proceed = can_proceed
        self.record_count = 0

    def can_proceed(self) -> bool:
        return self._can_proceed

    def record(self) -> None:
        self.record_count += 1


class _PreTradeRiskStub:
    def __init__(self, passed: bool = True) -> None:
        self._passed = passed
        self.calls = []

    def check(self, **kwargs):
        self.calls.append(kwargs)
        return type("CheckResult", (), {"passed": self._passed, "reason": "blocked"})()


def test_metadata_order_fields_take_precedence() -> None:
    bus = EventBus()
    router = _RouterSpy()
    SignalProcessor(event_bus=bus, order_router=router)

    bus.publish(
        SignalEvent(
            source="TurtleStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            metadata={
                "signal_action": "add",
                "order_side": "BUY",
                "order_qty": "0.2",
                "order_type": "LIMIT",
                "order_price": "50000.5",
                "time_in_force": "GTC",
                "post_only": True,
                "reduce_only": False,
            },
        )
    )

    assert len(router.intents) == 1
    intent = router.intents[0]
    assert intent.side == "BUY"
    assert str(intent.quantity) == "0.2"
    assert intent.order_type == "LIMIT"
    assert str(intent.price) == "50000.5"
    assert intent.reduce_only is False
    assert intent.strategy_id == "TurtleStrategy"


def test_fallback_to_direction_mapping_when_metadata_missing() -> None:
    bus = EventBus()
    router = _RouterSpy()
    SignalProcessor(event_bus=bus, order_router=router)

    bus.publish(
        SignalEvent(
            source="EMACrossStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            metadata={},
        )
    )

    assert len(router.intents) == 1
    intent = router.intents[0]
    assert intent.side == "BUY"
    assert str(intent.quantity) == "0.01"


def test_invalid_metadata_order_qty_is_ignored() -> None:
    bus = EventBus()
    router = _RouterSpy()
    SignalProcessor(event_bus=bus, order_router=router)

    bus.publish(
        SignalEvent(
            source="TurtleStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.SHORT,
            metadata={
                "order_side": "SELL",
                "order_qty": "not-a-number",
            },
        )
    )

    assert len(router.intents) == 0


def test_limit_order_without_price_is_ignored() -> None:
    bus = EventBus()
    router = _RouterSpy()
    SignalProcessor(event_bus=bus, order_router=router)

    bus.publish(
        SignalEvent(
            source="MicroScalpStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            metadata={
                "order_side": "BUY",
                "order_qty": "0.05",
                "order_type": "LIMIT",
            },
        )
    )

    assert len(router.intents) == 0


def test_rate_limiter_blocks_routing_and_emits_alert() -> None:
    bus = EventBus()
    router = _RouterSpy()
    rate_limiter = _RateLimiterStub(can_proceed=False)
    alerts = []
    bus.subscribe(EventType.RISK_ALERT, alerts.append)
    SignalProcessor(event_bus=bus, order_router=router, rate_limiter=rate_limiter)

    bus.publish(
        SignalEvent(
            source="EMACrossStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            metadata={"bar_close": "50000"},
        )
    )

    assert len(router.intents) == 0
    assert rate_limiter.record_count == 0
    assert len(alerts) == 1
    assert alerts[0].rule_name == "rate_limit"


def test_pre_trade_risk_blocks_routing() -> None:
    bus = EventBus()
    router = _RouterSpy()
    pre_trade_risk = _PreTradeRiskStub(passed=False)
    SignalProcessor(event_bus=bus, order_router=router, pre_trade_risk=pre_trade_risk)

    bus.publish(
        SignalEvent(
            source="EMACrossStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            metadata={"bar_close": "50000"},
        )
    )

    assert len(router.intents) == 0
    assert len(pre_trade_risk.calls) == 1
    assert pre_trade_risk.calls[0]["current_price"] == 50000


def test_risk_and_rate_limit_pass_before_routing_and_record_after_success() -> None:
    bus = EventBus()
    router = _RouterSpy()
    pre_trade_risk = _PreTradeRiskStub(passed=True)
    rate_limiter = _RateLimiterStub(can_proceed=True)
    SignalProcessor(
        event_bus=bus,
        order_router=router,
        pre_trade_risk=pre_trade_risk,
        rate_limiter=rate_limiter,
    )

    bus.publish(
        SignalEvent(
            source="EMACrossStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            metadata={"bar_close": "50000"},
        )
    )

    assert len(pre_trade_risk.calls) == 1
    assert len(router.intents) == 1
    assert rate_limiter.record_count == 1


def test_rate_limiter_not_recorded_when_router_rejects() -> None:
    bus = EventBus()
    router = _RouterRejectSpy()
    rate_limiter = _RateLimiterStub(can_proceed=True)
    SignalProcessor(event_bus=bus, order_router=router, rate_limiter=rate_limiter)

    bus.publish(
        SignalEvent(
            source="EMACrossStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            metadata={"bar_close": "50000"},
        )
    )

    assert len(router.intents) == 1
    assert rate_limiter.record_count == 0


def test_ignored_instrument_blocks_routing_and_emits_alert() -> None:
    bus = EventBus()
    router = _RouterSpy()
    ignored = IgnoredInstrumentRegistry(event_bus=bus)
    ignored.ignore(
        instrument_id="BTCUSDT-PERP.BINANCE",
        reason="existing_exchange_position_on_startup",
        source="test",
    )
    alerts = []
    bus.subscribe(EventType.RISK_ALERT, alerts.append)
    SignalProcessor(event_bus=bus, order_router=router, ignored_instruments=ignored)

    bus.publish(
        SignalEvent(
            source="EMACrossStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            metadata={"bar_close": "50000"},
        )
    )

    assert len(router.intents) == 0
    assert any(alert.rule_name == "ignored_instrument" for alert in alerts)
