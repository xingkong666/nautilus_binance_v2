"""TurtleStrategy 单元测试."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import SignalDirection
from src.strategy.turtle import TurtleConfig, TurtleStrategy

INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL")


@dataclass
class _FakeQty:
    value: Decimal

    def as_decimal(self) -> Decimal:
        return self.value


class _FakeAtr:
    def __init__(self, value: float) -> None:
        self.initialized = True
        self.value = value

    def reset(self) -> None:
        self.initialized = False
        self.value = 0.0


def make_strategy() -> TurtleStrategy:
    cfg = TurtleConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        entry_period=3,
        exit_period=2,
        atr_period=3,
        stop_atr_multiplier=2.0,
        unit_add_atr_step=0.5,
        max_units=4,
    )
    strategy = TurtleStrategy(config=cfg)
    strategy._resolve_order_quantity = lambda bar: _FakeQty(Decimal("0.1"))  # type: ignore[method-assign]
    return strategy


def make_bar(open_: float, high: float, low: float, close: float) -> SimpleNamespace:
    return SimpleNamespace(open=open_, high=high, low=low, close=close)


def _prefill_history(strategy: TurtleStrategy) -> None:
    strategy._atr_indicator = _FakeAtr(value=10.0)
    strategy.generate_signal(make_bar(100.0, 101.0, 99.0, 100.0))
    strategy.generate_signal(make_bar(100.0, 102.0, 98.0, 101.0))
    strategy.generate_signal(make_bar(101.0, 103.0, 97.0, 102.0))


def test_atr_not_initialized_blocks_signal() -> None:
    strategy = make_strategy()
    strategy._atr_indicator = SimpleNamespace(initialized=False, value=10.0)

    signal = strategy.generate_signal(make_bar(100.0, 110.0, 99.0, 109.0))

    assert signal is None


def test_breakout_triggers_long_entry() -> None:
    strategy = make_strategy()
    _prefill_history(strategy)

    signal = strategy.generate_signal(make_bar(102.0, 106.0, 101.0, 105.0))

    assert signal == SignalDirection.LONG
    assert strategy._position_side == "long"
    assert strategy._units_held == 1
    assert strategy._pending_order is not None
    assert strategy._pending_order.action == "entry"
    assert strategy._pending_order.side == "BUY"


def test_add_position_on_half_n_move() -> None:
    strategy = make_strategy()
    _prefill_history(strategy)

    strategy._position_side = "long"
    strategy._units_held = 1
    strategy._unit_qty = Decimal("0.1")
    strategy._last_add_price = 100.0
    strategy._stop_price = 80.0

    signal = strategy.generate_signal(make_bar(100.0, 106.0, 99.0, 105.0))

    assert signal == SignalDirection.LONG
    assert strategy._units_held == 2
    assert strategy._pending_order is not None
    assert strategy._pending_order.action == "add"


def test_stop_loss_triggers_flat_exit() -> None:
    strategy = make_strategy()
    _prefill_history(strategy)

    strategy._position_side = "long"
    strategy._units_held = 2
    strategy._unit_qty = Decimal("0.1")
    strategy._last_add_price = 100.0
    strategy._stop_price = 98.0

    signal = strategy.generate_signal(make_bar(99.0, 100.0, 95.0, 97.0))

    assert signal == SignalDirection.FLAT
    assert strategy._position_side == "flat"
    assert strategy._units_held == 0
    assert strategy._pending_order is not None
    assert strategy._pending_order.action == "exit"
    assert strategy._pending_order.reduce_only is True


def test_max_units_blocks_further_adds() -> None:
    strategy = make_strategy()
    _prefill_history(strategy)

    strategy._position_side = "long"
    strategy._units_held = 4
    strategy._unit_qty = Decimal("0.1")
    strategy._last_add_price = 100.0
    strategy._stop_price = 80.0

    signal = strategy.generate_signal(make_bar(100.0, 110.0, 99.0, 106.0))

    assert signal is None
    assert strategy._units_held == 4


def test_on_reset_clears_internal_state() -> None:
    strategy = make_strategy()
    _prefill_history(strategy)

    strategy._position_side = "short"
    strategy._units_held = 3
    strategy._unit_qty = Decimal("0.1")
    strategy._last_add_price = 80.0
    strategy._stop_price = 100.0

    strategy.on_reset()

    assert strategy._position_side == "flat"
    assert strategy._units_held == 0
    assert strategy._unit_qty is None
    assert strategy._last_add_price is None
    assert strategy._stop_price is None
