"""EMAPullbackATR 策略单元测试."""

from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import SignalDirection
from src.strategy.ema_pullback_atr import EMAPullbackATRConfig, EMAPullbackATRStrategy

INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL")


def make_strategy(cooldown: int = 3, adx_threshold: float = 0.0) -> EMAPullbackATRStrategy:
    """Build strategy.

    Args:
        cooldown: Cooldown.
        adx_threshold: ADX threshold.

    Returns:
        EMAPullbackATRStrategy: Result of make strategy.
    """
    cfg = EMAPullbackATRConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        fast_ema_period=10,
        slow_ema_period=30,
        pullback_atr_multiplier=1.0,
        min_trend_gap_ratio=0.0,
        signal_cooldown_bars=cooldown,
        adx_threshold=adx_threshold,
    )
    return EMAPullbackATRStrategy(config=cfg)


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


def test_atr_not_initialized_blocks_signal() -> None:
    """Verify that ATR not initialized blocks signal."""
    strategy = make_strategy()
    strategy.fast_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=95.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=False, value=5.0)

    signal = strategy.generate_signal(make_bar(100.0, 102.0, 95.0, 101.0))

    assert signal is None


def test_uptrend_pullback_rebound_triggers_long() -> None:
    """Verify that uptrend pullback rebound triggers long."""
    strategy = make_strategy(cooldown=0)
    strategy.fast_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=95.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)

    # bar1 触发回撤武装（low <= fast - ATR）
    first = strategy.generate_signal(make_bar(101.0, 102.0, 94.0, 98.0))
    assert first is None

    # bar2 回收快线（prev_close < fast 且 close >= fast）触发 LONG
    second = strategy.generate_signal(make_bar(98.0, 103.0, 97.0, 101.0))
    assert second == SignalDirection.LONG


def test_cooldown_blocks_immediate_reentry() -> None:
    """Verify that cooldown blocks immediate reentry."""
    strategy = make_strategy(cooldown=3)
    strategy.fast_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=95.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)

    strategy.generate_signal(make_bar(101.0, 102.0, 94.0, 98.0))
    first = strategy.generate_signal(make_bar(98.0, 103.0, 97.0, 101.0))
    assert first == SignalDirection.LONG

    strategy.generate_signal(make_bar(101.0, 103.0, 94.0, 98.0))
    second = strategy.generate_signal(make_bar(98.0, 103.0, 97.0, 101.0))
    assert second is None


def test_downtrend_pullback_rebound_triggers_short() -> None:
    """Verify that downtrend pullback rebound triggers short."""
    strategy = make_strategy(cooldown=0)
    strategy.fast_ema = SimpleNamespace(value=95.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)

    first = strategy.generate_signal(make_bar(94.0, 101.0, 93.0, 97.0))
    assert first is None

    second = strategy.generate_signal(make_bar(97.0, 98.0, 92.0, 94.0))
    assert second == SignalDirection.SHORT


def test_on_reset_clears_state() -> None:
    """Verify that on reset clears state."""
    strategy = make_strategy()
    strategy._prev_close = 100.0
    strategy._bar_index = 9
    strategy._last_signal_bar_index = 7
    strategy._long_pullback_armed = True
    strategy._short_pullback_armed = True

    strategy.on_reset()

    assert strategy._prev_close is None
    assert strategy._bar_index == 0
    assert strategy._last_signal_bar_index is None
    assert not strategy._long_pullback_armed
    assert not strategy._short_pullback_armed


def test_adx_threshold_blocks_when_trend_strength_low() -> None:
    """Verify that ADX threshold blocks when trend strength low."""
    strategy = make_strategy(cooldown=0, adx_threshold=25.0)
    strategy.fast_ema = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=95.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)
    strategy._adx.initialized = True
    strategy._adx.adx = 10.0

    strategy.generate_signal(make_bar(101.0, 102.0, 94.0, 98.0))
    signal = strategy.generate_signal(make_bar(98.0, 103.0, 97.0, 101.0))

    assert signal is None
