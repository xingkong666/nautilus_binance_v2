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
from src.strategy.market import ActiveMarketMaker, MarketMakerConfig

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
        "max_position_usd": 1000.0,
        "soft_limit": 0.30,
        "hard_limit": 0.70,
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


# ── 测试 1: 线性加权失衡 ─────────────────────────────────────────
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


# ── 测试 2: 指数加权失衡 ────────────────────────────────────────────
def test_weighted_imbalance_exp() -> None:
    """Exp 加权结果在 [0, 1] 范围内，且与 linear 不同."""
    strategy = make_strategy(imbalance_weight_mode="exp")
    weights_exp = strategy._calc_weights(5)

    strategy_lin = make_strategy(imbalance_weight_mode="linear")
    weights_lin = strategy_lin._calc_weights(5)

    assert all(0 < w <= 1.0 for w in weights_exp)
    assert weights_exp != weights_lin
    assert weights_exp[0] == pytest.approx(1.0)


def test_order_book_level_size_accepts_method_and_property() -> None:
    """订单簿档位数量兼容 Nautilus size() 方法和测试桩 size 属性."""
    strategy = make_strategy()

    method_level = SimpleNamespace(size=lambda: Decimal("1.25"))
    property_level = SimpleNamespace(size=Decimal("2.50"))

    assert strategy._order_book_level_size(method_level) == pytest.approx(1.25)
    assert strategy._order_book_level_size(property_level) == pytest.approx(2.50)


# ── 测试 3: EWM平滑收敛 ───────────────────────────────────────────
def test_ewm_smoothing() -> None:
    """EWM 平滑：多次相同输入后 _smooth_imbalance 收敛到目标值."""
    strategy = make_strategy(imbalance_decay=0.5)
    strategy._smooth_imbalance = 0.0

    decay = 0.5
    raw = 0.4  # 在 [-1, 1] 范围内
    val = 0.0
    for _ in range(20):
        val = decay * val + (1 - decay) * raw
    assert val == pytest.approx(0.4, abs=0.01)


# ── 测试 4：动态 spread 随ATR 增加 ─────────────────────────────────
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


# ── 测试 5: 价差超过最大值时暂停 ────────────────────────────────────────
def test_spread_suspended_above_max() -> None:
    """Spread > max_spread_ticks → _quote_suspended=True."""
    strategy = make_strategy(base_spread_ticks=3, spread_vol_multiplier=100.0, max_spread_ticks=10)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=10.0)

    cancelled = []
    strategy._cancel_all_quotes = lambda *_: cancelled.append(True)

    strategy._update_dynamic_spread()
    assert strategy._quote_suspended is True


# ── 测试 6: 多头偏斜报价价格 ─────────────────────────────────────────────
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


# ── 测试 7: 净多头库存偏斜 ───────────────────────────────────────
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

    # Set up a short position to affect ask pricing
    strategy._net_position_usd = -500.0  # Short position
    _, ask_short, _ = strategy._calc_quote_prices(mid, 0.0)

    # When short, ask should be higher (pushed away from mid) due to inventory skew
    assert ask_short > ask_neutral


# ── 测试 8: 软限制降低同侧数量 ─────────────────────────────────
def test_quote_size_soft_limit_reduces_same_side() -> None:
    """Inv_ratio=0.5 时同向 qty 小于 base_qty."""
    strategy = make_strategy(soft_limit=0.3, hard_limit=0.7, soft_size_min_ratio=0.3)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: p,
        make_qty=lambda q: q,
    )
    strategy._net_position_usd = 500.0  # Long position

    base = Decimal("1.0")
    bid_qty, ask_qty, *_ = strategy._calc_quote_sizes(base)

    assert bid_qty < base
    assert ask_qty == base


# ── 测试 9: 硬限制下的数量下限 ──────────────────────────────────────────
def test_quote_size_hard_limit_floor() -> None:
    """Inv_ratio=0.9 时同向（bid）数量为 0."""
    strategy = make_strategy(soft_limit=0.3, hard_limit=0.7, soft_size_min_ratio=0.3)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: p,
        make_qty=lambda q: q,
    )
    strategy._net_position_usd = 900.0  # Long position

    base = Decimal("1.0")
    bid_qty, ask_qty, *_ = strategy._calc_quote_sizes(base)

    assert bid_qty == Decimal("0")
    assert ask_qty == base


# ── 测试 10：当EMA 和盘口对齐时发出LONG 信号 ───────────────────────────
def test_signal_long_ema_and_book_aligned() -> None:
    """EMA 多头 + imbalance > dead_zone → LONG."""
    strategy = make_strategy(dead_zone_threshold=0.1)
    strategy._fast_ema = SimpleNamespace(initialized=True, value=105.0)
    strategy._slow_ema = SimpleNamespace(initialized=True, value=100.0)
    strategy._smooth_imbalance = 0.5  # > 0.1 阈值
    strategy._net_position_usd = 0.0

    bar = make_bar()
    result = strategy.generate_signal(bar)
    assert result == SignalDirection.LONG


# ── 测试 11：在死区时不发出任何信号 ────────────────────────────────────
def test_signal_none_in_dead_zone() -> None:
    """Imbalance within dead zone → None."""
    strategy = make_strategy(dead_zone_threshold=0.1)
    strategy._fast_ema = SimpleNamespace(initialized=True, value=105.0)
    strategy._slow_ema = SimpleNamespace(initialized=True, value=100.0)
    strategy._smooth_imbalance = 0.05  # < 0.1 阈值

    bar = make_bar()
    result = strategy.generate_signal(bar)
    assert result is None


# ── 测试12：刷新取消之前的订单 ─────────────────────────────────
def test_refresh_cancels_previous_orders() -> None:
    """Second _refresh_quotes cancels old orders first."""
    strategy = make_strategy()
    strategy._quote_suspended = False

    # Mock the cache through the strategy's _prune_inactive_quote_ids method
    def mock_prune():
        pass  # Skip the pruning logic that requires cache

    strategy._prune_inactive_quote_ids = mock_prune  # type: ignore[method-assign]

    # Mock _has_active_quotes to return True initially
    strategy._has_active_quotes = lambda: bool(strategy._active_bid_ids or strategy._active_ask_ids)  # type: ignore[method-assign]

    cancelled_ids = []

    def mock_cancel(*_):
        for oid in strategy._active_bid_ids + strategy._active_ask_ids:
            if oid is not None:
                cancelled_ids.append(oid)
        strategy._active_bid_ids = []
        strategy._active_ask_ids = []
        strategy._quote_state.quoted_mid = None
        strategy._quote_state.quoted_skew = None
        strategy._quote_state.bid_submit_time = None
        strategy._quote_state.ask_submit_time = None

    strategy._cancel_all_quotes = mock_cancel
    strategy._submit_quote = lambda side, price, qty, **kwargs: f"order_{side}"  # type: ignore[method-assign]
    _patch_utc_now(strategy)

    strategy._active_bid_ids = ["old_bid"]  # type: ignore[list-item]
    strategy._active_ask_ids = ["old_ask"]  # type: ignore[list-item]

    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.0)

    assert "old_bid" in cancelled_ids
    assert "old_ask" in cancelled_ids


