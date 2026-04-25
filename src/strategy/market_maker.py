"""单文件主动做市商策略.

合并 alpha / inventory / quote_engine / queue_model / reduce_manager，避免多重继承 MRO 对 NautilusTrader 事件回调分发造成干扰。
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, cast

import structlog
from nautilus_trader.common.enums import LogColor
from nautilus_trader.config import PositiveInt
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType, OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import AggressorSide, OrderSide, TimeInForce
from nautilus_trader.model.events import OrderCanceled, OrderCancelRejected, OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, PositionId

from src.core.events import EventBus, SignalDirection
from src.strategy.base import BaseStrategy, BaseStrategyConfig

logger = structlog.get_logger(__name__)


class LotStatus:
    """Lot 生命周期状态."""

    OPEN = "OPEN"  # 刚由 quote fill 创建，尚未有有效 reduce
    PENDING_PROTECT = "PENDING_PROTECT"  # reduce 已提交，等待 cache / adapter 收敛
    PROTECTED = "PROTECTED"  # 已存在有效 reduce
    CLOSING = "CLOSING"  # reduce 已挂出, 部分成交中
    CLOSED = "CLOSED"  # reduce 已完全关闭


@dataclass
class InventoryLot:
    """单笔库存 Lot — 对应一次 Quote 成交."""

    lot_id: str  # 唯一标识（quote fill 的 ClientOrderId 字符串）
    quote_order_id: ClientOrderId | None  # 增量成交订单ID
    side: OrderSide  # 建仓方向（BUY=多头 lot, SELL=空头 lot）
    entry_price: float  # 建仓价格
    filled_qty: Decimal  # 初始成交量
    remaining_qty: Decimal  # 当前未平仓量
    position_id: PositionId | None = None  # Binance 双向持仓 reduce_only 需要绑定的仓位 ID
    reduce_order_id: ClientOrderId | None = None  # 对应的 Reduce 订单ID
    status: str = field(default=LotStatus.OPEN)
    reduce_version: int = 0  # 每次补挂递增，避免旧单事件串扰
    flatten_order_id: ClientOrderId | None = None
    last_flatten_submit_at: datetime | None = None
    last_reduce_submit_at: datetime | None = None

    def is_open(self) -> bool:
        """判断 Lot 是否处于开放状态（未完全平仓且未关闭）."""
        return self.status != LotStatus.CLOSED and self.remaining_qty > 0

    def mark_closed(self) -> None:
        """标记 Lot 为已关闭状态."""
        self.status = LotStatus.CLOSED
        self.remaining_qty = Decimal("0")
        self.reduce_order_id = None
        self.last_reduce_submit_at = None


class CancelReason(StrEnum):
    """撤单原因枚举."""

    PRETRADE_ADVERSE = "pretrade_adverse"  # 价格即将穿越报价，主动撤单
    TTL_REFRESH = "ttl_refresh"  # 订单临近 TTL 到期且有更优价格
    DRIFT_REFRESH = "drift_refresh"  # mid/skew 漂移超过阈值，重新报价
    SPREAD_TOO_WIDE = "spread_too_wide"  # 价差过宽，暂停报价
    STALE_LOW_FILL_PROB = "stale_low_fill_prob"  # fill_prob 极低，撤销滞留单
    TOXIC_PREEMPTIVE = "toxic_preemptive"  # microprice 急速漂移，预防性撤单
    PNL_CIRCUIT_BREAKER = "pnl_circuit_breaker"  # PnL 熔断器触发
    TOXIC_QUEUE_MISS = "toxic_queue_miss"  # 有毒流 + 排不到队列
    QUOTE_SCORE_LOW = "quote_score_low"  # 报价评分过低
    STRATEGY_STOP = "strategy_stop"  # 策略停止
    ORDER_FILLED = "order_filled"  # 成交后重置报价
    KILL_SWITCH = "kill_switch"  # 仓位超限，kill switch 触发


class QuoteState:
    """当前报价快照状态（以第一层 quote 为主）."""

    quoted_mid: float | None = None
    quoted_skew: float | None = None

    bid_price: float | None = None
    ask_price: float | None = None

    bid_submit_time: datetime | None = None
    ask_submit_time: datetime | None = None

    bid_queue_on_submit: float | None = None
    ask_queue_on_submit: float | None = None

    def reset(self) -> None:
        """重置为初始状态."""
        self.quoted_mid = None
        self.quoted_skew = None
        self.bid_price = None
        self.ask_price = None
        self.bid_submit_time = None
        self.ask_submit_time = None
        self.bid_queue_on_submit = None
        self.ask_queue_on_submit = None


class MarketMakerConfig(BaseStrategyConfig, frozen=True):
    """ActiveMarketMaker 配置."""

    instrument_id: InstrumentId
    bar_type: BarType

    # sizing
    capital_pct_per_trade: float | None = 0.5
    trade_size: Decimal = Decimal("0.06")

    # L2 订单簿
    order_book_depth: int = 10
    imbalance_decay: float = 0.3
    imbalance_weight_mode: str = "linear"  # "linear" | "exp"（配置枚举值）

    # EMA辅助过滤
    fast_ema_period: PositiveInt = 20
    slow_ema_period: PositiveInt = 60

    # 动态价差
    base_spread_ticks: int = 4
    min_spread_ticks: int = 2
    max_spread_ticks: int = 30
    spread_vol_multiplier: float = 1.0
    spread_recovery_ratio: float = 0.85  # 迟滞恢复阈值

    # 偏斜参数
    alpha_scale_ticks: float = 2.0
    alpha_tanh_k: float = 2.0
    inv_scale_ticks: float = 3.0
    inv_tanh_scale: float = 2.0

    # 仓位预算（USD notional）
    max_position_usd: float = 1000.0

    # 库存分级（基于 long/short/gross lot 聚合库存）
    soft_limit: float = 0.30
    hard_limit: float = 0.70

    # 缩量下限
    soft_size_min_ratio: float = 0.30

    # 熔断阈值（gross_ratio >= kill_switch_limit）
    kill_switch_limit: float = 1.20

    # 订单生命周期
    limit_ttl_ms: int = 120000
    post_only: bool = True
    refresh_every_bar: bool = True
    drift_ticks: int = 2
    skew_drift_ticks: int = 1
    fill_cooldown_ms: int = 500

    # 不平衡死区（不感应区）
    dead_zone_threshold: float = 0.1

    # 强制开启订单簿订阅
    subscribe_order_book: bool = True

    # US-001: 微价格（规模加权中间价）
    use_microprice: bool = True

    # US-002: 逆向选择检测
    adverse_selection_ticks: int = 3
    adverse_selection_cooldown_ms: int = 2000

    # US-003: 订单队列感知（GTD刷新）
    order_refresh_ratio: float = 0.7

    # US-004: 三角洲驱动报价
    quote_on_delta: bool = False
    delta_quote_min_interval_ms: int = 100

    # US-005: 已实现波动率
    use_realized_vol: bool = False
    rv_window: int = 20

    # US-006: 分层报价
    quote_layers: int = 1
    layer_spread_step_ticks: float = 1.0
    layer_size_decay: float = 0.7

    # US-007: PNL速度熔断器
    max_loss_usd: float = 20.0
    loss_window_ms: int = 60000
    pnl_cb_cooldown_ms: int = 120000

    # US-008: 市场质量过滤
    max_book_spread_ticks: float = 20.0
    imbalance_spike_threshold: float = 0.9

    # US-009: 成本模型——最低预期收益
    min_expected_profit_bps: float = 1.0
    maker_fee_bps: float = 0.0
    adverse_cost_bps: float = 1.0

    # Reduce 防重复补挂窗口
    reduce_submit_grace_ms: int = 5000

    # V3-US-002: 成交流 阿尔法
    subscribe_trades: bool = True

    # V3-US-003: 交易前逆向撤单
    pretrade_cancel_ticks: float = 0.5

    # V3-US-004: 微价格深度利用
    microprice_skew_scale: float = 1.0

    # V3-US-005: 库存分层控制
    one_side_only_limit: float = 0.85
    deeper_layer_inv_threshold: float = 0.5

    # V4-US-001: 队列位置
    queue_improve_threshold: float = 0.7

    # V4-US-002: 有毒流
    toxic_decay: float = 0.9

    # V4-US-003: 报价质量分
    quote_score_threshold: float = -0.5
    toxic_one_side_threshold: float = 0.5

    # V5-US-001: 微价格 阿尔法驱动
    mp_alpha_weight: float = 0.5
    imbalance_weight: float = 0.3

    # V5-US-002: 非对称买/卖 阿尔法
    mp_bias_strength: float = 0.3

    # V5-US-003: 成交概率驱动执行
    withdraw_fill_prob_threshold: float = 0.05
    fill_prob_spread_adj: bool = True

    # V5-US-004: 有毒流预防性撤单
    toxic_mp_drift_ticks: float = 1.5

    # V5-US-005: 非对称分层报价
    asymmetric_layers: bool = True

    # Reduce/TP 池
    tp_pct: float = 0.001  # TP 百分比（0.1%）
    reduce_post_only: bool = False  # reduce 单是否 post_only（默认否，允许吃单）


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

        # 确保 ATR已创建，用于动态价差
        self._ensure_atr_indicator()

        # 报价状态
        self._quote_state = QuoteState()
        # L2 不平衡状态
        self._smooth_imbalance: float = 0.0  # 范围 [-1, 1]

        # 动态价差 状态
        self._current_spread_ticks: float = float(config.base_spread_ticks)
        self._quote_suspended: bool = False

        # 活跃报价订单 ID — US-006 分层报价
        self._active_bid_ids: list[ClientOrderId | None] = []
        self._active_ask_ids: list[ClientOrderId | None] = []
        # Quote 池订单来源标记
        self._quote_order_ids: set[ClientOrderId] = set()

        # 撤单原因追踪
        self._pending_cancel_reasons: dict[ClientOrderId, CancelReason] = {}

        # 成交冷却状态
        self._last_fill_ts: datetime | None = None

        # 熔断开关状态
        self._kill_switch: bool = False

        # Reduce/TP 池 — lot-based 库存追踪
        self._inventory_lots: dict[str, InventoryLot] = {}
        self._reduce_to_lot: dict[ClientOrderId, str] = {}
        self._lot_seq: int = 0

        # US-002: 逆向选择
        self._last_fill_price: float | None = None
        self._last_fill_side: OrderSide | None = None  # "BUY" 或 "SELL"
        self._adverse_cooldown_until: datetime | None = None

        # US-004: 三角洲驱动报价
        self._last_delta_quote_ts: datetime | None = None
        self._last_base_qty: Decimal | None = None

        # US-005: 已实现波动率
        self._price_returns: deque[float] = deque(maxlen=config.rv_window)
        self._last_mid_for_rv: float | None = None

        # US-007: PNL速度熔断器
        self._recent_fills: deque[tuple[datetime, float]] = deque()
        self._pnl_circuit_open: bool = False
        self._pnl_cb_reset_at: datetime | None = None

        # US-008: 市场质量过滤
        self._quote_quality_ok: bool = True

        # V3-US-001: microprice
        self._last_microprice: float | None = None

        # V3-US-002: 成交流 阿尔法
        self._agg_buy_vol: float = 0.0
        self._agg_sell_vol: float = 0.0

        # V4-US-001: 队列位置
        self._last_best_bid_size: float | None = None
        self._last_best_ask_size: float | None = None

        # V4-US-001: 队列位置快照
        self._bid_queue_consumed: float = 0.0
        self._ask_queue_consumed: float = 0.0

        # 成交事件幂等保护
        self._processed_fill_keys: set[str] = set()

        # V4-US-002: 有毒流
        self._toxic_flow_score: float = 0.0
        self._last_fill_mid: float | None = None

        # V4-US-003: 报价质量分
        self._last_quote_score: float = 0.0

        # V5-US-004: 有毒流预防性撤单
        self._prev_microprice: float | None = None

        # V5-US-005: 非对称分层报价
        self._last_dir_val: float = 0.0

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

    def _register_indicators(self) -> None:
        self.register_indicator_for_bars(self.config.bar_type, self._fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self._slow_ema)

    def _history_warmup_bars(self) -> int:
        return max(int(self.config.fast_ema_period), int(self.config.slow_ema_period), int(self.config.atr_period)) + 2

    def on_bar(self, bar: Bar) -> None:
        """覆盖基类 on_bar，注入报价刷新逻辑.

        Args:
            bar: 当前 bar.
        """
        self.log.info(repr(bar), LogColor.CYAN)

        if not self.indicators_initialized():
            self.log.info("Indicators not initialized（指标未初始化）, skipping bar", LogColor.YELLOW)
            return

        if bar.is_single_price():
            self.log.info("Single price bar(只有价格), skipping", LogColor.YELLOW)
            return

        if self._kill_switch:
            self._prune_reduce_orders()
            self._ensure_all_open_lots_protected()
            self._flatten_all_lots()
            self.log.info(
                "Kill switch active（kill switch 激活）, managing reduce/flatten only",
                LogColor.YELLOW,
            )
            return

        # US-007: PNL熔断器检查
        now = self._utc_now()
        if self._pnl_circuit_open:
            if self._pnl_cb_reset_at and now >= self._pnl_cb_reset_at:
                self._pnl_circuit_open = False
                self._pnl_cb_reset_at = None
            else:
                self.log.info("PNL circuit breaker open（pnl 熔断器打开）, skipping bar", LogColor.YELLOW)
                return
        # 清理过期成交记录
        cutoff = now - timedelta(milliseconds=self.config.loss_window_ms)
        while self._recent_fills and self._recent_fills[0][0] < cutoff:
            self._recent_fills.popleft()
        window_pnl = sum(p for _, p in self._recent_fills)
        if window_pnl < -self.config.max_loss_usd:
            self._pnl_circuit_open = True
            self._pnl_cb_reset_at = now + timedelta(milliseconds=self.config.pnl_cb_cooldown_ms)
            self._cancel_all_quotes(CancelReason.PNL_CIRCUIT_BREAKER)
            self.log.error(
                f"PnL circuit breaker opened（pnl 熔断器打开）: {window_pnl:.2f} USD loss in window",
                color=LogColor.RED,
            )
            return

        if self._last_fill_ts is not None:
            elapsed_ms = (self._utc_now() - self._last_fill_ts).total_seconds() * 1000
            if elapsed_ms < self.config.fill_cooldown_ms:
                self.log.info("Fill cooldown not met（fill 冷却未满足）, skipping bar", LogColor.YELLOW)
                return

        self._bar_index += 1
        self._update_dynamic_spread()

        if self._quote_suspended:
            self._prune_reduce_orders()
            self._ensure_all_open_lots_protected()
            self.log.info("Quote suspended（quote 挂起）, skipping bar", LogColor.YELLOW)
            return

        # V5-US-003: 撤销 fill_prob 极低的滞留单
        self._maybe_withdraw_stale_quotes()

        mid = self._get_mid_price(bar)
        if mid is None:
            self._prune_reduce_orders()
            self._ensure_all_open_lots_protected()
            self.log.info("Mid price not available（mid 价格不可用）, skipping bar", LogColor.YELLOW)
            return

        self._prune_reduce_orders()
        self._ensure_all_open_lots_protected()

        # US-005: 更新已实现波动率
        self._update_realized_vol(mid)

        # V5-US-003: 成交概率价差调整
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
            self.log.info("Quote quality not ok（quote 质量不优）, skipping bar", LogColor.YELLOW)
            return

        # US-009: 成本模型过滤
        expected_profit = self._calc_expected_profit_bps(mid)
        if expected_profit < self.config.min_expected_profit_bps:
            self.log.info("Expected profit too low（预期收益太低）, skipping bar", LogColor.YELLOW)
            return

        # US-002: 逆向选择检测
        adverse_side = self._check_adverse_selection(mid)
        if adverse_side is not None:
            self._adverse_cooldown_until = self._utc_now() + timedelta(
                milliseconds=self.config.adverse_selection_cooldown_ms,
            )
            self._last_fill_price = None  # 触发后重置

        in_adverse_cooldown = self._adverse_cooldown_until is not None and self._utc_now() < self._adverse_cooldown_until

        dir_val = self._compute_dir_val()
        self._last_dir_val = dir_val

        bid_price, ask_price, avg_shift = self._calc_quote_prices(mid, dir_val)

        clamped = self._clamp_quote_prices(bid_price, ask_price)
        if clamped is None:
            self.log.info("Clamp quote prices failed（clamp quote 价格失败）, skipping bar", LogColor.YELLOW)
            return
        bid_price, ask_price = clamped

        # V4-US-001: 队列优化：成交概率低 → 队列消耗慢 → 改善价格以提升成交优先级
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
            self.log.info("Resolve order quantity failed（resolve order 数量失败）, skipping bar", LogColor.YELLOW)
            return

        # US-004: 缓存基础数量供 三角洲驱动报价使用
        self._last_base_qty = base_qty.as_decimal()

        # 若处于冷却期则将 adverse_side 传给数量计算
        effective_adverse = adverse_side if in_adverse_cooldown else None
        bid_qty, ask_qty = self._calc_quote_sizes(base_qty.as_decimal(), adverse_side=effective_adverse)

        # V4+: 有毒且排不到队列 → 直接撤单
        fill_prob_bid = self._calc_queue_fill_prob("BUY")
        fill_prob_ask = self._calc_queue_fill_prob("SELL")
        fill_prob_combined = (fill_prob_bid + fill_prob_ask) / 2.0
        if abs(self._toxic_flow_score) > 0.6 and fill_prob_combined < 0.3:
            self._cancel_all_quotes(CancelReason.TOXIC_QUEUE_MISS)
            self.log.info("Toxic queue miss（toxic 队列 miss）, skipping bar", LogColor.YELLOW)
            return

        # V4: 报价分决策
        score = self._calc_quote_score(dir_val)
        self._last_quote_score = score

        if score < self.config.quote_score_threshold:
            self._cancel_all_quotes(CancelReason.QUOTE_SCORE_LOW)
            self.log.info("Quote score low（quote 分数低）, skipping bar", LogColor.YELLOW)
            return

        # 有毒流单边控制
        if self._toxic_flow_score > self.config.toxic_one_side_threshold:
            ask_qty = Decimal("0")

        elif self._toxic_flow_score < -self.config.toxic_one_side_threshold:
            bid_qty = Decimal("0")

        # 低分时扩大价差（不超过 max_spread_ticks）
        if score < 0:
            expanded = self._current_spread_ticks * 1.5
            self._current_spread_ticks = min(expanded, float(self.config.max_spread_ticks))

        if self.config.refresh_every_bar and not self.config.quote_on_delta:
            self._refresh_quotes(
                bid_price,
                ask_price,
                bid_qty,
                ask_qty,
                mid,
                avg_shift,
            )

        # V3-US-002: 每根 K 线结束后重置成交流累计量
        self._agg_buy_vol = 0.0
        self._agg_sell_vol = 0.0
        self._bid_queue_consumed = 0.0
        self._ask_queue_consumed = 0.0

    def on_stop(self) -> None:
        """停止策略时以 ActiveMarketMaker 自有订单池收敛，避免重复撤单/平仓."""
        self._cancel_all_orders(CancelReason.STRATEGY_STOP)
        if self.config.close_positions_on_stop:
            self._flatten_all_lots()
        self._sl_orders.clear()
        self._tp_orders.clear()
        self.unsubscribe_bars(self.config.bar_type)

    def on_reset(self) -> None:
        """重置策略状态."""
        super().on_reset()
        self._fast_ema.reset()
        self._slow_ema.reset()
        self._quote_state.reset()
        if self._atr_indicator is not None:
            self._atr_indicator.reset()
        self._smooth_imbalance = 0.0
        self._current_spread_ticks = float(self.config.base_spread_ticks)
        self._quote_suspended = False
        self._active_bid_ids = []
        self._active_ask_ids = []
        self._quote_order_ids.clear()
        self._pending_cancel_reasons.clear()
        self._inventory_lots.clear()
        self._reduce_to_lot.clear()
        self._last_fill_ts = None
        self._kill_switch = False
        # US-002: 重置逆向选择状态
        self._last_fill_price = None
        self._last_fill_side = None
        self._adverse_cooldown_until = None
        # US-004: 重置 三角洲报价状态
        self._last_base_qty = None
        self._last_delta_quote_ts = None
        # US-005: 重置已实现波动率
        self._price_returns.clear()
        self._last_mid_for_rv = None
        # US-007: 重置 PNL熔断器
        self._recent_fills.clear()
        self._pnl_circuit_open = False
        self._pnl_cb_reset_at = None
        # US-008: 重置市场质量标志
        self._quote_quality_ok = True
        # V3-US-001: 重置 microprice
        self._last_microprice = None
        # V3-US-002: 重置成交流
        self._agg_buy_vol = 0.0
        self._agg_sell_vol = 0.0

        # V4-US-001: 重置队列位置
        self._last_best_bid_size = None
        self._last_best_ask_size = None

        self._bid_queue_consumed = 0.0
        self._ask_queue_consumed = 0.0
        self._processed_fill_keys.clear()
        # V4-US-002: 重置有毒流
        self._toxic_flow_score = 0.0
        self._last_fill_mid = None
        # V4-US-003: 重置报价分
        self._last_quote_score = 0.0
        # V5-US-004: 重置前一微价格
        self._prev_microprice = None
        # V5-US-005: 重置上一方向值
        self._last_dir_val = 0.0

    def on_order_event(self, event: object) -> None:
        """兜底订单事件入口，用于捕获因竞态未能通过具体回调分发的成交事件.

        NautilusTrader 保证在调用具体回调（on_order_filled 等）之后，再调用此方法，
        因此不会造成重复分发。此处仅对 OrderFilled 做幂等补偿处理，其余类型
        仅做日志记录用于诊断。
        """
        if isinstance(event, OrderFilled):
            fill_key = self._fill_key(event)
            if fill_key not in self._processed_fill_keys:
                self.log.warning(
                    f"OrderFilled caught via on_order_event fallback (missed by on_order_filled): {event}",
                    color=LogColor.YELLOW,
                )
                self.on_order_filled(event)

    def on_event(self, event: object) -> None:
        """最外层事件观测入口，仅用于诊断."""
        self.log.debug(f"EVENT_RECEIVED type={type(event).__name__}: {event}", color=LogColor.CYAN)

    _last_best_ask_size: float | None
    _last_best_bid_size: float | None
    _last_fill_mid: float | None
    _last_microprice: float | None
    _last_mid_for_rv: float | None
    _prev_microprice: float | None

    def on_trade_tick(self, trade: TradeTick) -> None:
        """处理逐笔成交，追踪主动买卖量，并按方向估算队列消耗."""
        qty = float(trade.size)

        if trade.aggressor_side == AggressorSide.BUYER:
            self._agg_buy_vol += qty
            # 主动买打 ask，消耗 ask 队列
            self._ask_queue_consumed += qty
        elif trade.aggressor_side == AggressorSide.SELLER:
            self._agg_sell_vol += qty
            # 主动卖打 bid，消耗 bid 队列
            self._bid_queue_consumed += qty

        # V4-US-002: 更新有毒流分数
        self._update_toxic_flow(trade)

    def _calc_trade_flow_signal(self) -> float:
        """计算 trade flow 信号：(buy_vol - sell_vol) / (buy_vol + sell_vol + ε)."""
        total = self._agg_buy_vol + self._agg_sell_vol
        if total < 1e-10:
            return 0.0
        return (self._agg_buy_vol - self._agg_sell_vol) / total

    def _update_toxic_flow(self, trade: TradeTick) -> None:
        """基于 microprice drift 更新 toxic flow 分数（微观结构版）."""
        # 使用微价格而非中间价，响应更快
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

    def _calc_quote_score(self, dir_val: float) -> float:
        """统一报价质量评分:alpha + fill_prob - 库存惩罚 - toxic 惩罚 - queue 惩罚.

        Args:
            dir_val: 方向值.

        Returns:
            报价质量评分: float.
        """
        alpha = abs(dir_val)
        inv = self._inventory_snapshot()
        inv_ratio = max(inv["gross_ratio"], abs(inv["imbalance"]))
        toxic = abs(self._toxic_flow_score)
        fill_prob_bid = self._calc_queue_fill_prob("BUY")
        fill_prob_ask = self._calc_queue_fill_prob("SELL")
        fill_prob = (fill_prob_bid + fill_prob_ask) / 2.0
        queue_penalty = 1.0 - fill_prob
        return alpha + fill_prob * 1.2 - inv_ratio * 0.8 - toxic * 1.5 - queue_penalty

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        """处理 orderbook delta 更新，刷新加权 imbalance."""
        self._calc_weighted_imbalance()

        # V4-US-001: 队列位置 — 追踪最优买/卖量
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

        # V3-US-003: 交易前逆向撤单
        self._check_pretrade_cancel()

        # V5-US-004: 有毒流预防性撤单
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

        # US-004: 三角洲驱动报价
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
        # 线性模式：weight[i] = (n-i)/n（线性加权）
        return [(n - i) / n for i in range(n)]

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
                        # 回退：简单中间价
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

    def _calc_expected_profit_bps(self, mid: float) -> float:
        """计算 quote 预期收益（单位 bps）.

        注意：
        - quote 默认 post_only，应按 maker 成本计算；
        - reduce 是否吃单，属于平仓成本，不应直接过滤开仓 quote。
        """
        tick = 1.0
        if self.instrument is not None:
            tick = float(self.instrument.price_increment)
        if tick <= 0 or mid <= 0:
            return 0.0

        spread_price = self._current_spread_ticks * tick
        half_spread_bps = (spread_price / mid) * 10000 / 2.0

        return half_spread_bps - float(self.config.maker_fee_bps) - float(self.config.adverse_cost_bps)

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
        if self._last_fill_side == OrderSide.BUY and drift < -threshold:
            return "BUY"
        if self._last_fill_side == OrderSide.SELL and drift > threshold:
            return "SELL"
        return None

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        """基于连续 imbalance 生成方向性信号，用于信号总线兼容.

        Args:
            bar: 当前 bar.

        Returns:
            SignalDirection | None: 方向性信号.
            LONG: 多头方向.
            SHORT: 空头方向.
            None: 无方向.
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

        # 微价格不可用时降级：只用不平衡量 + 成交流
        if self._last_microprice is None:
            total = imb_w + tf_w
            raw = (imb * imb_w + tf * tf_w) / total if total > 0 else 0.0

        # EMA方向门控：方向不一致时衰减一半
        if self._fast_ema.initialized and self._slow_ema.initialized:
            ema_bull = float(self._fast_ema.value) > float(self._slow_ema.value)
            ema_bear = float(self._fast_ema.value) < float(self._slow_ema.value)
            if (raw > 0 and ema_bear) or (raw < 0 and ema_bull):
                raw *= 0.5

        return raw

    _last_fill_price: float | None
    _last_fill_side: OrderSide | None

    def _inventory_snapshot(self) -> dict[str, float]:
        """基于 open lots 计算库存快照.

        Returns:
            dict[str, float]:
                long_ratio: 多头总名义价值 / max_position_usd
                short_ratio: 空头总名义价值 / max_position_usd
                gross_ratio: (long + short) / max_position_usd
                imbalance: (long - short) / gross, 范围约 [-1, 1]
                long_usd: 多头总名义价值
                short_usd: 空头总名义价值
                gross_usd: 多空总名义价值
        """
        long_usd = 0.0
        short_usd = 0.0
        long_qty = 0.0
        short_qty = 0.0

        for lot in self._inventory_lots.values():
            if not lot.is_open():
                continue

            qty = float(lot.remaining_qty)
            if qty <= 0:
                continue

            notional = qty * float(lot.entry_price)
            if lot.side == OrderSide.BUY:
                long_usd += notional
                long_qty += qty
            else:
                short_usd += notional
                short_qty += qty

        gross_usd = long_usd + short_usd
        max_pos = max(float(self.config.max_position_usd), 1.0)

        long_ratio = long_usd / max_pos
        short_ratio = short_usd / max_pos
        gross_ratio = gross_usd / max_pos

        imbalance = (long_usd - short_usd) / gross_usd if gross_usd > 1e-9 else 0.0

        return {
            "long_ratio": long_ratio,
            "short_ratio": short_ratio,
            "gross_ratio": gross_ratio,
            "imbalance": imbalance,
            "long_usd": long_usd,
            "short_usd": short_usd,
            "gross_usd": gross_usd,
            "long_qty": long_qty,
            "short_qty": short_qty,
        }

    def _calc_quote_sizes(self, base_qty: Decimal, adverse_side: str | None = None) -> tuple[Decimal, Decimal]:
        """双向持仓下的 bid/ask 数量控制.

        规则：
        - 多仓越大，越压缩 bid（避免继续加多）
        - 空仓越大，越压缩 ask（避免继续加空）
        - gross 越大，双边都缩量
        - 某一侧库存过重时，仅关闭该侧开仓能力

        Args:
            base_qty: 基础下单数量.
            adverse_side: 若为 "BUY" 则禁 bid；若为 "SELL" 则禁 ask.

        Returns:
            tuple[Decimal, Decimal]:
                (bid_qty, ask_qty)
        """
        if self.instrument is None:
            return base_qty, base_qty

        step = float(self.instrument.size_increment)
        if step <= 0:
            step = 1e-8

        def round_to_step(val: float) -> Decimal:
            floored = math.floor(max(val, 0.0) / step) * step
            return Decimal(str(max(floored, 0.0)))

        def smooth_scale(r: float, soft: float, hard: float, min_ratio: float) -> float:
            if r <= soft:
                return 1.0
            if r >= hard:
                return min_ratio
            t = (r - soft) / max(hard - soft, 1e-9)
            return 1.0 - (t**2) * (1.0 - min_ratio)

        inv = self._inventory_snapshot()
        long_ratio = inv["long_ratio"]
        short_ratio = inv["short_ratio"]
        gross_ratio = inv["gross_ratio"]

        base_f = float(base_qty)

        # 总风险越高，双边都缩
        gross_scale = smooth_scale(
            gross_ratio,
            self.config.soft_limit,
            self.config.hard_limit,
            self.config.soft_size_min_ratio,
        )

        # 单边库存越重，压缩该边继续开仓能力
        bid_scale = gross_scale * smooth_scale(
            long_ratio,
            self.config.soft_limit,
            self.config.hard_limit,
            self.config.soft_size_min_ratio,
        )
        ask_scale = gross_scale * smooth_scale(
            short_ratio,
            self.config.soft_limit,
            self.config.hard_limit,
            self.config.soft_size_min_ratio,
        )

        bid_qty = round_to_step(base_f * bid_scale)
        ask_qty = round_to_step(base_f * ask_scale)

        # 单边过重：只禁止继续朝该方向加仓
        if long_ratio >= self.config.one_side_only_limit:
            bid_qty = Decimal("0")
        if short_ratio >= self.config.one_side_only_limit:
            ask_qty = Decimal("0")

        # alpha 轻度倾斜
        if self._last_dir_val > self.config.dead_zone_threshold:
            bid_qty = round_to_step(float(bid_qty) * 1.15)
            ask_qty = round_to_step(float(ask_qty) * 0.85)
        elif self._last_dir_val < -self.config.dead_zone_threshold:
            bid_qty = round_to_step(float(bid_qty) * 0.85)
            ask_qty = round_to_step(float(ask_qty) * 1.15)

        # 逆向选择侧禁开
        if adverse_side == "BUY":
            bid_qty = Decimal("0")
        elif adverse_side == "SELL":
            ask_qty = Decimal("0")

        return bid_qty, ask_qty

    def _fill_key(self, event: OrderFilled) -> str:
        """构造成交事件幂等 key，避免重复 fill 造成重复 lot / reduce."""
        return (
            f"{getattr(event, 'client_order_id', None)}:"
            f"{getattr(event, 'venue_order_id', None)}:"
            f"{getattr(event, 'trade_id', None)}:"
            f"{getattr(event, 'ts_event', None)}:"
            f"{event.last_qty}:"
            f"{event.last_px}"
        )

    def on_order_filled(self, event: OrderFilled) -> None:
        """处理订单成交事件."""
        self.log.info(f"ORDER_FILLED (订单成交): {event}", color=LogColor.CYAN)
        fill_key = self._fill_key(event)
        if fill_key in self._processed_fill_keys:
            self.log.warning(f"Duplicate fill ignored: {fill_key}", color=LogColor.YELLOW)
            return
        self._processed_fill_keys.add(fill_key)

        self._last_fill_ts = self._utc_now()

        fill_price = float(event.last_px)
        fill_qty = float(event.last_qty)
        self._last_fill_price = fill_price
        self._last_fill_side = event.order_side

        client_order_id = getattr(event, "client_order_id", None)

        # 1) Reduce fill
        if client_order_id is not None and client_order_id in self._reduce_to_lot:
            lot_id = self._reduce_to_lot[client_order_id]
            lot = self._inventory_lots.get(lot_id)
            self.log.info(f"REDUCE_FILL (止盈减仓订单成交): {event}", color=LogColor.CYAN)
            if lot is not None:
                self._handle_reduce_fill(event, lot)

            # reduce 成交后重新检查 lot 聚合风险
            self._check_lot_risk()
            return

        # 2) 只有 quote 池订单的 fill 才创建 lot
        is_quote_fill = client_order_id is not None and client_order_id in self._quote_order_ids
        if not is_quote_fill:
            self.log.info(
                f"Non-quote fill ignored for lot creation: oid={client_order_id} side={event.order_side.name} "
                f" qty={fill_qty:.8f} px={fill_price:.8f}",
                color=LogColor.YELLOW,
            )
            return

        # 3) Quote fill
        self.log.info(
            f"Quote filled: oid={client_order_id} side={event.order_side.name} qty={fill_qty:.8f} px={fill_price:.8f}",
            color=LogColor.YELLOW,
        )

        # quote 成交后，可撤 quote 池避免继续被动吃货；reduce 池绝不跟着撤
        if client_order_id is not None and self._quote_fill_completes_order(client_order_id, event):
            self._clear_quote_order_state(client_order_id)

        self._cancel_all_quotes(CancelReason.ORDER_FILLED)

        # 当前 oid 已成交，移出 quote 来源集合
        self._quote_order_ids.discard(client_order_id)

        lot = self._create_lot(event)
        self.log.info(f"QUOTE_FILLED (quote 订单成交),创建lot {lot}: {event}", color=LogColor.CYAN)
        self._place_reduce_order(lot)

        # m2m proxy pnl
        mid = self._last_microprice or 0.0
        if mid > 0:
            sign = 1.0 if event.order_side == OrderSide.BUY else -1.0
            proxy_pnl = (mid - fill_price) * fill_qty * sign
            self._recent_fills.append((self._utc_now(), proxy_pnl))

        # toxic flow
        mid_now = self._last_microprice
        if mid_now is not None:
            drift = mid_now - fill_price
            if event.order_side == OrderSide.BUY and drift < 0:
                self._toxic_flow_score = max(-1.0, self._toxic_flow_score - 0.3)
            elif event.order_side == OrderSide.SELL and drift > 0:
                self._toxic_flow_score = min(1.0, self._toxic_flow_score + 0.3)

        self._check_lot_risk()

    def _check_lot_risk(self) -> None:
        """基于 lot 聚合风险检查 kill switch."""
        inv = self._inventory_snapshot()

        self.log.info(
            "inventory_state "
            f"long_usd={inv['long_usd']:.4f} "
            f"short_usd={inv['short_usd']:.4f} "
            f"gross_usd={inv['gross_usd']:.4f} "
            f"long_ratio={inv['long_ratio']:.4f} "
            f"short_ratio={inv['short_ratio']:.4f} "
            f"gross_ratio={inv['gross_ratio']:.4f} "
            f"open_lots={sum(1 for lot in self._inventory_lots.values() if lot.is_open())}",
            color=LogColor.YELLOW,
        )

        if inv["gross_ratio"] >= self.config.kill_switch_limit and not self._kill_switch:
            self._kill_switch = True
            self._cancel_all_orders(CancelReason.KILL_SWITCH)
            self.log.error(
                f"Kill switch activated: gross_ratio={inv['gross_ratio']:.2f} >= {self.config.kill_switch_limit}",
                color=LogColor.RED,
            )
            self._flatten_all_lots()
        elif inv["gross_ratio"] < self.config.hard_limit and self._kill_switch:
            self._kill_switch = False
            self.log.info("Kill switch reset", color=LogColor.GREEN)

    def _flatten_all_lots(self) -> None:
        """按 open lots 逐笔反向市价平仓.

        说明：
        - 先撤 quote + reduce，避免与主动平仓打架
        - 再逐个 open lot 发 reduce_only 市价单
        - 不直接删除 lot，仍由成交回报驱动 lot 收敛
        """
        if self.instrument is None:
            return

        flattened = 0
        for lot in list(self._inventory_lots.values()):
            if not lot.is_open():
                continue

            qty = float(lot.remaining_qty)
            if qty <= 0:
                continue

            try:
                qty_obj = self.instrument.make_qty(qty)
                order_side = OrderSide.SELL if lot.side == OrderSide.BUY else OrderSide.BUY
                position_id = self._resolve_lot_position_id(lot)
                if position_id is None:
                    self.log.error(f"Skip flatten without position_id: lot={lot.lot_id}", color=LogColor.RED)
                    continue

                now = self._utc_now()

                if lot.flatten_order_id is not None and lot.last_flatten_submit_at is not None:
                    elapsed_ms = (now - lot.last_flatten_submit_at).total_seconds() * 1000
                    if elapsed_ms < self.config.reduce_submit_grace_ms:
                        continue

                order = self.order_factory.market(
                    instrument_id=self.config.instrument_id,
                    order_side=order_side,
                    quantity=qty_obj,
                    reduce_only=True,
                )
                self.submit_order(order, position_id=position_id)

                lot.flatten_order_id = order.client_order_id
                lot.last_flatten_submit_at = now
                lot.status = LotStatus.CLOSING

                flattened += 1
            except Exception as e:
                self.log.error(f"Failed to flatten lot {lot.lot_id}: {e}", color=LogColor.RED)

        if flattened > 0:
            self.log.warning(f"Flatten requested for {flattened} open lots", color=LogColor.YELLOW)

    def _estimate_queue_ahead(self, side: str) -> float:
        """估算当前最优价位前方排队量."""
        if side == "BUY":
            return self._last_best_bid_size or 0.0
        return self._last_best_ask_size or 0.0

    def _calc_queue_fill_prob(self, side: str) -> float:
        """下单后的队列消耗估算成交概率.

        BUY quote 排在 bid 队列，主要由主动卖成交消耗；
        SELL quote 排在 ask 队列，主要由主动买成交消耗。
        """
        qs = self._quote_state

        if side == "BUY":
            initial = qs.bid_queue_on_submit
            consumed = self._bid_queue_consumed
        else:
            initial = qs.ask_queue_on_submit
            consumed = self._ask_queue_consumed

        if initial is None or initial <= 0:
            return 1.0

        return min(consumed / initial, 1.0)

    def _cancel_order_with_reason(self, order: Any, reason: CancelReason) -> None:
        """记录撤单原因后发送撤单请求."""
        client_order_id = order.client_order_id
        if client_order_id in self._pending_cancel_reasons:
            self.log.debug(
                f"Skip duplicate cancel request: client_order_id={client_order_id} "
                f"reason={self._pending_cancel_reasons[client_order_id].value}",
            )
            return

        self._pending_cancel_reasons[client_order_id] = reason
        self.cancel_order(order)

    def _check_pretrade_cancel(self) -> None:
        """检测价格即将穿越报价时主动撤单（pre-trade adverse cancel）.

        只发送撤单请求，由 on_order_canceled 回调清理 ID 和状态，
        避免异步撤单确认前 ID 被置空导致 _refresh_quotes 提交重复订单。
        """
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

        qs = self._quote_state
        if qs.bid_price is not None and ba <= qs.bid_price + threshold and self._active_bid_ids:
            bid_id = self._active_bid_ids[0]
            if bid_id is not None:
                order = self.cache.order(bid_id)
                if order is not None and order.is_open and not order.is_pending_cancel:
                    self._cancel_order_with_reason(order, CancelReason.PRETRADE_ADVERSE)

        if qs.ask_price is not None and bb >= qs.ask_price - threshold and self._active_ask_ids:
            ask_id = self._active_ask_ids[0]
            if ask_id is not None:
                order = self.cache.order(ask_id)
                if order is not None and order.is_open and not order.is_pending_cancel:
                    self._cancel_order_with_reason(order, CancelReason.PRETRADE_ADVERSE)

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
                self._cancel_all_quotes(CancelReason.SPREAD_TOO_WIDE)
            self._quote_suspended = True
            return

        # 迟滞恢复：仅当价差降至恢复比例以下时才重新报价
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

    def _calc_quote_prices(self, mid: float, dir_val: float) -> tuple[float, float, float]:
        """双向持仓下的 side-aware bid/ask 报价价格.

        Args:
            mid: 当前 mid price.
            dir_val: 方向值.

        Returns:
            tuple[float, float, float]: 报价价格.
            bid: 买一价.
            ask: 卖一价.
            avg_shift: 平均偏移.
        """
        tick = 1.0
        if self.instrument is not None and hasattr(self.instrument, "price_increment"):
            tick = float(self.instrument.price_increment)
        if tick <= 0:
            tick = 1.0

        inv = self._inventory_snapshot()
        long_ratio = inv["long_ratio"]
        short_ratio = inv["short_ratio"]
        gross_ratio = inv["gross_ratio"]

        half_spread = self._current_spread_ticks * tick / 2.0
        alpha_shift = math.tanh(dir_val * float(self.config.alpha_tanh_k)) * float(self.config.alpha_scale_ticks) * tick
        alpha_weight = max(0.15, 1.0 - gross_ratio)

        bid_inv_skew = math.tanh(long_ratio * self.config.inv_tanh_scale) * float(self.config.inv_scale_ticks) * tick
        ask_inv_skew = math.tanh(short_ratio * self.config.inv_tanh_scale) * float(self.config.inv_scale_ticks) * tick
        gross_widen = math.tanh(gross_ratio * 1.5) * 1.5 * tick

        mp_shift = 0.0
        if self.config.use_microprice and self._last_microprice is not None:
            tick_val = float(self.instrument.price_increment) if self.instrument is not None else 1.0
            mp_bias_ticks = (self._last_microprice - mid) / tick_val if tick_val > 0 else 0.0
            mp_shift = mp_bias_ticks * float(self.config.microprice_skew_scale) * tick_val

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

        bid_shift = alpha_weight * alpha_shift * bid_alpha_mult + mp_shift - bid_inv_skew
        ask_shift = alpha_weight * alpha_shift * ask_alpha_mult + mp_shift + ask_inv_skew
        bid = mid - half_spread - gross_widen + bid_shift
        ask = mid + half_spread + gross_widen + ask_shift
        avg_shift = (bid_shift + ask_shift) / 2.0
        return bid, ask, avg_shift

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

    def _maybe_refresh_expiring_orders(self, mid: float) -> None:
        """若订单临近 TTL 到期且有更优价格，则提前刷新.

        Args:
            mid: 当前 mid price.
        """
        if not self._active_bid_ids and not self._active_ask_ids:
            return
        has_top_bid = bool(self._active_bid_ids and self._active_bid_ids[0] is not None)
        has_top_ask = bool(self._active_ask_ids and self._active_ask_ids[0] is not None)
        if not has_top_bid and not has_top_ask:
            return

        now = self._utc_now()
        ttl = timedelta(milliseconds=self.config.limit_ttl_ms)
        refresh_threshold = ttl * self.config.order_refresh_ratio

        dir_val = self._compute_dir_val()
        optimal_bid, optimal_ask, _ = self._calc_quote_prices(mid, dir_val)

        qs = self._quote_state
        if (
            qs.bid_submit_time is not None
            and self._active_bid_ids
            and self._active_bid_ids[0] is not None
            and (now - qs.bid_submit_time) > refresh_threshold
        ):
            order = self.cache.order(self._active_bid_ids[0])
            if order is not None and order.is_open and not order.is_pending_cancel:
                current_price = float(order.price)
                if current_price < optimal_bid:
                    self._cancel_order_with_reason(order, CancelReason.TTL_REFRESH)

        if (
            qs.ask_submit_time is not None
            and self._active_ask_ids
            and self._active_ask_ids[0] is not None
            and (now - qs.ask_submit_time) > refresh_threshold
        ):
            order = self.cache.order(self._active_ask_ids[0])
            if order is not None and order.is_open and not order.is_pending_cancel:
                current_price = float(order.price)
                if current_price > optimal_ask:
                    self._cancel_order_with_reason(order, CancelReason.TTL_REFRESH)

    def _has_open_order(self, oid: ClientOrderId | None) -> bool:
        """判断某个 client order id 是否仍然是 open 状态."""
        if oid is None:
            return False
        if oid in self._pending_cancel_reasons:
            return True
        order = self.cache.order(oid)
        return order is not None and (order.is_open or order.is_pending_cancel)

    def _has_active_quotes(self) -> bool:
        """判断当前是否仍有活跃挂单."""
        return any(self._has_open_order(oid) for oid in itertools.chain(self._active_bid_ids, self._active_ask_ids))

    def _prune_inactive_quote_ids(self) -> None:
        """将已不存在或已非 open 的订单 ID 置为 None，避免脏状态累积."""
        all_ids = list(self._active_bid_ids) + list(self._active_ask_ids)
        active_oids = {oid for oid in all_ids if oid is not None}

        for i, oid in enumerate(self._active_bid_ids):
            if oid is None:
                continue
            order = self.cache.order(oid)
            if order is None:
                if oid in self._pending_cancel_reasons:
                    continue
                self._active_bid_ids[i] = None
                self._quote_order_ids.discard(oid)
                continue
            if not order.is_open and not order.is_pending_cancel:
                self._active_bid_ids[i] = None
                self._quote_order_ids.discard(oid)

        for i, oid in enumerate(self._active_ask_ids):
            if oid is None:
                continue
            order = self.cache.order(oid)
            if order is None:
                if oid in self._pending_cancel_reasons:
                    continue
                self._active_ask_ids[i] = None
                self._quote_order_ids.discard(oid)
                continue
            if not order.is_open and not order.is_pending_cancel:
                self._active_ask_ids[i] = None
                self._quote_order_ids.discard(oid)

        # 清理已不再活跃的撤单原因，防止长期运行时 dict 无限增长
        tracked_oids = active_oids | self._quote_order_ids | set(self._reduce_to_lot)
        stale = [oid for oid in self._pending_cancel_reasons if oid not in tracked_oids]
        for oid in stale:
            self._pending_cancel_reasons.pop(oid, None)

    def _update_top_quote_state(self, bid_price: float, ask_price: float) -> None:
        """根据当前 active ids 更新第一层报价状态.

        Args:
            bid_price: 最新的 bid 价格
            ask_price: 最新的 ask 价格
        """
        qs = self._quote_state
        if self._active_bid_ids and self._active_bid_ids[0] is not None:
            qs.bid_queue_on_submit = self._estimate_queue_ahead("BUY")
            qs.bid_submit_time = self._utc_now()
            qs.bid_price = bid_price
        else:
            self._clear_bid_quote_state()

        if self._active_ask_ids and self._active_ask_ids[0] is not None:
            qs.ask_queue_on_submit = self._estimate_queue_ahead("SELL")
            qs.ask_submit_time = self._utc_now()
            qs.ask_price = ask_price
        else:
            self._clear_ask_quote_state()

    def _clear_bid_quote_state(self) -> None:
        """清除 bid 报价状态."""
        qs = self._quote_state
        qs.bid_price = None
        qs.bid_submit_time = None
        qs.bid_queue_on_submit = None

    def _clear_ask_quote_state(self) -> None:
        """清除 ask 报价状态."""
        qs = self._quote_state
        qs.ask_price = None
        qs.ask_submit_time = None
        qs.ask_queue_on_submit = None

    def _cancel_all_quotes(self, reason: CancelReason = CancelReason.DRIFT_REFRESH) -> None:
        """请求撤销当前所有双边挂单.

        注意：
        - 撤单是异步的，不能在这里立刻清空 _active_bid_ids/_active_ask_ids
        - 真正的状态收敛依赖 on_order_canceled / prune
        """
        self._cancel_quotes(OrderSide.BUY, reason)
        self._cancel_quotes(OrderSide.SELL, reason)
        # 重置状态
        self._quote_state.quoted_mid = None
        self._quote_state.quoted_skew = None

    def _cancel_quotes(self, side: OrderSide, reason: CancelReason = CancelReason.DRIFT_REFRESH) -> None:
        """撤销所有 bid 或 ask 报价.

        Args:
            side: 要撤销的订单方向（bid 或 ask）
            reason: 撤单原因
        """
        self._prune_inactive_quote_ids()
        total = 0
        active_ids = self._active_bid_ids if side == OrderSide.BUY else self._active_ask_ids
        side_str = "bid" if side == OrderSide.BUY else "ask"
        for _i, oid in enumerate(active_ids):
            if oid is None:
                continue
            order = self.cache.order(oid)
            if order is None:
                continue
            if order.is_open and not order.is_pending_cancel:
                try:
                    self._cancel_order_with_reason(order, reason)
                    total += 1
                    self.log.info(
                        f"Cancel {side_str} quote requested: client_order_id={oid} reason={reason.value}",
                        color=LogColor.YELLOW,
                    )
                except Exception as e:
                    self.log.error(f"Failed to cancel {side_str} quote {oid}: {e}", color=LogColor.RED)

        if side == OrderSide.BUY:
            self._clear_bid_quote_state()
        else:
            self._clear_ask_quote_state()

        if total > 0:
            self.log.info(f"Requested cancel for {total} active quotes reason={reason.value}", color=LogColor.YELLOW)

    @staticmethod
    def _is_unknown_order_cancel_rejection(reason: str) -> bool:
        """判断撤单拒绝是否表示交易所已无该订单."""
        return "-2011" in reason or "Unknown order sent" in reason

    @staticmethod
    def _quantity_to_decimal(value: Any) -> Decimal | None:
        """将 Nautilus Quantity 或普通数值安全转为 Decimal."""
        if value is None:
            return None
        if hasattr(value, "as_decimal"):
            try:
                return Decimal(str(value.as_decimal()))
            except (InvalidOperation, TypeError, ValueError):
                return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None

    def _quote_fill_completes_order(self, client_order_id: ClientOrderId, event: OrderFilled) -> bool:
        """判断本次 quote fill 是否已使原订单无剩余量.

        Binance 全成后再撤同一个订单会返回 ``-2011 Unknown order sent``。只有能从本地订单状态
        或单次 fill 数量明确确认订单已经无剩余量时才提前清理该 quote；部分成交仍保留在 active
        列表中，让后续 ``_cancel_all_quotes`` 撤掉剩余量。
        """
        order = self.cache.order(client_order_id)
        if order is None:
            return False

        order_qty = self._quantity_to_decimal(getattr(order, "quantity", None))
        last_qty = self._quantity_to_decimal(getattr(event, "last_qty", None))
        if order_qty is None or last_qty is None:
            return False

        leaves_qty = self._quantity_to_decimal(getattr(order, "leaves_qty", None))
        if leaves_qty is not None and leaves_qty <= Decimal("0"):
            return True

        filled_qty = self._quantity_to_decimal(getattr(order, "filled_qty", None)) or Decimal("0")
        return filled_qty >= order_qty or last_qty >= order_qty

    def _quote_position_id(self, side: OrderSide) -> PositionId:
        """为 Binance Futures 双向持仓生成 quote 订单的 PositionId."""
        suffix = "LONG" if side == OrderSide.BUY else "SHORT"
        return PositionId(f"{self.config.instrument_id}-{suffix}")

    def _submit_quote(self, side: OrderSide, price: float, qty: Decimal) -> ClientOrderId | None:
        """提交报价,只返回 client_order_id,不修改 active 列表."""
        if self.instrument is None:
            return None
        if qty <= 0:
            return None

        try:
            price_obj = self.instrument.make_price(price)
            qty_obj = self.instrument.make_qty(qty)
            if qty_obj.as_decimal() <= 0:
                return None

            notional = float(qty_obj.as_decimal()) * price
            if notional < 5.0:
                self.log.debug(f"Skipping quote: notional {notional:.2f} < 5.0 min")
                return None

            order = self.order_factory.limit(
                instrument_id=self.config.instrument_id,
                order_side=side,
                quantity=qty_obj,
                price=price_obj,
                time_in_force=TimeInForce.GTC,
                post_only=self.config.post_only,
            )

            position_id = self._quote_position_id(side)
            self.submit_order(order, position_id=position_id)

            self._quote_order_ids.add(order.client_order_id)
            self.log.debug(
                f"Quote submitted: oid={order.client_order_id} side={side.name} px={price:.8f} "
                f"qty={qty_obj.as_decimal()} position_id={position_id}"
            )
            return order.client_order_id
        except Exception as e:
            self.log.error(f"Failed to submit quote: {e}", color=LogColor.RED)
            return None

    def _submit_single_level_quotes(
        self,
        bid_price: float,
        ask_price: float,
        bid_qty: Decimal,
        ask_qty: Decimal,
    ) -> None:
        """提交单层双边报价."""
        bid_id = self._submit_quote(OrderSide.BUY, bid_price, bid_qty)
        ask_id = self._submit_quote(OrderSide.SELL, ask_price, ask_qty)
        self._active_bid_ids = [bid_id]
        self._active_ask_ids = [ask_id]
        self._update_top_quote_state(bid_price, ask_price)

    def _submit_layered_quotes(
        self,
        bid_price: float,
        ask_price: float,
        bid_qty: Decimal,
        ask_qty: Decimal,
    ) -> None:
        """提供分层报价，在多个价格档位提交分层报价.

        Args:
            bid_price: 第 0 层 bid 价格.
            ask_price: 第 0 层 ask 价格.
            bid_qty: 第 0 层 bid 数量.
            ask_qty: 第 0 层 ask 数量.
        """
        tick = 1.0
        if self.instrument is not None:
            tick = float(self.instrument.price_increment)

        inv = self._inventory_snapshot()  # 库存快照
        long_ratio = inv["long_ratio"]
        short_ratio = inv["short_ratio"]

        bid_ids: list[ClientOrderId | None] = []
        ask_ids: list[ClientOrderId | None] = []

        # 分层报价
        for i in range(self.config.quote_layers):
            decay = Decimal(str(self.config.layer_size_decay**i))
            step_offset = i * self.config.layer_spread_step_ticks * tick

            layer_bid_price = bid_price - step_offset
            layer_ask_price = ask_price + step_offset
            layer_bid_qty = Decimal(str(float(bid_qty) * float(decay)))
            layer_ask_qty = Decimal(str(float(ask_qty) * float(decay)))

            # 深层报价库存过滤——高库存时跳过同向深层
            if i > 0:
                if long_ratio > self.config.deeper_layer_inv_threshold:
                    layer_bid_qty = Decimal("0")
                if short_ratio > self.config.deeper_layer_inv_threshold:
                    layer_ask_qty = Decimal("0")
            # 非对称分层——逆方向只铺 1 层
            if self.config.asymmetric_layers and i > 0:
                if self._last_dir_val > 0:
                    layer_ask_qty = Decimal("0")
                elif self._last_dir_val < 0:
                    layer_bid_qty = Decimal("0")
            bid_id = self._submit_quote(OrderSide.BUY, layer_bid_price, layer_bid_qty)
            ask_id = self._submit_quote(OrderSide.SELL, layer_ask_price, layer_ask_qty)
            bid_ids.append(bid_id)
            ask_ids.append(ask_id)

        self._active_bid_ids = bid_ids
        self._active_ask_ids = ask_ids
        # 第一层提交时记录队列快照和提交时间
        self._update_top_quote_state(bid_price, ask_price)

    def _refresh_quotes(
        self,
        bid_price: float,
        ask_price: float,
        bid_qty: Decimal,
        ask_qty: Decimal,
        mid: float,
        current_skew: float,
    ) -> None:
        """当 mid 或 skew 漂移超过阈值时，撤销并重新提交报价.

        Args:
            bid_price: 第 0 层 bid 价格.
            ask_price: 第 0 层 ask 价格.
            bid_qty: 第 0 层 bid 数量.
            ask_qty: 第 0 层 ask 数量.
            mid: 当前 mid price.
            current_skew: 当前 skew
        """
        self._prune_inactive_quote_ids()
        qs = self._quote_state
        if qs.quoted_mid is not None and qs.quoted_skew is not None:
            tick = 1.0
            if self.instrument is not None:
                tick = float(self.instrument.price_increment)

            mid_drift = abs(mid - qs.quoted_mid)
            skew_drift = abs(current_skew - qs.quoted_skew)

            has_missing = any(x is None for x in self._active_bid_ids) or any(x is None for x in self._active_ask_ids)

            if not has_missing and mid_drift <= self.config.drift_ticks * tick and skew_drift <= self.config.skew_drift_ticks * tick:
                return

        if self._has_active_quotes():
            self._cancel_all_quotes(CancelReason.DRIFT_REFRESH)

        if self._quote_suspended:
            return

        if self.config.quote_layers > 1:
            self._submit_layered_quotes(bid_price, ask_price, bid_qty, ask_qty)
        else:
            self._submit_single_level_quotes(bid_price, ask_price, bid_qty, ask_qty)

        qs.quoted_mid = mid
        qs.quoted_skew = current_skew

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """订单撤销事件处理 — 三池识别.

        Args:
            event: OrderCanceled 订单撤销事件.
        """
        oid = event.client_order_id
        reason = self._pending_cancel_reasons.pop(oid, None)
        reason_str = f" reason={reason.value}" if reason is not None else ""

        # 1) reduce 池
        if oid in self._reduce_to_lot:
            self._handle_reduce_canceled(oid, reason)
            return

        # 2) quote 池 - bid
        for i, bid_id in enumerate(self._active_bid_ids):
            if oid == bid_id:
                self._active_bid_ids[i] = None
                self._quote_order_ids.discard(oid)
                if i == 0:
                    self._clear_bid_quote_state()
                self.log.info(f"Bid quote canceled: client_order_id={oid}{reason_str}", color=LogColor.YELLOW)
                return

        # 3) quote 池 - ask
        for i, ask_id in enumerate(self._active_ask_ids):
            if oid == ask_id:
                self._active_ask_ids[i] = None
                self._quote_order_ids.discard(oid)
                if i == 0:
                    self._clear_ask_quote_state()
                self.log.info(f"Ask quote canceled: client_order_id={oid}{reason_str}", color=LogColor.YELLOW)
                return

        # 兜底清理：处理 -2011 恢复路径中 active 槽已清空但仍在 _quote_order_ids 的订单
        self._quote_order_ids.discard(oid)

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        """订单撤销拒绝事件处理.

        Binance ``-2011 Unknown order sent`` 表示交易所侧已找不到该订单。对 quote 池而言可视作
        终态并清理本地活跃 ID，避免继续对同一个 client_order_id 重复发撤单请求；对 reduce 池而言则
        必须释放旧映射并立即重新补挂保护单。

        Args:
            event: OrderCancelRejected 订单撤销拒绝事件.
        """
        oid = event.client_order_id
        reason = self._pending_cancel_reasons.pop(oid, None)
        raw_reason = str(event.reason)
        if not self._is_unknown_order_cancel_rejection(raw_reason):
            self.log.warning(
                f"Order cancel rejected but order may still be active: client_order_id={oid} reason={raw_reason}",
                color=LogColor.YELLOW,
            )
            return

        if oid in self._reduce_to_lot:
            self._handle_reduce_cancel_unknown(oid, reason, raw_reason)
            return

        was_quote_order = oid in self._quote_order_ids
        cleared_quote_state = self._clear_quote_order_state(oid)
        if cleared_quote_state or was_quote_order:
            # -2011 说明订单在 Binance 侧已消失（可能已成交）。即使 active 槽位已被
            # 新 quote 覆盖，也保留 quote 来源标记，确保晚到的 OrderFilled 仍会创建 lot/reduce。
            self._quote_order_ids.add(oid)
            # 主动向 Binance 查询该订单的最终状态，触发 NautilusTrader 补发
            # 任何遗漏的 OrderFilled 事件（通过对账路径）。
            self._query_quote_order_after_unknown_cancel(oid, raw_reason)
            return

        self.log.warning(
            f"Cancel rejected as unknown order for untracked order: client_order_id={oid} reason={raw_reason}",
            color=LogColor.YELLOW,
        )

    def _query_quote_order_after_unknown_cancel(self, oid: ClientOrderId, raw_reason: str) -> None:
        """查询未知撤单拒绝后的 quote 终态，兼容未启动策略的单元测试桩."""
        try:
            cache = self.cache
        except Exception:
            cache = None

        order = None
        if cache is not None:
            try:
                order = cache.order(oid)
            except Exception as exc:
                self.log.warning(
                    f"Quote cancel rejected as unknown order, cache lookup failed: "
                    f"client_order_id={oid} reason={raw_reason} err={exc}",
                    color=LogColor.YELLOW,
                )

        if order is not None and not bool(getattr(order, "is_closed", False)):
            try:
                self.query_order(order)
                self.log.warning(
                    f"Quote cancel rejected as unknown order, querying fill status: client_order_id={oid} reason={raw_reason}",
                    color=LogColor.YELLOW,
                )
            except Exception as exc:
                self.log.warning(
                    f"Quote cancel rejected as unknown order, query_order failed: client_order_id={oid} reason={raw_reason} err={exc}",
                    color=LogColor.YELLOW,
                )
            return

        self.log.warning(
            f"Quote cancel rejected as unknown order, order not queryable: "
            f"client_order_id={oid} reason={raw_reason} "
            f"order_closed={getattr(order, 'is_closed', 'not_in_cache') if order else 'not_in_cache'}",
            color=LogColor.YELLOW,
        )

    def _clear_quote_order_state(self, client_order_id: ClientOrderId) -> bool:
        """清理本地 quote 订单 ID 及顶层 quote 状态."""
        for i, bid_id in enumerate(self._active_bid_ids):
            if bid_id == client_order_id:
                self._active_bid_ids[i] = None
                self._quote_order_ids.discard(client_order_id)
                if i == 0:
                    self._clear_bid_quote_state()
                return True

        for i, ask_id in enumerate(self._active_ask_ids):
            if ask_id == client_order_id:
                self._active_ask_ids[i] = None
                self._quote_order_ids.discard(client_order_id)
                if i == 0:
                    self._clear_ask_quote_state()
                return True

        return False

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

    def _maybe_withdraw_stale_quotes(self) -> None:
        """fill_prob 极低时撤销滞留单（不再有成交机会的挂单）."""
        threshold = self.config.withdraw_fill_prob_threshold
        if self._active_bid_ids and self._active_bid_ids[0] is not None and self._calc_queue_fill_prob("BUY") < threshold:
            bid_id = self._active_bid_ids[0]
            order = self.cache.order(bid_id)
            if order is not None and order.is_open and not order.is_pending_cancel:
                self._cancel_order_with_reason(order, CancelReason.STALE_LOW_FILL_PROB)

        if self._active_ask_ids and self._active_ask_ids[0] is not None and self._calc_queue_fill_prob("SELL") < threshold:
            ask_id = self._active_ask_ids[0]
            order = self.cache.order(ask_id)
            if order is not None and order.is_open and not order.is_pending_cancel:
                self._cancel_order_with_reason(order, CancelReason.STALE_LOW_FILL_PROB)

    def _check_toxic_preemptive(self) -> None:
        """Microprice 急速漂移时预防性撤单（toxic 前置防御）."""
        if self._last_microprice is None or self._prev_microprice is None:
            return
        if self.instrument is None:
            return
        tick = float(self.instrument.price_increment)
        threshold = self.config.toxic_mp_drift_ticks * tick
        instant_drift = self._last_microprice - self._prev_microprice

        if instant_drift < -threshold and self._active_bid_ids and self._active_bid_ids[0] is not None:
            bid_id = self._active_bid_ids[0]
            order = self.cache.order(bid_id)
            if order is not None and order.is_open and not order.is_pending_cancel:
                self._cancel_order_with_reason(order, CancelReason.TOXIC_PREEMPTIVE)

        if instant_drift > threshold and self._active_ask_ids and self._active_ask_ids[0] is not None:
            ask_id = self._active_ask_ids[0]
            order = self.cache.order(ask_id)
            if order is not None and order.is_open and not order.is_pending_cancel:
                self._cancel_order_with_reason(order, CancelReason.TOXIC_PREEMPTIVE)

    def _next_lot_id(self, event: OrderFilled) -> str:
        """生成 fill 级唯一 lot_id."""
        self._lot_seq += 1
        oid = getattr(event, "client_order_id", None)
        ts = getattr(event, "ts_event", None)
        return f"lot:{oid or 'na'}:{ts or 'na'}:{self._lot_seq}"

    def _create_lot(self, event: OrderFilled) -> InventoryLot:
        """Quote 成交时创建 inventory lot."""
        qty = Decimal(str(float(event.last_qty)))

        lot = InventoryLot(
            lot_id=self._next_lot_id(event),
            quote_order_id=getattr(event, "client_order_id", None),
            side=event.order_side,
            entry_price=float(event.last_px),
            filled_qty=qty,
            remaining_qty=qty,
            position_id=getattr(event, "position_id", None),
        )
        self._inventory_lots[lot.lot_id] = lot
        self.log.info(
            f"Lot created: id={lot.lot_id} side={event.order_side.name} qty={lot.filled_qty} "
            f"entry={lot.entry_price:.8f} position_id={lot.position_id}",
            color=LogColor.GREEN,
        )
        return lot

    def _resolve_lot_position_id(self, lot: InventoryLot) -> PositionId | None:
        """解析 lot 对应的持仓 ID，供 reduce 订单绑定使用."""
        if lot.position_id is not None:
            return lot.position_id

        positions_open = getattr(self.cache, "positions_open", None)
        if not callable(positions_open):
            self.log.error(
                f"Cannot resolve position_id for lot={lot.lot_id}: cache.positions_open unavailable",
                color=LogColor.RED,
            )
            return None

        try:
            positions = positions_open(instrument_id=self.config.instrument_id)
        except TypeError:
            # 某些测试桩/兼容实现可能不支持 instrument_id 参数
            try:
                positions = positions_open()
            except Exception as exc:
                self.log.error(
                    f"Failed to resolve position_id for lot={lot.lot_id}: {exc}",
                    color=LogColor.RED,
                )
                return None
        except Exception as exc:
            self.log.error(
                f"Failed to resolve position_id for lot={lot.lot_id}: {exc}",
                color=LogColor.RED,
            )
            return None

        positions_iter = cast(Iterable[Any], positions)
        candidates: list[Any] = []
        for position in positions_iter:
            if getattr(position, "instrument_id", None) != self.config.instrument_id:
                continue

            is_long = bool(getattr(position, "is_long", False))
            if lot.side == OrderSide.BUY and not is_long:
                continue
            if lot.side == OrderSide.SELL and is_long:
                continue

            candidates.append(position)

        if not candidates:
            self.log.error(
                f"Cannot resolve position_id for lot={lot.lot_id}: no matching open position side={lot.side.name}",
                color=LogColor.RED,
            )
            return None

        if len(candidates) == 1:
            position = candidates[0]
            position_id = getattr(position, "id", None) or getattr(position, "position_id", None)
            if position_id is None:
                self.log.error(
                    f"Resolved position missing id for lot={lot.lot_id}",
                    color=LogColor.RED,
                )
                return None

            lot.position_id = position_id
            self.log.info(
                f"Resolved position_id for lot={lot.lot_id}: {position_id}",
                color=LogColor.BLUE,
            )
            return position_id

        # 多候选时，按 qty/entry_price 近似匹配
        lot_qty = float(lot.remaining_qty)
        lot_px = float(lot.entry_price)

        def score(position: Any) -> tuple[float, float]:
            qty = abs(float(getattr(position, "quantity", 0.0)))
            px = float(getattr(position, "avg_px_open", 0.0))
            return (abs(qty - lot_qty), abs(px - lot_px))

        candidates.sort(key=score)

        best = candidates[0]
        best_score = score(best)
        second_score = score(candidates[1]) if len(candidates) > 1 else None

        # 若前两名几乎一样，视为歧义，拒绝盲绑
        if second_score is not None and best_score == second_score:
            self.log.error(
                f"Cannot resolve position_id for lot={lot.lot_id}: ambiguous candidates={len(candidates)} side={lot.side.name}",
                color=LogColor.RED,
            )
            return None

        position_id = getattr(best, "id", None) or getattr(best, "position_id", None)
        if position_id is None:
            self.log.error(
                f"Resolved position missing id for lot={lot.lot_id}",
                color=LogColor.RED,
            )
            return None

        lot.position_id = position_id
        self.log.warning(
            f"Resolved position_id by heuristic for lot={lot.lot_id}: {position_id}",
            color=LogColor.YELLOW,
        )
        return position_id

    def _calc_reduce_price(self, lot: InventoryLot) -> tuple[OrderSide, float] | None:
        """计算 reduce 价格."""
        if lot.side == OrderSide.BUY:
            side = OrderSide.SELL
            price = lot.entry_price * (1 + self.config.tp_pct)
        else:
            side = OrderSide.BUY
            price = lot.entry_price * (1 - self.config.tp_pct)

        if price <= 0:
            return None
        return side, price

    def _place_reduce_order(self, lot: InventoryLot) -> ClientOrderId | None:
        """为单个 lot 挂保护性 reduce 单."""
        if self.instrument is None or not lot.is_open():
            return None

        calc = self._calc_reduce_price(lot)
        if calc is None:
            return None
        side, price = calc

        try:
            price_obj = self.instrument.make_price(price)
            qty_obj = self.instrument.make_qty(lot.remaining_qty)
            if qty_obj.as_decimal() <= 0:
                return None
            position_id = self._resolve_lot_position_id(lot)
            if position_id is None:
                self.log.error(f"Skip reduce placement without position_id: lot={lot.lot_id}", color=LogColor.RED)
                return None

            order = self.order_factory.limit(
                instrument_id=self.config.instrument_id,
                order_side=side,
                quantity=qty_obj,
                price=price_obj,
                time_in_force=TimeInForce.GTC,
                post_only=self.config.reduce_post_only,  # False 可以吃单
                reduce_only=True,
            )
            self.submit_order(order, position_id=position_id)
            lot.reduce_version += 1
            lot.reduce_order_id = order.client_order_id
            lot.status = LotStatus.PENDING_PROTECT
            lot.last_reduce_submit_at = self._utc_now()
            self._reduce_to_lot[order.client_order_id] = lot.lot_id
            self.log.info(
                f"Reduce placed: lot={lot.lot_id} v={lot.reduce_version} "
                f"oid={order.client_order_id} side={side.name} px={price:.8f} "
                f"qty={lot.remaining_qty}",
                color=LogColor.GREEN,
            )
            return order.client_order_id
        except Exception as e:
            self.log.error(f"Failed to place reduce order for lot={lot.lot_id}: {e}", color=LogColor.RED)
            return None

    def _ensure_reduce_for_lot(self, lot: InventoryLot) -> None:
        """Lot 只要还开着，就必须有 reduce.

        注意：
        reduce 提交后，cache / adapter 可能需要几秒才收敛。
        这段逻辑避免因为 cache 暂时查不到订单而重复补挂 reduce。
        """
        if not lot.is_open():
            return

        now = self._utc_now()

        if lot.reduce_order_id is not None:
            order = self.cache.order(lot.reduce_order_id)

            if order is not None:
                if order.is_open and not order.is_pending_cancel:
                    lot.status = LotStatus.PROTECTED
                    return

                if order.is_pending_cancel:
                    return

            if lot.last_reduce_submit_at is not None:
                elapsed_ms = (now - lot.last_reduce_submit_at).total_seconds() * 1000
                if elapsed_ms < self.config.reduce_submit_grace_ms:
                    lot.status = LotStatus.PENDING_PROTECT
                    return

            self.log.warning(
                f"Reduce order not found/open after grace window: lot={lot.lot_id} oid={lot.reduce_order_id}, will resubmit",
                color=LogColor.YELLOW,
            )
            self._reduce_to_lot.pop(lot.reduce_order_id, None)

        lot.reduce_order_id = None
        lot.status = LotStatus.OPEN
        self._place_reduce_order(lot)

    def _ensure_all_open_lots_protected(self) -> None:
        """为所有开着的 lot 确保有 reduce."""
        for lot in list(self._inventory_lots.values()):
            if lot.is_open():
                self._ensure_reduce_for_lot(lot)

    def _handle_reduce_fill(self, event: OrderFilled, lot: InventoryLot) -> None:
        """处理 reduce 成交，支持 partial fill."""
        fill_qty = Decimal(str(float(event.last_qty)))
        fill_price = float(event.last_px)

        lot.remaining_qty -= fill_qty
        if lot.remaining_qty < 0:
            lot.remaining_qty = Decimal("0")

        # 记录 realized pnl
        if lot.side == OrderSide.BUY:
            realized = (fill_price - lot.entry_price) * float(fill_qty)
        else:
            realized = (lot.entry_price - fill_price) * float(fill_qty)
        self._recent_fills.append((self._utc_now(), realized))

        if lot.remaining_qty <= 0:
            old_oid = lot.reduce_order_id
            if old_oid is not None:
                self._reduce_to_lot.pop(old_oid, None)

            lot.mark_closed()
            self.log.info(
                f"Reduce fully filled: lot={lot.lot_id} close_px={fill_price:.8f}",
                color=LogColor.GREEN,
            )
            return

        # 部分成交：仍保留映射，不 pop
        lot.status = LotStatus.CLOSING
        self.log.info(
            f"Reduce partial fill: lot={lot.lot_id} fill_qty={fill_qty} remain={lot.remaining_qty}",
            color=LogColor.YELLOW,
        )

    def _handle_reduce_canceled(self, client_order_id: ClientOrderId, reason: CancelReason | None) -> None:
        """处理 reduce 订单被撤销的情况."""
        lot_id = self._reduce_to_lot.pop(client_order_id, None)
        if lot_id is None:
            return

        lot = self._inventory_lots.get(lot_id)
        if lot is None or not lot.is_open():
            return

        if lot.reduce_order_id == client_order_id:
            lot.reduce_order_id = None
            lot.status = LotStatus.OPEN
            lot.last_reduce_submit_at = None

        self.log.warning(
            f"Reduce canceled: lot={lot_id} oid={client_order_id} reason={reason.value if reason else 'unknown'}",
            color=LogColor.YELLOW,
        )

        # 只有策略停止/kill switch/全平等少数场景允许不补挂
        if reason in {
            CancelReason.STRATEGY_STOP,
            CancelReason.KILL_SWITCH,
        }:
            return

        self._place_reduce_order(lot)

    def _handle_reduce_cancel_unknown(
        self,
        client_order_id: ClientOrderId,
        reason: CancelReason | None,
        raw_reason: str,
    ) -> None:
        """处理 reduce 撤单被交易所报告为 unknown 的情况."""
        lot_id = self._reduce_to_lot.pop(client_order_id, None)
        if lot_id is None:
            return

        lot = self._inventory_lots.get(lot_id)
        if lot is None or not lot.is_open():
            return

        if lot.reduce_order_id == client_order_id:
            lot.reduce_order_id = None
            lot.status = LotStatus.OPEN
            lot.last_reduce_submit_at = None

        self.log.warning(
            f"Reduce cancel rejected as unknown order: lot={lot_id} oid={client_order_id} "
            f"reason={reason.value if reason else 'unknown'} raw_reason={raw_reason}",
            color=LogColor.YELLOW,
        )

        if reason in {
            CancelReason.STRATEGY_STOP,
            CancelReason.KILL_SWITCH,
        }:
            return

        self._place_reduce_order(lot)

    def _cancel_reduce_orders(self, reason: CancelReason) -> None:
        """撤销 Reduce 池所有活跃订单."""
        total = 0
        for oid in list(self._reduce_to_lot.keys()):
            order = self.cache.order(oid)
            if order is not None and order.is_open and not order.is_pending_cancel:
                try:
                    self._cancel_order_with_reason(order, reason)
                    total += 1
                except Exception as e:
                    self.log.error(f"Failed to cancel reduce order {oid}: {e}", color=LogColor.RED)
        if total:
            self.log.info(f"Requested cancel {total} reduce orders reason={reason.value}", color=LogColor.YELLOW)

    def _cancel_all_orders(self, reason: CancelReason) -> None:
        """撤销所有订单（Quote 池 + Reduce 池）."""
        self._cancel_all_quotes(reason)
        self._cancel_reduce_orders(reason)

    def _prune_reduce_orders(self) -> None:
        """只做脏状态修剪，不主动关闭 lot.

        注意：
        PENDING_PROTECT 状态下，cache 可能短时间查不到订单；
        不要在 grace window 内清理 reduce_order_id。
        """
        now = self._utc_now()
        stale_oids: list[ClientOrderId] = []

        for oid in list(self._reduce_to_lot.keys()):
            lot_id = self._reduce_to_lot.get(oid)
            lot = self._inventory_lots.get(lot_id) if lot_id is not None else None
            order = self.cache.order(oid)

            if order is None:
                if lot is not None and lot.reduce_order_id == oid and lot.last_reduce_submit_at is not None:
                    elapsed_ms = (now - lot.last_reduce_submit_at).total_seconds() * 1000
                    if elapsed_ms < self.config.reduce_submit_grace_ms:
                        continue
                stale_oids.append(oid)
                continue

            if not order.is_open and not order.is_pending_cancel:
                stale_oids.append(oid)

        for oid in stale_oids:
            lot_id = self._reduce_to_lot.pop(oid, None)
            if lot_id is None:
                continue

            lot = self._inventory_lots.get(lot_id)
            if lot is not None and lot.reduce_order_id == oid and lot.is_open():
                lot.reduce_order_id = None
                lot.status = LotStatus.OPEN
                lot.last_reduce_submit_at = None

        closed_ids = [lid for lid, lot in self._inventory_lots.items() if lot.status == LotStatus.CLOSED]
        for lid in closed_ids:
            del self._inventory_lots[lid]
