"""做市商报价引擎与订单生命周期管理."""

from __future__ import annotations

import itertools
import math
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderCanceled
from nautilus_trader.model.identifiers import ClientOrderId, PositionId


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

    def reset(self: Any) -> None:
        """重置为初始状态."""
        self.quoted_mid = None
        self.quoted_skew = None
        self.bid_price = None
        self.ask_price = None
        self.bid_submit_time = None
        self.ask_submit_time = None
        self.bid_queue_on_submit = None
        self.ask_queue_on_submit = None


class QuoteEngineMixin:
    """封装报价定价、撤挂单、分层报价和 delta 驱动刷新逻辑."""

    def _cancel_order_with_reason(self: Any, order: Any, reason: CancelReason) -> None:
        """记录撤单原因后发送撤单请求."""
        self._pending_cancel_reasons[order.client_order_id] = reason
        self.cancel_order(order)

    def _check_pretrade_cancel(self: Any) -> None:
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

    def _update_dynamic_spread(self: Any) -> None:
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

    def _calc_quote_prices(self: Any, mid: float, dir_val: float) -> tuple[float, float, float]:
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

    def _clamp_quote_prices(self: Any, bid_price: float, ask_price: float) -> tuple[float, float] | None:
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

    def _maybe_refresh_expiring_orders(self: Any, mid: float) -> None:
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

    def _has_open_order(self: Any, oid: ClientOrderId | None) -> bool:
        """判断某个 client order id 是否仍然是 open 状态."""
        if oid is None:
            return False
        order = self.cache.order(oid)
        return order is not None and order.is_open

    def _has_active_quotes(self: Any) -> bool:
        """判断当前是否仍有活跃挂单."""
        return any(self._has_open_order(oid) for oid in itertools.chain(self._active_bid_ids, self._active_ask_ids))

    def _prune_inactive_quote_ids(self: Any) -> None:
        """将已不存在、已非 open 或正在撤销中的订单 ID 置为 None，避免脏状态累积."""
        all_ids = list(self._active_bid_ids) + list(self._active_ask_ids)
        active_oids = {oid for oid in all_ids if oid is not None}

        for i, oid in enumerate(self._active_bid_ids):
            if oid is None:
                continue
            order = self.cache.order(oid)
            if order is None or not order.is_open or order.is_pending_cancel:
                self._active_bid_ids[i] = None
                self._quote_order_ids.discard(oid)

        for i, oid in enumerate(self._active_ask_ids):
            if oid is None:
                continue
            order = self.cache.order(oid)
            if order is None or not order.is_open or order.is_pending_cancel:
                self._active_ask_ids[i] = None
                self._quote_order_ids.discard(oid)

        # 清理已不再活跃的撤单原因，防止长期运行时 dict 无限增长
        stale = [oid for oid in self._pending_cancel_reasons if oid not in active_oids]
        for oid in stale:
            self._pending_cancel_reasons.pop(oid, None)

    def _update_top_quote_state(self: Any, bid_price: float, ask_price: float) -> None:
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

    def _clear_bid_quote_state(self: Any) -> None:
        """清除 bid 报价状态."""
        qs = self._quote_state
        qs.bid_price = None
        qs.bid_submit_time = None
        qs.bid_queue_on_submit = None

    def _clear_ask_quote_state(self: Any) -> None:
        """清除 ask 报价状态."""
        qs = self._quote_state
        qs.ask_price = None
        qs.ask_submit_time = None
        qs.ask_queue_on_submit = None

    def _cancel_all_quotes(self: Any, reason: CancelReason = CancelReason.DRIFT_REFRESH) -> None:
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

    def _cancel_quotes(self: Any, side: OrderSide, reason: CancelReason = CancelReason.DRIFT_REFRESH) -> None:
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

    def _submit_quote(self: Any, side: OrderSide, price: float, qty: Decimal) -> ClientOrderId | None:
        """提交报价,只返回 client_order_id,不修改 active 列表.

        Args:
            side: 订单方向.
            price: 报价价格.
            qty: 报价数量.
        """
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
            #  Binance Futures + post_only=True 时，adapter 会将 GTC 转为 GTX（Post-Only），订单正常显示在订单列表中，
            # 不再被 GTX 强制覆盖导致 GTD 失效。订单生命周期管理完全由策略已有的 _refresh_quotes drift 刷新逻辑承担（每 limit_ttl_ms *
            # order_refresh_ratio 触发一次检查）
            order = self.order_factory.limit(
                instrument_id=self.config.instrument_id,
                order_side=side,
                quantity=qty_obj,
                price=price_obj,
                time_in_force=TimeInForce.GTC,
                post_only=self.config.post_only,
            )
            self.submit_order(order)

            # 只给 quote 池打标记
            self._quote_order_ids.add(order.client_order_id)
            return order.client_order_id
        except Exception as e:
            self.log.error(f"Failed to submit quote: {e}", color=LogColor.RED)
            return None

    def _submit_single_level_quotes(
        self: Any,
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
        self: Any,
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
        self: Any,
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

    def on_order_canceled(self: Any, event: OrderCanceled) -> None:
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

    def _try_quote_on_delta(self: Any) -> None:
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

    def _maybe_withdraw_stale_quotes(self: Any) -> None:
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

    def _check_toxic_preemptive(self: Any) -> None:
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