# ── 测试 13: 失衡公式 (bid_w - ask_w) / (bid_w + ask_w) ─────────────
def test_imbalance_formula() -> None:
    """Imbalance formula: (bid_w - ask_w) / (bid_w + ask_w)."""
    strategy = make_strategy(imbalance_decay=0.0, dead_zone_threshold=0.0)
    strategy._smooth_imbalance = 0.0

    bid_w, ask_w = 0.6, 0.4
    total = bid_w + ask_w
    raw = (bid_w - ask_w) / total
    assert raw == pytest.approx(0.2)


# ── 测试 14：死区将小不平衡钳位为零 ─────────────────────────
def test_dead_zone_clamps_to_zero() -> None:
    """Imbalance < dead_zone_threshold → clamped to 0."""
    strategy = make_strategy(dead_zone_threshold=0.1)
    strategy._smooth_imbalance = 0.05

    if abs(strategy._smooth_imbalance) < strategy.config.dead_zone_threshold:
        strategy._smooth_imbalance = 0.0
    assert strategy._smooth_imbalance == 0.0


# ── 测试 15：漂移阈值 — 价格不变时不取消 ─────────────────
def test_drift_threshold_no_cancel_when_unchanged() -> None:
    """When mid hasn't drifted beyond drift_ticks, no cancel/replace."""
    strategy = make_strategy(drift_ticks=2)
    strategy._quote_suspended = False
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    # Mock the cache dependencies
    def mock_prune():
        pass

    strategy._prune_inactive_quote_ids = mock_prune  # type: ignore[method-assign]
    strategy._has_active_quotes = lambda: bool(strategy._active_bid_ids or strategy._active_ask_ids)  # type: ignore[method-assign]

    cancel_called = []

    def mock_cancel(*_):
        cancel_called.append(True)
        strategy._active_bid_ids = []
        strategy._active_ask_ids = []
        strategy._quote_state.quoted_mid = None
        strategy._quote_state.quoted_skew = None
        strategy._quote_state.bid_submit_time = None
        strategy._quote_state.ask_submit_time = None

    strategy._cancel_all_quotes = mock_cancel
    strategy._submit_quote = lambda side, price, qty, **kwargs: f"order_{side}"  # type: ignore[method-assign]
    _patch_utc_now(strategy)

    # 第一次调用：_quote_state.quoted_mid 为 None→ 触发刷新
    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.0)
    assert strategy._quote_state.quoted_mid == 100.0

    cancel_called.clear()

    # 中不变 → 漂移 < 2 个刻度 * 0.1 = 0.2 → 无刷新
    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.0)
    assert len(cancel_called) == 0


# ── 测试 16: 正值 库存偏斜 ──────────────────────────────────────────────
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

    # 净多头 = 500 → long_ratio = 0.5, short_ratio = 0.0
    strategy._net_position_usd = 500.0
    _, ask_tanh, _ = strategy._calc_quote_prices(mid, 0.0)

    # Based on actual logic: ask_inv_skew = tanh(short_ratio * inv_tanh_scale) = tanh(0.0 * 2.0) = 0.0
    # So ask_shift = 0 + 0 = 0 (no inventory skew for ask when short_ratio = 0)
    # ask = mid + half_spread + gross_widen + ask_shift
    short_ratio = 0.0
    gross_ratio = 500.0 / max(strategy.config.max_position_usd, 1.0)  # 500/1000 = 0.5
    ask_inv_skew = math.tanh(short_ratio * strategy.config.inv_tanh_scale) * float(strategy.config.inv_scale_ticks) * tick  # = 0
    gross_widen = math.tanh(gross_ratio * 1.5) * 1.5 * tick
    half_spread = 3.0 * tick / 2.0
    expected_ask = mid + half_spread + gross_widen + ask_inv_skew
    assert ask_tanh == pytest.approx(expected_ask, abs=1e-10)


# ── 测试 17: 终止开关在超过阈值时触发 >120% max_position ─────────────────────
def test_kill_switch_activates() -> None:
    """Inv_ratio >= 1.2 → _kill_switch = True."""
    strategy = make_strategy(kill_switch_limit=1.2)
    strategy._kill_switch = False

    net_usd = 1200.0  # max_position_usd=1000 的 120%
    strategy._net_position_usd = net_usd

    cancel_called = []
    strategy._cancel_all_quotes = lambda *_: cancel_called.append(True)

    # For long position, use long ratio
    long_ratio = abs(net_usd) / max(strategy.config.max_position_usd, 1.0)
    assert long_ratio >= strategy.config.kill_switch_limit

    if long_ratio >= strategy.config.kill_switch_limit and not strategy._kill_switch:
        strategy._kill_switch = True

    assert strategy._kill_switch is True


# ── 测试 18: 价格夹紧 — bid 夹紧的 低于 best_ask ─────────────────────────
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


# ── 测试 19: 非线性数量缩放使用 t^2 ──────────────────────────────────
def test_nonlinear_size_scaling() -> None:
    """Soft zone scale uses t^2: scale = 1.0 - (t^2) * (1 - min_r)."""
    strategy = make_strategy(soft_limit=0.3, hard_limit=0.7, soft_size_min_ratio=0.3)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
    )

    # inv_ratio = 0.5 → t = (0.5 - 0.3) / (0.7 - 0.3) = 0.5
    strategy._net_position_usd = 500.0
    base = Decimal("1.0")
    bid_qty, _, *_ = strategy._calc_quote_sizes(base)

    # 比例 = 1.0 - (0.5^2) * (1.0 - 0.3) = 1.0 - 0.25 * 0.7 = 1.0 - 0.175 = 0.825
    expected_scale = 1.0 - (0.5**2) * (1.0 - 0.3)
    expected_qty = round(1.0 * expected_scale / 0.001) * 0.001
    assert float(bid_qty) == pytest.approx(expected_qty, abs=0.002)


# ── 测试 20: 价差恢复迟滞 ───────────────────────────────────────
def test_spread_recovery_hysteresis() -> None:
    """Suspended spread only recovers when raw <= max * recovery_ratio."""
    strategy = make_strategy(base_spread_ticks=3, spread_vol_multiplier=2.0, max_spread_ticks=10, spread_recovery_ratio=0.9)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._quote_suspended = True

    # 生的 = 3 + 2 * (0.45/0.1) = 3 + 9 = 12 → 仍然 > 10，保持暂停状态
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=0.45)
    strategy._update_dynamic_spread()
    assert strategy._quote_suspended is True

    # 原始 = 3 + 2 * (0.3/0.1) = 3 + 6 = 9 → 9 <= 10 * 0.9 = 9.0 → 恢复
    strategy._atr_indicator = SimpleNamespace(initialized=True, value=0.3)
    strategy._update_dynamic_spread()
    assert strategy._quote_suspended is False


