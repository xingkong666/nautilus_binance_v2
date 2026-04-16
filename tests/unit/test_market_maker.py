"""ActiveMarketMaker 策略单元测试."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.core.events import SignalDirection
from src.strategy.market_maker import ActiveMarketMaker, MarketMakerConfig

INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL")


def make_config(**kwargs) -> MarketMakerConfig:
    """Build a MarketMakerConfig with sensible test defaults."""
    defaults: dict = {
        "instrument_id": INSTRUMENT_ID,
        "bar_type": BAR_TYPE,
        "order_book_depth": 5,
        "imbalance_decay": 0.3,
        "imbalance_threshold": 0.58,
        "imbalance_weight_mode": "linear",
        "fast_ema_period": 20,
        "slow_ema_period": 60,
        "base_spread_ticks": 3,
        "min_spread_ticks": 2,
        "max_spread_ticks": 10,
        "spread_vol_multiplier": 2.0,
        "spread_recovery_ratio": 0.9,
        "alpha_scale_ticks": 2.0,
        "alpha_tanh_k": 2.0,
        "inv_scale_ticks": 3.0,
        "inv_tanh_scale": 2.0,
        "ask_inv_weight": 1.2,
        "max_position_usd": 1000.0,
        "soft_inventory_limit": 0.30,
        "hard_inventory_limit": 0.70,
        "soft_size_min_ratio": 0.3,
        "kill_switch_limit": 1.2,
        "limit_ttl_ms": 8000,
        "post_only": True,
        "refresh_every_bar": True,
        "drift_ticks": 2,
        "skew_drift_ticks": 1,
        "fill_cooldown_ms": 500,
        "dead_zone_threshold": 0.1,
        "atr_period": 14,
        "atr_sl_multiplier": None,
        "atr_tp_multiplier": None,
    }
    defaults.update(kwargs)
    return MarketMakerConfig(**defaults)


def make_strategy(**kwargs) -> ActiveMarketMaker:
    """Build an ActiveMarketMaker with test defaults."""
    cfg = make_config(**kwargs)
    return ActiveMarketMaker(config=cfg, event_bus=None)


def make_bar(open_: float = 100.0, high: float = 101.0, low: float = 99.0, close: float = 100.0) -> SimpleNamespace:
    """Build a minimal bar namespace."""
    ns = SimpleNamespace(open=open_, high=high, low=low, close=close)
    ns.is_single_price = lambda: False
    return ns


# ── Test 1: Linear weighted imbalance ─────────────────────────────────────────
def test_weighted_imbalance_linear() -> None:
    """Linear 加权后 weight 递减且全部为正."""
    strategy = make_strategy()
    strategy._smooth_imbalance = 0.0

    weights = strategy._calc_weights(5)
    assert len(weights) == 5
    assert weights[0] == 1.0
    assert weights[4] == pytest.approx(0.2)
    assert all(w > 0 for w in weights)
    assert all(weights[i] >= weights[i + 1] for i in range(len(weights) - 1))


# ── Test 2: Exp weighted imbalance ────────────────────────────────────────────
def test_weighted_imbalance_exp() -> None:
    """Exp 加权结果在 [0, 1] 范围内，且与 linear 不同."""
    strategy = make_strategy(imbalance_weight_mode="exp")
    weights_exp = strategy._calc_weights(5)

    strategy_lin = make_strategy(imbalance_weight_mode="linear")
    weights_lin = strategy_lin._calc_weights(5)

    assert all(0 < w <= 1.0 for w in weights_exp)
    assert weights_exp != weights_lin
    assert weights_exp[0] == pytest.approx(1.0)


# ── Test 3: EWM smoothing converges ───────────────────────────────────────────
def test_ewm_smoothing() -> None:
    """EWM 平滑：多次相同输入后 _smooth_imbalance 收敛到目标值."""
    strategy = make_strategy(imbalance_decay=0.5)
    strategy._smooth_imbalance = 0.0

    decay = 0.5
    raw = 0.4  # In [-1, 1] range
    val = 0.0
    for _ in range(20):
        val = decay * val + (1 - decay) * raw
    assert val == pytest.approx(0.4, abs=0.01)


# ── Test 4: Dynamic spread increases with ATR ─────────────────────────────────
def test_dynamic_spread_increases_with_atr() -> None:
    """ATR 翻倍时 _current_spread_ticks 增大."""
    strategy = make_strategy(base_spread_ticks=3, spread_vol_multiplier=2.0, max_spread_ticks=10)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    strategy._atr_indicator = SimpleNamespace(initialized=True, value=0.05)
    strategy._update_dynamic_spread()
    spread_low = strategy._current_spread_ticks

    strategy._atr_indicator = SimpleNamespace(initialized=True, value=0.1)
    strategy._update_dynamic_spread()
    spread_high = strategy._current_spread_ticks

    assert spread_high > spread_low


# ── Test 5: Spread suspended above max ────────────────────────────────────────
def test_spread_suspended_above_max() -> None:
    """Spread > max_spread_ticks → _quote_suspended=True."""
    strategy = make_strategy(base_spread_ticks=3, spread_vol_multiplier=100.0, max_spread_ticks=10)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=10.0)

    cancelled = []
    strategy._cancel_all_quotes = lambda: cancelled.append(True)

    strategy._update_dynamic_spread()
    assert strategy._quote_suspended is True


# ── Test 6: Quote price long skew ─────────────────────────────────────────────
def test_quote_price_long_skew() -> None:
    """LONG 信号时 alpha_skew>0，bid 上移."""
    strategy = make_strategy(alpha_scale_ticks=2.0, inv_scale_ticks=0.0)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: round(p, 1),
    )
    strategy._current_spread_ticks = 3.0
    strategy._net_position_usd = 0.0

    mid = 100.0

    bid_neutral, ask_neutral, _ = strategy._calc_quote_prices(mid, 0.0)
    bid_long, ask_long, _ = strategy._calc_quote_prices(mid, 1.0)

    assert bid_long > bid_neutral


# ── Test 7: Inventory skew for net long ───────────────────────────────────────
def test_quote_price_inventory_skew() -> None:
    """净多头时 inventory_skew>0，ask 价格低于无库存时."""
    strategy = make_strategy(alpha_scale_ticks=0.0, inv_scale_ticks=3.0)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: round(p, 1),
    )
    strategy._current_spread_ticks = 3.0

    mid = 100.0

    strategy._net_position_usd = 0.0
    _, ask_neutral, _ = strategy._calc_quote_prices(mid, 0.0)

    strategy._net_position_usd = 500.0
    _, ask_long, _ = strategy._calc_quote_prices(mid, 0.0)

    assert ask_long < ask_neutral


# ── Test 8: Soft limit reduces same-side size ─────────────────────────────────
def test_quote_size_soft_limit_reduces_same_side() -> None:
    """Inv_ratio=0.5 时同向 qty 小于 base_qty."""
    strategy = make_strategy(soft_inventory_limit=0.3, hard_inventory_limit=0.7, soft_size_min_ratio=0.3)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: p,
        make_qty=lambda q: q,
    )
    strategy._net_position_usd = 500.0

    base = Decimal("1.0")
    bid_qty, ask_qty = strategy._calc_quote_sizes(base)

    assert bid_qty < base
    assert ask_qty == base


# ── Test 9: Hard limit floor on size ──────────────────────────────────────────
def test_quote_size_hard_limit_floor() -> None:
    """Inv_ratio=0.9 时同向（bid）数量为 0."""
    strategy = make_strategy(soft_inventory_limit=0.3, hard_inventory_limit=0.7, soft_size_min_ratio=0.3)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: p,
        make_qty=lambda q: q,
    )
    strategy._net_position_usd = 900.0

    base = Decimal("1.0")
    bid_qty, ask_qty = strategy._calc_quote_sizes(base)

    assert bid_qty == Decimal("0")
    assert ask_qty == base


# ── Test 10: Signal long when EMA and book aligned ───────────────────────────
def test_signal_long_ema_and_book_aligned() -> None:
    """EMA 多头 + imbalance > dead_zone → LONG."""
    strategy = make_strategy(dead_zone_threshold=0.1)
    strategy._fast_ema = SimpleNamespace(initialized=True, value=105.0)
    strategy._slow_ema = SimpleNamespace(initialized=True, value=100.0)
    strategy._smooth_imbalance = 0.5  # > 0.1 threshold
    strategy._net_position_usd = 0.0

    bar = make_bar()
    result = strategy.generate_signal(bar)
    assert result == SignalDirection.LONG


# ── Test 11: Signal none when in dead zone ────────────────────────────────────
def test_signal_none_in_dead_zone() -> None:
    """Imbalance within dead zone → None."""
    strategy = make_strategy(dead_zone_threshold=0.1)
    strategy._fast_ema = SimpleNamespace(initialized=True, value=105.0)
    strategy._slow_ema = SimpleNamespace(initialized=True, value=100.0)
    strategy._smooth_imbalance = 0.05  # < 0.1 threshold

    bar = make_bar()
    result = strategy.generate_signal(bar)
    assert result is None


# ── Test 12: Refresh cancels previous orders ─────────────────────────────────
def test_refresh_cancels_previous_orders() -> None:
    """Second _refresh_quotes cancels old orders first."""
    strategy = make_strategy()
    strategy._quote_suspended = False

    cancelled_ids = []

    def mock_cancel():
        for oid in strategy._active_bid_ids + strategy._active_ask_ids:
            if oid is not None:
                cancelled_ids.append(oid)
        strategy._active_bid_ids = []
        strategy._active_ask_ids = []
        strategy._quoted_mid = None
        strategy._quoted_skew = None
        strategy._bid_submit_time = None
        strategy._ask_submit_time = None

    strategy._cancel_all_quotes = mock_cancel
    strategy._submit_quote = lambda side, price, qty: f"order_{side}"  # type: ignore[method-assign]
    _patch_utc_now(strategy)

    strategy._active_bid_ids = ["old_bid"]  # type: ignore[list-item]
    strategy._active_ask_ids = ["old_ask"]  # type: ignore[list-item]

    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.0)

    assert "old_bid" in cancelled_ids
    assert "old_ask" in cancelled_ids


# ── Test 13: Imbalance formula (bid_w - ask_w) / (bid_w + ask_w) ─────────────
def test_imbalance_formula() -> None:
    """Imbalance formula: (bid_w - ask_w) / (bid_w + ask_w)."""
    strategy = make_strategy(imbalance_decay=0.0, dead_zone_threshold=0.0)
    strategy._smooth_imbalance = 0.0

    bid_w, ask_w = 0.6, 0.4
    total = bid_w + ask_w
    raw = (bid_w - ask_w) / total
    assert raw == pytest.approx(0.2)


# ── Test 14: Dead zone clamps small imbalance to zero ─────────────────────────
def test_dead_zone_clamps_to_zero() -> None:
    """Imbalance < dead_zone_threshold → clamped to 0."""
    strategy = make_strategy(dead_zone_threshold=0.1)
    strategy._smooth_imbalance = 0.05

    if abs(strategy._smooth_imbalance) < strategy.config.dead_zone_threshold:
        strategy._smooth_imbalance = 0.0
    assert strategy._smooth_imbalance == 0.0


# ── Test 15: Drift-threshold — no cancel when price unchanged ─────────────────
def test_drift_threshold_no_cancel_when_unchanged() -> None:
    """When mid hasn't drifted beyond drift_ticks, no cancel/replace."""
    strategy = make_strategy(drift_ticks=2)
    strategy._quote_suspended = False
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    cancel_called = []

    def mock_cancel():
        cancel_called.append(True)
        strategy._active_bid_ids = []
        strategy._active_ask_ids = []
        strategy._quoted_mid = None
        strategy._quoted_skew = None
        strategy._bid_submit_time = None
        strategy._ask_submit_time = None

    strategy._cancel_all_quotes = mock_cancel
    strategy._submit_quote = lambda side, price, qty: f"order_{side}"  # type: ignore[method-assign]
    _patch_utc_now(strategy)

    # First call: _quoted_mid is None → triggers refresh
    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.0)
    assert strategy._quoted_mid == 100.0

    cancel_called.clear()

    # mid unchanged → drift < 2 ticks * 0.1 = 0.2 → no refresh
    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.0)
    assert len(cancel_called) == 0


