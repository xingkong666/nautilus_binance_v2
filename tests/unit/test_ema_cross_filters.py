"""EMACross 过滤器行为单元测试."""

from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import SignalDirection
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy

INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL")


def make_strategy(entry_min_atr_ratio: float = 0.0015, signal_cooldown_bars: int = 3) -> EMACrossStrategy:
    """Build strategy.

    Args:
        entry_min_atr_ratio: Entry min ATR ratio.
        signal_cooldown_bars: Signal cooldown bars.

    Returns:
        EMACrossStrategy: Result of make strategy.
    """
    cfg = EMACrossConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        fast_ema_period=5,
        slow_ema_period=20,
        entry_min_atr_ratio=entry_min_atr_ratio,
        signal_cooldown_bars=signal_cooldown_bars,
    )
    return EMACrossStrategy(config=cfg)


def test_atr_not_initialized_blocks_signal() -> None:
    """Verify that ATR not initialized blocks signal."""
    strategy = make_strategy(entry_min_atr_ratio=0.001)
    strategy.fast_ema = SimpleNamespace(value=101.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy._prev_fast_above = False
    strategy._bar_index = 10
    strategy._atr_indicator = SimpleNamespace(initialized=False, value=100.0)

    signal = strategy.generate_signal(SimpleNamespace(close=50_000.0))

    assert signal is None
    assert strategy._last_signal_bar_index is None


def test_atr_ratio_below_threshold_blocks_signal() -> None:
    """Verify that ATR ratio below threshold blocks signal."""
    strategy = make_strategy(entry_min_atr_ratio=0.001)
    strategy.fast_ema = SimpleNamespace(value=101.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy._prev_fast_above = False
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=10.0)

    signal = strategy.generate_signal(SimpleNamespace(close=50_000.0))

    assert signal is None
    assert strategy._last_signal_bar_index is None


def test_atr_ratio_above_threshold_allows_signal() -> None:
    """Verify that ATR ratio above threshold allows signal."""
    strategy = make_strategy(entry_min_atr_ratio=0.001)
    strategy.fast_ema = SimpleNamespace(value=101.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy._prev_fast_above = False
    strategy._bar_index = 10
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=100.0)

    signal = strategy.generate_signal(SimpleNamespace(close=50_000.0))

    assert signal == SignalDirection.LONG
    assert strategy._last_signal_bar_index == 11


def test_cooldown_blocks_rapid_reentry() -> None:
    """Verify that cooldown blocks rapid reentry."""
    strategy = make_strategy(entry_min_atr_ratio=0.0, signal_cooldown_bars=3)
    strategy.fast_ema = SimpleNamespace(value=101.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy._prev_fast_above = False

    bar = SimpleNamespace(close=50_000.0)

    first = strategy.generate_signal(bar)
    assert first == SignalDirection.LONG

    strategy.fast_ema = SimpleNamespace(value=99.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    second = strategy.generate_signal(bar)
    assert second is None

    strategy.fast_ema = SimpleNamespace(value=101.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    third = strategy.generate_signal(bar)
    assert third is None

    strategy.fast_ema = SimpleNamespace(value=99.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    fourth = strategy.generate_signal(bar)
    assert fourth == SignalDirection.SHORT


def test_on_reset_clears_filter_state() -> None:
    """Verify that on reset clears filter state."""
    strategy = make_strategy(entry_min_atr_ratio=0.001, signal_cooldown_bars=3)
    strategy._prev_fast_above = True
    strategy._bar_index = 8
    strategy._last_signal_bar_index = 5

    strategy.on_reset()

    assert strategy._prev_fast_above is None
    assert strategy._bar_index == 0
    assert strategy._last_signal_bar_index is None