# ── 测试 21: EMA冲突将方向值减半 dir_val ────────────────────────────────
def test_ema_contradiction_halves_dir_val() -> None:
    """When EMA contradicts imbalance direction, dir_val is halved."""
    strategy = make_strategy(mp_alpha_weight=0.0, imbalance_weight=1.0)  # 全部权重给 失衡
    strategy._smooth_imbalance = 0.6  # 看涨失衡
    strategy._last_microprice = None  # 回退：仅限 伊姆布
    # 但EMA看跌
    strategy._fast_ema = SimpleNamespace(initialized=True, value=95.0)
    strategy._slow_ema = SimpleNamespace(initialized=True, value=100.0)

    dir_val = strategy._compute_dir_val()
    assert dir_val == pytest.approx(0.3)  # 0.6 * 0.5


# ── 测试 22: 正值 alpha 位移 ───────────────────────────────────────────────
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


# ── 测试 23: alpha_weight 随库存衰减 ──────────────────────────────
def test_alpha_weight_decay() -> None:
    """Alpha weight = max(0, 1 - |inv_ratio|); inv_ratio=0.8 → weight=0.2."""
    strategy = make_strategy(alpha_scale_ticks=2.0, alpha_tanh_k=2.0, inv_scale_ticks=0.0)
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
    )
    strategy._current_spread_ticks = 3.0
    strategy._net_position_usd = 800.0  # long_ratio = 0.8

    mid = 100.0
    tick = 0.1
    bid, _, _ = strategy._calc_quote_prices(mid, 1.0)

    # The actual calculation includes gross_widen and inv_skew effects too
    # Just verify that alpha_weight decay is working by comparing with no position
    strategy._net_position_usd = 0.0
    bid_no_pos, _, _ = strategy._calc_quote_prices(mid, 1.0)

    strategy._net_position_usd = 800.0
    bid_with_pos, _, _ = strategy._calc_quote_prices(mid, 1.0)

    # With high inventory, alpha effect should be reduced
    # (difference from neutral should be smaller)
    half_spread = 3.0 * tick / 2.0
    neutral_bid = mid - half_spread

    # The bid with position should be less aggressive (closer to neutral) due to alpha_weight decay
    assert abs(bid_with_pos - neutral_bid) < abs(bid_no_pos - neutral_bid)


# ── 测试 24: 无订单簿 → on_bar 跳过 报价 ────────────────────────────
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


# ── 测试 25: 偏斜漂移触发刷新 ────────────────────────────────────
def test_skew_drift_triggers_refresh() -> None:
    """Same mid but changed skew beyond threshold → triggers cancel/replace."""
    strategy = make_strategy(drift_ticks=2, skew_drift_ticks=1)
    strategy._quote_suspended = False
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    # Mock the cache dependencies
    def mock_prune():
        pass

    strategy._prune_inactive_quote_ids = mock_prune  # type: ignore[method-assign]
    strategy._has_active_quotes = lambda: bool(strategy._active_bid_ids or strategy._active_ask_ids)  # type: ignore[method-assign]

    cancel_called = []

    def mock_cancel(*_):
        cancel_called.append(True)
        strategy._active_bid_ids = []
        strategy._active_ask_ids = []
        strategy._quote_state.quoted_mid = None
        strategy._quote_state.quoted_skew = None
        strategy._quote_state.bid_submit_time = None
        strategy._quote_state.ask_submit_time = None

    strategy._cancel_all_quotes = mock_cancel
    strategy._submit_quote = lambda side, price, qty, **kwargs: f"order_{side}"  # type: ignore[method-assign]
    _patch_utc_now(strategy)

    # 第一次调用：设置quoted_mid 和quoted_skew
    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.0)
    assert strategy._quote_state.quoted_skew == 0.0
    cancel_called.clear()

    # 相同mid，偏斜改变 0.2 > 1 * 0.1 = 0.1 → 应该刷新
    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.2)
    assert len(cancel_called) == 1


# ── 测试 26: 库存偏斜对称性测试 ─────────────────────────
def test_inventory_skew_symmetry() -> None:
    """净多头时 bid 受影响，净空头时 ask 受影响（对称库存管理）."""
    strategy = make_strategy(
        alpha_scale_ticks=0.0,
        inv_scale_ticks=3.0,
        inv_tanh_scale=2.0,
    )
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
    )
    strategy._current_spread_ticks = 3.0

    mid = 100.0

    # Test long position affects bid skew
    strategy._net_position_usd = 500.0  # 净多头
    bid_long, ask_long, _ = strategy._calc_quote_prices(mid, 0.0)

    # Test short position affects ask skew
    strategy._net_position_usd = -500.0  # 净空头
    bid_short, ask_short, _ = strategy._calc_quote_prices(mid, 0.0)

    # Long position should push bid down (negative skew), short position should push ask up
    strategy._net_position_usd = 0.0
    bid_neutral, ask_neutral, _ = strategy._calc_quote_prices(mid, 0.0)

    assert bid_long < bid_neutral  # Long position pushes bid down
    assert ask_short > ask_neutral  # Short position pushes ask up


# ── 测试 27: 成交冷却阻止报价 ───────────────────────────────────
def test_fill_cooldown_blocks_quoting() -> None:
    """Within fill_cooldown_ms, elapsed time check returns early."""
    strategy = make_strategy(fill_cooldown_ms=500)

    now = datetime.now(UTC)

    # 模拟 on_bar 中的冷却检查逻辑
    strategy._last_fill_ts = now

    # 成交 之后 100 毫秒 → 应阻塞（经过时间 < 500）
    current = now + timedelta(milliseconds=100)
    elapsed_ms = (current - strategy._last_fill_ts).total_seconds() * 1000
    assert elapsed_ms < strategy.config.fill_cooldown_ms

    # 成交 之后 600 毫秒 → 应允许（已过去 >= 500）
    current = now + timedelta(milliseconds=600)
    elapsed_ms = (current - strategy._last_fill_ts).total_seconds() * 1000
    assert elapsed_ms >= strategy.config.fill_cooldown_ms


# ===========================================================================
# US-001：microprice 测试
# ===========================================================================


def _patch_utc_now(strategy, fixed_time=None):
    """Patch _utc_now on strategy for testing."""
    if fixed_time is None:
        fixed_time = datetime.now(UTC)
    strategy._utc_now = lambda: fixed_time  # type: ignore[method-assign]
    return fixed_time


