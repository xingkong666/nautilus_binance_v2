"""做市商 alpha、订单簿和市场质量模型."""

from __future__ import annotations

import math
import statistics
from typing import Any, cast

from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.data import Bar, OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import AggressorSide, OrderSide

from src.core.events import SignalDirection


class AlphaMixin:
    """封装订单簿 imbalance、microprice、trade flow 和市场质量相关逻辑."""

    _last_best_ask_size: float | None
    _last_best_bid_size: float | None
    _last_fill_mid: float | None
    _last_microprice: float | None
    _last_mid_for_rv: float | None
    _prev_microprice: float | None

    def on_trade_tick(self: Any, trade: TradeTick) -> None:
        """处理逐笔成交，追踪主动买卖量."""
        qty = float(trade.size)
        if trade.aggressor_side == AggressorSide.BUYER:
            self._agg_buy_vol += qty
        elif trade.aggressor_side == AggressorSide.SELLER:
            self._agg_sell_vol += qty

        # V4+: 累计队列消耗量（复用已计算的数量）
        self._queue_traded_volume += qty

        # V4-US-002: 更新有毒流分数
        self._update_toxic_flow(trade)

    def _calc_trade_flow_signal(self: Any) -> float:
        """计算 trade flow 信号：(buy_vol - sell_vol) / (buy_vol + sell_vol + ε)."""
        total = self._agg_buy_vol + self._agg_sell_vol
        if total < 1e-10:
            return 0.0
        return (self._agg_buy_vol - self._agg_sell_vol) / total

    def _update_toxic_flow(self: Any, trade: TradeTick) -> None:
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

    def _calc_quote_score(self: Any, dir_val: float) -> float:
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

    def on_order_book_deltas(self: Any, deltas: OrderBookDeltas) -> None:
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

    def _calc_weighted_imbalance(self: Any) -> None:
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

    def _order_book_level_size(self: Any, level: Any) -> float:
        """读取 order book 档位数量，兼容 Nautilus 方法式 size() 与测试桩属性式 size."""
        size_attr = level.size
        size = size_attr() if callable(size_attr) else size_attr
        return float(cast(Any, size))

    def _calc_weights(self: Any, n: int) -> list[float]:
        if self.config.imbalance_weight_mode == "exp":
            lam = 0.5
            return [math.exp(-lam * i) for i in range(n)]
        # 线性模式：weight[i] = (n-i)/n（线性加权）
        return [(n - i) / n for i in range(n)]

    def _get_microprice(self: Any, bar: Bar | None) -> float | None:
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

    def _get_mid_price(self: Any, bar: Bar | None) -> float | None:
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

    def _update_realized_vol(self: Any, mid: float) -> float | None:
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

    def _get_rv_ticks(self: Any) -> float:
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

    def _check_market_quality(self: Any, best_bid: float, best_ask: float) -> None:
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

    def _calc_expected_profit_bps(self: Any, mid: float) -> float:
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

    def _check_adverse_selection(self: Any, mid: float) -> str | None:
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

    def generate_signal(self: Any, bar: Bar) -> SignalDirection | None:
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

    def _calc_microprice_signal(self: Any) -> float:
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

    def _compute_dir_val(self: Any) -> float:
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
