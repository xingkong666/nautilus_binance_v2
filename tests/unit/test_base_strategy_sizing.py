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