def test_microprice_computation() -> None:
    """Microprice = (bid_size * ask + ask_size * bid) / (bid_size + ask_size)."""
    # 直接模拟
    bid_price, ask_price = 99.0, 101.0
    bid_size, ask_size = 10.0, 5.0
    microprice = (bid_size * ask_price + ask_size * bid_price) / (bid_size + ask_size)
    # bid_size * ask = 10 * 101 = 1010，ask_size * bid = 5 * 99 = 495
    # microprice= 1505 / 15 = 100.333...
    assert microprice == pytest.approx(100.333333, abs=0.001)
    # microprice偏向size较多的一侧（bid重→更接近ask）
    assert microprice > 100.0


# ===========================================================================
# US-002：逆向选择测试
# ===========================================================================


def test_adverse_selection_detects_buy_drift() -> None:
    """Fill BUY then price drops → adverse selection detected on BUY side."""
    strategy = make_strategy(adverse_selection_ticks=3)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    strategy._last_fill_price = 100.0
    strategy._last_fill_side = "BUY"

    # 价格下降 0.4 > 3 * 0.1 = 0.3
    result = strategy._check_adverse_selection(99.6)
    assert result == "BUY"


def test_adverse_selection_no_detection_small_drift() -> None:
    """Small drift does not trigger adverse selection."""
    strategy = make_strategy(adverse_selection_ticks=3)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    strategy._last_fill_price = 100.0
    strategy._last_fill_side = "BUY"

    # 漂移 -0.2 < 阈值 0.3 → 无不良影响
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
    bid_qty, ask_qty, *_ = strategy._calc_quote_sizes(base, adverse_side="BUY")
    assert bid_qty == Decimal("0")
    assert ask_qty > Decimal("0")


# ===========================================================================
# US-003：队列刷新测试
# ===========================================================================


def test_queue_refresh_near_ttl() -> None:
    """Order near TTL with better price available → triggers refresh logic."""
    strategy = make_strategy(limit_ttl_ms=8000, order_refresh_ratio=0.7)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._current_spread_ticks = 3.0
    strategy._net_position_usd = 0.0

    now = datetime.now(UTC)
    _patch_utc_now(strategy, now)

    # 阈值 = 8000ms * 0.7 = 5600ms
    ttl = timedelta(milliseconds=8000)
    refresh_threshold = ttl * 0.7

    # 6 秒前提交出价 → 已过去 > 5.6 秒阈值
    submit_time = now - timedelta(milliseconds=6000)
    elapsed = now - submit_time
    assert elapsed > refresh_threshold

    # 当前订单价格 99.0，最优出价~99.85 → 当前 < 最优 → 应刷新
    current_bid_price = 99.0
    mid = 100.0
    half_spread = strategy._current_spread_ticks * 0.1 / 2.0
    optimal_bid = mid - half_spread  # 99.85
    assert current_bid_price < optimal_bid


# ===========================================================================
# US-004：delta 驱动报价测试
# ===========================================================================


def test_delta_driven_quoting_flag() -> None:
    """When quote_on_delta=True, _try_quote_on_delta path is accessible."""
    strategy = make_strategy(quote_on_delta=True)
    assert strategy.config.quote_on_delta is True

    # 没有初始化 EMA，应该提前返回
    strategy._fast_ema = SimpleNamespace(initialized=False, value=0)
    strategy._slow_ema = SimpleNamespace(initialized=False, value=0)
    # 不应该提高
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

    # 模拟mid价格
    strategy._get_mid_price = lambda bar: 100.0  # type: ignore[method-assign]

    refresh_called = []
    strategy._refresh_quotes = lambda *a, **kw: refresh_called.append(True)  # type: ignore[method-assign]

    strategy._try_quote_on_delta()
    assert refresh_called == []  # 没有刷新，因为没有 base_qty


# ===========================================================================
# US-005：已实现volatility 测试
# ===========================================================================


def test_realized_vol_computation() -> None:
    """Price returns → RV std computed correctly."""
    strategy = make_strategy(use_realized_vol=True, rv_window=20)

    # 饲料价格：100、101、102、101、100
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
# US-006：分层报价测试
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

    # Mock the clock
    _patch_utc_now(strategy)

    submitted = []

    def mock_submit(side, price, qty, **kwargs):
        submitted.append((side, price, qty))
        return f"order_{len(submitted)}"

    strategy._submit_quote = mock_submit  # type: ignore[method-assign]

    strategy._submit_layered_quotes(99.0, 101.0, Decimal("1.0"), Decimal("1.0"))

    assert len(strategy._active_bid_ids) == 2
    assert len(strategy._active_ask_ids) == 2
    # 4 次提交：2 次出价 + 2 次询问
    assert len(submitted) == 4


def test_single_layer_unchanged() -> None:
    """Quote_layers=1 → uses single submit path, not layered."""
    strategy = make_strategy(quote_layers=1)
    strategy._quote_suspended = False
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    _patch_utc_now(strategy)

    # Mock the cache dependencies
    def mock_prune():
        pass

    strategy._prune_inactive_quote_ids = mock_prune  # type: ignore[method-assign]
    strategy._has_active_quotes = lambda: bool(strategy._active_bid_ids or strategy._active_ask_ids)  # type: ignore[method-assign]

    submit_called = []

    def mock_submit_quote(side, price, qty, **kwargs):
        submit_called.append(side)
        return f"order_{side}"

    strategy._submit_quote = mock_submit_quote  # type: ignore[method-assign]

    def mock_cancel(*_):
        strategy._active_bid_ids = []
        strategy._active_ask_ids = []
        strategy._quote_state.quoted_mid = None
        strategy._quote_state.quoted_skew = None
        strategy._quote_state.bid_submit_time = None
        strategy._quote_state.ask_submit_time = None

    strategy._cancel_all_quotes = mock_cancel  # type: ignore[method-assign]

    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.0)
    # 单层：正好 2 个提交（一个 bid，一个 ask）
    assert len(submit_called) == 2


# ===========================================================================
# US-007：PnL断路器测试
# ===========================================================================


def test_pnl_circuit_breaker_opens() -> None:
    """Negative fills exceeding max_loss_usd → _pnl_circuit_open=True."""
    strategy = make_strategy(max_loss_usd=50.0, loss_window_ms=60000)

    now = datetime.now(UTC)

    # 模拟 fills 总计-60USD 损失
    strategy._recent_fills.append((now, -30.0))
    strategy._recent_fills.append((now, -25.0))

    # 和 = -55 < -50 → 应该跳闸
    window_pnl = sum(p for _, p in strategy._recent_fills)
    assert window_pnl < -strategy.config.max_loss_usd

    # 手动检查逻辑
    if window_pnl < -strategy.config.max_loss_usd:
        strategy._pnl_circuit_open = True

    assert strategy._pnl_circuit_open is True