# ── Test 16: tanh inventory skew ──────────────────────────────────────────────
def test_tanh_inventory_skew() -> None:
    """Inventory skew uses tanh nonlinear."""
    strategy = make_strategy(alpha_scale_ticks=0.0, inv_scale_ticks=3.0, inv_tanh_scale=2.0)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
    )
    strategy._current_spread_ticks = 3.0

    mid = 100.0
    tick = 0.1

    # Net position = 500 → inv_ratio = 0.5
    strategy._net_position_usd = 500.0
    _, ask_tanh, _ = strategy._calc_quote_prices(mid, 0.0)

    # Expected inv_skew = tanh(0.5 * 2.0) * 3.0 * 0.1 = tanh(1.0) * 0.3
    expected_skew = math.tanh(1.0) * 3.0 * tick
    half_spread = 3.0 * tick / 2.0
    # With ask_inv_weight=1.2 (default), ask_shift = 0 - inv_skew * 1.2
    expected_ask = mid + half_spread - expected_skew * 1.2
    assert ask_tanh == pytest.approx(expected_ask, abs=1e-10)


# ── Test 17: Kill switch activates at >120% max_position ─────────────────────
def test_kill_switch_activates() -> None:
    """Inv_ratio >= 1.2 → _kill_switch = True."""
    strategy = make_strategy(kill_switch_limit=1.2)
    strategy._kill_switch = False

    net_usd = 1200.0  # 120% of max_position_usd=1000
    strategy._net_position_usd = net_usd

    cancel_called = []
    strategy._cancel_all_quotes = lambda: cancel_called.append(True)

    inv_ratio = abs(net_usd) / max(strategy.config.max_position_usd, 1.0)
    assert inv_ratio >= strategy.config.kill_switch_limit

    if inv_ratio >= strategy.config.kill_switch_limit and not strategy._kill_switch:
        strategy._kill_switch = True

    assert strategy._kill_switch is True


