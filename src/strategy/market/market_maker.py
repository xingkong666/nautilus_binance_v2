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

from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal

import structlog
from nautilus_trader.common.enums import LogColor
from nautilus_trader.config import PositiveInt
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId

from src.core.events import EventBus
from src.strategy.base import BaseStrategy, BaseStrategyConfig
from src.strategy.market.alpha import AlphaMixin
from src.strategy.market.inventory import InventoryMixin
from src.strategy.market.queue_model import QueueModelMixin
from src.strategy.market.quote_engine import QuoteEngineMixin, QuoteState

logger = structlog.get_logger(__name__)


class MarketMakerConfig(BaseStrategyConfig, frozen=True):
    """ActiveMarketMaker 配置."""

    instrument_id: InstrumentId
    bar_type: BarType

    # L2 订单簿
    order_book_depth: int = 10
    imbalance_decay: float = 0.3
    imbalance_threshold: float = 0.58
    imbalance_weight_mode: str = "linear"  # "linear" | "exp"（配置枚举值）

    # EMA辅助过滤
    fast_ema_period: PositiveInt = 20
    slow_ema_period: PositiveInt = 60

    # 动态价差
    base_spread_ticks: int = 3
    min_spread_ticks: int = 2
    max_spread_ticks: int = 10
    spread_vol_multiplier: float = 2.0
    spread_recovery_ratio: float = 0.9  # 迟滞恢复阈值

    # 偏斜参数
    alpha_scale_ticks: float = 2.0
    alpha_tanh_k: float = 2.0
    inv_scale_ticks: float = 3.0
    inv_tanh_scale: float = 2.0

    # 仓位预算（USD notional）
    max_position_usd: float = 1000.0

    # 库存分级（单向净仓比例）
    soft_limit: float = 0.30
    hard_limit: float = 0.70

    # 缩量下限
    soft_size_min_ratio: float = 0.30

    # 熔断阈值（gross_ratio >= kill_switch_limit）
    kill_switch_limit: float = 1.20

    # 订单生命周期
    limit_ttl_ms: int = 8000
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
    max_loss_usd: float = 50.0
    loss_window_ms: int = 60000
    pnl_cb_cooldown_ms: int = 300000

    # US-008: 市场质量过滤
    max_book_spread_ticks: float = 20.0
    imbalance_spike_threshold: float = 0.9

    # US-009: 成本模型——最低预期收益
    min_expected_profit_bps: float = 1.0
    taker_fee_bps: float = 4.0

    # V3-US-002: 成交流 阿尔法
    subscribe_trades: bool = True
    trade_flow_weight: float = 0.4

    # V3-US-003: 交易前逆向撤单
    pretrade_cancel_ticks: float = 0.5

    # V3-US-004: 微价格深度利用
    microprice_skew_scale: float = 1.0

    # V3-US-005: 库存分层控制
    one_side_only_limit: float = 0.85
    deeper_layer_inv_threshold: float = 0.5

    # V4-US-001: 队列位置
    queue_norm_volume: float = 10000.0
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

    allow_reduce_only_rebalance: bool = True