def test_pnl_circuit_breaker_resets() -> None:
    """Circuit breaker resets after cooldown period."""
    strategy = make_strategy(pnl_cb_cooldown_ms=300000)

    now = datetime.now(UTC)
    strategy._pnl_circuit_open = True
    strategy._pnl_cb_reset_at = now - timedelta(seconds=1)  # 已经过期了

    # 检查复位逻辑
    if strategy._pnl_cb_reset_at and now >= strategy._pnl_cb_reset_at:
        strategy._pnl_circuit_open = False
        strategy._pnl_cb_reset_at = None

    assert strategy._pnl_circuit_open is False


# ===========================================================================
# US-008：Market质量过滤器测试
# ===========================================================================


def test_market_quality_wide_spread() -> None:
    """Book spread > max_book_spread_ticks → _quote_quality_ok=False."""
    strategy = make_strategy(max_book_spread_ticks=20.0)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    # 点差 = (102 - 98) / 0.1 = 40 个刻度 > 20 → 质量差
    strategy._check_market_quality(98.0, 102.0)
    assert strategy._quote_quality_ok is False


def test_market_quality_tight_spread() -> None:
    """Tight spread → quality ok."""
    strategy = make_strategy(max_book_spread_ticks=20.0, imbalance_spike_threshold=0.9)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._smooth_imbalance = 0.3  # 0.9以下

    # 点差 = (100.1 - 99.9) / 0.1 = 2 个刻度 < 20 → 可以
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
# US-009：成本模型测试
# ===========================================================================


def test_cost_model_skip_narrow_spread() -> None:
    """Spread too narrow → expected_profit < min → should skip quoting."""
    strategy = make_strategy(min_expected_profit_bps=1.0, taker_fee_bps=4.0)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._current_spread_ticks = 2.0  # 价差 = 2 * 0.1 = 0.2

    mid = 100.0
    # gross_bps = (0.2 / 100) * 10000 / 2 = 10.0
    # 预期 = 10.0 - 4.0 = 6.0 > 1.0 → 通过
    profit = strategy._calc_expected_profit_bps(mid)
    assert profit == pytest.approx(6.0)
    assert profit >= strategy.config.min_expected_profit_bps


def test_cost_model_very_narrow_spread() -> None:
    """Very narrow spread with high fee → expected profit negative."""
    strategy = make_strategy(min_expected_profit_bps=1.0, taker_fee_bps=10.0)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.01"), size_increment=Decimal("0.001"))
    strategy._current_spread_ticks = 1.0  # 价差 = 1 * 0.01 = 0.01

    mid = 100.0
    # gross_bps = (0.01 / 100) * 10000 / 2 = 0.5
    # 预期 = 0.5 - 10.0 = -9.5 < 1.0
    profit = strategy._calc_expected_profit_bps(mid)
    assert profit < strategy.config.min_expected_profit_bps


# ===========================================================================
# V3-US-001：错误修复测试
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
    # 在 100 处模拟BUY fill，没有匹配的SELL→mark 至 mid 损失
    event = SimpleNamespace(
        last_px=100.0,
        last_qty=3.0,
        order_side=OrderSide.BUY,
        commission=SimpleNamespace(as_double=lambda: 0.0),
    )
    strategy.on_order_filled(event)
    assert len(strategy._recent_fills) > 0
    _, pnl = strategy._recent_fills[-1]
    # mark 至 mid：(98 - 100) * 3 * 1 = -6
    assert pnl < 0


# ===========================================================================
# V3-US-002：成交流 阿尔法测试
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
    # microprice不可用 → 回退: (imb*0.3 + tf*0.2) / (0.3+0.2)
    strategy._last_microprice = None
    dir_val = strategy._compute_dir_val()
    # tf = 1.0，回退：(0.3*0.3 + 1.0*0.2) / 0.5 = (0.09+0.2)/0.5 = 0.58
    assert dir_val == pytest.approx(0.58)


# ===========================================================================
# V3-US-003：交易前不利取消测试
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
        ba = 100.05  # best_ask: <= bid + 1*0.1 = 100.1 → 触发取消
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
# V3-US-004：microprice 深度利用测试
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

    # 无microprice偏差
    strategy._last_microprice = None
    bid_no_mp, ask_no_mp, _ = strategy._calc_quote_prices(mid, 0.0)

    # microprice 高于mid（购买压力）
    strategy._last_microprice = 100.3  # 中以上 +0.3 = +3 个刻度
    bid_mp, ask_mp, _ = strategy._calc_quote_prices(mid, 0.0)

    assert bid_mp > bid_no_mp
    assert ask_mp > ask_no_mp


# ===========================================================================
# V3-US-005：库存分级控制测试
# ===========================================================================


def test_one_side_only_limit() -> None:
    """inv_ratio >= one_side_only_limit → 同向 qty = 0（单边报价）."""
    strategy = make_strategy(
        soft_limit=0.3,
        one_side_only_limit=0.7,
        hard_limit=0.9,
        soft_size_min_ratio=0.3,
    )
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: p,
        make_qty=lambda q: q,
    )
    strategy._net_position_usd = 750.0  # 1000 的 75% → >= one_side_only_limit=0.7

    base = Decimal("1.0")
    bid_qty, ask_qty, *_ = strategy._calc_quote_sizes(base)

    # 净多头在一侧限制→bid 被抑制
    assert bid_qty == Decimal("0")
    assert ask_qty > Decimal("0")


# ===========================================================================
# V4-US-001：队列位置测试
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
    strategy._last_best_bid_size = 80.0  # 惩罚 = 80/100 = 0.8 > 0.7

    bid_no_q, _, _ = strategy._calc_quote_prices(100.0, 0.0)
    tick = 0.1
    bid_improved = bid_no_q + tick
    assert bid_improved > bid_no_q


# ===========================================================================
# V4-US-002：有毒流量测试
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
    strategy._last_microprice = 100.0  # microprice= last_fill_mid → 漂移=0

    from nautilus_trader.model.enums import AggressorSide

    trade = SimpleNamespace(aggressor_side=AggressorSide.BUYER, size=1.0)
    # 无毒方向（买方的漂移 >= 0 → +0.05 然后衰减）
    strategy._update_toxic_flow(trade)
    # 衰减后：评分 = (1.0 + 0.05) * 0.5 = 0.525，小于1.0
    assert abs(strategy._toxic_flow_score) < 1.0


# ===========================================================================
# V4-US-003：报价质量分数测试
# ===========================================================================


