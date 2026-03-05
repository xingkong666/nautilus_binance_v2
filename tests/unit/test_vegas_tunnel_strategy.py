"""VegasTunnel 策略单元测试."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import SignalDirection
from src.strategy.vegas_tunnel import VegasTunnelConfig, VegasTunnelStrategy

INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")


@dataclass
class _FakeQty:
    value: Decimal

    def as_decimal(self) -> Decimal:
        return self.value


def make_strategy(cooldown: int = 0) -> VegasTunnelStrategy:
    cfg = VegasTunnelConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        signal_cooldown_bars=cooldown,
    )
    strategy = VegasTunnelStrategy(config=cfg)
    strategy._resolve_order_quantity = lambda _bar: _FakeQty(Decimal("1.0"))  # type: ignore[method-assign]
    return strategy


def make_bar(close: float, high: float | None = None, low: float | None = None) -> SimpleNamespace:
    hi = close if high is None else high
    lo = close if low is None else low
    return SimpleNamespace(open=close, high=hi, low=lo, close=close)


def test_long_entry_on_cross_above_tunnel() -> None:
    strategy = make_strategy(cooldown=0)
    strategy.fast_ema = SimpleNamespace(value=120.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=110.0)  # type: ignore[assignment]
    strategy.tunnel_ema_1 = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.tunnel_ema_2 = SimpleNamespace(value=98.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)
    strategy._prev_fast_above_slow = False

    signal = strategy.generate_signal(make_bar(close=125.0))

    assert signal == SignalDirection.LONG
    assert strategy._pending_order is not None
    assert strategy._pending_order.action == "entry"
    assert strategy._pending_order.side == "BUY"
    assert strategy._position_side == "long"
    assert strategy._stop_price == 120.0


def test_tp1_triggers_partial_exit_and_move_stop_to_breakeven() -> None:
    strategy = make_strategy(cooldown=0)
    strategy.fast_ema = SimpleNamespace(value=120.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=110.0)  # type: ignore[assignment]
    strategy.tunnel_ema_1 = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.tunnel_ema_2 = SimpleNamespace(value=98.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)
    strategy._prev_fast_above_slow = False

    first = strategy.generate_signal(make_bar(close=125.0))
    assert first == SignalDirection.LONG

    # tunnel_width=2, tp1=127
    second = strategy.generate_signal(make_bar(close=127.2))

    assert second == SignalDirection.FLAT
    assert strategy._pending_order is not None
    assert strategy._pending_order.action == "tp1"
    assert strategy._pending_order.reduce_only is True
    assert strategy._pending_order.side == "SELL"
    assert strategy._pending_order.qty == Decimal("0.40")
    assert strategy._stop_price == 125.0


def test_cooldown_blocks_immediate_reentry() -> None:
    strategy = make_strategy(cooldown=3)
    strategy.fast_ema = SimpleNamespace(value=120.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=110.0)  # type: ignore[assignment]
    strategy.tunnel_ema_1 = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.tunnel_ema_2 = SimpleNamespace(value=98.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)

    strategy._prev_fast_above_slow = False
    first = strategy.generate_signal(make_bar(close=125.0))
    assert first == SignalDirection.LONG

    strategy._close_full(reason="manual_test_close")

    strategy._prev_fast_above_slow = False
    second = strategy.generate_signal(make_bar(close=126.0))
    assert second is None


def test_on_reset_clears_runtime_state() -> None:
    strategy = make_strategy(cooldown=0)
    strategy._position_side = "long"
    strategy._entry_price = 100.0
    strategy._remaining_qty = Decimal("0.5")
    strategy._stop_price = 99.0
    strategy._tp_prices = [101.0, 102.0, 103.0]
    strategy._tp_qtys = [Decimal("0.2"), Decimal("0.15"), Decimal("0.15")]
    strategy._tp_filled = [True, False, False]
    strategy._bar_index = 10
    strategy._last_signal_bar_index = 8

    strategy.on_reset()

    assert strategy._position_side == "flat"
    assert strategy._entry_price is None
    assert strategy._remaining_qty == Decimal("0")
    assert strategy._stop_price is None
    assert strategy._tp_prices == []
    assert strategy._tp_qtys == []
    assert strategy._tp_filled == [False, False, False]
    assert strategy._bar_index == 0
    assert strategy._last_signal_bar_index is None


def test_tp3_closes_remaining_position() -> None:
    strategy = make_strategy(cooldown=0)
    strategy.fast_ema = SimpleNamespace(value=120.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=110.0)  # type: ignore[assignment]
    strategy.tunnel_ema_1 = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.tunnel_ema_2 = SimpleNamespace(value=98.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)
    strategy._prev_fast_above_slow = False

    first = strategy.generate_signal(make_bar(close=125.0))
    assert first == SignalDirection.LONG

    # 连续触发三档止盈：tp1=127, tp2≈128.236, tp3≈130.236
    strategy.generate_signal(make_bar(close=127.2))
    strategy.generate_signal(make_bar(close=128.3))
    third = strategy.generate_signal(make_bar(close=130.3))

    assert third == SignalDirection.FLAT
    assert strategy._pending_order is not None
    assert strategy._pending_order.action == "tp3"
    assert strategy._pending_order.reduce_only is True
    assert strategy._position_side == "flat"
    assert strategy._remaining_qty == Decimal("0")


def test_split_quantities_normalizes_invalid_ratios() -> None:
    cfg = VegasTunnelConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        tp_split_1=2.0,
        tp_split_2=2.0,
        tp_split_3=2.0,
    )
    strategy = VegasTunnelStrategy(config=cfg)

    q1, q2, q3 = strategy._split_quantities(Decimal("1.0"))
    total = q1 + q2 + q3

    assert q1 > Decimal("0")
    assert q2 > Decimal("0")
    assert q3 > Decimal("0")
    assert total == Decimal("1.0")


def test_split_quantities_respects_min_step_for_tiny_position() -> None:
    strategy = make_strategy(cooldown=0)

    strategy.instrument = SimpleNamespace(size_increment="0.001")  # type: ignore[assignment]
    q1, q2, q3 = strategy._split_quantities(Decimal("0.001"))

    assert q1 + q2 + q3 == Decimal("0.001")
    assert q1 % Decimal("0.001") == Decimal("0")
    assert q2 % Decimal("0.001") == Decimal("0")
    assert q3 % Decimal("0.001") == Decimal("0")
    assert sum(1 for q in (q1, q2, q3) if q > 0) == 1
