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


class _MockPortfolio:
    """Minimal portfolio mock that tracks position side for Vegas unit tests."""

    def __init__(self) -> None:
        self._side: str = "flat"  # "flat" / "long" / "short"

    def open_long(self) -> None:
        self._side = "long"

    def open_short(self) -> None:
        self._side = "short"

    def close(self) -> None:
        self._side = "flat"

    def is_net_long(self, _instrument_id: object) -> bool:
        return self._side == "long"

    def is_net_short(self, _instrument_id: object) -> bool:
        return self._side == "short"

    def is_flat(self, _instrument_id: object) -> bool:
        return self._side == "flat"


def make_strategy(cooldown: int = 0) -> VegasTunnelStrategy:
    """Build strategy.

    Args:
        cooldown: Cooldown.

    Returns:
        VegasTunnelStrategy: Result of make strategy.
    """
    cfg = VegasTunnelConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        signal_cooldown_bars=cooldown,
    )
    strategy = VegasTunnelStrategy(config=cfg)
    strategy._resolve_order_quantity = lambda _bar: _FakeQty(Decimal("1.0"))  # type: ignore[method-assign]
    return strategy


def _set_portfolio_side(strategy: VegasTunnelStrategy, side: str) -> None:
    """No-op — Vegas now uses internal _is_long state, not portfolio."""


def make_bar(close: float, high: float | None = None, low: float | None = None) -> SimpleNamespace:
    """Build bar.

    Args:
        close: Close.
        high: High.
        low: Low.

    Returns:
        SimpleNamespace: Result of make bar.
    """
    hi = close if high is None else high
    lo = close if low is None else low
    return SimpleNamespace(open=close, high=hi, low=lo, close=close)


def test_long_entry_on_cross_above_tunnel() -> None:
    """Verify that long entry on cross above tunnel."""
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
    # Position is open: entry_price set, remaining_qty > 0
    assert strategy._entry_price == 125.0
    assert strategy._remaining_qty > 0
    assert strategy._is_long is True
    assert strategy._stop_price == 120.0


def test_tp1_triggers_partial_exit_and_move_stop_to_breakeven() -> None:
    """Verify that tp1 triggers partial exit and move stop to breakeven."""
    strategy = make_strategy(cooldown=0)
    strategy.fast_ema = SimpleNamespace(value=120.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=110.0)  # type: ignore[assignment]
    strategy.tunnel_ema_1 = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.tunnel_ema_2 = SimpleNamespace(value=98.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)
    strategy._prev_fast_above_slow = False

    first = strategy.generate_signal(make_bar(close=125.0))
    assert first == SignalDirection.LONG
    _set_portfolio_side(strategy, "long")  # sync mock after open

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
    """Verify that cooldown blocks immediate reentry."""
    strategy = make_strategy(cooldown=3)
    strategy.fast_ema = SimpleNamespace(value=120.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=110.0)  # type: ignore[assignment]
    strategy.tunnel_ema_1 = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.tunnel_ema_2 = SimpleNamespace(value=98.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)

    strategy._prev_fast_above_slow = False
    first = strategy.generate_signal(make_bar(close=125.0))
    assert first == SignalDirection.LONG
    _set_portfolio_side(strategy, "long")  # sync mock after open

    strategy._close_full(reason="manual_test_close")
    _set_portfolio_side(strategy, "flat")  # sync mock after close

    strategy._prev_fast_above_slow = False
    second = strategy.generate_signal(make_bar(close=126.0))
    assert second is None


