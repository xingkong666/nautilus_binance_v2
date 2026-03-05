"""SignalProcessor 单元测试."""

from __future__ import annotations

from src.core.events import EventBus, SignalDirection, SignalEvent
from src.execution.signal_processor import SignalProcessor


class _RouterSpy:
    def __init__(self) -> None:
        self.intents = []

    def route(self, intent):
        self.intents.append(intent)
        return True


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
