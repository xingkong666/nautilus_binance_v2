"""BaseStrategy 下单数量计算测试."""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy

INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL")


class _DummyQty:
    def __init__(self, value: Decimal) -> None:
        self._value = value

    def as_decimal(self) -> Decimal:
        return self._value


class _DummyInstrument:
    def __init__(self, size_increment: str = "0.001") -> None:
        self.size_increment = size_increment

    @staticmethod
    def make_qty(value: Decimal) -> _DummyQty:
        return _DummyQty(value)


def _make_strategy(capital_pct: float | None) -> EMACrossStrategy:
    cfg = EMACrossConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        fast_ema_period=5,
        slow_ema_period=20,
        trade_size=Decimal("0.01"),
        capital_pct_per_trade=capital_pct,
    )
    return EMACrossStrategy(config=cfg)


def test_fixed_trade_size_used_when_no_capital_pct() -> None:
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument()  # type: ignore[assignment]
    qty = strategy._resolve_order_quantity(  # type: ignore[arg-type]
        type("BarStub", (), {"close": 50_000.0})(),
    )

    assert qty is not None
    assert qty.as_decimal() == Decimal("0.01")


def test_capital_pct_sizing_overrides_fixed_trade_size() -> None:
    strategy = _make_strategy(capital_pct=10.0)
    strategy.instrument = _DummyInstrument()  # type: ignore[assignment]
    strategy._resolve_qty_from_capital_pct = (  # type: ignore[method-assign]
        lambda _capital_pct, _close: _DummyQty(Decimal("0.02"))
    )
    qty = strategy._resolve_order_quantity(  # type: ignore[arg-type]
        type("BarStub", (), {"close": 50_000.0})(),
    )

    assert qty is not None
    # 10000 * 10% / 50000 = 0.02
    assert qty.as_decimal() == Decimal("0.02")


def test_split_quantity_by_ratios_preserves_total() -> None:
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument(size_increment="0.001")  # type: ignore[assignment]

    chunks = strategy._split_quantity_by_ratios(
        total_qty=Decimal("1.0"),
        ratios=[Decimal("0.4"), Decimal("0.3"), Decimal("0.3")],
    )

    assert len(chunks) == 3
    assert sum(chunks, start=Decimal("0")) == Decimal("1.0")
    assert all(c >= 0 for c in chunks)


def test_split_quantity_by_ratios_respects_step_for_tiny_qty() -> None:
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument(size_increment="0.001")  # type: ignore[assignment]

    chunks = strategy._split_quantity_by_ratios(
        total_qty=Decimal("0.001"),
        ratios=[Decimal("0.4"), Decimal("0.3"), Decimal("0.3")],
    )

    assert sum(chunks, start=Decimal("0")) == Decimal("0.001")
    assert all(c % Decimal("0.001") == Decimal("0") for c in chunks)
    assert sum(1 for c in chunks if c > 0) == 1


def test_split_quantity_preserve_total_keeps_remainder_on_last_chunk() -> None:
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument(size_increment="0.001")  # type: ignore[assignment]

    chunks = strategy._split_quantity_by_ratios_preserve_total(
        total_qty=Decimal("0.1234"),
        ratios=[Decimal("1")],
    )

    assert chunks == [Decimal("0.1234")]


def test_split_quantity_strict_step_discards_non_step_remainder() -> None:
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument(size_increment="0.001")  # type: ignore[assignment]

    chunks = strategy._split_quantity_by_ratios_strict_step(
        total_qty=Decimal("0.1234"),
        ratios=[Decimal("1")],
    )

    assert chunks == [Decimal("0.123")]


def test_resolve_order_quantity_decimal_primary_path() -> None:
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument(size_increment="0.001")  # type: ignore[assignment]
    strategy._resolve_order_quantity = lambda _bar: _DummyQty(Decimal("0.1234"))  # type: ignore[method-assign]

    qty = strategy._resolve_order_quantity_decimal(  # type: ignore[arg-type]
        type("BarStub", (), {"close": 50_000.0})(),
        fallback_trade_size=False,
    )

    assert qty == Decimal("0.123")


def test_resolve_order_quantity_decimal_fallback_trade_size() -> None:
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument(size_increment="0.001")  # type: ignore[assignment]
    strategy._resolve_order_quantity = lambda _bar: None  # type: ignore[method-assign]

    qty = strategy._resolve_order_quantity_decimal(  # type: ignore[arg-type]
        type("BarStub", (), {"close": 50_000.0})(),
        fallback_trade_size=True,
    )

    assert qty == Decimal("0.01")
