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

    # 使用固定模式创建 位置调整器
    position_sizer = PositionSizer(config={"mode": "fixed", "fixed_size": "0.1"})

    SignalProcessor(
        event_bus=bus,
        order_router=router,
        position_sizer=position_sizer,
    )

    # 发射强度为 0.5 的信号
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

    # 当 fixed_size=0.1 和 signal_strength=0.5 时，预期数量 = 0.1 * 0.5 = 0.05
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

    # 发射没有明确强度的信号（默认为 1.0）
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

    # 当 fixed_size=0.2 和 默认 signal_strength=1.0 时，预期数量 = 0.2 * 1.0 = 0.2
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

    # 发射具有 metadata 中明确的命令数量和强度的信号
    bus.publish(
        SignalEvent(
            source="TestStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            strength=0.8,
            metadata={
                "order_side": "BUY",
                "order_qty": "1.0",  # 这应该被头寸调整所覆盖
                "bar_close": "60000.0",
                "account_equity": "20000.0",
            },
        )
    )

    assert len(router.intents) == 1
    intent = router.intents[0]

    # 头寸调整器应覆盖显式的 order_qty
    # 当 fixed_size=0.3 和 signal_strength=0.8 时，预期数量 = 0.3 * 0.8 = 0.24
    assert str(intent.quantity) == "0.24"


def test_no_position_sizer_uses_original_quantity() -> None:
    """Verify that without PositionSizer, original quantities are preserved."""
    bus = EventBus()
    router = _RouterSpy()

    # 未提供头寸调整器
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

    # 没有头寸调整器，应使用原始 metadata数量
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

    # 发出没有当前价格或账户权益的信号
    bus.publish(
        SignalEvent(
            source="TestStrategy",
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            strength=0.5,
            metadata={},  # 否 bar_close 或 account_equity
        )
    )

    assert len(router.intents) == 1
    intent = router.intents[0]

    # 当头寸规模调整失败时，应回落至default数量 (0.01)
    assert str(intent.quantity) == "0.01"