class ActiveMarketMaker(AlphaMixin, InventoryMixin, QuoteEngineMixin, QueueModelMixin, BaseStrategy):
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
        self._pending_requote_ts: datetime | None = None

        # 库存跟踪（单向持仓）
        self._net_position_usd: float = 0.0
        self._position_qty: float = 0.0  # 正=多头, 负=空头

        # 活跃报价订单 ID — US-006 分层报价
        self._active_bid_ids: list[ClientOrderId | None] = []
        self._active_ask_ids: list[ClientOrderId | None] = []

        # 成交冷却状态
        self._last_fill_ts: datetime | None = None

        # 熔断开关状态
        self._kill_switch: bool = False

        # 净仓聚合 TP 单
        self._net_tp_order_id: ClientOrderId | None = None

        # US-002: 逆向选择
        self._last_fill_price: float | None = None
        self._last_fill_side: str | None = None  # "BUY" 或 "SELL"
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

        # V3-US-001: 已实现 PNL追踪（替代名义额）
        self._open_fills: list[tuple[float, float, str]] = []
        self._last_microprice: float | None = None

        # V3-US-002: 成交流 阿尔法
        self._agg_buy_vol: float = 0.0
        self._agg_sell_vol: float = 0.0

        # V4-US-001: 队列位置
        self._last_best_bid_size: float | None = None
        self._last_best_ask_size: float | None = None

        # V4-US-001: 队列位置快照
        self._queue_traded_volume: float = 0.0

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
            return

        if bar.is_single_price():
            return

        if self._kill_switch:
            return

        # US-007: PNL熔断器检查
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
            self.log.error(
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

        in_adverse_cooldown = self._adverse_cooldown_until is not None and self._utc_now() < self._adverse_cooldown_until

        dir_val = self._compute_dir_val()
        self._last_dir_val = dir_val

        bid_price, ask_price, avg_shift = self._calc_quote_prices(mid, dir_val)

        clamped = self._clamp_quote_prices(bid_price, ask_price)
        if clamped is None:
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
            return

        # US-004: 缓存基础数量供 三角洲驱动报价使用
        self._last_base_qty = base_qty.as_decimal()

        # 若处于冷却期则将 adverse_side 传给数量计算
        effective_adverse = adverse_side if in_adverse_cooldown else None
        bid_qty, ask_qty, bid_reduce_only, ask_reduce_only = self._calc_quote_sizes(
            base_qty.as_decimal(),
            adverse_side=effective_adverse,
        )

        # V4+: 有毒且排不到队列 → 直接撤单
        fill_prob_bid = self._calc_queue_fill_prob("BUY")
        fill_prob_ask = self._calc_queue_fill_prob("SELL")
        fill_prob_combined = (fill_prob_bid + fill_prob_ask) / 2.0
        if abs(self._toxic_flow_score) > 0.6 and fill_prob_combined < 0.3:
            self._cancel_all_quotes()
            return

        # V4: 报价分决策
        score = self._calc_quote_score(dir_val)
        self._last_quote_score = score

        if score < self.config.quote_score_threshold:
            self._cancel_all_quotes()
            return

        # 有毒流单边控制
        if self._toxic_flow_score > self.config.toxic_one_side_threshold:
            ask_qty = Decimal("0")
            ask_reduce_only = False
        elif self._toxic_flow_score < -self.config.toxic_one_side_threshold:
            bid_qty = Decimal("0")
            bid_reduce_only = False

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
                bid_reduce_only=bid_reduce_only,
                ask_reduce_only=ask_reduce_only,
            )

        # V3-US-002: 每根 K 线结束后重置成交流累计量
        self._agg_buy_vol = 0.0
        self._agg_sell_vol = 0.0
        self._queue_traded_volume = 0.0

    def on_stop(self) -> None:
        """停止策略时撤销所有挂单."""
        self._cancel_all_quotes()
        if self._net_tp_order_id is not None:
            order = self.cache.order(self._net_tp_order_id)
            if order is not None and order.is_open:
                self.cancel_order(order)
            self._net_tp_order_id = None
        super().on_stop()

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
        self._pending_requote_ts = None
        self._active_bid_ids = []
        self._active_ask_ids = []
        self._net_tp_order_id = None
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
        # V3-US-001: 重置已实现 PNL追踪
        self._open_fills.clear()
        self._last_microprice = None
        # V3-US-002: 重置成交流
        self._agg_buy_vol = 0.0
        self._agg_sell_vol = 0.0

        # V4-US-001: 重置队列位置
        self._last_best_bid_size = None
        self._last_best_ask_size = None

        self._queue_traded_volume = 0.0
        # V4-US-002: 重置有毒流
        self._toxic_flow_score = 0.0
        self._last_fill_mid = None
        # V4-US-003: 重置报价分
        self._last_quote_score = 0.0
        # V5-US-004: 重置前一微价格
        self._prev_microprice = None
        # V5-US-005: 重置上一方向值
        self._last_dir_val = 0.0
        self._net_position_usd = 0.0
        self._position_qty = 0.0