def test_on_reset_clears_runtime_state() -> None:
    """Verify that on reset clears runtime state."""
    strategy = make_strategy(cooldown=0)
    strategy._entry_price = 100.0
    strategy._remaining_qty = Decimal("0.5")
    strategy._stop_price = 99.0
    strategy._tp_prices = [101.0, 102.0, 103.0]
    strategy._tp_qtys = [Decimal("0.2"), Decimal("0.15"), Decimal("0.15")]
    strategy._tp_filled = [True, False, False]
    strategy._bar_index = 10
    strategy._last_signal_bar_index = 8

    strategy.on_reset()

    assert strategy._entry_price is None
    assert strategy._remaining_qty == Decimal("0")
    assert strategy._stop_price is None
    assert strategy._tp_prices == []
    assert strategy._tp_qtys == []
    assert strategy._tp_filled == [False, False, False]
    assert strategy._bar_index == 0
    assert strategy._last_signal_bar_index is None


def test_tp3_closes_remaining_position() -> None:
    """Verify that tp3 closes remaining position."""
    strategy = make_strategy(cooldown=0)
    strategy.fast_ema = SimpleNamespace(value=120.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=110.0)  # type: ignore[assignment]
    strategy.tunnel_ema_1 = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.tunnel_ema_2 = SimpleNamespace(value=98.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)
    strategy._prev_fast_above_slow = False

    first = strategy.generate_signal(make_bar(close=125.0))
    assert first == SignalDirection.LONG
    _set_portfolio_side(strategy, "long")  # sync mock after open

    # 连续触发三档止盈：tp1=127, tp2≈128.236, tp3≈130.236
    strategy.generate_signal(make_bar(close=127.2))
    strategy.generate_signal(make_bar(close=128.3))
    third = strategy.generate_signal(make_bar(close=130.3))

    assert third == SignalDirection.FLAT
    assert strategy._pending_order is not None
    assert strategy._pending_order.action == "tp3"
    assert strategy._pending_order.reduce_only is True
    assert strategy._remaining_qty == Decimal("0")
    assert strategy._entry_price is None


def test_split_quantities_normalizes_invalid_ratios() -> None:
    """Verify that split quantities normalizes invalid ratios."""
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
    """Verify that split quantities respects min step for tiny position."""
    strategy = make_strategy(cooldown=0)

    strategy.instrument = SimpleNamespace(size_increment="0.001")  # type: ignore[assignment]
    q1, q2, q3 = strategy._split_quantities(Decimal("0.001"))

    assert q1 + q2 + q3 == Decimal("0.001")
    assert q1 % Decimal("0.001") == Decimal("0")
    assert q2 % Decimal("0.001") == Decimal("0")
    assert q3 % Decimal("0.001") == Decimal("0")
    assert sum(1 for q in (q1, q2, q3) if q > 0) == 1


def test_historical_bar_prefills_cross_state() -> None:
    """Verify that historical bar prefills cross state."""
    strategy = make_strategy(cooldown=0)
    strategy.fast_ema = SimpleNamespace(value=120.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=110.0)  # type: ignore[assignment]

    strategy._on_historical_bar(make_bar(close=125.0))  # type: ignore[arg-type]

    assert strategy._bar_index == 1
    assert strategy._prev_fast_above_slow is True


def test_min_tunnel_width_pct_filters_flat_tunnel() -> None:
    """Verify that min_tunnel_width_pct suppresses signals when tunnel is too flat."""
    cfg = VegasTunnelConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        min_tunnel_width_pct=0.01,  # 隧道宽度需 >= 1% 价格
    )
    strategy = VegasTunnelStrategy(config=cfg)
    strategy._resolve_order_quantity = lambda _bar: _FakeQty(Decimal("1.0"))  # type: ignore[method-assign]

    # close=100, tunnel=100.5/100.0 → width=0.5 → 0.5/100=0.005 < 0.01 → 过滤
    strategy.fast_ema = SimpleNamespace(value=105.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=103.0)  # type: ignore[assignment]
    strategy.tunnel_ema_1 = SimpleNamespace(value=100.5)  # type: ignore[assignment]
    strategy.tunnel_ema_2 = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=1.0)
    strategy.rsi = SimpleNamespace(initialized=False, value=50.0)  # type: ignore[assignment]
    strategy._prev_fast_above_slow = False

    signal = strategy.generate_signal(make_bar(close=100.0))

    assert signal is None