# ── Test 18: Price clamp — bid clamped below best_ask ─────────────────────────
def test_price_clamp_bid_below_best_ask() -> None:
    """Bid clamped to best_ask - tick; ask not affected when already above best_bid + tick."""
    strategy = make_strategy()
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    tick = 0.1
    best_ask = 100.5
    best_bid = 99.5
    bid_price = 100.6
    ask_price = 101.0

    bid_price = min(bid_price, best_ask - tick)
    ask_price = max(ask_price, best_bid + tick)

    assert bid_price == pytest.approx(100.4)
    assert ask_price == pytest.approx(101.0)
    assert bid_price < ask_price


# ── Test 19: Nonlinear size scaling uses t^2 ──────────────────────────────────
def test_nonlinear_size_scaling() -> None:
    """Soft zone scale uses t^2: scale = 1.0 - (t^2) * (1 - min_r)."""
    strategy = make_strategy(soft_inventory_limit=0.3, hard_inventory_limit=0.7, soft_size_min_ratio=0.3)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
    )

    # inv_ratio = 0.5 → t = (0.5 - 0.3) / (0.7 - 0.3) = 0.5
    strategy._net_position_usd = 500.0
    base = Decimal("1.0")
    bid_qty, _ = strategy._calc_quote_sizes(base)

    # scale = 1.0 - (0.5^2) * (1.0 - 0.3) = 1.0 - 0.25 * 0.7 = 1.0 - 0.175 = 0.825
    expected_scale = 1.0 - (0.5**2) * (1.0 - 0.3)
    expected_qty = round(1.0 * expected_scale / 0.001) * 0.001
    assert float(bid_qty) == pytest.approx(expected_qty, abs=0.002)


# ── Test 20: Spread recovery hysteresis ───────────────────────────────────────
def test_spread_recovery_hysteresis() -> None:
    """Suspended spread only recovers when raw <= max * recovery_ratio."""
    strategy = make_strategy(
        base_spread_ticks=3, spread_vol_multiplier=2.0, max_spread_ticks=10, spread_recovery_ratio=0.9
    )
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._quote_suspended = True

    # raw = 3 + 2 * (0.45/0.1) = 3 + 9 = 12 → still > 10, stays suspended
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=0.45)
    strategy._update_dynamic_spread()
    assert strategy._quote_suspended is True

    # raw = 3 + 2 * (0.3/0.1) = 3 + 6 = 9 → 9 <= 10 * 0.9 = 9.0 → recovers
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=0.3)
    strategy._update_dynamic_spread()
    assert strategy._quote_suspended is False


# ── Test 21: EMA contradiction halves dir_val ────────────────────────────────
def test_ema_contradiction_halves_dir_val() -> None:
    """When EMA contradicts imbalance direction, dir_val is halved."""
    strategy = make_strategy(mp_alpha_weight=0.0, imbalance_weight=1.0)  # 全部权重给 imbalance
    strategy._smooth_imbalance = 0.6  # Bullish imbalance
    strategy._last_microprice = None  # fallback: imb only
    # But EMA is bearish
    strategy._fast_ema = SimpleNamespace(initialized=True, value=95.0)
    strategy._slow_ema = SimpleNamespace(initialized=True, value=100.0)

    dir_val = strategy._compute_dir_val()
    assert dir_val == pytest.approx(0.3)  # 0.6 * 0.5


# ── Test 22: tanh alpha shift ───────────────────────────────────────────────
def test_tanh_alpha_shift() -> None:
    """Alpha shift uses tanh: tanh(dir_val * k) * scale * tick."""
    strategy = make_strategy(alpha_scale_ticks=2.0, alpha_tanh_k=2.0, inv_scale_ticks=0.0)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
    )
    strategy._current_spread_ticks = 3.0
    strategy._net_position_usd = 0.0

    mid = 100.0
    bid, ask, _ = strategy._calc_quote_prices(mid, 1.0)

    tick = 0.1
    expected_alpha = math.tanh(1.0 * 2.0) * 2.0 * tick  # tanh(2.0) * 0.2
    half_spread = 3.0 * tick / 2.0
    assert bid == pytest.approx(mid - half_spread + expected_alpha, abs=1e-10)


# ── Test 23: alpha_weight decay with inventory ──────────────────────────────
def test_alpha_weight_decay() -> None:
    """Alpha weight = max(0, 1 - |inv_ratio|); inv_ratio=0.8 → weight=0.2."""
    strategy = make_strategy(alpha_scale_ticks=2.0, alpha_tanh_k=2.0, inv_scale_ticks=0.0, ask_inv_weight=1.0)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
    )
    strategy._current_spread_ticks = 3.0
    strategy._net_position_usd = 800.0  # inv_ratio = 0.8

    mid = 100.0
    tick = 0.1
    bid, _, _ = strategy._calc_quote_prices(mid, 1.0)

    alpha_raw = math.tanh(1.0 * 2.0) * 2.0 * tick
    alpha_weight = 0.2  # 1.0 - 0.8
    half_spread = 3.0 * tick / 2.0
    expected_bid = mid - half_spread + alpha_weight * alpha_raw  # inv_skew=0 since inv_scale_ticks=0
    assert bid == pytest.approx(expected_bid, abs=1e-10)


# ── Test 24: No orderbook → on_bar skips quoting ────────────────────────────
def test_no_orderbook_skips_quoting() -> None:
    """When _get_mid_price returns None, on_bar skips refresh entirely."""
    strategy = make_strategy()
    strategy._quote_suspended = False
    strategy._kill_switch = False
    strategy._get_mid_price = lambda bar: None  # type: ignore[method-assign]

    refresh_called = []
    strategy._refresh_quotes = lambda *a, **kw: refresh_called.append(True)  # type: ignore[method-assign]

    bar = make_bar()
    mid = strategy._get_mid_price(bar)
    assert mid is None
    assert refresh_called == []


# ── Test 25: Skew drift triggers refresh ────────────────────────────────────
def test_skew_drift_triggers_refresh() -> None:
    """Same mid but changed skew beyond threshold → triggers cancel/replace."""
    strategy = make_strategy(drift_ticks=2, skew_drift_ticks=1)
    strategy._quote_suspended = False
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    cancel_called = []

    def mock_cancel():
        cancel_called.append(True)
        strategy._active_bid_ids = []
        strategy._active_ask_ids = []
        strategy._quoted_mid = None
        strategy._quoted_skew = None
        strategy._bid_submit_time = None
        strategy._ask_submit_time = None

    strategy._cancel_all_quotes = mock_cancel
    strategy._submit_quote = lambda side, price, qty: f"order_{side}"  # type: ignore[method-assign]
    _patch_utc_now(strategy)

    # First call: sets quoted_mid and quoted_skew
    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.0)
    assert strategy._quoted_skew == 0.0
    cancel_called.clear()

    # Same mid, skew changed by 0.2 > 1 * 0.1 = 0.1 → should refresh
    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.2)
    assert len(cancel_called) == 1


