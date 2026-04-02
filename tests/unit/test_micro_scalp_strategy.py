"""MicroScalp 策略单元测试."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import EventBus, EventType, SignalDirection
from src.strategy.micro_scalp import MicroScalpConfig, MicroScalpStrategy

INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL")


class _DummyQty:
    def __init__(self, value: Decimal) -> None:
        self._value = value

    def as_decimal(self) -> Decimal:
        return self._value


def make_strategy(event_bus: EventBus | None = None, cooldown: int = 2) -> MicroScalpStrategy:
    """Build strategy.

    Args:
        event_bus: Event bus used for cross-module communication.
        cooldown: Cooldown.

    Returns:
        MicroScalpStrategy: Result of make strategy.
    """
    cfg = MicroScalpConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        signal_cooldown_bars=cooldown,
    )
    return MicroScalpStrategy(config=cfg, event_bus=event_bus)


def make_bar(open_: float, high: float, low: float, close: float) -> SimpleNamespace:
    """Build bar.

    Args:
        open_: Open.
        high: High.
        low: Low.
        close: Close.

    Returns:
        SimpleNamespace: Result of make bar.
    """
    return SimpleNamespace(open=open_, high=high, low=low, close=close)


def test_trend_pullback_rebound_triggers_long() -> None:
    """Verify that trend pullback rebound triggers long."""
    strategy = make_strategy(cooldown=0)
    strategy.fast_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=95.0)  # type: ignore[assignment]
    strategy.rsi = SimpleNamespace(value=50.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=10.0)
    strategy._adx = SimpleNamespace(update=lambda *_: None, initialized=True, value=25.0)  # type: ignore[assignment]

    first = strategy.generate_signal(make_bar(100.0, 101.0, 95.0, 98.0))
    assert first is None

    second = strategy.generate_signal(make_bar(98.0, 102.0, 97.0, 101.0))
    assert second == SignalDirection.LONG


def test_range_rsi_cross_triggers_long() -> None:
    """Verify that range RSI cross triggers long."""
    strategy = make_strategy(cooldown=0)
    strategy.fast_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.rsi = SimpleNamespace(value=30.0)  # type: ignore[assignment]
    strategy._prev_rsi = 20.0
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=10.0)
    strategy._adx = SimpleNamespace(update=lambda *_: None, initialized=True, value=10.0)  # type: ignore[assignment]

    signal = strategy.generate_signal(make_bar(100.0, 101.0, 99.0, 100.5))
    assert signal == SignalDirection.LONG


def test_cooldown_blocks_signal() -> None:
    """Verify that cooldown blocks signal."""
    strategy = make_strategy(cooldown=2)
    strategy.fast_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.rsi = SimpleNamespace(value=30.0)  # type: ignore[assignment]
    strategy._prev_rsi = 20.0
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=10.0)
    strategy._adx = SimpleNamespace(update=lambda *_: None, initialized=True, value=10.0)  # type: ignore[assignment]
    strategy._bar_index = 5
    strategy._last_signal_bar_index = 5

    signal = strategy.generate_signal(make_bar(100.0, 101.0, 99.0, 100.5))
    assert signal is None


def test_publish_signal_emits_limit_metadata() -> None:
    """Verify that publish signal emits limit metadata."""
    bus = EventBus()
    received = []
    bus.subscribe(EventType.SIGNAL, received.append)

    strategy = make_strategy(event_bus=bus, cooldown=0)
    strategy.instrument = SimpleNamespace(price_increment="0.5", make_price=lambda p: round(p * 2) / 2)  # type: ignore[assignment]
    strategy._resolve_order_quantity = lambda _bar: _DummyQty(Decimal("0.03"))  # type: ignore[method-assign]
    strategy._last_mode = "trend"

    strategy._publish_signal(SignalDirection.LONG, make_bar(100.0, 100.0, 100.0, 100.0))

    assert len(received) == 1
    signal = received[0]
    assert signal.metadata["order_type"] == "LIMIT"
    assert signal.metadata["order_side"] == "BUY"
    assert signal.metadata["order_qty"] == "0.03"
    assert signal.metadata["order_price"] == "99.5"


def test_historical_bar_updates_prev_close_and_adx_state() -> None:
    """Verify that historical bar updates prev close and ADX state."""
    strategy = make_strategy(cooldown=0)
    strategy.rsi = SimpleNamespace(value=52.0)  # type: ignore[assignment]

    strategy._on_historical_bar(make_bar(100.0, 101.0, 99.0, 100.0))  # type: ignore[arg-type]
    strategy._on_historical_bar(make_bar(100.0, 102.0, 98.0, 101.0))  # type: ignore[arg-type]

    assert strategy._bar_index == 2
    assert strategy._prev_close == 101.0
    assert strategy._prev_rsi == 52.0
    assert strategy._adx._prev_close == 101.0