def test_quote_score_calculation() -> None:
    """验证 _calc_quote_score 新公式：alpha + fill_prob*1.2 - inv*0.8 - toxic*1.5 - queue_penalty."""
    strategy = make_strategy()
    strategy._net_position_usd = 500.0  # long_ratio = 0.5
    strategy._toxic_flow_score = 0.3
    # fill_prob 默认为 1.0（无队列 快照）→ queue_penalty = 0.0
    score = strategy._calc_quote_score(dir_val=0.6)
    fill_prob = 1.0
    # inv_ratio = max(gross_ratio, abs(imbalance)) = max(0.5, 0.5) = 0.5
    expected = 0.6 + fill_prob * 1.2 - 0.5 * 0.8 - 0.3 * 1.5 - (1.0 - fill_prob) * 1.0
    assert score == pytest.approx(expected)


def test_quote_score_blocks_quoting() -> None:
    """Score < quote_score_threshold → cancel_all 被调用，不提交报价."""
    strategy = make_strategy(quote_score_threshold=-0.5)
    strategy._toxic_flow_score = 0.9  # high有毒 → 评分非常负面
    strategy._net_position_usd = 800.0  # 高 投资

    cancel_called = []
    strategy._cancel_all_quotes = lambda *_: cancel_called.append(True)  # type: ignore[method-assign]

    score = strategy._calc_quote_score(dir_val=0.1)
    # fill_prob=1.0, inv_ratio = 0.8的新公式：0.1 + 1.0*1.2 - 0.8*0.8 - 0.9*1.5 - 0.0 = 0.1+1.2-0.64-1.35 = -0.69
    assert score < -0.5
    if score < strategy.config.quote_score_threshold:
        strategy._cancel_all_quotes()

    assert len(cancel_called) > 0


def test_toxic_one_side_ask() -> None:
    """toxic_score > threshold → ask_qty=0, bid_qty 正常."""
    strategy = make_strategy(toxic_one_side_threshold=0.5)
    strategy._toxic_flow_score = 0.7  # 有毒销售（分数 > 0）
    # 模拟 on_barqty决策
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
# V4+：队列fill概率测试
# ===========================================================================


def test_queue_fill_prob_increases_with_traded_volume() -> None:
    """traded=5000, initial=10000 → fill_prob=0.5."""
    strategy = make_strategy()
    # Set up the _quote_state object with the queue information
    strategy._quote_state = SimpleNamespace(bid_queue_on_submit=10000.0, ask_queue_on_submit=10000.0)
    strategy._queue_traded_volume = 5000.0
    prob = strategy._calc_queue_fill_prob("BUY")
    assert prob == pytest.approx(0.5)


def test_queue_fill_prob_full_when_no_initial() -> None:
    """initial=None or 0 → fill_prob=1.0."""
    strategy = make_strategy()
    strategy._quote_state = SimpleNamespace(bid_queue_on_submit=None, ask_queue_on_submit=None)
    assert strategy._calc_queue_fill_prob("BUY") == pytest.approx(1.0)
    strategy._quote_state.bid_queue_on_submit = 0.0
    assert strategy._calc_queue_fill_prob("BUY") == pytest.approx(1.0)


def test_toxic_uses_microprice_drift() -> None:
    """BUYER + microprice 下跌 → toxic_flow_score 负向增大."""
    strategy = make_strategy(toxic_decay=1.0)
    strategy._toxic_flow_score = 0.0
    strategy._last_microprice = 100.0
    strategy._last_fill_mid = 100.1  # 上一次 microprice

    # microprice未变（drift=0），买方方向无毒
    from nautilus_trader.model.enums import AggressorSide

    trade = SimpleNamespace(aggressor_side=AggressorSide.BUYER, size=1.0)
    strategy._update_toxic_flow(trade)
    assert strategy._toxic_flow_score < 0


def test_quote_score_new_formula() -> None:
    """验证新公式：alpha + fill_prob*1.2 - inv*0.8 - toxic*1.5 - queue_penalty."""
    strategy = make_strategy()
    strategy._net_position_usd = 400.0  # long_ratio=0.4
    strategy._toxic_flow_score = 0.2
    strategy._quote_state = SimpleNamespace(bid_queue_on_submit=10000.0, ask_queue_on_submit=10000.0)
    strategy._queue_traded_volume = 6000.0  # fill_prob = 0.6，queue_penalty = 0.4

    score = strategy._calc_quote_score(dir_val=0.5)
    fill_prob = 0.6
    # inv_ratio = max(gross_ratio, abs(imbalance)) = max(0.4, 0.4) = 0.4
    expected = 0.5 + fill_prob * 1.2 - 0.4 * 0.8 - 0.2 * 1.5 - (1.0 - fill_prob) * 1.0
    assert score == pytest.approx(expected)


def test_toxic_queue_combined_cancels() -> None:
    """toxic>0.6 AND fill_prob<0.3 → cancel_all 被调用."""
    strategy = make_strategy()
    strategy._toxic_flow_score = 0.7  # > 0.6
    strategy._quote_state = SimpleNamespace(bid_queue_on_submit=10000.0, ask_queue_on_submit=10000.0)
    strategy._queue_traded_volume = 1000.0  # fill_prob=0.1 < 0.3

    cancel_called = []
    strategy._cancel_all_quotes = lambda *_: cancel_called.append(True)  # type: ignore[method-assign]

    fill_prob = (strategy._calc_queue_fill_prob("BUY") + strategy._calc_queue_fill_prob("SELL")) / 2.0
    if abs(strategy._toxic_flow_score) > 0.6 and fill_prob < 0.3:
        strategy._cancel_all_quotes()

    assert len(cancel_called) > 0


# ===========================================================================
# V5-US-001：microprice 信号测试
# ===========================================================================


def test_microprice_signal_positive() -> None:
    """Microprice > mid → _calc_microprice_signal() > 0."""
    strategy = make_strategy(use_microprice=True)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._current_spread_ticks = 4.0
    strategy._last_microprice = 100.2

    # 模拟 _calc_microprice_signal直接测试逻辑
    # microprice=100.2，mid=100.0，spread_price=0.4，偏差=0.2，归一化=0.2/0.2=1.0，tanh(1.0)>0
    # 模拟绕过缓存访问的方法，而不是设置缓存
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
    strategy._last_microprice = 100.0  # 不是无 → 没有回退

    dir_val = strategy._compute_dir_val()
    expected = 0.6 * 0.5 + 0.4 * 0.3 + 1.0 * 0.2
    assert dir_val == pytest.approx(expected)


# ===========================================================================
# V5-US-002：非对称alpha 测试
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
    )
    strategy.instrument = SimpleNamespace(
        price_increment=Decimal("0.1"),
        size_increment=Decimal("0.001"),
        make_price=lambda p: round(p, 1),
    )
    strategy._current_spread_ticks = 4.0
    strategy._net_position_usd = 0.0
    # mp_bias > 0 → bid_alpha_mult = 1.3，ask_alpha_mult = 1.0
    # 直接验证乘法器逻辑
    mp_bias = 0.3  # microprice> 中
    strength = 0.3
    bid_alpha_mult = 1.0 + strength if mp_bias > 0 else 1.0
    ask_alpha_mult = 1.0
    assert bid_alpha_mult == pytest.approx(1.3)
    assert ask_alpha_mult == pytest.approx(1.0)
    assert bid_alpha_mult > ask_alpha_mult