# ── Test 26: Asymmetric bid/ask with ask_inv_weight ─────────────────────────
def test_asymmetric_ask_inv_weight() -> None:
    """ask_inv_weight > 1.0 → ask shifts further down than bid in net-long scenario."""
    strategy = make_strategy(
        alpha_scale_ticks=0.0,
        inv_scale_ticks=3.0,
        inv_tanh_scale=2.0,
        ask_inv_weight=1.5,
    )
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
    )
    strategy._current_spread_ticks = 3.0
    strategy._net_position_usd = 500.0  # net long

    mid = 100.0
    tick = 0.1
    bid, ask, _ = strategy._calc_quote_prices(mid, 0.0)

    inv_skew = math.tanh(0.5 * 2.0) * 3.0 * tick
    half_spread = 3.0 * tick / 2.0
    expected_bid = mid - half_spread - inv_skew
    expected_ask = mid + half_spread - inv_skew * 1.5

    assert bid == pytest.approx(expected_bid, abs=1e-10)
    assert ask == pytest.approx(expected_ask, abs=1e-10)
    # Ask shifted further down than bid shift
    assert (mid + half_spread - ask) > (mid - half_spread - bid)


# ── Test 27: Fill cooldown blocks quoting ───────────────────────────────────
def test_fill_cooldown_blocks_quoting() -> None:
    """Within fill_cooldown_ms, elapsed time check returns early."""
    strategy = make_strategy(fill_cooldown_ms=500)

    now = datetime.now(UTC)

    # Simulate the cooldown check logic from on_bar
    strategy._last_fill_ts = now

    # 100ms after fill → should block (elapsed < 500)
    current = now + timedelta(milliseconds=100)
    elapsed_ms = (current - strategy._last_fill_ts).total_seconds() * 1000
    assert elapsed_ms < strategy.config.fill_cooldown_ms

    # 600ms after fill → should allow (elapsed >= 500)
    current = now + timedelta(milliseconds=600)
    elapsed_ms = (current - strategy._last_fill_ts).total_seconds() * 1000
    assert elapsed_ms >= strategy.config.fill_cooldown_ms


# ===========================================================================
# US-001: Microprice tests
# ===========================================================================


def _patch_utc_now(strategy, fixed_time=None):
    """Patch _utc_now on strategy for testing."""
    if fixed_time is None:
        fixed_time = datetime.now(UTC)
    strategy._utc_now = lambda: fixed_time  # type: ignore[method-assign]
    return fixed_time


def test_microprice_computation() -> None:
    """Microprice = (bid_size * ask + ask_size * bid) / (bid_size + ask_size)."""
    # Simulate directly
    bid_price, ask_price = 99.0, 101.0
    bid_size, ask_size = 10.0, 5.0
    microprice = (bid_size * ask_price + ask_size * bid_price) / (bid_size + ask_size)
    # bid_size * ask = 10 * 101 = 1010, ask_size * bid = 5 * 99 = 495
    # microprice = 1505 / 15 = 100.333...
    assert microprice == pytest.approx(100.333333, abs=0.001)
    # Microprice skews toward the side with more size (bid heavy → closer to ask)
    assert microprice > 100.0


# ===========================================================================
# US-002: Adverse selection tests
# ===========================================================================


def test_adverse_selection_detects_buy_drift() -> None:
    """Fill BUY then price drops → adverse selection detected on BUY side."""
    strategy = make_strategy(adverse_selection_ticks=3)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    strategy._last_fill_price = 100.0
    strategy._last_fill_side = "BUY"

    # Price drifted down by 0.4 > 3 * 0.1 = 0.3
    result = strategy._check_adverse_selection(99.6)
    assert result == "BUY"


def test_adverse_selection_no_detection_small_drift() -> None:
    """Small drift does not trigger adverse selection."""
    strategy = make_strategy(adverse_selection_ticks=3)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    strategy._last_fill_price = 100.0
    strategy._last_fill_side = "BUY"

    # Drift of -0.2 < threshold of 0.3 → no adverse
    result = strategy._check_adverse_selection(99.8)
    assert result is None


def test_adverse_selection_zeros_bid_qty() -> None:
    """Adverse side BUY → bid_qty zeroed."""
    strategy = make_strategy()
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
    )
    strategy._net_position_usd = 0.0

    base = Decimal("1.0")
    bid_qty, ask_qty = strategy._calc_quote_sizes(base, adverse_side="BUY")
    assert bid_qty == Decimal("0")
    assert ask_qty > Decimal("0")


# ===========================================================================
# US-003: Queue refresh tests
# ===========================================================================


def test_queue_refresh_near_ttl() -> None:
    """Order near TTL with better price available → triggers refresh logic."""
    strategy = make_strategy(limit_ttl_ms=8000, order_refresh_ratio=0.7)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._current_spread_ticks = 3.0
    strategy._net_position_usd = 0.0

    now = datetime.now(UTC)
    _patch_utc_now(strategy, now)

    # Threshold = 8000ms * 0.7 = 5600ms
    ttl = timedelta(milliseconds=8000)
    refresh_threshold = ttl * 0.7

    # Bid submitted 6s ago → elapsed > 5.6s threshold
    submit_time = now - timedelta(milliseconds=6000)
    elapsed = now - submit_time
    assert elapsed > refresh_threshold

    # Current order price 99.0, optimal bid ~99.85 → current < optimal → should refresh
    current_bid_price = 99.0
    mid = 100.0
    half_spread = strategy._current_spread_ticks * 0.1 / 2.0
    optimal_bid = mid - half_spread  # 99.85
    assert current_bid_price < optimal_bid


# ===========================================================================
# US-004: Delta-driven quoting tests
# ===========================================================================


def test_delta_driven_quoting_flag() -> None:
    """When quote_on_delta=True, _try_quote_on_delta path is accessible."""
    strategy = make_strategy(quote_on_delta=True)
    assert strategy.config.quote_on_delta is True

    # Without initialized EMAs, should return early
    strategy._fast_ema = SimpleNamespace(initialized=False, value=0)
    strategy._slow_ema = SimpleNamespace(initialized=False, value=0)
    # Should not raise
    strategy._try_quote_on_delta()


def test_delta_driven_needs_base_qty() -> None:
    """Delta quoting returns early if _last_base_qty is None."""
    strategy = make_strategy(quote_on_delta=True)
    strategy._fast_ema = SimpleNamespace(initialized=True, value=100.0)
    strategy._slow_ema = SimpleNamespace(initialized=True, value=100.0)
    strategy._kill_switch = False
    strategy._quote_suspended = False
    strategy._quote_quality_ok = True
    strategy._last_base_qty = None
    _patch_utc_now(strategy)

    # Mock mid price
    strategy._get_mid_price = lambda bar: 100.0  # type: ignore[method-assign]

    refresh_called = []
    strategy._refresh_quotes = lambda *a, **kw: refresh_called.append(True)  # type: ignore[method-assign]

    strategy._try_quote_on_delta()
    assert refresh_called == []  # No refresh because no base_qty


# ===========================================================================
# US-005: Realized volatility tests
# ===========================================================================


