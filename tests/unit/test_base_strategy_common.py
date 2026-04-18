"""BaseStrategy 公共能力测试.

验证上移到基类的公共行为：
- _PendingOrder 可从 base 导入
- _cooldown_passed() 冷却逻辑
- _ensure_atr_indicator() ATR 初始化
- on_reset() 清理 _bar_index / _last_signal_bar_index
- 子类 on_reset() 调用 super() 时传播重置
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.strategy.base import _PendingOrder
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy

INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL")


def _make_ema_strategy(cooldown: int = 0) -> EMACrossStrategy:
    cfg = EMACrossConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        fast_ema_period=5,
        slow_ema_period=10,
        signal_cooldown_bars=cooldown,
        entry_min_atr_ratio=0.0,
    )
    return EMACrossStrategy(config=cfg)


# ---------------------------------------------------------------------------
# _PendingOrder
# ---------------------------------------------------------------------------


def test_pending_order_importable_from_base() -> None:
    """_PendingOrder is a public dataclass exported from src.strategy.base."""
    order = _PendingOrder(
        action="entry",
        side="BUY",
        qty=Decimal("0.1"),
        reduce_only=False,
        reason="test",
    )
    assert order.action == "entry"
    assert order.side == "BUY"
    assert order.qty == Decimal("0.1")
    assert order.reduce_only is False
    assert order.reason == "test"


def test_pending_order_is_base_class() -> None:
    """_PendingOrder is defined in base module (not a local copy)."""
    assert _PendingOrder.__module__ == "src.strategy.base"


# ---------------------------------------------------------------------------
# _cooldown_passed()
# ---------------------------------------------------------------------------


def test_cooldown_passed_no_prior_signal_returns_true() -> None:
    """_cooldown_passed() returns True when no signal has been emitted yet."""
    strategy = _make_ema_strategy(cooldown=5)
    assert strategy._last_signal_bar_index is None
    assert strategy._cooldown_passed() is True


def test_cooldown_passed_zero_cooldown_always_returns_true() -> None:
    """_cooldown_passed() returns True when cooldown_bars is 0."""
    strategy = _make_ema_strategy(cooldown=0)
    strategy._bar_index = 10
    strategy._last_signal_bar_index = 9  # 仅 1 K 线 前
    assert strategy._cooldown_passed() is True


def test_cooldown_passed_within_cooldown_returns_false() -> None:
    """_cooldown_passed() returns False when still within cooldown window."""
    strategy = _make_ema_strategy(cooldown=5)
    strategy._bar_index = 10
    strategy._last_signal_bar_index = 8  # 2bars 前，冷却时间=5
    assert strategy._cooldown_passed() is False


def test_cooldown_passed_at_boundary_returns_true() -> None:
    """_cooldown_passed() returns True exactly when cooldown bars have elapsed."""
    strategy = _make_ema_strategy(cooldown=3)
    strategy._bar_index = 10
    strategy._last_signal_bar_index = 7  # 恰好 3 K 线 前
    assert strategy._cooldown_passed() is True


def test_cooldown_passed_after_cooldown_expires_returns_true() -> None:
    """_cooldown_passed() returns True when more than cooldown_bars have elapsed."""
    strategy = _make_ema_strategy(cooldown=3)
    strategy._bar_index = 20
    strategy._last_signal_bar_index = 7  # 13bars 前，冷却时间=3
    assert strategy._cooldown_passed() is True


# ---------------------------------------------------------------------------
# _ensure_atr_indicator()
# ---------------------------------------------------------------------------


def test_ensure_atr_indicator_creates_atr_when_none() -> None:
    """_ensure_atr_indicator() creates AverageTrueRange when _atr_indicator is None."""
    strategy = _make_ema_strategy()
    # entry_min_atr_ratio=0.0 → _atr_indicator 可能会也可能不会根据配置进行设置
    strategy._atr_indicator = None  # 强制为无
    strategy._ensure_atr_indicator()
    assert strategy._atr_indicator is not None


def test_ensure_atr_indicator_noop_when_already_set() -> None:
    """_ensure_atr_indicator() does not replace an existing indicator."""
    strategy = _make_ema_strategy()
    strategy._ensure_atr_indicator()
    original = strategy._atr_indicator
    strategy._ensure_atr_indicator()
    assert strategy._atr_indicator is original  # 同一个物体


# ---------------------------------------------------------------------------
# on_reset()传播
# ---------------------------------------------------------------------------


def test_base_on_reset_clears_bar_index_and_last_signal() -> None:
    """BaseStrategy.on_reset() resets _bar_index and _last_signal_bar_index."""
    strategy = _make_ema_strategy()
    strategy._bar_index = 42
    strategy._last_signal_bar_index = 38
    strategy.on_reset()
    assert strategy._bar_index == 0
    assert strategy._last_signal_bar_index is None


def test_subclass_on_reset_propagates_to_base() -> None:
    """EMACrossStrategy.on_reset() calls super(), so base fields are cleared."""
    strategy = _make_ema_strategy(cooldown=3)
    strategy._bar_index = 99
    strategy._last_signal_bar_index = 95
    # 手动设置EMA状态以确认子类重置也运行
    strategy._prev_fast_above = True

    strategy.on_reset()

    # 通过 super() 清除基本字段
    assert strategy._bar_index == 0
    assert strategy._last_signal_bar_index is None
    # 子类特定字段也被清除
    assert strategy._prev_fast_above is None


def test_on_reset_also_clears_sl_tp_orders() -> None:
    """on_reset() still clears bracket order maps (existing behaviour preserved)."""
    from nautilus_trader.model.identifiers import ClientOrderId

    strategy = _make_ema_strategy()
    strategy._sl_orders["pos-1"] = ClientOrderId("SL-001")
    strategy._tp_orders["pos-1"] = ClientOrderId("TP-001")

    strategy.on_reset()

    assert strategy._sl_orders == {}
    assert strategy._tp_orders == {}
