"""主动做市商策略.

主动做市商策略：
- 加权 L2 imbalance 信号（前 N 档，linear/exp 加权，EWM 平滑）
- 完整报价模型：bid/ask = mid ± spread/2 ± skew
- skew = alpha_skew（imbalance 方向）+ inventory_skew（净敞口）
- 动态 spread（基于 ATR/tick_size 波动率）
- 三层库存控制：正常 → 软限制（偏价+缩 size）→ 硬限制（停单）
- 超过 120% max_position 触发 kill switch
- 订单生命周期：drift-threshold 触发 cancel + submit 新双边 limit 单

架构说明（刻意偏离）：
    做市商策略通过 submit_order() 直接下单，绕过 EventBus → OrderRouter →
    AlgoExecution → RateLimiter 链路。原因：做市商需要毫秒级双边报价刷新，
    通过信号链路会引入不可接受的延迟并破坏 cancel/replace 生命周期管理.
    风控前置检查（PreTradeRisk）由策略自身的库存硬限制替代.
    Rate limit 风险通过 refresh_every_bar 开关 + limit_ttl_ms 控制.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import structlog
from nautilus_trader.common.enums import LogColor
from nautilus_trader.config import PositiveInt
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType, OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import AggressorSide, OrderSide, TimeInForce
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
    spread_recovery_ratio: float = 0.9  # 迟滞恢复阈值

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

    # Imbalance dead zone（不感应区）
    dead_zone_threshold: float = 0.1

    # 强制开启订单簿订阅
    subscribe_order_book: bool = True

    # US-001: Microprice（规模加权 mid）
    use_microprice: bool = True

    # US-002: 逆向选择检测
    adverse_selection_ticks: int = 3
    adverse_selection_cooldown_ms: int = 2000

    # US-003: 订单队列感知（GTD 刷新）
    order_refresh_ratio: float = 0.7

    # US-004: delta 驱动报价
    quote_on_delta: bool = False
    delta_quote_min_interval_ms: int = 100

    # US-005: 已实现波动率
    use_realized_vol: bool = False
    rv_window: int = 20

    # US-006: 分层报价
    quote_layers: int = 1
    layer_spread_step_ticks: float = 1.0
    layer_size_decay: float = 0.7

    # US-007: PnL 速度熔断器
    max_loss_usd: float = 50.0
    loss_window_ms: int = 60000
    pnl_cb_cooldown_ms: int = 300000

    # US-008: 市场质量过滤
    max_book_spread_ticks: float = 20.0
    imbalance_spike_threshold: float = 0.9

    # US-009: 成本模型——最低预期收益
    min_expected_profit_bps: float = 1.0
    taker_fee_bps: float = 4.0

    # V3-US-002: Trade flow alpha
    subscribe_trades: bool = True
    trade_flow_weight: float = 0.4

    # V3-US-003: Pre-trade adverse cancel
    pretrade_cancel_ticks: float = 0.5

    # V3-US-004: Microprice deep utilization
    microprice_skew_scale: float = 1.0

    # V3-US-005: Inventory tiered control
    one_side_only_limit: float = 0.7
    deeper_layer_inv_threshold: float = 0.5

    # V4-US-001: Queue position
    queue_norm_volume: float = 10000.0
    queue_improve_threshold: float = 0.7

    # V4-US-002: Toxic flow
    toxic_decay: float = 0.9

    # V4-US-003: Quote quality score
    quote_score_threshold: float = -0.5
    toxic_one_side_threshold: float = 0.5

    # V5-US-001: Microprice alpha driver
    mp_alpha_weight: float = 0.5
    imbalance_weight: float = 0.3

    # V5-US-002: Asymmetric bid/ask alpha
    mp_bias_strength: float = 0.3

    # V5-US-003: Fill-prob driven execution
    withdraw_fill_prob_threshold: float = 0.05
    fill_prob_spread_adj: bool = True

    # V5-US-004: Toxic flow preemptive cancel
    toxic_mp_drift_ticks: float = 1.5

    # V5-US-005: Asymmetric layered quoting
    asymmetric_layers: bool = True


class ActiveMarketMaker(BaseStrategy):
    """主动做市商策略."""

    def __init__(self, config: MarketMakerConfig, event_bus: EventBus | None = None) -> None:
        """初始化做市商策略.

        Args:
            config: 策略配置.
            event_bus: 可选，实盘模式下使用的 event bus.
        """
        super().__init__(config, event_bus)
        self._fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self._slow_ema = ExponentialMovingAverage(config.slow_ema_period)

        # 确保 ATR 已创建，用于动态 spread
        self._ensure_atr_indicator()

        # L2 imbalance 状态
        self._smooth_imbalance: float = 0.0  # 范围 [-1, 1]

        # 动态 spread 状态
        self._current_spread_ticks: float = float(config.base_spread_ticks)
        self._quote_suspended: bool = False

        # 库存跟踪
        self._net_position_usd: float = 0.0

        # 活跃报价订单 ID — US-006 分层报价
        self._active_bid_ids: list[ClientOrderId | None] = []
        self._active_ask_ids: list[ClientOrderId | None] = []

        # drift-threshold 状态
        self._quoted_mid: float | None = None
        self._quoted_skew: float | None = None

        # 成交冷却状态
        self._last_fill_ts: datetime | None = None

        # Kill switch 状态
        self._kill_switch: bool = False

        # US-002: 逆向选择
        self._last_fill_price: float | None = None
        self._last_fill_side: str | None = None  # "BUY" 或 "SELL"
        self._adverse_cooldown_until: datetime | None = None

        # US-003: 订单队列感知
        self._bid_submit_time: datetime | None = None
        self._ask_submit_time: datetime | None = None

        # US-004: delta 驱动报价
        self._last_delta_quote_ts: datetime | None = None
        self._last_base_qty: Decimal | None = None

        # US-005: 已实现波动率
        self._price_returns: deque[float] = deque(maxlen=config.rv_window)
        self._last_mid_for_rv: float | None = None

        # US-007: PnL 速度熔断器
        self._recent_fills: deque[tuple[datetime, float]] = deque()
        self._pnl_circuit_open: bool = False
        self._pnl_cb_reset_at: datetime | None = None

        # US-008: 市场质量过滤
        self._quote_quality_ok: bool = True

        # V3-US-001: 已实现 PnL 追踪（替代 notional）
        self._open_fills: list[tuple[float, float, str]] = []
        self._last_microprice: float | None = None

        # V3-US-002: Trade flow alpha
        self._agg_buy_vol: float = 0.0
        self._agg_sell_vol: float = 0.0

        # V3-US-003: Pre-trade adverse cancel
        self._quoted_bid_price: float | None = None
        self._quoted_ask_price: float | None = None

        # V4-US-001: Queue position
        self._last_best_bid_size: float | None = None
        self._last_best_ask_size: float | None = None

        # V4-US-001: 队列位置快照
        self._bid_queue_on_submit: float | None = None
        self._ask_queue_on_submit: float | None = None
        self._queue_traded_volume: float = 0.0

        # V4-US-002: Toxic flow
        self._toxic_flow_score: float = 0.0
        self._last_fill_mid: float | None = None

        # V4-US-003: Quote quality score
        self._last_quote_score: float = 0.0

        # V5-US-004: Toxic preemptive cancel
        self._prev_microprice: float | None = None

        # V5-US-005: Asymmetric layered quoting
        self._last_dir_val: float = 0.0

    # ------------------------------------------------------------------
    # US-006: 向后兼容的单层访问属性
    # ------------------------------------------------------------------

    @property
    def _active_bid_id(self) -> ClientOrderId | None:
        """获取第一层 bid 订单 ID."""
        return self._active_bid_ids[0] if self._active_bid_ids else None

    @_active_bid_id.setter
    def _active_bid_id(self, val: ClientOrderId | None) -> None:
        if self._active_bid_ids:
            self._active_bid_ids[0] = val
        else:
            self._active_bid_ids = [val]

    @property
    def _active_ask_id(self) -> ClientOrderId | None:
        """获取第一层 ask 订单 ID."""
        return self._active_ask_ids[0] if self._active_ask_ids else None

    @_active_ask_id.setter
    def _active_ask_id(self, val: ClientOrderId | None) -> None:
        if self._active_ask_ids:
            self._active_ask_ids[0] = val
        else:
            self._active_ask_ids = [val]

    def _utc_now(self) -> datetime:
        """获取当前 UTC 时间。独立提取以便于单元测试."""
        return self.clock.utc_now()

    def on_start(self) -> None:
        """启动策略，订阅逐笔成交数据."""
        super().on_start()
        if self.config.subscribe_trades:
            self.subscribe_trade_ticks(self.config.instrument_id)

    def on_trade_tick(self, trade: TradeTick) -> None:
        """处理逐笔成交，追踪主动买卖量."""
        qty = float(trade.size)
        if trade.aggressor_side == AggressorSide.BUYER:
            self._agg_buy_vol += qty
        elif trade.aggressor_side == AggressorSide.SELLER:
            self._agg_sell_vol += qty

        # V4+: 累计队列消耗量（复用已计算的 qty）
        self._queue_traded_volume += qty

        # V4-US-002: 更新 toxic flow 分数
        self._update_toxic_flow(trade)

    def _calc_trade_flow_signal(self) -> float:
        """计算 trade flow 信号：(buy_vol - sell_vol) / (buy_vol + sell_vol + ε)."""
        total = self._agg_buy_vol + self._agg_sell_vol
        if total < 1e-10:
            return 0.0
        return (self._agg_buy_vol - self._agg_sell_vol) / total

    # ------------------------------------------------------------------
    # V4-US-001: Queue Position
    # ------------------------------------------------------------------

    def _estimate_queue_ahead(self, side: str) -> float:
        """当前价位排队量估算（用 best_bid/ask_size 作为代理）."""
        if side == "BUY":
            return self._last_best_bid_size or 0.0
        return self._last_best_ask_size or 0.0

    def _calc_queue_penalty(self, side: str) -> float:
        """队列越长惩罚越大，归一化到 [0, 1]."""
        queue = self._estimate_queue_ahead(side)
        return min(queue / max(self.config.queue_norm_volume, 1.0), 1.0)

    def _calc_queue_fill_prob(self, side: str) -> float:
        """下单后的队列消耗估算成交概率：traded_volume / initial_queue."""
        initial = self._bid_queue_on_submit if side == "BUY" else self._ask_queue_on_submit
        if initial is None or initial <= 0:
            return 1.0
        return min(self._queue_traded_volume / initial, 1.0)

    # ------------------------------------------------------------------
    # V4-US-002: Toxic Flow
    # ------------------------------------------------------------------

    def _update_toxic_flow(self, trade: TradeTick) -> None:
        """基于 microprice drift 更新 toxic flow 分数（微观结构版）."""
        # 使用 microprice 而非 mid，响应更快
        mp = self._last_microprice
        if mp is None:
            return
        if self._last_fill_mid is None:
            self._last_fill_mid = mp
            return
        mp_drift = mp - self._last_fill_mid
        if trade.aggressor_side == AggressorSide.BUYER:
            self._toxic_flow_score += -0.3 if mp_drift < 0 else 0.05
        elif trade.aggressor_side == AggressorSide.SELLER:
            self._toxic_flow_score += 0.3 if mp_drift > 0 else -0.05
        self._toxic_flow_score *= self.config.toxic_decay
        self._toxic_flow_score = max(-1.0, min(1.0, self._toxic_flow_score))
        self._last_fill_mid = mp

    # ------------------------------------------------------------------
    # V4-US-003: Quote Quality Score
    # ------------------------------------------------------------------

    def _calc_quote_score(self, dir_val: float) -> float:
        """统一报价质量评分：alpha + fill_prob - 库存惩罚 - toxic 惩罚 - queue 惩罚."""
        alpha = abs(dir_val)
        inv_ratio = abs(self._net_position_usd) / max(self.config.max_position_usd, 1.0)
        toxic = abs(self._toxic_flow_score)
        fill_prob_bid = self._calc_queue_fill_prob("BUY")
        fill_prob_ask = self._calc_queue_fill_prob("SELL")
        fill_prob = (fill_prob_bid + fill_prob_ask) / 2.0
        queue_penalty = 1.0 - fill_prob
        return alpha + fill_prob * 1.2 - inv_ratio * 0.8 - toxic * 1.5 - queue_penalty

    def _register_indicators(self) -> None:
        self.register_indicator_for_bars(self.config.bar_type, self._fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self._slow_ema)

    def _history_warmup_bars(self) -> int:
        return max(int(self.config.fast_ema_period), int(self.config.slow_ema_period), int(self.config.atr_period)) + 2

    # ------------------------------------------------------------------
    # V3-US-003: Pre-trade adverse cancel
    # ------------------------------------------------------------------

    def _check_pretrade_cancel(self) -> None:
        """检测价格即将穿越报价时主动撤单（pre-trade adverse cancel）."""
        if self.instrument is None:
            return
        tick = float(self.instrument.price_increment)
        threshold = self.config.pretrade_cancel_ticks * tick
        try:
            ob = self.cache.order_book(self.config.instrument_id)
            if ob is None:
                return
            best_bid = ob.best_bid_price()
            best_ask = ob.best_ask_price()
            if best_bid is None or best_ask is None:
                return
            ba = float(best_ask)
            bb = float(best_bid)
        except Exception:
            return

        # best_ask 接近我的 bid → 价格即将向下穿越 → 撤 bid
        if self._quoted_bid_price is not None and ba <= self._quoted_bid_price + threshold and self._active_bid_ids:
            bid_id = self._active_bid_ids[0]
            if bid_id is not None:
                order = self.cache.order(bid_id)
                if order is not None and order.is_open:
                    self.cancel_order(order)
                self._active_bid_ids[0] = None
                self._quoted_bid_price = None

        # best_bid 接近我的 ask → 价格即将向上穿越 → 撤 ask
        if self._quoted_ask_price is not None and bb >= self._quoted_ask_price - threshold and self._active_ask_ids:
            ask_id = self._active_ask_ids[0]
            if ask_id is not None:
                order = self.cache.order(ask_id)
                if order is not None and order.is_open:
                    self.cancel_order(order)
                self._active_ask_ids[0] = None
                self._quoted_ask_price = None

    # ------------------------------------------------------------------
    # L2 imbalance
    # ------------------------------------------------------------------

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        """处理 orderbook delta 更新，刷新加权 imbalance."""
        self._calc_weighted_imbalance()

        # V4-US-001: Queue position — 追踪 best bid/ask size
        try:
            ob = self.cache.order_book(self.config.instrument_id)
            if ob is not None:
                bbs = ob.best_bid_size()
                bas = ob.best_ask_size()
                if bbs is not None:
                    self._last_best_bid_size = float(bbs)
                if bas is not None:
                    self._last_best_ask_size = float(bas)
        except Exception:
            pass

        # V3-US-003: Pre-trade adverse cancel
        self._check_pretrade_cancel()

        # V5-US-004: Toxic preemptive cancel
        self._check_toxic_preemptive()
        try:
            order_book = self.cache.order_book(self.config.instrument_id)
            if order_book is not None:
                best_bid = order_book.best_bid_price()
                best_ask = order_book.best_ask_price()
                if best_bid is not None and best_ask is not None:
                    self._check_market_quality(float(best_bid), float(best_ask))
        except Exception:
            pass

        # US-004: delta 驱动报价
        if self.config.quote_on_delta:
            self._try_quote_on_delta()

    def _calc_weighted_imbalance(self) -> None:
        """计算前 N 档加权 imbalance，并用 EWM 平滑.

        公式：raw = (bid_w - ask_w) / (bid_w + ask_w)，范围 [-1, 1].
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

        bid_w = sum(wb[i] * self._order_book_level_size(bids[i]) for i in range(len(bids))) if bids else 0.0
        ask_w = sum(wa[i] * self._order_book_level_size(asks[i]) for i in range(len(asks))) if asks else 0.0
        total = bid_w + ask_w
        if total <= 0:
            return

        raw = (bid_w - ask_w) / total
        d = self.config.imbalance_decay
        self._smooth_imbalance = d * self._smooth_imbalance + (1.0 - d) * raw

        if abs(self._smooth_imbalance) < self.config.dead_zone_threshold:
            self._smooth_imbalance = 0.0

    def _order_book_level_size(self, level: Any) -> float:
        """读取 order book 档位数量，兼容 Nautilus 方法式 size() 与测试桩属性式 size."""
        size_attr = level.size
        size = size_attr() if callable(size_attr) else size_attr
        return float(cast(Any, size))

    def _calc_weights(self, n: int) -> list[float]:
        if self.config.imbalance_weight_mode == "exp":
            lam = 0.5
            return [math.exp(-lam * i) for i in range(n)]
        # linear: weight[i] = (n-i)/n（线性加权）
        return [(n - i) / n for i in range(n)]

    # ------------------------------------------------------------------
    # Mid Price（US-001: Microprice）
    # ------------------------------------------------------------------

    def _get_microprice(self, bar: Bar | None) -> float | None:
        """从 orderbook 买一/卖一的 size 加权计算 microprice.

        Args:
            bar: 当前 bar（未使用）.

        Returns:
            microprice 浮点数；若 orderbook 不可用则返回 None.
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
                        # 回退：简单 mid
                        return (bb + ba) / 2.0
        except Exception:
            pass
        return None

    def _get_mid_price(self, bar: Bar | None) -> float | None:
        """从 orderbook 计算 mid price.

        Args:
            bar: 当前 bar（未使用，保留以兼容 API）.

        Returns:
            mid price 浮点数；若 orderbook 不可用则返回 None.
        """
        if self.config.use_microprice:
            mp = self._get_microprice(bar)
            if mp is not None:
                self._prev_microprice = self._last_microprice
                self._last_microprice = mp
            return mp

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
    # US-005: 已实现波动率（Realized Volatility）
    # ------------------------------------------------------------------

    def _update_realized_vol(self, mid: float) -> float | None:
        """用对数收益率更新已实现波动率.

        Args:
            mid: 当前 mid price.

        Returns:
            对数收益率的样本标准差；数据不足时返回 None.
        """
        if self._last_mid_for_rv is not None and mid > 0 and self._last_mid_for_rv > 0:
            ret = math.log(mid / self._last_mid_for_rv)
            self._price_returns.append(ret)
        self._last_mid_for_rv = mid
        if len(self._price_returns) < 2:
            return None
        return statistics.stdev(self._price_returns)

    def _get_rv_ticks(self) -> float:
        """将已实现波动率转换为 tick 单位.

        Returns:
            以 tick 为单位的波动率；数据不足时返回 0.0.
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
    # US-008: 市场质量过滤（Market Quality Filter）
    # ------------------------------------------------------------------

    def _check_market_quality(self, best_bid: float, best_ask: float) -> None:
        """根据 orderbook spread 和 imbalance spike 检查市场质量.

        Args:
            best_bid: 买一价.
            best_ask: 卖一价.
        """
        tick = 1.0
        if self.instrument is not None:
            tick = float(self.instrument.price_increment)
        if tick <= 0:
            tick = 1.0
        book_spread_ticks = (best_ask - best_bid) / tick
        spread_bad = book_spread_ticks > self.config.max_book_spread_ticks
        imbalance_bad = abs(self._smooth_imbalance) > self.config.imbalance_spike_threshold
        was_ok = self._quote_quality_ok
        self._quote_quality_ok = not (spread_bad or imbalance_bad)
        if not was_ok and self._quote_quality_ok:
            self.log.info("Market quality restored, resuming quotes", color=LogColor.GREEN)
        if was_ok and not self._quote_quality_ok:
            self.log.warning("Market quality degraded, pausing quotes", color=LogColor.YELLOW)

    # ------------------------------------------------------------------
    # US-009: 成本模型（Cost Model）
    # ------------------------------------------------------------------

    def _calc_expected_profit_bps(self, mid: float) -> float:
        """计算预期收益（单位：bps）.

        Args:
            mid: 当前 mid price.

        Returns:
            扣除手续费后的预期收益，单位 bps.
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
    # US-002: 逆向选择检测（Adverse Selection Detection）
    # ------------------------------------------------------------------

    def _check_adverse_selection(self, mid: float) -> str | None:
        """检查上次成交是否遭到逆向选择.

        Args:
            mid: 当前 mid price.

        Returns:
            检测到逆向选择时返回 "BUY" 或 "SELL"，否则返回 None.
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
    # 动态 spread（已集成 US-005）
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

        # 迟滞恢复：仅当 spread 降至恢复比例以下时才重新报价
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
    # 报价价格与数量计算
    # ------------------------------------------------------------------

    def _calc_quote_prices(self, mid: float, dir_val: float) -> tuple[float, float, float]:
        """计算含 alpha skew 和 inventory skew 的 bid/ask 报价价格.

        Alpha skew：    基于方向性信号的 tanh 非线性偏移.
        Inventory skew：净多头（inv_ratio > 0）→ bid & ask 整体下移（促进卖出）.
        Alpha weight：  随库存增大而衰减 alpha 贡献.

        Returns:
            元组 (bid, ask, avg_shift)，avg_shift 用于 drift 跟踪.
        """
        tick = 1.0
        if self.instrument is not None and hasattr(self.instrument, "price_increment"):
            tick = float(self.instrument.price_increment)

        half_spread = self._current_spread_ticks * tick / 2.0

        alpha_shift = math.tanh(dir_val * float(self.config.alpha_tanh_k)) * float(self.config.alpha_scale_ticks) * tick

        inv_ratio = self._net_position_usd / max(float(self.config.max_position_usd), 1.0)
        inv_skew = math.tanh(inv_ratio * self.config.inv_tanh_scale) * float(self.config.inv_scale_ticks) * tick

        alpha_weight = max(0.0, 1.0 - abs(inv_ratio))

        # V3-US-004: Microprice 偏离：microprice > mid → 买盘强 → bid/ask 上移
        mp_shift = 0.0
        if self.config.use_microprice and self._last_microprice is not None:
            tick_val = float(self.instrument.price_increment) if self.instrument is not None else 1.0
            mp_bias_ticks = (self._last_microprice - mid) / tick_val if tick_val > 0 else 0.0
            mp_shift = mp_bias_ticks * float(self.config.microprice_skew_scale) * tick_val

        # V5-US-002: 非对称 alpha——microprice 方向决定偏置侧
        mp_bias = 0.0
        if self.config.use_microprice and self._last_microprice is not None and self.instrument is not None:
            try:
                ob = self.cache.order_book(self.config.instrument_id)
                if ob is not None:
                    bb_p = ob.best_bid_price()
                    ba_p = ob.best_ask_price()
                    if bb_p is not None and ba_p is not None:
                        mid_v = (float(bb_p) + float(ba_p)) / 2.0
                        mp_bias = self._last_microprice - mid_v
            except Exception:
                pass

        strength = float(self.config.mp_bias_strength)
        if mp_bias > 0:
            bid_alpha_mult = 1.0 + strength
            ask_alpha_mult = 1.0
        else:
            bid_alpha_mult = 1.0
            ask_alpha_mult = 1.0 + strength

        bid_shift = alpha_weight * alpha_shift * bid_alpha_mult - inv_skew + mp_shift
        ask_shift = (
            alpha_weight * alpha_shift * ask_alpha_mult - inv_skew * float(self.config.ask_inv_weight) + mp_shift
        )
        bid = mid - half_spread + bid_shift
        ask = mid + half_spread + ask_shift
        avg_shift = (bid_shift + ask_shift) / 2.0
        return bid, ask, avg_shift

    def _calc_quote_sizes(
        self,
        base_qty: Decimal,
        adverse_side: str | None = None,
    ) -> tuple[Decimal, Decimal]:
        """计算含软限制缩放的 bid/ask 下单数量.

        Args:
            base_qty: 基础下单数量.
            adverse_side: 若为 "BUY" 则将 bid 归零；若为 "SELL" 则将 ask 归零（US-002）.
        """
        if self.instrument is None:
            return base_qty, base_qty

        inv_ratio = abs(self._net_position_usd) / self.config.max_position_usd
        soft = self.config.soft_inventory_limit
        one_side = self.config.one_side_only_limit
        hard = self.config.hard_inventory_limit
        min_r = self.config.soft_size_min_ratio

        at_hard = inv_ratio >= hard
        at_one_side = inv_ratio >= one_side

        if inv_ratio <= soft:
            scale = 1.0
        elif inv_ratio < one_side:
            t = (inv_ratio - soft) / (one_side - soft)
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
            # 净多头：硬限制或单边阈值时停报 bid，单边报 ask
            bid_qty = Decimal("0") if at_hard or at_one_side else round_to_step(base_f * scale)
            ask_qty = round_to_step(base_f)
        elif self._net_position_usd < 0:
            # 净空头：硬限制或单边阈值时停报 ask，单边报 bid
            bid_qty = round_to_step(base_f)
            ask_qty = Decimal("0") if at_hard or at_one_side else round_to_step(base_f * scale)
        else:
            bid_qty = round_to_step(base_f)
            ask_qty = round_to_step(base_f)

        # US-002: 将逆向选择侧数量归零
        if adverse_side == "BUY":
            bid_qty = Decimal("0")
        elif adverse_side == "SELL":
            ask_qty = Decimal("0")

        return bid_qty, ask_qty

    # ------------------------------------------------------------------
    # 信号生成（保留以兼容信号总线）
    # ------------------------------------------------------------------

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        """基于连续 imbalance 生成方向性信号，用于信号总线兼容.

        dir_val > dead_zone_threshold 返回 LONG，
        dir_val < -dead_zone_threshold 返回 SHORT，否则返回 None.
        """
        dir_val = self._compute_dir_val()
        if dir_val > self.config.dead_zone_threshold:
            return SignalDirection.LONG
        if dir_val < -self.config.dead_zone_threshold:
            return SignalDirection.SHORT
        return None

    def _calc_microprice_signal(self) -> float:
        """Microprice 偏离 mid 的归一化信号，用于 alpha 主驱动."""
        if self._last_microprice is None:
            return 0.0
        tick = 1.0
        if self.instrument is not None:
            tick = float(self.instrument.price_increment)
        spread_price = self._current_spread_ticks * tick
        if spread_price <= 0:
            return 0.0
        try:
            ob = self.cache.order_book(self.config.instrument_id)
            if ob is None:
                return 0.0
            bb = ob.best_bid_price()
            ba = ob.best_ask_price()
            if bb is None or ba is None:
                return 0.0
            mid = (float(bb) + float(ba)) / 2.0
        except Exception:
            return 0.0
        bias = self._last_microprice - mid
        normalized = bias / (spread_price / 2.0)
        return math.tanh(normalized)

    def _compute_dir_val(self) -> float:
        """计算连续方向值：microprice 主驱动 + imbalance + trade flow 三路混合."""
        mp_w = float(self.config.mp_alpha_weight)
        imb_w = float(self.config.imbalance_weight)
        tf_w = max(0.0, 1.0 - mp_w - imb_w)

        mp_sig = self._calc_microprice_signal()
        imb = self._smooth_imbalance
        tf = self._calc_trade_flow_signal()

        raw = mp_sig * mp_w + imb * imb_w + tf * tf_w

        # microprice 不可用时降级：只用 imbalance + trade_flow
        if self._last_microprice is None:
            total = imb_w + tf_w
            raw = (imb * imb_w + tf * tf_w) / total if total > 0 else 0.0

        # EMA 方向门控：方向不一致时衰减一半
        if self._fast_ema.initialized and self._slow_ema.initialized:
            ema_bull = float(self._fast_ema.value) > float(self._slow_ema.value)
            ema_bear = float(self._fast_ema.value) < float(self._slow_ema.value)
            if (raw > 0 and ema_bear) or (raw < 0 and ema_bull):
                raw *= 0.5

        return raw

    # ------------------------------------------------------------------
    # 价格夹紧（Price Clamp）
    # ------------------------------------------------------------------

    def _clamp_quote_prices(self, bid_price: float, ask_price: float) -> tuple[float, float] | None:
        """将 bid 夹紧至低于 best_ask，将 ask 夹紧至高于 best_bid.

        Returns:
            夹紧后的 (bid, ask) 元组；夹紧后仍交叉则返回 None.
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
    # US-003: 订单队列感知（GTD 刷新）
    # ------------------------------------------------------------------

    def _maybe_refresh_expiring_orders(self, mid: float) -> None:
        """若订单临近 TTL 到期且有更优价格，则提前刷新.

        Args:
            mid: 当前 mid price.
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

        # 检查 bid
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

        # 检查 ask
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
    # 订单生命周期
    # ------------------------------------------------------------------

    def _cancel_all_quotes(self) -> None:
        # US-006: 撤销所有分层报价
        for oid in self._active_bid_ids + self._active_ask_ids:
            if oid is not None:
                order = self.cache.order(oid)
                if order is not None and order.is_open:
                    self.cancel_order(order)
        self._active_bid_ids = []
        self._active_ask_ids = []
        self._quoted_mid = None
        self._quoted_skew = None
        # US-003: 重置提交时间
        self._bid_submit_time = None
        self._ask_submit_time = None
        # V3-US-003: 重置报价价格
        self._quoted_bid_price = None
        self._quoted_ask_price = None

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
            # 记录下单时的排队量快照，用于计算成交概率
            if side == OrderSide.BUY:
                self._bid_queue_on_submit = self._estimate_queue_ahead("BUY")
            elif side == OrderSide.SELL:
                self._ask_queue_on_submit = self._estimate_queue_ahead("SELL")
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
        """在多个价格档位提交分层报价（US-006）.

        Args:
            bid_price: 第 0 层 bid 价格.
            ask_price: 第 0 层 ask 价格.
            bid_qty: 第 0 层 bid 数量.
            ask_qty: 第 0 层 ask 数量.
        """
        tick = 1.0
        if self.instrument is not None:
            tick = float(self.instrument.price_increment)

        self._active_bid_ids = []
        self._active_ask_ids = []

        inv_ratio = abs(self._net_position_usd) / max(self.config.max_position_usd, 1.0)

        for i in range(self.config.quote_layers):
            decay = Decimal(str(self.config.layer_size_decay**i))
            step_offset = i * self.config.layer_spread_step_ticks * tick

            layer_bid_price = bid_price - step_offset
            layer_ask_price = ask_price + step_offset
            layer_bid_qty = Decimal(str(float(bid_qty) * float(decay)))
            layer_ask_qty = Decimal(str(float(ask_qty) * float(decay)))

            # V3-US-005: 深层报价库存过滤——高库存时跳过同向深层
            if i > 0 and inv_ratio > self.config.deeper_layer_inv_threshold:
                if self._net_position_usd > 0:
                    layer_bid_qty = Decimal("0")
                elif self._net_position_usd < 0:
                    layer_ask_qty = Decimal("0")

            # V5-US-005: 非对称分层——逆方向只铺 1 层
            if self.config.asymmetric_layers and i > 0:
                if self._last_dir_val > 0:
                    layer_ask_qty = Decimal("0")
                elif self._last_dir_val < 0:
                    layer_bid_qty = Decimal("0")

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
        """当 mid 或 skew 漂移超过阈值时，撤销并重新提交报价."""
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

        # US-003: 记录提交时间
        self._bid_submit_time = self._utc_now() if self._active_bid_id else None
        self._ask_submit_time = self._utc_now() if self._active_ask_id else None

        # V3-US-003: 记录报价价格用于 pre-trade cancel
        if self._active_bid_ids and self._active_bid_ids[0]:
            self._quoted_bid_price = bid_price
        if self._active_ask_ids and self._active_ask_ids[0]:
            self._quoted_ask_price = ask_price

    def on_order_canceled(self, event: OrderCanceled) -> None:  # noqa: D102
        oid = event.client_order_id
        # US-006: 遍历所有分层
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

        # US-002: 记录成交信息用于逆向选择检测
        fill_price = float(event.last_px)
        fill_qty = float(event.last_qty)
        fill_side = "BUY" if event.order_side == OrderSide.BUY else "SELL"
        self._last_fill_price = fill_price
        self._last_fill_side = fill_side
        # 匹配对手方 open fills（FIFO）
        realized = 0.0
        remaining = fill_qty
        opposite = "SELL" if fill_side == "BUY" else "BUY"
        new_open: list[tuple[float, float, str]] = []
        for op, oq, os_ in self._open_fills:
            if os_ == opposite and remaining > 0:
                matched = min(oq, remaining)
                if fill_side == "BUY":
                    realized += (op - fill_price) * matched
                else:
                    realized += (fill_price - op) * matched
                remaining -= matched
                if oq > matched:
                    new_open.append((op, oq - matched, os_))
            else:
                new_open.append((op, oq, os_))
        self._open_fills = new_open
        if remaining > 0:
            self._open_fills.append((fill_price, remaining, fill_side))
        # 无 realized → 使用 mark-to-mid 代理
        if abs(realized) < 1e-10 and remaining > 0:
            mid = self._last_microprice or 0.0
            if mid > 0:
                sign = 1.0 if fill_side == "BUY" else -1.0
                realized = (mid - fill_price) * remaining * sign
        self._recent_fills.append((self._utc_now(), realized))

        # V4-US-002: 成交后根据 mid 漂移强化 toxic 分数
        mid_now = self._last_microprice
        if mid_now is not None:
            drift = mid_now - fill_price
            if fill_side == "BUY" and drift < 0:
                self._toxic_flow_score = max(-1.0, self._toxic_flow_score - 0.3)
            elif fill_side == "SELL" and drift > 0:
                self._toxic_flow_score = min(1.0, self._toxic_flow_score + 0.3)

    # ------------------------------------------------------------------
    # US-004: delta 驱动报价
    # ------------------------------------------------------------------

    def _try_quote_on_delta(self) -> None:
        """在 orderbook delta 事件触发时尝试刷新报价."""
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
    # V5-US-003: Fill-prob driven stale quote withdrawal
    # ------------------------------------------------------------------

    def _maybe_withdraw_stale_quotes(self) -> None:
        """fill_prob 极低时撤销滞留单（不再有成交机会的挂单）."""
        threshold = self.config.withdraw_fill_prob_threshold
        if (
            self._active_bid_ids
            and self._active_bid_ids[0] is not None
            and self._calc_queue_fill_prob("BUY") < threshold
        ):
            bid_id = self._active_bid_ids[0]
            order = self.cache.order(bid_id)
            if order is not None and order.is_open:
                self.cancel_order(order)
            self._active_bid_ids[0] = None
            self._quoted_bid_price = None
        if (
            self._active_ask_ids
            and self._active_ask_ids[0] is not None
            and self._calc_queue_fill_prob("SELL") < threshold
        ):
            ask_id = self._active_ask_ids[0]
            order = self.cache.order(ask_id)
            if order is not None and order.is_open:
                self.cancel_order(order)
            self._active_ask_ids[0] = None
            self._quoted_ask_price = None

    # ------------------------------------------------------------------
    # V5-US-004: Toxic flow preemptive cancel
    # ------------------------------------------------------------------

    def _check_toxic_preemptive(self) -> None:
        """Microprice 急速漂移时预防性撤单（toxic 前置防御）."""
        if self._last_microprice is None or self._prev_microprice is None:
            return
        if self.instrument is None:
            return
        tick = float(self.instrument.price_increment)
        threshold = self.config.toxic_mp_drift_ticks * tick
        instant_drift = self._last_microprice - self._prev_microprice

        # microprice 急跌 → 撤 bid
        if instant_drift < -threshold and self._active_bid_ids and self._active_bid_ids[0] is not None:
            bid_id = self._active_bid_ids[0]
            order = self.cache.order(bid_id)
            if order is not None and order.is_open:
                self.cancel_order(order)
            self._active_bid_ids[0] = None
            self._quoted_bid_price = None

        # microprice 急涨 → 撤 ask
        if instant_drift > threshold and self._active_ask_ids and self._active_ask_ids[0] is not None:
            ask_id = self._active_ask_ids[0]
            order = self.cache.order(ask_id)
            if order is not None and order.is_open:
                self.cancel_order(order)
            self._active_ask_ids[0] = None
            self._quoted_ask_price = None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """覆盖基类 on_bar，注入报价刷新逻辑."""
        self.log.info(repr(bar), LogColor.CYAN)

        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        if self._kill_switch:
            return

        # US-007: PnL 熔断器检查
        now = self._utc_now()
        if self._pnl_circuit_open:
            if self._pnl_cb_reset_at and now >= self._pnl_cb_reset_at:
                self._pnl_circuit_open = False
                self._pnl_cb_reset_at = None
            else:
                return
        # 清理过期成交记录
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

        # V5-US-003: 撤销 fill_prob 极低的滞留单
        self._maybe_withdraw_stale_quotes()

        mid = self._get_mid_price(bar)
        if mid is None:
            return

        # US-005: 更新已实现波动率
        self._update_realized_vol(mid)

        # V5-US-003: Fill-prob spread adjustment
        if self.config.fill_prob_spread_adj:
            fp_bid = self._calc_queue_fill_prob("BUY")
            fp_ask = self._calc_queue_fill_prob("SELL")
            fp_avg = (fp_bid + fp_ask) / 2.0
            if fp_avg < 0.2:
                self._current_spread_ticks = max(
                    float(self.config.min_spread_ticks),
                    self._current_spread_ticks - 0.5,
                )
            elif fp_avg > 0.8:
                self._current_spread_ticks = min(
                    float(self.config.max_spread_ticks),
                    self._current_spread_ticks + 0.5,
                )

        # US-008: 市场质量过滤
        if not self._quote_quality_ok:
            return

        # US-009: 成本模型过滤
        expected_profit = self._calc_expected_profit_bps(mid)
        if expected_profit < self.config.min_expected_profit_bps:
            return

        # US-002: 逆向选择检测
        adverse_side = self._check_adverse_selection(mid)
        if adverse_side is not None:
            self._adverse_cooldown_until = self._utc_now() + timedelta(
                milliseconds=self.config.adverse_selection_cooldown_ms,
            )
            self._last_fill_price = None  # 触发后重置

        in_adverse_cooldown = (
            self._adverse_cooldown_until is not None and self._utc_now() < self._adverse_cooldown_until
        )

        dir_val = self._compute_dir_val()
        self._last_dir_val = dir_val

        bid_price, ask_price, avg_shift = self._calc_quote_prices(mid, dir_val)

        clamped = self._clamp_quote_prices(bid_price, ask_price)
        if clamped is None:
            return
        bid_price, ask_price = clamped

        # V4-US-001: Queue 优化：fill_prob 低 → queue 消耗慢 → improve price 提升成交优先级
        if self.instrument is not None:
            tick_size = float(self.instrument.price_increment)
            fill_prob_bid = self._calc_queue_fill_prob("BUY")
            fill_prob_ask = self._calc_queue_fill_prob("SELL")
            if fill_prob_bid < (1.0 - self.config.queue_improve_threshold):
                bid_price += tick_size
            if fill_prob_ask < (1.0 - self.config.queue_improve_threshold):
                ask_price -= tick_size

        # US-003: 主刷新前检查即将到期的订单
        self._maybe_refresh_expiring_orders(mid)

        base_qty = self._resolve_order_quantity(bar)
        if base_qty is None:
            return

        # US-004: 缓存 base qty 供 delta 驱动报价使用
        self._last_base_qty = base_qty.as_decimal()

        # 若处于冷却期则将 adverse_side 传给数量计算
        effective_adverse = adverse_side if in_adverse_cooldown else None
        bid_qty, ask_qty = self._calc_quote_sizes(base_qty.as_decimal(), adverse_side=effective_adverse)

        # V4+: 有毒且排不到队列 → 直接撤单
        fill_prob_bid = self._calc_queue_fill_prob("BUY")
        fill_prob_ask = self._calc_queue_fill_prob("SELL")
        fill_prob_combined = (fill_prob_bid + fill_prob_ask) / 2.0
        if abs(self._toxic_flow_score) > 0.6 and fill_prob_combined < 0.3:
            self._cancel_all_quotes()
            return

        # V4: Quote Score 决策
        score = self._calc_quote_score(dir_val)
        self._last_quote_score = score

        if score < self.config.quote_score_threshold:
            self._cancel_all_quotes()
            return

        # Toxic flow 单边控制
        if self._toxic_flow_score > self.config.toxic_one_side_threshold:
            ask_qty = Decimal("0")
        elif self._toxic_flow_score < -self.config.toxic_one_side_threshold:
            bid_qty = Decimal("0")

        # 低分时扩 spread（不超过 max_spread_ticks）
        if score < 0:
            expanded = self._current_spread_ticks * 1.5
            self._current_spread_ticks = min(expanded, float(self.config.max_spread_ticks))

        if self.config.refresh_every_bar and not self.config.quote_on_delta:
            self._refresh_quotes(bid_price, ask_price, bid_qty, ask_qty, mid, avg_shift)

        # V3-US-002: 每根 bar 结束后重置 trade flow 累计量
        self._agg_buy_vol = 0.0
        self._agg_sell_vol = 0.0
        self._queue_traded_volume = 0.0

    # ------------------------------------------------------------------
    # 库存跟踪
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
    # 生命周期
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
        # US-002: 重置逆向选择状态
        self._last_fill_price = None
        self._last_fill_side = None
        self._adverse_cooldown_until = None
        # US-003: 重置提交时间
        self._bid_submit_time = None
        self._ask_submit_time = None
        # US-004: 重置 delta 报价状态
        self._last_base_qty = None
        self._last_delta_quote_ts = None
        # US-005: 重置已实现波动率
        self._price_returns.clear()
        self._last_mid_for_rv = None
        # US-007: 重置 PnL 熔断器
        self._recent_fills.clear()
        self._pnl_circuit_open = False
        self._pnl_cb_reset_at = None
        # US-008: 重置市场质量标志
        self._quote_quality_ok = True
        # V3-US-001: 重置 realized PnL 追踪
        self._open_fills.clear()
        self._last_microprice = None
        # V3-US-002: 重置 trade flow
        self._agg_buy_vol = 0.0
        self._agg_sell_vol = 0.0
        # V3-US-003: 重置报价价格
        self._quoted_bid_price = None
        self._quoted_ask_price = None
        # V4-US-001: 重置 queue position
        self._last_best_bid_size = None
        self._last_best_ask_size = None
        self._bid_queue_on_submit = None
        self._ask_queue_on_submit = None
        self._queue_traded_volume = 0.0
        # V4-US-002: 重置 toxic flow
        self._toxic_flow_score = 0.0
        self._last_fill_mid = None
        # V4-US-003: 重置 quote score
        self._last_quote_score = 0.0
        # V5-US-004: 重置 prev microprice
        self._prev_microprice = None
        # V5-US-005: 重置 last dir_val
        self._last_dir_val = 0.0