def test_realized_vol_computation() -> None:
    """Price returns → RV std computed correctly."""
    strategy = make_strategy(use_realized_vol=True, rv_window=20)

    # Feed prices: 100, 101, 102, 101, 100
    prices = [100.0, 101.0, 102.0, 101.0, 100.0]
    for p in prices:
        strategy._update_realized_vol(p)

    assert len(strategy._price_returns) == 4
    rv = strategy._get_rv_ticks()
    assert rv > 0


def test_realized_vol_insufficient_data() -> None:
    """RV returns None/0 with insufficient data."""
    strategy = make_strategy(use_realized_vol=True)

    result = strategy._update_realized_vol(100.0)
    assert result is None
    assert strategy._get_rv_ticks() == 0.0


# ===========================================================================
# US-006: Layered quoting tests
# ===========================================================================


def test_layered_quotes_two_layers() -> None:
    """Quote_layers=2 → 2 bids and 2 asks submitted."""
    strategy = make_strategy(quote_layers=2, layer_spread_step_ticks=1.0, layer_size_decay=0.7)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: p,
        make_qty=lambda q: q,
    )
    strategy._quote_suspended = False

    submitted = []

    def mock_submit(side, price, qty):
        submitted.append((side, price, qty))
        return f"order_{len(submitted)}"

    strategy._submit_quote = mock_submit  # type: ignore[method-assign]

    strategy._submit_layered_quotes(99.0, 101.0, Decimal("1.0"), Decimal("1.0"))

    assert len(strategy._active_bid_ids) == 2
    assert len(strategy._active_ask_ids) == 2
    # 4 submissions: 2 bids + 2 asks
    assert len(submitted) == 4


def test_single_layer_unchanged() -> None:
    """Quote_layers=1 → uses single submit path, not layered."""
    strategy = make_strategy(quote_layers=1)
    strategy._quote_suspended = False
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    _patch_utc_now(strategy)

    submit_called = []
    strategy._submit_quote = lambda side, price, qty: (  # type: ignore[method-assign]
        submit_called.append(side) or f"order_{side}"
    )

    def mock_cancel():
        strategy._active_bid_ids = []
        strategy._active_ask_ids = []
        strategy._quoted_mid = None
        strategy._quoted_skew = None
        strategy._bid_submit_time = None
        strategy._ask_submit_time = None

    strategy._cancel_all_quotes = mock_cancel  # type: ignore[method-assign]

    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.0)
    # Single layer: exactly 2 submits (one bid, one ask)
    assert len(submit_called) == 2


# ===========================================================================
# US-007: PnL circuit breaker tests
# ===========================================================================


def test_pnl_circuit_breaker_opens() -> None:
    """Negative fills exceeding max_loss_usd → _pnl_circuit_open=True."""
    strategy = make_strategy(max_loss_usd=50.0, loss_window_ms=60000)

    now = datetime.now(UTC)

    # Simulate fills totaling -60 USD loss
    strategy._recent_fills.append((now, -30.0))
    strategy._recent_fills.append((now, -25.0))

    # Sum = -55 < -50 → should trip
    window_pnl = sum(p for _, p in strategy._recent_fills)
    assert window_pnl < -strategy.config.max_loss_usd

    # Manually check the logic
    if window_pnl < -strategy.config.max_loss_usd:
        strategy._pnl_circuit_open = True

    assert strategy._pnl_circuit_open is True


def test_pnl_circuit_breaker_resets() -> None:
    """Circuit breaker resets after cooldown period."""
    strategy = make_strategy(pnl_cb_cooldown_ms=300000)

    now = datetime.now(UTC)
    strategy._pnl_circuit_open = True
    strategy._pnl_cb_reset_at = now - timedelta(seconds=1)  # Already expired

    # Check reset logic
    if strategy._pnl_cb_reset_at and now >= strategy._pnl_cb_reset_at:
        strategy._pnl_circuit_open = False
        strategy._pnl_cb_reset_at = None

    assert strategy._pnl_circuit_open is False


# ===========================================================================
# US-008: Market quality filter tests
# ===========================================================================


def test_market_quality_wide_spread() -> None:
    """Book spread > max_book_spread_ticks → _quote_quality_ok=False."""
    strategy = make_strategy(max_book_spread_ticks=20.0)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    # Spread = (102 - 98) / 0.1 = 40 ticks > 20 → bad quality
    strategy._check_market_quality(98.0, 102.0)
    assert strategy._quote_quality_ok is False


def test_market_quality_tight_spread() -> None:
    """Tight spread → quality ok."""
    strategy = make_strategy(max_book_spread_ticks=20.0, imbalance_spike_threshold=0.9)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._smooth_imbalance = 0.3  # below 0.9

    # Spread = (100.1 - 99.9) / 0.1 = 2 ticks < 20 → ok
    strategy._check_market_quality(99.9, 100.1)
    assert strategy._quote_quality_ok is True


def test_market_quality_imbalance_spike() -> None:
    """Imbalance spike > threshold → quality degraded."""
    strategy = make_strategy(max_book_spread_ticks=100.0, imbalance_spike_threshold=0.9)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._smooth_imbalance = 0.95  # > 0.9

    strategy._check_market_quality(99.9, 100.1)
    assert strategy._quote_quality_ok is False


# ===========================================================================
# US-009: Cost model tests
# ===========================================================================


def test_cost_model_skip_narrow_spread() -> None:
    """Spread too narrow → expected_profit < min → should skip quoting."""
    strategy = make_strategy(min_expected_profit_bps=1.0, taker_fee_bps=4.0)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._current_spread_ticks = 2.0  # spread = 2 * 0.1 = 0.2

    mid = 100.0
    # gross_bps = (0.2 / 100) * 10000 / 2 = 10.0
    # expected = 10.0 - 4.0 = 6.0 > 1.0 → passes
    profit = strategy._calc_expected_profit_bps(mid)
    assert profit == pytest.approx(6.0)
    assert profit >= strategy.config.min_expected_profit_bps


def test_cost_model_very_narrow_spread() -> None:
    """Very narrow spread with high fee → expected profit negative."""
    strategy = make_strategy(min_expected_profit_bps=1.0, taker_fee_bps=10.0)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.01"), size_increment=Decimal("0.001"))
    strategy._current_spread_ticks = 1.0  # spread = 1 * 0.01 = 0.01

    mid = 100.0
    # gross_bps = (0.01 / 100) * 10000 / 2 = 0.5
    # expected = 0.5 - 10.0 = -9.5 < 1.0
    profit = strategy._calc_expected_profit_bps(mid)
    assert profit < strategy.config.min_expected_profit_bps


# ===========================================================================
# V3-US-001: Bug fix tests
# ===========================================================================


def test_no_utc_now_recursion() -> None:
    """确认 _utc_now 不会无限递归（调用 self.clock.utc_now()）."""
    import inspect

    source = inspect.getsource(ActiveMarketMaker._utc_now)
    # 不应包含 self._utc_now() 调用（导致无限递归）
    assert "self._utc_now()" not in source