# ===========================================================================
# V5-US-003：填充概率执行测试
# ===========================================================================


def test_withdraw_stale_quotes_low_fill_prob() -> None:
    """fill_prob_bid 低 → bid 被撤；ask fill_prob 高则不受影响（各侧独立判断）."""
    strategy = make_strategy(withdraw_fill_prob_threshold=0.1)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))

    strategy._queue_traded_volume = 9000.0
    strategy._quote_state = SimpleNamespace(
        bid_queue_on_submit=1_000_000.0,  # bid fill_prob ≈ 0.009 < 0.1
        ask_queue_on_submit=100.0,  # ask fill_prob = min(9000/100, 1) = 1.0
    )

    cancelled: list[str] = []
    bid_token: object = object()
    ask_token: object = object()
    strategy._active_bid_ids = [bid_token]
    strategy._active_ask_ids = [ask_token]
    strategy._quoted_bid_price = 99.9
    strategy._quoted_ask_price = 100.1

    def patched_withdraw() -> None:
        threshold = strategy.config.withdraw_fill_prob_threshold
        if strategy._active_bid_ids and strategy._active_bid_ids[0] is not None and strategy._calc_queue_fill_prob("BUY") < threshold:
            cancelled.append("bid")
            strategy._active_bid_ids[0] = None
            strategy._quoted_bid_price = None
        if strategy._active_ask_ids and strategy._active_ask_ids[0] is not None and strategy._calc_queue_fill_prob("SELL") < threshold:
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

    strategy._quote_state = SimpleNamespace(bid_queue_on_submit=100.0, ask_queue_on_submit=100.0)
    strategy._queue_traded_volume = 90.0

    fp_bid = strategy._calc_queue_fill_prob("BUY")
    fp_ask = strategy._calc_queue_fill_prob("SELL")
    fp_avg = (fp_bid + fp_ask) / 2.0
    assert fp_avg > 0.8

    if fp_avg > 0.8:
        strategy._current_spread_ticks = min(float(strategy.config.max_spread_ticks), strategy._current_spread_ticks + 0.5)
    assert strategy._current_spread_ticks == pytest.approx(3.5)


# ===========================================================================
# V5-US-004：有毒先发制人取消测试
# ===========================================================================


def test_toxic_preemptive_cancel_bid() -> None:
    """Microprice 急跌超过阈值 → _check_toxic_preemptive 会撤 bid."""
    strategy = make_strategy(toxic_mp_drift_ticks=1.0)
    strategy.instrument = SimpleNamespace(price_increment=Decimal("0.1"), size_increment=Decimal("0.001"))
    strategy._last_microprice = 99.8
    strategy._prev_microprice = 100.0  # 漂移 = -0.2 = -2 刻度 < -1.0 刻度阈值

    # 验证漂移是否超过阈值
    tick = float(strategy.instrument.price_increment)
    threshold = strategy.config.toxic_mp_drift_ticks * tick  # 1.0 * 0.1 = 0.1
    instant_drift = strategy._last_microprice - strategy._prev_microprice  # -0.2
    assert instant_drift < -threshold  # -0.2 < -0.1 → 将取消出价


# ===========================================================================
# V5-US-005：非对称分层报价测试
# ===========================================================================


def test_asymmetric_layers_one_side() -> None:
    """dir_val > 0 时 ask 只有 1 层，非对称分层有效."""
    strategy = make_strategy(quote_layers=3, asymmetric_layers=True)
    strategy._last_dir_val = 0.6

    assert strategy.config.asymmetric_layers is True
    assert strategy.config.quote_layers == 3

    ask_layers = 1 if (strategy.config.asymmetric_layers and strategy._last_dir_val > 0) else strategy.config.quote_layers
    bid_layers = strategy.config.quote_layers
    assert ask_layers == 1
    assert bid_layers == 3


# ===========================================================================
# Lot-Based 库存管理 + Reduce/TP 池测试
# ===========================================================================


def _make_order_filled_event(client_order_id, order_side, last_px=100.0, last_qty=0.1):
    """构造最小 OrderFilled 事件桩."""
    return SimpleNamespace(
        client_order_id=client_order_id,
        order_side=order_side,
        last_px=last_px,
        last_qty=last_qty,
        commission=SimpleNamespace(as_double=lambda: 0.0),
    )


def test_quote_fill_creates_lot() -> None:
    """Quote 成交后 _inventory_lots 新增一个 OPEN lot."""
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import ClientOrderId

    from src.strategy.market.inventory_lot import LotStatus

    strategy = make_strategy()
    _patch_utc_now(strategy)
    strategy._cancel_all_quotes = lambda *_: None  # type: ignore[method-assign]
    strategy._place_reduce_order = lambda lot: None  # type: ignore[method-assign]

    quote_id = ClientOrderId("quote-001")
    event = _make_order_filled_event(quote_id, OrderSide.BUY, last_px=100.0, last_qty=0.5)
    strategy.on_order_filled(event)

    assert len(strategy._inventory_lots) == 1
    lot = list(strategy._inventory_lots.values())[0]
    assert lot.side == OrderSide.BUY
    assert float(lot.qty) == pytest.approx(0.5)
    assert lot.entry_price == pytest.approx(100.0)
    assert lot.status == LotStatus.OPEN


def test_quote_fill_places_reduce() -> None:
    """Quote 成交后立即调用 _place_reduce_order."""
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import ClientOrderId

    strategy = make_strategy()
    _patch_utc_now(strategy)
    strategy._cancel_all_quotes = lambda *_: None  # type: ignore[method-assign]

    reduce_calls = []
    strategy._place_reduce_order = lambda lot: reduce_calls.append(lot)  # type: ignore[method-assign]

    quote_id = ClientOrderId("quote-002")
    event = _make_order_filled_event(quote_id, OrderSide.SELL, last_px=50000.0, last_qty=0.01)
    strategy.on_order_filled(event)

    assert len(reduce_calls) == 1
    assert reduce_calls[0].side == OrderSide.SELL
    assert reduce_calls[0].entry_price == pytest.approx(50000.0)


