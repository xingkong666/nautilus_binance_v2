"""SignalProcessor position sizing integration tests."""

from __future__ import annotations

from src.core.events import EventBus, SignalDirection, SignalEvent
from src.execution.signal_processor import SignalProcessor
from src.risk.position_sizer import PositionSizer


class _RouterSpy:
    def __init__(self) -> None:
        self.intents = []

    def route(self, intent):
        self.intents.append(intent)
        return True


def test_position_sizer_applied_to_signal_with_strength() -> None:
    """Verify that PositionSizer is applied when signal has strength."""
    bus = EventBus()
    router = _RouterSpy()

    # Create PositionSizer with fixed mode
    position_sizer = PositionSizer(config={"mode": "fixed", "fixed_size": "0.1"})

    SignalProcessor(
        event_bus=bus,
        order_router=router,
        position_sizer=position_sizer,
    )

    # Emit signal with 0.5 strength
    bus.publish(
        SignalEvent(
            source="TestStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            strength=0.5,
            metadata={
                "bar_close": "50000.0",
                "account_equity": "10000.0",
            },
        )
    )

    assert len(router.intents) == 1
    intent = router.intents[0]

    # With fixed_size=0.1 and signal_strength=0.5, expected quantity = 0.1 * 0.5 = 0.05
    assert str(intent.quantity) == "0.05"
    assert intent.strategy_id == "TestStrategy"


def test_position_sizer_fallback_without_strength() -> None:
    """Verify that PositionSizer works when signal has no explicit strength."""
    bus = EventBus()
    router = _RouterSpy()

    position_sizer = PositionSizer(config={"mode": "fixed", "fixed_size": "0.2"})

    SignalProcessor(
        event_bus=bus,
        order_router=router,
        position_sizer=position_sizer,
    )

    # Emit signal without explicit strength (defaults to 1.0)
    bus.publish(
        SignalEvent(
            source="TestStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.SHORT,
            metadata={
                "bar_close": "45000.0",
                "account_equity": "5000.0",
            },
        )
    )

    assert len(router.intents) == 1
    intent = router.intents[0]

    # With fixed_size=0.2 and default signal_strength=1.0, expected quantity = 0.2 * 1.0 = 0.2
    assert str(intent.quantity) == "0.20"


def test_position_sizer_with_metadata_order_fields() -> None:
    """Verify that PositionSizer is applied when using metadata order fields."""
    bus = EventBus()
    router = _RouterSpy()

    position_sizer = PositionSizer(config={"mode": "fixed", "fixed_size": "0.3"})

    SignalProcessor(
        event_bus=bus,
        order_router=router,
        position_sizer=position_sizer,
    )

    # Emit signal with explicit order quantity in metadata and strength
    bus.publish(
        SignalEvent(
            source="TestStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            strength=0.8,
            metadata={
                "order_side": "BUY",
                "order_qty": "1.0",  # This should be overridden by position sizing
                "bar_close": "60000.0",
                "account_equity": "20000.0",
            },
        )
    )

    assert len(router.intents) == 1
    intent = router.intents[0]

    # Position sizer should override the explicit order_qty
    # With fixed_size=0.3 and signal_strength=0.8, expected quantity = 0.3 * 0.8 = 0.24
    assert str(intent.quantity) == "0.24"


def test_no_position_sizer_uses_original_quantity() -> None:
    """Verify that without PositionSizer, original quantities are preserved."""
    bus = EventBus()
    router = _RouterSpy()

    # No position sizer provided
    SignalProcessor(
        event_bus=bus,
        order_router=router,
    )

    bus.publish(
        SignalEvent(
            source="TestStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            strength=0.5,
            metadata={
                "bar_close": "50000.0",
                "quantity": "0.15",
            },
        )
    )

    assert len(router.intents) == 1
    intent = router.intents[0]

    # Without position sizer, should use the original metadata quantity
    assert str(intent.quantity) == "0.15"


def test_position_sizer_invalid_params_fallback() -> None:
    """Verify that PositionSizer falls back gracefully when params are invalid."""
    bus = EventBus()
    router = _RouterSpy()

    position_sizer = PositionSizer(config={"mode": "fixed", "fixed_size": "0.1"})

    SignalProcessor(
        event_bus=bus,
        order_router=router,
        position_sizer=position_sizer,
    )

    # Emit signal without current price or account equity
    bus.publish(
        SignalEvent(
            source="TestStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            strength=0.5,
            metadata={},  # No bar_close or account_equity
        )
    )

    assert len(router.intents) == 1
    intent = router.intents[0]

    # Should fall back to default quantity (0.01) when position sizing fails
    assert str(intent.quantity) == "0.01"