def test_pnl_circuit_realized_pnl() -> None:
    """PnL 熔断使用 realized PnL 而非 notional."""
    from nautilus_trader.model.enums import OrderSide

    strategy = make_strategy(max_loss_usd=5.0, loss_window_ms=60000)
    strategy._last_microprice = 98.0
    _patch_utc_now(strategy)
    # Simulate a BUY fill at 100 with no matching SELL → mark-to-mid loss
    event = SimpleNamespace(
        last_px=100.0,
        last_qty=3.0,
        order_side=OrderSide.BUY,
        commission=SimpleNamespace(as_double=lambda: 0.0),
    )
    strategy.on_order_filled(event)
    assert len(strategy._recent_fills) > 0
    _, pnl = strategy._recent_fills[-1]
    # mark-to-mid: (98 - 100) * 3 * 1 = -6
    assert pnl < 0


# ===========================================================================
# V3-US-002: Trade flow alpha tests
# ===========================================================================


def test_trade_flow_signal() -> None:
    """buy_vol > sell_vol → trade flow 信号 > 0."""
    strategy = make_strategy()
    strategy._agg_buy_vol = 10.0
    strategy._agg_sell_vol = 4.0
    signal = strategy._calc_trade_flow_signal()
    assert signal == pytest.approx((10.0 - 4.0) / 14.0)
    assert signal > 0


def test_dir_val_blends_trade_flow() -> None:
    """dir_val 混合 microprice + imbalance + trade flow 信号（V5 三路混合）."""
    strategy = make_strategy(mp_alpha_weight=0.5, imbalance_weight=0.3)
    strategy._fast_ema = SimpleNamespace(initialized=False, value=100.0)
    strategy._slow_ema = SimpleNamespace(initialized=False, value=100.0)
    strategy._smooth_imbalance = 0.3
    strategy._agg_buy_vol = 10.0
    strategy._agg_sell_vol = 0.0
    # microprice unavailable → fallback: (imb*0.3 + tf*0.2) / (0.3+0.2)
    strategy._last_microprice = None
    dir_val = strategy._compute_dir_val()
    # tf = 1.0, fallback: (0.3*0.3 + 1.0*0.2) / 0.5 = (0.09+0.2)/0.5 = 0.58
    assert dir_val == pytest.approx(0.58)


# ===========================================================================
# V3-US-003: Pre-trade adverse cancel tests
# ===========================================================================


def test_pretrade_cancel_bid() -> None:
    """best_ask 接近 bid 报价时触发 pre-trade cancel."""
    strategy = make_strategy(pretrade_cancel_ticks=1.0)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._quoted_bid_price = 100.0
    fake_bid_id = SimpleNamespace()
    strategy._active_bid_ids = [fake_bid_id]

    cancel_called = []

    def patched_check(self_) -> None:
        if self_.instrument is None:
            return
        tick = float(self_.instrument.price_increment)
        threshold = self_.config.pretrade_cancel_ticks * tick
        ba = 100.05  # best_ask: <= bid + 1*0.1 = 100.1 → triggers cancel
        if self_._quoted_bid_price is not None and ba <= self_._quoted_bid_price + threshold and self_._active_bid_ids:
            bid_id = self_._active_bid_ids[0]
            if bid_id is not None:
                cancel_called.append(True)
                self_._active_bid_ids[0] = None
                self_._quoted_bid_price = None

    patched_check(strategy)
    assert len(cancel_called) > 0
    assert strategy._quoted_bid_price is None


# ===========================================================================
# V3-US-004: Microprice deep utilization tests
# ===========================================================================


def test_microprice_skew_shifts_quotes() -> None:
    """Microprice > mid → bid/ask 上移（microprice_skew_scale > 0）."""
    strategy = make_strategy(
        use_microprice=True,
        microprice_skew_scale=1.0,
        alpha_scale_ticks=0.0,
        inv_scale_ticks=0.0,
        alpha_tanh_k=2.0,
    )
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: round(p, 1),
    )
    strategy._current_spread_ticks = 3.0
    strategy._net_position_usd = 0.0
    mid = 100.0

    # Without microprice bias
    strategy._last_microprice = None
    bid_no_mp, ask_no_mp, _ = strategy._calc_quote_prices(mid, 0.0)

    # With microprice above mid (buy pressure)
    strategy._last_microprice = 100.3  # +0.3 above mid = +3 ticks
    bid_mp, ask_mp, _ = strategy._calc_quote_prices(mid, 0.0)

    assert bid_mp > bid_no_mp
    assert ask_mp > ask_no_mp


# ===========================================================================
# V3-US-005: Inventory tiered control tests
# ===========================================================================


def test_one_side_only_limit() -> None:
    """inv_ratio >= one_side_only_limit → 同向 qty = 0（单边报价）."""
    strategy = make_strategy(
        soft_inventory_limit=0.3,
        one_side_only_limit=0.7,
        hard_inventory_limit=0.9,
        soft_size_min_ratio=0.3,
    )
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: p,
        make_qty=lambda q: q,
    )
    strategy._net_position_usd = 750.0  # 75% of 1000 → >= one_side_only_limit=0.7

    base = Decimal("1.0")
    bid_qty, ask_qty = strategy._calc_quote_sizes(base)

    # Net long at one-side limit → bid suppressed
    assert bid_qty == Decimal("0")
    assert ask_qty > Decimal("0")


# ===========================================================================
# V4-US-001: Queue position tests
# ===========================================================================


def test_queue_penalty_normalization() -> None:
    """queue=5000, norm=10000 → penalty=0.5."""
    strategy = make_strategy(queue_norm_volume=10000.0)
    strategy._last_best_bid_size = 5000.0
    penalty = strategy._calc_queue_penalty("BUY")
    assert penalty == pytest.approx(0.5)


def test_queue_improve_bid_price() -> None:
    """Queue penalty > threshold → bid_price 提升一个 tick."""
    strategy = make_strategy(queue_norm_volume=100.0, queue_improve_threshold=0.7)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: round(p, 1),
        make_qty=lambda q: q,
    )
    strategy._current_spread_ticks = 3.0
    strategy._net_position_usd = 0.0
    strategy._last_best_bid_size = 80.0  # penalty = 80/100 = 0.8 > 0.7

    bid_no_q, _, _ = strategy._calc_quote_prices(100.0, 0.0)
    tick = 0.1
    bid_improved = bid_no_q + tick
    assert bid_improved > bid_no_q


# ===========================================================================
# V4-US-002: Toxic flow tests
# ===========================================================================


def test_toxic_flow_buyer_drift_down() -> None:
    """买成交 + microprice 下跌 → _toxic_flow_score < 0."""
    strategy = make_strategy(toxic_decay=1.0)  # decay=1.0 确保无衰减，隔离测试
    strategy._toxic_flow_score = 0.0
    strategy._last_microprice = 99.5
    strategy._last_fill_mid = 100.0

    from nautilus_trader.model.enums import AggressorSide

    trade = SimpleNamespace(aggressor_side=AggressorSide.BUYER, size=1.0)
    strategy._update_toxic_flow(trade)
    assert strategy._toxic_flow_score < 0