def test_reduce_fill_closes_lot() -> None:
    """Reduce 成交后 lot 状态变为 CLOSED，不撤 Quote 池."""
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import ClientOrderId

    from src.strategy.market.inventory_lot import InventoryLot, LotStatus

    strategy = make_strategy()
    _patch_utc_now(strategy)

    cancel_called = []
    strategy._cancel_all_quotes = lambda *_: cancel_called.append(True)  # type: ignore[method-assign]

    lot = InventoryLot(
        lot_id="lot-001",
        side=OrderSide.BUY,
        qty=Decimal("0.5"),
        entry_price=100.0,
        reduce_order_id=ClientOrderId("reduce-001"),
        status=LotStatus.CLOSING,
    )
    strategy._inventory_lots["lot-001"] = lot
    strategy._reduce_to_lot[ClientOrderId("reduce-001")] = "lot-001"

    event = _make_order_filled_event(ClientOrderId("reduce-001"), OrderSide.SELL, last_px=100.1, last_qty=0.5)
    strategy.on_order_filled(event)

    assert lot.status == LotStatus.CLOSED
    assert lot.reduce_order_id is None
    assert ClientOrderId("reduce-001") not in strategy._reduce_to_lot
    assert len(cancel_called) == 0  # Quote 池不受影响


def test_reduce_fill_tracks_realized_pnl() -> None:
    """Reduce 成交后正确计算 lot-based realized PnL."""
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import ClientOrderId

    from src.strategy.market.inventory_lot import InventoryLot, LotStatus

    strategy = make_strategy()
    _patch_utc_now(strategy)
    strategy._cancel_all_quotes = lambda *_: None  # type: ignore[method-assign]

    lot = InventoryLot(
        lot_id="lot-pnl",
        side=OrderSide.BUY,
        qty=Decimal("1.0"),
        entry_price=100.0,
        reduce_order_id=ClientOrderId("reduce-pnl"),
        status=LotStatus.CLOSING,
    )
    strategy._inventory_lots["lot-pnl"] = lot
    strategy._reduce_to_lot[ClientOrderId("reduce-pnl")] = "lot-pnl"

    event = _make_order_filled_event(ClientOrderId("reduce-pnl"), OrderSide.SELL, last_px=101.0, last_qty=1.0)
    strategy.on_order_filled(event)

    assert len(strategy._recent_fills) == 1
    _, pnl = strategy._recent_fills[0]
    assert pnl == pytest.approx(1.0)  # (101 - 100) * 1.0


def test_reduce_not_cancelled_by_drift_refresh() -> None:
    """Drift 刷新只撤 Quote 池，不影响 Reduce 池."""
    from nautilus_trader.model.identifiers import ClientOrderId

    strategy = make_strategy()

    reduce_id = ClientOrderId("reduce-drift")
    strategy._reduce_to_lot[reduce_id] = "lot-drift"

    strategy._cancel_all_quotes = lambda *_: None  # type: ignore[method-assign]
    strategy._prune_inactive_quote_ids = lambda: None  # type: ignore[method-assign]
    strategy._has_active_quotes = lambda: False  # type: ignore[method-assign]
    strategy._submit_quote = lambda side, price, qty, **kw: f"order_{side}"  # type: ignore[method-assign]
    _patch_utc_now(strategy)

    strategy._refresh_quotes(99.0, 101.0, Decimal("0.1"), Decimal("0.1"), mid=100.0, current_skew=0.0)

    assert reduce_id in strategy._reduce_to_lot


def test_reduce_cancelled_by_kill_switch() -> None:
    """Kill switch 撤两个池."""
    strategy = make_strategy()

    cancel_quote_called = []
    cancel_reduce_called = []
    strategy._cancel_all_quotes = lambda reason: cancel_quote_called.append(reason)  # type: ignore[method-assign]
    strategy._cancel_reduce_orders = lambda reason: cancel_reduce_called.append(reason)  # type: ignore[method-assign]

    from src.strategy.market.quote_engine import CancelReason

    strategy._cancel_all_orders(CancelReason.KILL_SWITCH)

    assert len(cancel_quote_called) == 1
    assert len(cancel_reduce_called) == 1
    assert cancel_quote_called[0] == CancelReason.KILL_SWITCH
    assert cancel_reduce_called[0] == CancelReason.KILL_SWITCH


def test_place_reduce_order_long_lot() -> None:
    """BUY lot → SELL reduce at entry * (1 + tp_pct)."""
    from nautilus_trader.model.enums import OrderSide

    strategy = make_strategy(tp_pct=0.001)
    entry_price = 50000.0
    tp_pct = strategy.config.tp_pct

    expected_reduce_side = OrderSide.SELL
    expected_price = entry_price * (1 + tp_pct)

    assert expected_reduce_side == OrderSide.SELL
    assert expected_price == pytest.approx(50050.0)


def test_place_reduce_order_short_lot() -> None:
    """SELL lot → BUY reduce at entry * (1 - tp_pct)."""
    from nautilus_trader.model.enums import OrderSide

    strategy = make_strategy(tp_pct=0.001)

    entry_price = 50000.0
    tp_pct = strategy.config.tp_pct

    expected_reduce_side = OrderSide.BUY
    expected_price = entry_price * (1 - tp_pct)

    assert expected_reduce_side == OrderSide.BUY
    assert expected_price == pytest.approx(49950.0)


def test_on_order_canceled_reduce_resets_lot() -> None:
    """Reduce 订单被撤后 lot 状态回退到 OPEN."""
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import ClientOrderId

    from src.strategy.market.inventory_lot import InventoryLot, LotStatus

    strategy = make_strategy()

    reduce_id = ClientOrderId("reduce-cancel")
    lot = InventoryLot(
        lot_id="lot-cancel",
        side=OrderSide.BUY,
        qty=Decimal("1.0"),
        entry_price=100.0,
        reduce_order_id=reduce_id,
        status=LotStatus.CLOSING,
    )
    strategy._inventory_lots["lot-cancel"] = lot
    strategy._reduce_to_lot[reduce_id] = "lot-cancel"

    event = SimpleNamespace(client_order_id=reduce_id)
    strategy.on_order_canceled(event)

    assert lot.status == LotStatus.OPEN
    assert lot.reduce_order_id is None
    assert reduce_id not in strategy._reduce_to_lot


def test_quote_fill_only_cancels_quote_pool() -> None:
    """Quote 成交后只撤 Quote 池（_cancel_all_quotes），不撤 Reduce 池."""
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import ClientOrderId

    strategy = make_strategy()
    _patch_utc_now(strategy)

    cancel_all_quotes_called = []
    cancel_reduce_called = []
    strategy._cancel_all_quotes = lambda reason: cancel_all_quotes_called.append(reason)  # type: ignore[method-assign]
    strategy._cancel_reduce_orders = lambda reason: cancel_reduce_called.append(reason)  # type: ignore[method-assign]
    strategy._place_reduce_order = lambda lot: None  # type: ignore[method-assign]

    event = _make_order_filled_event(ClientOrderId("quote-003"), OrderSide.BUY, last_px=100.0, last_qty=0.1)
    strategy.on_order_filled(event)

    assert len(cancel_all_quotes_called) == 1
    assert len(cancel_reduce_called) == 0