def test_rsi_filter_blocks_overbought_long() -> None:
    """Verify that RSI >= rsi_long_max prevents long entry."""
    cfg = VegasTunnelConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        rsi_long_max=70.0,
    )
    strategy = VegasTunnelStrategy(config=cfg)
    strategy._resolve_order_quantity = lambda _bar: _FakeQty(Decimal("1.0"))  # type: ignore[method-assign]

    # 金叉 + 位于隧道上方，但 RSI=75 超买
    strategy.fast_ema = SimpleNamespace(value=120.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=110.0)  # type: ignore[assignment]
    strategy.tunnel_ema_1 = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.tunnel_ema_2 = SimpleNamespace(value=98.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)
    strategy.rsi = SimpleNamespace(initialized=True, value=75.0)  # type: ignore[assignment]
    strategy._prev_fast_above_slow = False

    signal = strategy.generate_signal(make_bar(close=125.0))

    assert signal is None


def test_rsi_filter_blocks_oversold_short() -> None:
    """Verify that RSI <= rsi_short_min prevents short entry."""
    cfg = VegasTunnelConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        rsi_short_min=30.0,
    )
    strategy = VegasTunnelStrategy(config=cfg)
    strategy._resolve_order_quantity = lambda _bar: _FakeQty(Decimal("1.0"))  # type: ignore[method-assign]

    # 死叉 + 位于隧道下方，但 RSI=25 超卖
    strategy.fast_ema = SimpleNamespace(value=80.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=90.0)  # type: ignore[assignment]
    strategy.tunnel_ema_1 = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy.tunnel_ema_2 = SimpleNamespace(value=102.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)
    strategy.rsi = SimpleNamespace(initialized=True, value=25.0)  # type: ignore[assignment]
    strategy._prev_fast_above_slow = True

    signal = strategy.generate_signal(make_bar(close=85.0))

    assert signal is None


def test_trail_stop_after_tp2_moves_stop_to_tunnel_lower() -> None:
    """Verify that TP2 hit with trail_stop_after_tp2=True moves stop to tunnel_lower."""
    cfg = VegasTunnelConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        trail_stop_after_tp2=True,
        tp_fib_1=1.0,
        tp_fib_2=1.618,
        tp_fib_3=2.618,
    )
    strategy = VegasTunnelStrategy(config=cfg)
    strategy._resolve_order_quantity = lambda _bar: _FakeQty(Decimal("1.0"))  # type: ignore[method-assign]

    # 设置多头入场状态，tunnel_width=2
    # 入场 close=125, tunnel_upper=102, tunnel_lower=100
    strategy.fast_ema = SimpleNamespace(value=120.0)  # type: ignore[assignment]
    strategy.slow_ema = SimpleNamespace(value=110.0)  # type: ignore[assignment]
    strategy.tunnel_ema_1 = SimpleNamespace(value=102.0)  # type: ignore[assignment]
    strategy.tunnel_ema_2 = SimpleNamespace(value=100.0)  # type: ignore[assignment]
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=5.0)
    strategy.rsi = SimpleNamespace(initialized=False, value=50.0)  # type: ignore[assignment]
    strategy._prev_fast_above_slow = False

    # 入场 bar
    strategy.generate_signal(make_bar(close=125.0))
    assert strategy._is_long is True
    assert strategy._entry_price == 125.0

    # tunnel_width=2; tp1=127, tp2≈128.236
    # TP1 bar
    strategy.generate_signal(make_bar(close=127.1))
    assert strategy._stop_price == 125.0  # 保本上移

    # TP2 bar — close=128.3 >= tp2(≈128.236)，触发 trail_stop_after_tp2
    # 此时 _tunnel_lower=100.0（最后一个 bar 的隧道下边界）
    strategy.generate_signal(make_bar(close=128.3))
    assert strategy._stop_price == 100.0  # 追踪至 tunnel_lower