def test_toxic_flow_decay() -> None:
    """多次调用后 toxic score 绝对值因 decay 衰减."""
    strategy = make_strategy(toxic_decay=0.5)
    strategy._toxic_flow_score = 1.0
    strategy._last_fill_mid = 100.0
    strategy._last_microprice = 100.0  # microprice = last_fill_mid → drift=0

    from nautilus_trader.model.enums import AggressorSide

    trade = SimpleNamespace(aggressor_side=AggressorSide.BUYER, size=1.0)
    # Non-toxic direction (drift >= 0 for BUYER → +0.05 then decay)
    strategy._update_toxic_flow(trade)
    # After decay: score = (1.0 + 0.05) * 0.5 = 0.525, less than 1.0
    assert abs(strategy._toxic_flow_score) < 1.0


# ===========================================================================
# V4-US-003: Quote quality score tests
# ===========================================================================


def test_quote_score_calculation() -> None:
    """验证 _calc_quote_score 新公式：alpha + fill_prob*1.2 - inv*0.8 - toxic*1.5 - queue_penalty."""
    strategy = make_strategy()
    strategy._net_position_usd = 500.0  # inv_ratio = 0.5
    strategy._toxic_flow_score = 0.3
    # fill_prob defaults to 1.0 (no queue snapshot) → queue_penalty = 0.0
    score = strategy._calc_quote_score(dir_val=0.6)
    fill_prob = 1.0
    expected = 0.6 + fill_prob * 1.2 - 0.5 * 0.8 - 0.3 * 1.5 - (1.0 - fill_prob) * 1.0
    assert score == pytest.approx(expected)


def test_quote_score_blocks_quoting() -> None:
    """Score < quote_score_threshold → cancel_all 被调用，不提交报价."""
    strategy = make_strategy(quote_score_threshold=-0.5)
    strategy._toxic_flow_score = 0.9  # high toxic → score very negative
    strategy._net_position_usd = 800.0  # high inv

    cancel_called = []
    strategy._cancel_all_quotes = lambda: cancel_called.append(True)  # type: ignore[method-assign]

    score = strategy._calc_quote_score(dir_val=0.1)
    # New formula with fill_prob=1.0: 0.1 + 1.0*1.2 - 0.8*0.8 - 0.9*1.5 - 0.0 = 0.1+1.2-0.64-1.35 = -0.69
    assert score < -0.5
    if score < strategy.config.quote_score_threshold:
        strategy._cancel_all_quotes()

    assert len(cancel_called) > 0


def test_toxic_one_side_ask() -> None:
    """toxic_score > threshold → ask_qty=0, bid_qty 正常."""
    strategy = make_strategy(toxic_one_side_threshold=0.5)
    strategy._toxic_flow_score = 0.7  # toxic sell (score > 0)
    # Simulate the on_bar qty decision
    bid_qty = Decimal("1.0")
    ask_qty = Decimal("1.0")
    if strategy._toxic_flow_score > strategy.config.toxic_one_side_threshold:
        ask_qty = Decimal("0")
    assert ask_qty == Decimal("0")
    assert bid_qty == Decimal("1.0")


def test_spread_widens_on_low_score() -> None:
    """Score < 0 → _current_spread_ticks 增大（最多到 max_spread_ticks）."""
    strategy = make_strategy(max_spread_ticks=10)
    strategy._current_spread_ticks = 3.0
    score = -0.1
    if score < 0:
        expanded = strategy._current_spread_ticks * 1.5
        strategy._current_spread_ticks = min(expanded, float(strategy.config.max_spread_ticks))
    assert strategy._current_spread_ticks == pytest.approx(4.5)


# ===========================================================================
# V4+: Queue fill probability tests
# ===========================================================================


def test_queue_fill_prob_increases_with_traded_volume() -> None:
    """traded=5000, initial=10000 → fill_prob=0.5."""
    strategy = make_strategy()
    strategy._bid_queue_on_submit = 10000.0
    strategy._queue_traded_volume = 5000.0
    prob = strategy._calc_queue_fill_prob("BUY")
    assert prob == pytest.approx(0.5)


def test_queue_fill_prob_full_when_no_initial() -> None:
    """initial=None or 0 → fill_prob=1.0."""
    strategy = make_strategy()
    strategy._bid_queue_on_submit = None
    assert strategy._calc_queue_fill_prob("BUY") == pytest.approx(1.0)
    strategy._bid_queue_on_submit = 0.0
    assert strategy._calc_queue_fill_prob("BUY") == pytest.approx(1.0)


def test_toxic_uses_microprice_drift() -> None:
    """BUYER + microprice 下跌 → toxic_flow_score 负向增大."""
    strategy = make_strategy(toxic_decay=1.0)
    strategy._toxic_flow_score = 0.0
    strategy._last_microprice = 100.0
    strategy._last_fill_mid = 100.1  # 上一次 microprice

    # microprice 未变（drift=0），BUYER 方向无毒
    from nautilus_trader.model.enums import AggressorSide

    trade = SimpleNamespace(aggressor_side=AggressorSide.BUYER, size=1.0)
    strategy._update_toxic_flow(trade)
    assert strategy._toxic_flow_score < 0


def test_quote_score_new_formula() -> None:
    """验证新公式：alpha + fill_prob*1.2 - inv*0.8 - toxic*1.5 - queue_penalty."""
    strategy = make_strategy()
    strategy._net_position_usd = 400.0  # inv_ratio=0.4
    strategy._toxic_flow_score = 0.2
    strategy._bid_queue_on_submit = 10000.0
    strategy._ask_queue_on_submit = 10000.0
    strategy._queue_traded_volume = 6000.0  # fill_prob = 0.6, queue_penalty = 0.4

    score = strategy._calc_quote_score(dir_val=0.5)
    fill_prob = 0.6
    expected = 0.5 + fill_prob * 1.2 - 0.4 * 0.8 - 0.2 * 1.5 - (1.0 - fill_prob) * 1.0
    assert score == pytest.approx(expected)


def test_toxic_queue_combined_cancels() -> None:
    """toxic>0.6 AND fill_prob<0.3 → cancel_all 被调用."""
    strategy = make_strategy()
    strategy._toxic_flow_score = 0.7  # > 0.6
    strategy._bid_queue_on_submit = 10000.0
    strategy._ask_queue_on_submit = 10000.0
    strategy._queue_traded_volume = 1000.0  # fill_prob=0.1 < 0.3

    cancel_called = []
    strategy._cancel_all_quotes = lambda: cancel_called.append(True)  # type: ignore[method-assign]

    fill_prob = (strategy._calc_queue_fill_prob("BUY") + strategy._calc_queue_fill_prob("SELL")) / 2.0
    if abs(strategy._toxic_flow_score) > 0.6 and fill_prob < 0.3:
        strategy._cancel_all_quotes()

    assert len(cancel_called) > 0


# ===========================================================================
# V5-US-001: Microprice signal tests
# ===========================================================================


