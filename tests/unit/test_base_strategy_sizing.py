"""BaseStrategy 下单数量计算测试."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from src.core.events import EventBus
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy

INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL")


class _DummyQty:
    def __init__(self, value: Decimal) -> None:
        self._value = value

    def as_decimal(self) -> Decimal:
        return self._value


class _DummyInstrument:
    def __init__(self, size_increment: str = "0.001", min_qty: str | None = None) -> None:
        self.size_increment = size_increment
        self.quote_currency = "USDT"
        self.min_qty = Decimal(min_qty) if min_qty is not None else None

    def make_qty(self, value: Decimal) -> _DummyQty:
        if self.min_qty is not None and value < self.min_qty:
            raise ValueError("quantity rounded to zero")
        return _DummyQty(value)


class _DummyBalance:
    def __init__(self, value: Decimal) -> None:
        self._value = value

    def as_decimal(self) -> Decimal:
        return self._value


class _DummyAccount:
    def __init__(self, equity: Decimal) -> None:
        self._equity = equity

    def balance_total(self, _quote_ccy=None) -> _DummyBalance:
        return _DummyBalance(self._equity)


class _DummyPortfolio:
    def __init__(self, equity: Decimal) -> None:
        self._account = _DummyAccount(equity)

    def account(self, venue=None) -> _DummyAccount:
        return self._account


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


def _make_historical_bars(prices: list[int]) -> list[Bar]:
    base_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1_000_000_000)
    bars: list[Bar] = []
    for index, close in enumerate(prices):
        ts = base_ts + index * 900 * 1_000_000_000
        bars.append(
            Bar(
                bar_type=BAR_TYPE,
                open=Price.from_str(str(close)),
                high=Price.from_str(str(close + 1)),
                low=Price.from_str(str(close - 1)),
                close=Price.from_str(str(close)),
                volume=Quantity.from_str("1"),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return bars


def test_fixed_trade_size_used_when_no_capital_pct() -> None:
    """Verify that fixed trade size used when no capital percent."""
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument()  # type: ignore[assignment]
    qty = strategy._resolve_order_quantity(  # type: ignore[arg-type]
        type("BarStub", (), {"close": 50_000.0})(),
    )

    assert qty is not None
    assert qty.as_decimal() == Decimal("0.01")


def test_capital_pct_sizing_overrides_fixed_trade_size() -> None:
    """Verify that capital percent sizing overrides fixed trade size."""
    strategy = _make_strategy(capital_pct=10.0)
    strategy.instrument = _DummyInstrument()  # type: ignore[assignment]
    strategy._resolve_qty_from_notional_pct = (  # type: ignore[method-assign]
        lambda _capital_pct, _close: _DummyQty(Decimal("0.02"))
    )
    qty = strategy._resolve_order_quantity(  # type: ignore[arg-type]
        type("BarStub", (), {"close": 50_000.0})(),
    )

    assert qty is not None
    # 10000 * 10% / 50000 = 0.02
    assert qty.as_decimal() == Decimal("0.02")


def test_gross_exposure_pct_sizing_uses_notional_pct() -> None:
    """Verify that gross exposure percent sizing uses notional percent."""
    cfg = EMACrossConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        fast_ema_period=5,
        slow_ema_period=20,
        trade_size=Decimal("0.01"),
        gross_exposure_pct_per_trade=250.0,
    )
    strategy = EMACrossStrategy(config=cfg)
    strategy.instrument = _DummyInstrument()  # type: ignore[assignment]
    strategy._resolve_equity = lambda: Decimal("1000")  # type: ignore[method-assign]

    qty = strategy._resolve_order_quantity(  # type: ignore[arg-type]
        type("BarStub", (), {"close": 100.0})(),
    )

    assert qty is not None
    assert qty.as_decimal() == Decimal("25")


def test_margin_pct_sizing_uses_sizing_leverage() -> None:
    """Verify that margin percent sizing uses sizing leverage."""
    cfg = EMACrossConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        fast_ema_period=5,
        slow_ema_period=20,
        trade_size=Decimal("0.01"),
        margin_pct_per_trade=10.0,
        sizing_leverage=5.0,
    )
    strategy = EMACrossStrategy(config=cfg)
    strategy.instrument = _DummyInstrument()  # type: ignore[assignment]
    strategy._resolve_equity = lambda: Decimal("1000")  # type: ignore[method-assign]

    qty = strategy._resolve_order_quantity(  # type: ignore[arg-type]
        type("BarStub", (), {"close": 100.0})(),
    )

    assert qty is not None
    assert qty.as_decimal() == Decimal("5")


def test_notional_pct_sizing_returns_none_when_below_min_increment() -> None:
    """Verify that notional percent sizing returns none when below min increment."""
    strategy = _make_strategy(capital_pct=0.001)
    strategy.instrument = _DummyInstrument(size_increment="0.001", min_qty="0.001")  # type: ignore[assignment]
    strategy._resolve_equity = lambda: Decimal("1000")  # type: ignore[method-assign]

    qty = strategy._resolve_qty_from_notional_pct(0.001, 50_000.0)

    assert qty is None


def test_split_quantity_by_ratios_preserves_total() -> None:
    """Verify that split quantity by ratios preserves total."""
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
    """Verify that split quantity by ratios respects step for tiny quantity."""
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
    """Verify that split quantity preserve total keeps remainder on last chunk."""
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument(size_increment="0.001")  # type: ignore[assignment]

    chunks = strategy._split_quantity_by_ratios_preserve_total(
        total_qty=Decimal("0.1234"),
        ratios=[Decimal("1")],
    )

    assert chunks == [Decimal("0.1234")]


def test_split_quantity_strict_step_discards_non_step_remainder() -> None:
    """Verify that split quantity strict step discards non step remainder."""
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument(size_increment="0.001")  # type: ignore[assignment]

    chunks = strategy._split_quantity_by_ratios_strict_step(
        total_qty=Decimal("0.1234"),
        ratios=[Decimal("1")],
    )

    assert chunks == [Decimal("0.123")]


def test_resolve_order_quantity_decimal_primary_path() -> None:
    """Verify that resolve order quantity decimal primary path."""
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument(size_increment="0.001")  # type: ignore[assignment]
    strategy._resolve_order_quantity = lambda _bar: _DummyQty(Decimal("0.1234"))  # type: ignore[method-assign]

    qty = strategy._resolve_order_quantity_decimal(  # type: ignore[arg-type]
        type("BarStub", (), {"close": 50_000.0})(),
        fallback_trade_size=False,
    )

    assert qty == Decimal("0.123")


def test_resolve_order_quantity_decimal_fallback_trade_size() -> None:
    """Verify that resolve order quantity decimal fallback trade size."""
    strategy = _make_strategy(capital_pct=None)
    strategy.instrument = _DummyInstrument(size_increment="0.001")  # type: ignore[assignment]
    strategy._resolve_order_quantity = lambda _bar: None  # type: ignore[method-assign]

    qty = strategy._resolve_order_quantity_decimal(  # type: ignore[arg-type]
        type("BarStub", (), {"close": 50_000.0})(),
        fallback_trade_size=True,
    )

    assert qty == Decimal("0.01")


def test_bar_type_interval_parses_external_and_internal_specs() -> None:
    """Verify that bar type interval parses external and internal specs."""
    external = _make_strategy(capital_pct=None)
    internal = EMACrossStrategy(
        config=EMACrossConfig(
            instrument_id=INSTRUMENT_ID,
            bar_type=BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-INTERNAL@1-MINUTE-EXTERNAL"),
            fast_ema_period=5,
            slow_ema_period=20,
        )
    )

    assert external._bar_type_interval() == timedelta(minutes=15)
    assert internal._bar_type_interval() == timedelta(hours=1)


def test_resolved_warmup_bars_uses_strategy_default_and_explicit_override() -> None:
    """Verify that resolved warmup bars uses strategy default and explicit override."""
    strategy = _make_strategy(capital_pct=None)
    overridden = EMACrossStrategy(
        config=EMACrossConfig(
            instrument_id=INSTRUMENT_ID,
            bar_type=BAR_TYPE,
            fast_ema_period=5,
            slow_ema_period=20,
            live_warmup_bars=50,
        )
    )

    assert strategy._resolved_warmup_bars() == 22
    assert overridden._resolved_warmup_bars() == 50


def test_request_warmup_history_uses_strategy_warmup_and_margin() -> None:
    """Verify that request warmup history uses strategy warmup and margin."""
    strategy = EMACrossStrategy(
        config=EMACrossConfig(
            instrument_id=INSTRUMENT_ID,
            bar_type=BAR_TYPE,
            fast_ema_period=5,
            slow_ema_period=20,
            live_warmup_margin_bars=3,
        ),
        event_bus=EventBus(),
    )
    requested: dict[str, object] = {}

    def _capture_request_bars(bar_type: BarType, *, start, limit: int) -> None:
        requested["bar_type"] = bar_type
        requested["start"] = start
        requested["limit"] = limit

    strategy.request_bars = _capture_request_bars  # type: ignore[method-assign]

    strategy._request_warmup_history()

    assert requested["bar_type"] == BAR_TYPE
    assert requested["limit"] == 25
    assert strategy._warmup_history_requested is True


def test_preload_history_primes_indicators_and_skips_runtime_request() -> None:
    """Verify that preload history primes indicators and skips runtime request."""
    strategy = EMACrossStrategy(
        config=EMACrossConfig(
            instrument_id=INSTRUMENT_ID,
            bar_type=BAR_TYPE,
            fast_ema_period=3,
            slow_ema_period=5,
            entry_min_atr_ratio=0.0,
        ),
        event_bus=EventBus(),
    )
    loaded = strategy.preload_history(_make_historical_bars([100, 101, 102, 103, 104, 105]))
    requested = {"called": False}

    strategy.request_bars = lambda *args, **kwargs: requested.__setitem__("called", True)  # type: ignore[method-assign]
    strategy._request_warmup_history()

    assert loaded == 6
    assert strategy.fast_ema.initialized is True
    assert strategy.slow_ema.initialized is True
    assert strategy.indicators_initialized() is True
    assert strategy._bar_index == 6
    assert strategy._prev_fast_above is True
    assert strategy._warmup_history_preloaded is True
    assert requested["called"] is False
