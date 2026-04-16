"""Active Market Maker strategy.

主动做市商策略：
- 加权 L2 Imbalance 信号（前 N 档，linear/exp 加权，EWM 平滑）
- 完整报价模型：bid/ask = mid ± spread/2 ± skew
- skew = alpha_skew（imbalance方向）+ inventory_skew（净敞口）
- 动态 spread（基于 ATR/tick_size 波动率）
- 三层库存控制：正常 → 软限制（偏价+缩size）→ 硬限制（停单）
- Kill switch at >120% max_position
- 订单生命周期：drift-threshold triggered cancel + submit 新双边 limit 单

架构说明（刻意偏离）：
    做市商策略通过 submit_order() 直接下单，绕过 EventBus → OrderRouter →
    AlgoExecution → RateLimiter 链路。原因：做市商需要毫秒级双边报价刷新，
    通过信号链路会引入不可接受的延迟并破坏 cancel/replace 生命周期管理。
    风控前置检查（PreTradeRisk）由策略自身的库存硬限制替代。
    Rate limit 风险通过 refresh_every_bar 开关 + limit_ttl_ms 控制。
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal

import structlog
from nautilus_trader.common.enums import LogColor
from nautilus_trader.config import PositiveInt
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType, OrderBookDeltas
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderCanceled, OrderFilled, PositionChanged, PositionClosed, PositionOpened
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId

from src.core.events import EventBus, SignalDirection
from src.strategy.base import BaseStrategy, BaseStrategyConfig

logger = structlog.get_logger(__name__)


class MarketMakerConfig(BaseStrategyConfig, frozen=True):
    """ActiveMarketMaker 配置."""

    instrument_id: InstrumentId
    bar_type: BarType

    # L2 订单簿
    order_book_depth: int = 10
    imbalance_decay: float = 0.3
    imbalance_threshold: float = 0.58
    imbalance_weight_mode: str = "linear"  # "linear" | "exp"

    # EMA 辅助过滤
    fast_ema_period: PositiveInt = 20
    slow_ema_period: PositiveInt = 60

    # 动态 spread
    base_spread_ticks: int = 3
    min_spread_ticks: int = 2
    max_spread_ticks: int = 10
    spread_vol_multiplier: float = 2.0
    spread_recovery_ratio: float = 0.9  # hysteresis recovery threshold

    # skew 参数
    alpha_scale_ticks: float = 2.0
    alpha_tanh_k: float = 2.0
    inv_scale_ticks: float = 3.0
    inv_tanh_scale: float = 2.0
    ask_inv_weight: float = 1.2

    # 库存控制
    max_position_usd: float = 1000.0
    soft_inventory_limit: float = 0.30
    hard_inventory_limit: float = 0.70
    soft_size_min_ratio: float = 0.3
    kill_switch_limit: float = 1.2

    # 订单生命周期
    limit_ttl_ms: int = 8000
    post_only: bool = True
    refresh_every_bar: bool = True
    drift_ticks: int = 2
    skew_drift_ticks: int = 1
    fill_cooldown_ms: int = 500

    # Imbalance dead zone
    dead_zone_threshold: float = 0.1

    # 强制开启订单簿订阅
    subscribe_order_book: bool = True

    # US-001: Microprice
    use_microprice: bool = True

    # US-002: Adverse selection detection
    adverse_selection_ticks: int = 3
    adverse_selection_cooldown_ms: int = 2000

    # US-003: Order queue awareness (GTD refresh)
    order_refresh_ratio: float = 0.7

    # US-004: Delta-driven quoting
    quote_on_delta: bool = False
    delta_quote_min_interval_ms: int = 100

    # US-005: Realized volatility
    use_realized_vol: bool = False
    rv_window: int = 20

    # US-006: Layered quoting
    quote_layers: int = 1
    layer_spread_step_ticks: float = 1.0
    layer_size_decay: float = 0.7

    # US-007: PnL speed circuit breaker
    max_loss_usd: float = 50.0
    loss_window_ms: int = 60000
    pnl_cb_cooldown_ms: int = 300000

    # US-008: Market quality filter
    max_book_spread_ticks: float = 20.0
    imbalance_spike_threshold: float = 0.9

    # US-009: Cost model — minimum expected profit
    min_expected_profit_bps: float = 1.0
    taker_fee_bps: float = 4.0


class ActiveMarketMaker(BaseStrategy):
    """主动做市商策略."""

    def __init__(self, config: MarketMakerConfig, event_bus: EventBus | None = None) -> None:
        """Initialize the market maker strategy.

        Args:
            config: Strategy configuration.
            event_bus: Optional event bus for live mode.
        """
        super().__init__(config, event_bus)
        self._fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self._slow_ema = ExponentialMovingAverage(config.slow_ema_period)

        # Ensure ATR is created for dynamic spread
        self._ensure_atr_indicator()

        # L2 imbalance state
        self._smooth_imbalance: float = 0.0  # range [-1, 1]

        # Dynamic spread state
        self._current_spread_ticks: float = float(config.base_spread_ticks)
        self._quote_suspended: bool = False

        # Inventory tracking
        self._net_position_usd: float = 0.0

        # Active quote order IDs — US-006 layered
        self._active_bid_ids: list[ClientOrderId | None] = []
        self._active_ask_ids: list[ClientOrderId | None] = []

        # Drift-threshold state
        self._quoted_mid: float | None = None
        self._quoted_skew: float | None = None

        # Fill cooldown state
        self._last_fill_ts: datetime | None = None

        # Kill switch state
        self._kill_switch: bool = False

        # US-002: Adverse selection
        self._last_fill_price: float | None = None
        self._last_fill_side: str | None = None  # "BUY" or "SELL"
        self._adverse_cooldown_until: datetime | None = None

        # US-003: Order queue awareness
        self._bid_submit_time: datetime | None = None
        self._ask_submit_time: datetime | None = None

        # US-004: Delta-driven quoting
        self._last_delta_quote_ts: datetime | None = None
        self._last_base_qty: Decimal | None = None

        # US-005: Realized volatility
        self._price_returns: deque[float] = deque(maxlen=config.rv_window)
        self._last_mid_for_rv: float | None = None

        # US-007: PnL speed circuit breaker
        self._recent_fills: deque[tuple[datetime, float]] = deque()
        self._pnl_circuit_open: bool = False
        self._pnl_cb_reset_at: datetime | None = None

        # US-008: Market quality filter
        self._quote_quality_ok: bool = True

    # ------------------------------------------------------------------
    # US-006: Backward-compatible properties for single-layer access
    # ------------------------------------------------------------------

    @property
    def _active_bid_id(self) -> ClientOrderId | None:
        """Get the first layer bid order ID."""
        return self._active_bid_ids[0] if self._active_bid_ids else None

    @_active_bid_id.setter
    def _active_bid_id(self, val: ClientOrderId | None) -> None:
        if self._active_bid_ids:
            self._active_bid_ids[0] = val
        else:
            self._active_bid_ids = [val]

    @property
    def _active_ask_id(self) -> ClientOrderId | None:
        """Get the first layer ask order ID."""
        return self._active_ask_ids[0] if self._active_ask_ids else None

    @_active_ask_id.setter
    def _active_ask_id(self, val: ClientOrderId | None) -> None:
        if self._active_ask_ids:
            self._active_ask_ids[0] = val
        else:
            self._active_ask_ids = [val]

    def _utc_now(self) -> datetime:
        """Get current UTC time. Extracted for testability."""
        return self._utc_now()

    def _register_indicators(self) -> None:
        self.register_indicator_for_bars(self.config.bar_type, self._fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self._slow_ema)

    def _history_warmup_bars(self) -> int:
        return max(int(self.config.fast_ema_period), int(self.config.slow_ema_period), int(self.config.atr_period)) + 2

    # ------------------------------------------------------------------
    # L2 Imbalance
    # ------------------------------------------------------------------

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        """Process order book deltas and update weighted imbalance."""
        self._calc_weighted_imbalance()

        # US-008: Market quality filter
        try:
            order_book = self.cache.order_book(self.config.instrument_id)
            if order_book is not None:
                best_bid = order_book.best_bid_price()
                best_ask = order_book.best_ask_price()
                if best_bid is not None and best_ask is not None:
                    self._check_market_quality(float(best_bid), float(best_ask))
        except Exception:
            pass

        # US-004: Delta-driven quoting
        if self.config.quote_on_delta:
            self._try_quote_on_delta()

    def _calc_weighted_imbalance(self) -> None:
        """Calculate front-N weighted imbalance with EWM smoothing.

        Formula: raw = (bid_w - ask_w) / (bid_w + ask_w), range [-1, 1].
        """
        try:
            order_book = self.cache.order_book(self.config.instrument_id)
        except Exception:
            return
        if order_book is None:
            return

        depth = self.config.order_book_depth
        bids = list(order_book.bids())[:depth]
        asks = list(order_book.asks())[:depth]

        wb = self._calc_weights(len(bids)) if bids else []
        wa = self._calc_weights(len(asks)) if asks else []

        bid_w = sum(wb[i] * float(bids[i].size) for i in range(len(bids))) if bids else 0.0
        ask_w = sum(wa[i] * float(asks[i].size) for i in range(len(asks))) if asks else 0.0
        total = bid_w + ask_w
        if total <= 0:
            return

        raw = (bid_w - ask_w) / total
        d = self.config.imbalance_decay
        self._smooth_imbalance = d * self._smooth_imbalance + (1.0 - d) * raw

        if abs(self._smooth_imbalance) < self.config.dead_zone_threshold:
            self._smooth_imbalance = 0.0

    def _calc_weights(self, n: int) -> list[float]:
        if self.config.imbalance_weight_mode == "exp":
            lam = 0.5
            return [math.exp(-lam * i) for i in range(n)]
        # linear: weight[i] = (n-i)/n
        return [(n - i) / n for i in range(n)]

    # ------------------------------------------------------------------
    # Mid Price (US-001: Microprice)
    # ------------------------------------------------------------------

    def _get_microprice(self, bar: Bar | None) -> float | None:
        """Compute microprice from orderbook size-weighted mid.

        Args:
            bar: Current bar (unused).

        Returns:
            Microprice as float, or None if orderbook unavailable.
        """
        try:
            order_book = self.cache.order_book(self.config.instrument_id)
            if order_book is not None:
                best_bid = order_book.best_bid_price()
                best_ask = order_book.best_ask_price()
                if best_bid is not None and best_ask is not None:
                    bb = float(best_bid)
                    ba = float(best_ask)
                    if bb > 0 and ba > 0:
                        bid_size = order_book.best_bid_size()
                        ask_size = order_book.best_ask_size()
                        if bid_size is not None and ask_size is not None:
                            bs = float(bid_size)
                            as_ = float(ask_size)
                            if bs > 0 and as_ > 0:
                                return (bs * ba + as_ * bb) / (bs + as_)
                        # Fallback: simple mid
                        return (bb + ba) / 2.0
        except Exception:
            pass
        return None

    def _get_mid_price(self, bar: Bar | None) -> float | None:
        """Compute mid from orderbook.

        Args:
            bar: Current bar (unused, kept for API compatibility).

        Returns:
            Mid price as float, or None if orderbook unavailable.
        """
        if self.config.use_microprice:
            return self._get_microprice(bar)

        try:
            order_book = self.cache.order_book(self.config.instrument_id)
            if order_book is not None:
                best_bid = order_book.best_bid_price()
                best_ask = order_book.best_ask_price()
                if best_bid is not None and best_ask is not None:
                    bb = float(best_bid)
                    ba = float(best_ask)
                    if bb > 0 and ba > 0:
                        return (bb + ba) / 2.0
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # US-005: Realized Volatility
    # ------------------------------------------------------------------

    def _update_realized_vol(self, mid: float) -> float | None:
        """Update realized volatility from log returns.

        Args:
            mid: Current mid price.

        Returns:
            Sample std of log returns, or None if insufficient data.
        """
        if self._last_mid_for_rv is not None and mid > 0 and self._last_mid_for_rv > 0:
            ret = math.log(mid / self._last_mid_for_rv)
            self._price_returns.append(ret)
        self._last_mid_for_rv = mid
        if len(self._price_returns) < 2:
            return None
        return statistics.stdev(self._price_returns)

    def _get_rv_ticks(self) -> float:
        """Convert realized volatility to tick units.

        Returns:
            Volatility in tick units, or 0.0 if insufficient data.
        """
        if len(self._price_returns) < 2:
            return 0.0
        std = statistics.stdev(self._price_returns)
        tick = 1.0
        if self.instrument is not None:
            tick = float(self.instrument.price_increment)
        if tick <= 0:
            tick = 1.0
        mid = self._last_mid_for_rv or 1.0
        vol_price = std * mid
        return vol_price / tick

    # ------------------------------------------------------------------
    # US-008: Market Quality Filter
    # ------------------------------------------------------------------

    def _check_market_quality(self, best_bid: float, best_ask: float) -> None:
        """Check market quality based on book spread and imbalance spike.

        Args:
            best_bid: Best bid price.
            best_ask: Best ask price.
        """
        tick = 1.0
        if self.instrument is not None:
            tick = float(self.instrument.price_increment)
        if tick <= 0:
            tick = 1.0
        book_spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        spread_bad = book_spread_ticks > self.config.max_book_spread_ticks
        imbalance_bad = abs(self._smooth_imbalance) > self.config.imbalance_spike_threshold
        was_ok = self._quote_quality_ok
        self._quote_quality_ok = not (spread_bad or imbalance_bad)
        if not was_ok and self._quote_quality_ok:
            self.log.info("Market quality restored, resuming quotes", color=LogColor.GREEN)
        if was_ok and not self._quote_quality_ok:
            self.log.warning("Market quality degraded, pausing quotes", color=LogColor.YELLOW)

    # ------------------------------------------------------------------
    # US-009: Cost Model
    # ------------------------------------------------------------------

    def _calc_expected_profit_bps(self, mid: float) -> float:
        """Calculate expected profit in basis points.

        Args:
            mid: Current mid price.

        Returns:
            Expected profit after fees in bps.
        """
        tick = 1.0
        if self.instrument is not None:
            tick = float(self.instrument.price_increment)
        if tick <= 0:
            tick = 1.0
        spread_price = self._current_spread_ticks * tick
        if mid <= 0:
            return 0.0
        gross_bps = (spread_price / mid) * 10000 / 2
        return gross_bps - self.config.taker_fee_bps

    # ------------------------------------------------------------------
    # US-002: Adverse Selection Detection
    # ------------------------------------------------------------------

    def _check_adverse_selection(self, mid: float) -> str | None:
        """Check if last fill was adversely selected.

        Args:
            mid: Current mid price.

        Returns:
            "BUY" or "SELL" if adverse selection detected, None otherwise.
        """
        if self._last_fill_price is None or self._last_fill_side is None:
            return None
        tick = float(self.instrument.price_increment) if self.instrument is not None else 1.0
        if tick <= 0:
            tick = 1.0
        drift = mid - self._last_fill_price
        threshold = self.config.adverse_selection_ticks * tick
        if self._last_fill_side == "BUY" and drift < -threshold:
            return "BUY"
        if self._last_fill_side == "SELL" and drift > threshold:
            return "SELL"
        return None

    # ------------------------------------------------------------------
    # Dynamic Spread (US-005 integrated)
    # ------------------------------------------------------------------

    def _update_dynamic_spread(self) -> None:
        if self.config.use_realized_vol:
            rv_ticks = self._get_rv_ticks()
            if rv_ticks > 0:
                raw = float(self.config.base_spread_ticks) + float(self.config.spread_vol_multiplier) * rv_ticks
            else:
                raw = float(self.config.base_spread_ticks)
        else:
            if self._atr_indicator is None or not self._atr_indicator.initialized:
                self._current_spread_ticks = float(self.config.base_spread_ticks)
                return

            tick = 1.0
            if self.instrument is not None:
                tick = float(self.instrument.price_increment)
            if tick <= 0:
                tick = 1.0

            atr_ticks = float(self._atr_indicator.value) / tick
            raw = float(self.config.base_spread_ticks) + float(self.config.spread_vol_multiplier) * atr_ticks

        if raw > float(self.config.max_spread_ticks):
            if not self._quote_suspended:
                self.log.warning("Spread too wide, suspending quotes", color=LogColor.YELLOW)
                self._cancel_all_quotes()
            self._quote_suspended = True
            return

        # Hysteresis recovery: only resume when spread drops below recovery ratio
        if self._quote_suspended:
            if raw <= float(self.config.max_spread_ticks) * self.config.spread_recovery_ratio:
                self._quote_suspended = False
                self.log.info("Spread recovered, resuming quotes", color=LogColor.GREEN)
            else:
                return

        self._current_spread_ticks = max(
            float(self.config.min_spread_ticks),
            min(float(self.config.max_spread_ticks), raw),
        )

    # ------------------------------------------------------------------
    # Quote Price & Size Calculation
    # ------------------------------------------------------------------

    def _calc_quote_prices(self, mid: float, dir_val: float) -> tuple[float, float, float]:
        """Calculate bid/ask prices with alpha skew and inventory skew.

        Alpha skew:     tanh-based nonlinear shift from directional signal.
        Inventory skew: net long (inv_ratio > 0) → bid & ask shift DOWN (promote selling).
        Alpha weight:   decays alpha contribution as inventory grows.

        Returns:
            Tuple of (bid, ask, avg_shift) where avg_shift is used for drift tracking.
        """
        tick = 1.0
        if self.instrument is not None and hasattr(self.instrument, "price_increment"):
            tick = float(self.instrument.price_increment)

        half_spread = self._current_spread_ticks * tick / 2.0

        alpha_shift = math.tanh(dir_val * float(self.config.alpha_tanh_k)) * float(self.config.alpha_scale_ticks) * tick

        inv_ratio = self._net_position_usd / max(float(self.config.max_position_usd), 1.0)
        inv_skew = math.tanh(inv_ratio * self.config.inv_tanh_scale) * float(self.config.inv_scale_ticks) * tick

        alpha_weight = max(0.0, 1.0 - abs(inv_ratio))

        bid_shift = alpha_weight * alpha_shift - inv_skew
        ask_shift = alpha_weight * alpha_shift - inv_skew * float(self.config.ask_inv_weight)
        bid = mid - half_spread + bid_shift
        ask = mid + half_spread + ask_shift
        avg_shift = (bid_shift + ask_shift) / 2.0
        return bid, ask, avg_shift

    def _calc_quote_sizes(
        self,
        base_qty: Decimal,
        adverse_side: str | None = None,
    ) -> tuple[Decimal, Decimal]:
        """Calculate bid/ask sizes with soft-limit scaling.

        Args:
            base_qty: Base order quantity.
            adverse_side: If "BUY", zero bid; if "SELL", zero ask (US-002).
        """
        if self.instrument is None:
            return base_qty, base_qty

        inv_ratio = abs(self._net_position_usd) / self.config.max_position_usd
        soft = self.config.soft_inventory_limit
        hard = self.config.hard_inventory_limit
        min_r = self.config.soft_size_min_ratio

        at_hard = inv_ratio >= hard

        if inv_ratio <= soft:
            scale = 1.0
        elif inv_ratio < hard:
            t = (inv_ratio - soft) / (hard - soft)
            # Nonlinear size scaling: scale shrinks quadratically in soft zone
            scale = 1.0 - (t**2) * (1.0 - min_r)
        else:
            scale = min_r

        step = float(self.instrument.size_increment)

        def round_to_step(val: float) -> Decimal:
            if step <= 0:
                return Decimal(str(val))
            rounded = round(val / step) * step
            return Decimal(str(rounded))

        base_f = float(base_qty)

        if self._net_position_usd > 0:
            bid_qty = Decimal("0") if at_hard else round_to_step(base_f * scale)
            ask_qty = round_to_step(base_f)
        elif self._net_position_usd < 0:
            bid_qty = round_to_step(base_f)
            ask_qty = Decimal("0") if at_hard else round_to_step(base_f * scale)
        else:
            bid_qty = round_to_step(base_f)
            ask_qty = round_to_step(base_f)

        # US-002: Zero out adverse side
        if adverse_side == "BUY":
            bid_qty = Decimal("0")
        elif adverse_side == "SELL":
            ask_qty = Decimal("0")

        return bid_qty, ask_qty

    # ------------------------------------------------------------------
    # Signal Generation (kept for signal bus backward compatibility)
    # ------------------------------------------------------------------

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        """Generate directional signal based on continuous imbalance for signal bus compatibility.

        Returns LONG if dir_val > dead_zone_threshold, SHORT if < -dead_zone_threshold, else None.
        """
        dir_val = self._compute_dir_val()
        if dir_val > self.config.dead_zone_threshold:
            return SignalDirection.LONG
        if dir_val < -self.config.dead_zone_threshold:
            return SignalDirection.SHORT
        return None

    def _compute_dir_val(self) -> float:
        """Compute continuous directional value from imbalance + EMA gate.

        Returns:
            Continuous value in approximately [-1, 1].
        """
        dir_val = self._smooth_imbalance

        if self._fast_ema.initialized and self._slow_ema.initialized:
            ema_bull = float(self._fast_ema.value) > float(self._slow_ema.value)
            ema_bear = float(self._fast_ema.value) < float(self._slow_ema.value)
            # EMA contradicts imbalance → halve signal
            if (dir_val > 0 and ema_bear) or (dir_val < 0 and ema_bull):
                dir_val *= 0.5

        return dir_val

    # ------------------------------------------------------------------
    # Price Clamp
    # ------------------------------------------------------------------

    def _clamp_quote_prices(self, bid_price: float, ask_price: float) -> tuple[float, float] | None:
        """Clamp bid below best_ask and ask above best_bid.

        Returns:
            Clamped (bid, ask) tuple, or None if crossed after clamping.
        """
        tick = 1.0
        if self.instrument is not None:
            tick = float(self.instrument.price_increment)

        try:
            order_book = self.cache.order_book(self.config.instrument_id)
            if order_book is not None:
                best_bid_price = order_book.best_bid_price()
                best_ask_price = order_book.best_ask_price()
                if best_bid_price is not None and best_ask_price is not None:
                    best_bid = float(best_bid_price)
                    best_ask = float(best_ask_price)
                    bid_price = min(bid_price, best_ask - tick)
                    ask_price = max(ask_price, best_bid + tick)
        except Exception:
            pass

        if bid_price >= ask_price:
            self.log.warning("Quote prices crossed after clamp, skipping refresh", color=LogColor.YELLOW)
            return None

        return bid_price, ask_price

    # ------------------------------------------------------------------
    # US-003: Order Queue Awareness (GTD Refresh)
    # ------------------------------------------------------------------

    def _maybe_refresh_expiring_orders(self, mid: float) -> None:
        """Refresh orders nearing TTL expiry if a better price is available.

        Args:
            mid: Current mid price.
        """
        if not self._active_bid_ids and not self._active_ask_ids:
            return
        if self._active_bid_id is None and self._active_ask_id is None:
            return

        now = self._utc_now()
        ttl = timedelta(milliseconds=self.config.limit_ttl_ms)
        refresh_threshold = ttl * self.config.order_refresh_ratio

        dir_val = self._compute_dir_val()
        optimal_bid, optimal_ask, _ = self._calc_quote_prices(mid, dir_val)

        # Check bid
        if (
            self._bid_submit_time is not None
            and self._active_bid_id is not None
            and (now - self._bid_submit_time) > refresh_threshold
        ):
            order = self.cache.order(self._active_bid_id)
            if order is not None and order.is_open:
                current_price = float(order.price)
                if current_price < optimal_bid:
                    self.cancel_order(order)
                    self._active_bid_id = None
                    self._bid_submit_time = None

        # Check ask
        if (
            self._ask_submit_time is not None
            and self._active_ask_id is not None
            and (now - self._ask_submit_time) > refresh_threshold
        ):
            order = self.cache.order(self._active_ask_id)
            if order is not None and order.is_open:
                current_price = float(order.price)
                if current_price > optimal_ask:
                    self.cancel_order(order)
                    self._active_ask_id = None
                    self._ask_submit_time = None

    # ------------------------------------------------------------------
    # Order Lifecycle
    # ------------------------------------------------------------------

    def _cancel_all_quotes(self) -> None:
        # US-006: Cancel all layers
        for oid in self._active_bid_ids + self._active_ask_ids:
            if oid is not None:
                order = self.cache.order(oid)
                if order is not None and order.is_open:
                    self.cancel_order(order)
        self._active_bid_ids = []
        self._active_ask_ids = []
        self._quoted_mid = None
        self._quoted_skew = None
        # US-003
        self._bid_submit_time = None
        self._ask_submit_time = None

    def _submit_quote(self, side: OrderSide, price: float, qty: Decimal) -> ClientOrderId | None:
        if self.instrument is None:
            return None
        if qty <= 0:
            return None
        try:
            price_obj = self.instrument.make_price(price)
            qty_obj = self.instrument.make_qty(qty)
            if qty_obj.as_decimal() <= 0:
                return None

            expire_time = self._utc_now() + timedelta(milliseconds=self.config.limit_ttl_ms)
            order = self.order_factory.limit(
                instrument_id=self.config.instrument_id,
                order_side=side,
                quantity=qty_obj,
                price=price_obj,
                time_in_force=TimeInForce.GTD,
                expire_time=expire_time,
                post_only=self.config.post_only,
                reduce_only=False,
            )
            self.submit_order(order)
            return order.client_order_id
        except Exception as e:
            self.log.error(f"Failed to submit quote: {e}")
            return None

    def _submit_layered_quotes(
        self,
        bid_price: float,
        ask_price: float,
        bid_qty: Decimal,
        ask_qty: Decimal,
    ) -> None:
        """Submit layered quotes at multiple price levels (US-006).

        Args:
            bid_price: Layer-0 bid price.
            ask_price: Layer-0 ask price.
            bid_qty: Layer-0 bid quantity.
            ask_qty: Layer-0 ask quantity.
        """
        tick = 1.0
        if self.instrument is not None:
            tick = float(self.instrument.price_increment)

        self._active_bid_ids = []
        self._active_ask_ids = []

        for i in range(self.config.quote_layers):
            decay = Decimal(str(self.config.layer_size_decay**i))
            step_offset = i * self.config.layer_spread_step_ticks * tick

            layer_bid_price = bid_price - step_offset
            layer_ask_price = ask_price + step_offset
            layer_bid_qty = Decimal(str(float(bid_qty) * float(decay)))
            layer_ask_qty = Decimal(str(float(ask_qty) * float(decay)))

            bid_id = self._submit_quote(OrderSide.BUY, layer_bid_price, layer_bid_qty)
            ask_id = self._submit_quote(OrderSide.SELL, layer_ask_price, layer_ask_qty)
            self._active_bid_ids.append(bid_id)
            self._active_ask_ids.append(ask_id)

    def _refresh_quotes(
        self,
        bid_price: float,
        ask_price: float,
        bid_qty: Decimal,
        ask_qty: Decimal,
        mid: float,
        current_skew: float,
    ) -> None:
        """Cancel and resubmit quotes when mid or skew drifts beyond threshold."""
        if self._quoted_mid is not None and self._quoted_skew is not None:
            tick = 1.0
            if self.instrument is not None:
                tick = float(self.instrument.price_increment)
            mid_drift = abs(mid - self._quoted_mid)
            skew_drift = abs(current_skew - self._quoted_skew)
            if mid_drift <= self.config.drift_ticks * tick and skew_drift <= self.config.skew_drift_ticks * tick:
                return

        self._cancel_all_quotes()
        if self._quote_suspended:
            return

        if self.config.quote_layers > 1:
            self._submit_layered_quotes(bid_price, ask_price, bid_qty, ask_qty)
        else:
            self._active_bid_id = self._submit_quote(OrderSide.BUY, bid_price, bid_qty)
            self._active_ask_id = self._submit_quote(OrderSide.SELL, ask_price, ask_qty)

        self._quoted_mid = mid
        self._quoted_skew = current_skew

        # US-003: Track submission times
        self._bid_submit_time = self._utc_now() if self._active_bid_id else None
        self._ask_submit_time = self._utc_now() if self._active_ask_id else None

    def on_order_canceled(self, event: OrderCanceled) -> None:  # noqa: D102
        oid = event.client_order_id
        # US-006: Check all layers
        for i, bid_id in enumerate(self._active_bid_ids):
            if oid == bid_id:
                self._active_bid_ids[i] = None
                return
        for i, ask_id in enumerate(self._active_ask_ids):
            if oid == ask_id:
                self._active_ask_ids[i] = None
                return

    def on_order_filled(self, event: OrderFilled) -> None:  # noqa: D102
        self._last_fill_ts = self._utc_now()

        # US-002: Track fill for adverse selection
        self._last_fill_price = float(event.last_px)
        self._last_fill_side = "BUY" if event.order_side == OrderSide.BUY else "SELL"

        # US-007: Record fill for PnL circuit breaker
        pnl_estimate = float(event.last_qty) * float(event.last_px) * (1 if event.order_side == OrderSide.SELL else -1)
        self._recent_fills.append((self._utc_now(), pnl_estimate))

    # ------------------------------------------------------------------
    # US-004: Delta-driven quoting
    # ------------------------------------------------------------------

    def _try_quote_on_delta(self) -> None:
        """Attempt to refresh quotes on orderbook delta events."""
        if not self._fast_ema.initialized or not self._slow_ema.initialized:
            return
        if self._kill_switch or self._quote_suspended:
            return
        if not self._quote_quality_ok:
            return

        now = self._utc_now()
        if self._last_delta_quote_ts is not None:
            elapsed_ms = (now - self._last_delta_quote_ts).total_seconds() * 1000
            if elapsed_ms < self.config.delta_quote_min_interval_ms:
                return

        mid = self._get_mid_price(None)
        if mid is None:
            return

        if self._last_base_qty is None:
            return

        dir_val = self._compute_dir_val()
        bid_price, ask_price, current_skew = self._calc_quote_prices(mid, dir_val)

        clamped = self._clamp_quote_prices(bid_price, ask_price)
        if clamped is None:
            return
        bid_price, ask_price = clamped

        bid_qty, ask_qty = self._calc_quote_sizes(self._last_base_qty)
        self._refresh_quotes(bid_price, ask_price, bid_qty, ask_qty, mid, current_skew)
        self._last_delta_quote_ts = now

    # ------------------------------------------------------------------
    # Main Loop
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """Override base on_bar to inject quote refresh logic."""
        self.log.info(repr(bar), LogColor.CYAN)

        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        if self._kill_switch:
            return

        # US-007: PnL circuit breaker check
        now = self._utc_now()
        if self._pnl_circuit_open:
            if self._pnl_cb_reset_at and now >= self._pnl_cb_reset_at:
                self._pnl_circuit_open = False
                self._pnl_cb_reset_at = None
            else:
                return
        # Prune old fills
        cutoff = now - timedelta(milliseconds=self.config.loss_window_ms)
        while self._recent_fills and self._recent_fills[0][0] < cutoff:
            self._recent_fills.popleft()
        window_pnl = sum(p for _, p in self._recent_fills)
        if window_pnl < -self.config.max_loss_usd:
            self._pnl_circuit_open = True
            self._pnl_cb_reset_at = now + timedelta(milliseconds=self.config.pnl_cb_cooldown_ms)
            self._cancel_all_quotes()
            self.log.critical(
                f"PnL circuit breaker opened: {window_pnl:.2f} USD loss in window",
                color=LogColor.RED,
            )
            return

        if self._last_fill_ts is not None:
            elapsed_ms = (self._utc_now() - self._last_fill_ts).total_seconds() * 1000
            if elapsed_ms < self.config.fill_cooldown_ms:
                return

        self._bar_index += 1
        self._update_dynamic_spread()

        if self._quote_suspended:
            return

        mid = self._get_mid_price(bar)
        if mid is None:
            return

        # US-005: Update realized volatility
        self._update_realized_vol(mid)

        # US-008: Market quality filter
        if not self._quote_quality_ok:
            return

        # US-009: Cost model filter
        expected_profit = self._calc_expected_profit_bps(mid)
        if expected_profit < self.config.min_expected_profit_bps:
            return

        # US-002: Adverse selection detection
        adverse_side = self._check_adverse_selection(mid)
        if adverse_side is not None:
            self._adverse_cooldown_until = self._utc_now() + timedelta(
                milliseconds=self.config.adverse_selection_cooldown_ms,
            )
            self._last_fill_price = None  # reset after triggering

        in_adverse_cooldown = (
            self._adverse_cooldown_until is not None and self._utc_now() < self._adverse_cooldown_until
        )

        dir_val = self._compute_dir_val()

        bid_price, ask_price, avg_shift = self._calc_quote_prices(mid, dir_val)

        clamped = self._clamp_quote_prices(bid_price, ask_price)
        if clamped is None:
            return
        bid_price, ask_price = clamped

        # US-003: Check for expiring orders before main refresh
        self._maybe_refresh_expiring_orders(mid)

        base_qty = self._resolve_order_quantity(bar)
        if base_qty is None:
            return

        # US-004: Store base qty for delta-driven quoting
        self._last_base_qty = base_qty.as_decimal()

        # Pass adverse_side to sizing if in cooldown
        effective_adverse = adverse_side if in_adverse_cooldown else None
        bid_qty, ask_qty = self._calc_quote_sizes(base_qty.as_decimal(), adverse_side=effective_adverse)

        if self.config.refresh_every_bar and not self.config.quote_on_delta:
            self._refresh_quotes(bid_price, ask_price, bid_qty, ask_qty, mid, avg_shift)

    # ------------------------------------------------------------------
    # Inventory Tracking
    # ------------------------------------------------------------------

    def _update_net_position(self) -> None:
        positions = self.cache.positions_open(instrument_id=self.config.instrument_id)
        net_usd = 0.0
        for pos in positions:
            qty = float(pos.quantity)
            price = float(pos.avg_px_open)
            sign = 1.0 if pos.is_long else -1.0
            net_usd += sign * qty * price
        self._net_position_usd = net_usd

        inv_ratio = abs(net_usd) / max(self.config.max_position_usd, 1.0)
        if inv_ratio >= self.config.kill_switch_limit and not self._kill_switch:
            self._kill_switch = True
            self._cancel_all_quotes()
            self.log.critical(
                f"Kill switch activated: inv_ratio={inv_ratio:.2f} >= {self.config.kill_switch_limit}",
                color=LogColor.RED,
            )
        elif inv_ratio < self.config.hard_inventory_limit and self._kill_switch:
            self._kill_switch = False
            self.log.info("Kill switch reset", color=LogColor.GREEN)

    def on_position_opened(self, event: PositionOpened) -> None:  # noqa: D102
        super().on_position_opened(event)
        self._update_net_position()

    def on_position_changed(self, event: PositionChanged) -> None:  # noqa: D102
        super().on_position_changed(event)
        self._update_net_position()

    def on_position_closed(self, event: PositionClosed) -> None:  # noqa: D102
        super().on_position_closed(event)
        self._update_net_position()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_stop(self) -> None:  # noqa: D102
        self._cancel_all_quotes()
        super().on_stop()

    def on_reset(self) -> None:  # noqa: D102
        super().on_reset()
        self._fast_ema.reset()
        self._slow_ema.reset()
        if self._atr_indicator is not None:
            self._atr_indicator.reset()
        self._smooth_imbalance = 0.0
        self._current_spread_ticks = float(self.config.base_spread_ticks)
        self._quote_suspended = False
        self._net_position_usd = 0.0
        self._active_bid_ids = []
        self._active_ask_ids = []
        self._quoted_mid = None
        self._quoted_skew = None
        self._last_fill_ts = None
        self._kill_switch = False
        # US-002
        self._last_fill_price = None
        self._last_fill_side = None
        self._adverse_cooldown_until = None
        # US-003
        self._bid_submit_time = None
        self._ask_submit_time = None
        # US-004
        self._last_base_qty = None
        self._last_delta_quote_ts = None
        # US-005
        self._price_returns.clear()
        self._last_mid_for_rv = None
        # US-007
        self._recent_fills.clear()
        self._pnl_circuit_open = False
        self._pnl_cb_reset_at = None
        # US-008
        self._quote_quality_ok = True