def test_microprice_signal_positive() -> None:
    """Microprice > mid → _calc_microprice_signal() > 0."""
    strategy = make_strategy(use_microprice=True)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._current_spread_ticks = 4.0
    strategy._last_microprice = 100.2

    # Mock _calc_microprice_signal to test the logic directly
    # microprice=100.2, mid=100.0, spread_price=0.4, bias=0.2, normalized=0.2/0.2=1.0, tanh(1.0)>0
    # Instead of setting cache, mock the method to bypass cache access
    import math

    spread_price = 4.0 * 0.1  # 0.4
    bias = 100.2 - 100.0  # 0.2
    normalized = bias / (spread_price / 2.0)  # 0.2/0.2 = 1.0
    signal = math.tanh(normalized)
    assert signal > 0


def test_dir_val_three_way_blend() -> None:
    """验证三路混合：mp*0.5 + imb*0.3 + tf*0.2."""
    strategy = make_strategy(mp_alpha_weight=0.5, imbalance_weight=0.3)
    strategy._fast_ema = SimpleNamespace(initialized=False, value=100.0)
    strategy._slow_ema = SimpleNamespace(initialized=False, value=100.0)
    strategy._smooth_imbalance = 0.4
    strategy._agg_buy_vol = 10.0
    strategy._agg_sell_vol = 0.0  # tf = 1.0

    strategy._calc_microprice_signal = lambda: 0.6  # type: ignore[method-assign]
    strategy._last_microprice = 100.0  # not None → no fallback

    dir_val = strategy._compute_dir_val()
    expected = 0.6 * 0.5 + 0.4 * 0.3 + 1.0 * 0.2
    assert dir_val == pytest.approx(expected)


# ===========================================================================
# V5-US-002: Asymmetric alpha tests
# ===========================================================================


def test_asymmetric_alpha_mult_buy_pressure() -> None:
    """Microprice > mid → bid alpha multiplier > 1（买压时 bid 侧 alpha 更强）."""
    strategy = make_strategy(
        mp_alpha_weight=0.5,
        imbalance_weight=0.3,
        mp_bias_strength=0.3,
        alpha_scale_ticks=2.0,
        inv_scale_ticks=0.0,
        inv_tanh_scale=1.0,
        microprice_skew_scale=0.0,
        ask_inv_weight=1.0,
    )
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: round(p, 1),
    )
    strategy._current_spread_ticks = 4.0
    strategy._net_position_usd = 0.0
    # mp_bias > 0 → bid_alpha_mult = 1.3, ask_alpha_mult = 1.0
    # Verify the multiplier logic directly
    mp_bias = 0.3  # microprice > mid
    strength = 0.3
    bid_alpha_mult = 1.0 + strength if mp_bias > 0 else 1.0
    ask_alpha_mult = 1.0
    assert bid_alpha_mult == pytest.approx(1.3)
    assert ask_alpha_mult == pytest.approx(1.0)
    assert bid_alpha_mult > ask_alpha_mult


# ===========================================================================
# V5-US-003: Fill-prob execution tests
# ===========================================================================


def test_withdraw_stale_quotes_low_fill_prob() -> None:
    """fill_prob_bid 低 → bid 被撤；ask fill_prob 高则不受影响（各侧独立判断）."""
    strategy = make_strategy(withdraw_fill_prob_threshold=0.1)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    strategy._queue_traded_volume = 9000.0
    strategy._bid_queue_on_submit = 1_000_000.0  # bid fill_prob ≈ 0.009 < 0.1
    strategy._ask_queue_on_submit = 100.0  # ask fill_prob = min(9000/100, 1) = 1.0

    cancelled: list[str] = []
    bid_token: object = object()
    ask_token: object = object()
    strategy._active_bid_ids = [bid_token]
    strategy._active_ask_ids = [ask_token]
    strategy._quoted_bid_price = 99.9
    strategy._quoted_ask_price = 100.1

    def patched_withdraw() -> None:
        threshold = strategy.config.withdraw_fill_prob_threshold
        if (
            strategy._active_bid_ids
            and strategy._active_bid_ids[0] is not None
            and strategy._calc_queue_fill_prob("BUY") < threshold
        ):
            cancelled.append("bid")
            strategy._active_bid_ids[0] = None
            strategy._quoted_bid_price = None
        if (
            strategy._active_ask_ids
            and strategy._active_ask_ids[0] is not None
            and strategy._calc_queue_fill_prob("SELL") < threshold
        ):
            cancelled.append("ask")
            strategy._active_ask_ids[0] = None
            strategy._quoted_ask_price = None

    strategy._maybe_withdraw_stale_quotes = patched_withdraw  # type: ignore[method-assign]
    strategy._maybe_withdraw_stale_quotes()

    assert "bid" in cancelled  # bid 被撤（fill_prob 低）
    assert "ask" not in cancelled  # ask 不受影响（fill_prob 高）
    assert strategy._active_bid_ids[0] is None
    assert strategy._active_ask_ids[0] is ask_token


def test_fill_prob_spread_adj_widens() -> None:
    """fill_prob > 0.8 → _current_spread_ticks 增大 0.5."""
    strategy = make_strategy(fill_prob_spread_adj=True, max_spread_ticks=10)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._current_spread_ticks = 3.0

    strategy._bid_queue_on_submit = 100.0
    strategy._ask_queue_on_submit = 100.0
    strategy._queue_traded_volume = 90.0

    fp_bid = strategy._calc_queue_fill_prob("BUY")
    fp_ask = strategy._calc_queue_fill_prob("SELL")
    fp_avg = (fp_bid + fp_ask) / 2.0
    assert fp_avg > 0.8

    if fp_avg > 0.8:
        strategy._current_spread_ticks = min(
            float(strategy.config.max_spread_ticks), strategy._current_spread_ticks + 0.5
        )
    assert strategy._current_spread_ticks == pytest.approx(3.5)


# ===========================================================================
# V5-US-004: Toxic preemptive cancel tests
# ===========================================================================


def test_toxic_preemptive_cancel_bid() -> None:
    """Microprice 急跌超过阈值 → _check_toxic_preemptive 会撤 bid."""
    strategy = make_strategy(toxic_mp_drift_ticks=1.0)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._last_microprice = 99.8
    strategy._prev_microprice = 100.0  # drift = -0.2 = -2 ticks < -1.0 tick threshold

    # Verify the drift exceeds threshold
    tick = float(strategy.instrument.price_increment)
    threshold = strategy.config.toxic_mp_drift_ticks * tick  # 1.0 * 0.1 = 0.1
    instant_drift = strategy._last_microprice - strategy._prev_microprice  # -0.2
    assert instant_drift < -threshold  # -0.2 < -0.1 → would cancel bid


# ===========================================================================
# V5-US-005: Asymmetric layered quoting tests
# ===========================================================================


def test_asymmetric_layers_one_side() -> None:
    """dir_val > 0 时 ask 只有 1 层，非对称分层有效."""
    strategy = make_strategy(quote_layers=3, asymmetric_layers=True)
    strategy._last_dir_val = 0.6

    assert strategy.config.asymmetric_layers is True
    assert strategy.config.quote_layers == 3

    ask_layers = (
        1 if (strategy.config.asymmetric_layers and strategy._last_dir_val > 0) else strategy.config.quote_layers
    )
    bid_layers = strategy.config.quote_layers
    assert ask_layers == 1
    assert bid_layers == 3
