"""做市商库存、PnL 和净仓 TP 管理."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled, PositionChanged, PositionClosed, PositionOpened

from src.strategy.base import BaseStrategy


class InventoryMixin:
    """封装库存分级、报价数量、成交 PnL、持仓同步和净仓 TP 逻辑."""

    _last_fill_price: float | None
    _last_fill_side: str | None

    def _inventory_snapshot(self: Any) -> dict[str, float]:
        """库存快照.

        Returns:
            dict[str, float]: 库存快照.
            "long_ratio": 多头仓位占比.
            "short_ratio": 空头仓位占比.
            "gross_ratio": 总仓位占比（abs净仓/预算）.
            "imbalance": 净敞口方向（正=多, 负=空）.
        """
        abs_usd = abs(self._net_position_usd)
        ratio = abs_usd / max(self.config.max_position_usd, 1.0)
        long_ratio = ratio if self._net_position_usd > 0 else 0.0
        short_ratio = ratio if self._net_position_usd < 0 else 0.0
        gross_ratio = ratio
        imbalance = math.copysign(min(ratio, 1.0), self._net_position_usd) if abs_usd > 1e-9 else 0.0
        return {
            "long_ratio": long_ratio,
            "short_ratio": short_ratio,
            "gross_ratio": gross_ratio,
            "imbalance": imbalance,
        }

    def _calc_quote_sizes(
        self: Any,
        base_qty: Decimal,
        adverse_side: str | None = None,
    ) -> tuple[Decimal, Decimal, bool, bool]:
        """单向持仓下的 bid/ask 数量与 reduce_only 标志.

        Args:
            base_qty: 基础下单数量.
            adverse_side: 若为 "BUY" 则将 bid 归零；若为 "SELL" 则将 ask 归零（US-002）.

        Returns:
            tuple[Decimal, Decimal, bool, bool]: (bid_qty, ask_qty, bid_reduce_only, ask_reduce_only).
        """
        if self.instrument is None:
            return base_qty, base_qty, False, False

        step = float(self.instrument.size_increment)
        if step <= 0:
            step = 1e-8

        def round_to_step(val: float) -> Decimal:
            rounded = round(val / step) * step
            return Decimal(str(max(rounded, 0.0)))

        inv = self._inventory_snapshot()
        ratio = inv["gross_ratio"]  # abs(net) / max_position_usd
        base_f = float(base_qty)

        def smooth_scale(r: float, soft: float, hard: float, min_ratio: float) -> float:
            if r <= soft:
                return 1.0
            if r >= hard:
                return min_ratio
            t = (r - soft) / max(hard - soft, 1e-9)
            return 1.0 - (t**2) * (1.0 - min_ratio)

        scale = smooth_scale(ratio, self.config.soft_limit, self.config.hard_limit, self.config.soft_size_min_ratio)

        # 多头仓位 → 压缩 bid（不想继续加多），空头仓位 → 压缩 ask
        if self._net_position_usd > 0:
            bid_scale = scale
            ask_scale = 1.0
        else:
            bid_scale = 1.0
            ask_scale = scale

        bid_qty = round_to_step(base_f * bid_scale)
        ask_qty = round_to_step(base_f * ask_scale)
        bid_reduce_only = False
        ask_reduce_only = False

        # one_side_only：仓位过重时只挂平仓方向
        if ratio >= self.config.one_side_only_limit:
            if self._net_position_usd > 0:
                bid_qty = Decimal("0")
            else:
                ask_qty = Decimal("0")

        # hard limit：强制挂 reduce_only 平仓单
        if getattr(self.config, "allow_reduce_only_rebalance", True) and ratio >= self.config.hard_limit:
            if self._net_position_usd > 0 and self._position_qty > 0:
                ask_qty = round_to_step(base_f)
                ask_reduce_only = True
            elif self._net_position_usd < 0 and self._position_qty < 0:
                bid_qty = round_to_step(base_f)
                bid_reduce_only = True

        if self._last_dir_val > self.config.dead_zone_threshold:
            bid_qty = round_to_step(float(bid_qty) * 1.15)
            ask_qty = round_to_step(float(ask_qty) * 0.85)
        elif self._last_dir_val < -self.config.dead_zone_threshold:
            bid_qty = round_to_step(float(bid_qty) * 0.85)
            ask_qty = round_to_step(float(ask_qty) * 1.15)

        if adverse_side == "BUY":
            bid_qty = Decimal("0")
            bid_reduce_only = False
        elif adverse_side == "SELL":
            ask_qty = Decimal("0")
            ask_reduce_only = False

        return bid_qty, ask_qty, bid_reduce_only, ask_reduce_only

    def _resolve_tp_ref_price(self: Any, abs_qty: float, net_qty: float) -> float:
        """解析 TP 单参考价：microprice > orderbook mid > 开仓均价（成本价）.

        Args:
            abs_qty: 净仓位绝对值（合约数量）.
            net_qty: 净仓位（正=多头, 负=空头），用于 warning 日志.

        Returns:
            参考价；无法获取时返回 0.0.
        """
        if self._last_microprice is not None and self._last_microprice > 0:
            return self._last_microprice
        try:
            ob = self.cache.order_book(self.config.instrument_id)
            if ob is not None:
                bb = ob.best_bid_price()
                ba = ob.best_ask_price()
                if bb is not None and ba is not None:
                    return (float(bb) + float(ba)) / 2.0
        except Exception:
            pass
        cost_price = (abs(self._net_position_usd) / abs_qty) if abs_qty > 0 else 0.0
        self.log.warning(
            f"Net TP using cost-basis price as ref (microprice/orderbook unavailable): cost_px={cost_price:.8f} net_qty={net_qty:.8f}",
            color=LogColor.YELLOW,
        )
        return cost_price

    def _sync_net_tp_order(self: Any) -> None:
        """根据当前净仓位重新计算并挂聚合 reduce_only TP 单.

        先撤旧 TP，若净仓位为 0 则不挂新单.
        BUY 净多头 → SELL reduce_only（平多）
        SELL 净空头 → BUY reduce_only（平空）
        """
        # 撤旧 TP（cancel 是异步的，旧单可能尚未确认撤销就提交新单；
        # 交易所在单向持仓下会自动拒绝超额 reduce_only，风险可控）
        if self._net_tp_order_id is not None:
            old_order = self.cache.order(self._net_tp_order_id)
            if old_order is not None and old_order.is_open:
                self.log.debug(f"Canceling stale net TP before sync: client_order_id={self._net_tp_order_id}")
                self.cancel_order(old_order)
            self._net_tp_order_id = None

        if self.instrument is None:
            return

        net_qty = self._position_qty  # 正=多头, 负=空头
        abs_qty = abs(net_qty)
        if abs_qty < float(self.instrument.size_increment):
            return  # 无持仓，不挂 TP

        tick = float(self.instrument.price_increment)
        if tick <= 0:
            tick = 1.0

        exit_ticks = max(float(self.config.min_spread_ticks), float(self.config.base_spread_ticks))

        ref_price = self._resolve_tp_ref_price(abs_qty, net_qty)
        if ref_price <= 0:
            self.log.warning("Net TP skipped: ref_price is zero or negative", color=LogColor.YELLOW)
            return

        if net_qty > 0:
            # 净多头 → SELL reduce_only
            exit_price = ref_price + exit_ticks * tick
            side = OrderSide.SELL
        else:
            # 净空头 → BUY reduce_only
            exit_price = ref_price - exit_ticks * tick
            side = OrderSide.BUY

        if exit_price <= 0:
            return

        try:
            qty_obj = self.instrument.make_qty(abs_qty)
            if qty_obj.as_decimal() <= 0:
                return
            price_obj = self.instrument.make_price(exit_price)
            order = self.order_factory.limit(
                instrument_id=self.config.instrument_id,
                order_side=side,
                quantity=qty_obj,
                price=price_obj,
                time_in_force=TimeInForce.GTC,
                post_only=self.config.post_only,
                reduce_only=True,
            )
            self.submit_order(order)
            self._net_tp_order_id = order.client_order_id
            self.log.info(
                f"Net TP synced: side={side} px={exit_price:.8f} qty={abs_qty:.8f} net_pos={net_qty:.8f}",
                color=LogColor.GREEN,
            )
        except Exception as e:
            self.log.error(f"Failed to sync net TP order: {e}", color=LogColor.RED)

    def on_order_filled(self: Any, event: OrderFilled) -> None:
        """订单成交事件处理.

        Args:
            event: OrderFilled 事件实例.
        """
        self._last_fill_ts = self._utc_now()

        fill_price = float(event.last_px)
        fill_qty = float(event.last_qty)
        fill_side = "BUY" if event.order_side == OrderSide.BUY else "SELL"
        self._last_fill_price = fill_price  # 最近成交价格
        self._last_fill_side = fill_side  # 最近成交方向

        # 聚合 TP 单成交 → 清字段并返回（仓位事件会触发 _sync_net_tp_order 重建）
        # 注意：依赖 NautilusTrader 保证 OrderFilled 先于 PositionChanged 触发，
        # 使得此处清空 _net_tp_order_id 后，后续 _sync_net_tp_order 不会重复撤单。
        client_order_id = getattr(event, "client_order_id", None)
        if self._net_tp_order_id is not None and client_order_id == self._net_tp_order_id:
            self._net_tp_order_id = None
            self.log.info(f"Net TP filled: client_order_id={client_order_id}", color=LogColor.GREEN)
            return

        # 取消同方向订单
        self.log.info(f"Order filled: side={fill_side} qty={fill_qty:.8f} px={fill_price:.8f}", color=LogColor.YELLOW)
        self._cancel_quotes(event.order_side)
        msg = "Bid" if event.order_side == OrderSide.BUY else "Ask"
        self.log.info(f"{msg} quotes canceled after {fill_side} fill", color=LogColor.YELLOW)

        # 匹配对手方未平成交（先进先出）
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

        # 无已实现损益 → 使用按 microprice 标记的代理值
        if abs(realized) < 1e-10 and remaining > 0:
            mid = self._last_microprice or 0.0
            if mid > 0:
                sign = 1.0 if fill_side == "BUY" else -1.0
                realized = (mid - fill_price) * remaining * sign

        self._recent_fills.append((self._utc_now(), realized))

        # 成交后根据 microprice 漂移强化 toxic flow 分数
        mid_now = self._last_microprice
        if mid_now is not None:
            drift = mid_now - fill_price
            if fill_side == "BUY" and drift < 0:
                self._toxic_flow_score = max(-1.0, self._toxic_flow_score - 0.3)
            elif fill_side == "SELL" and drift > 0:
                self._toxic_flow_score = min(1.0, self._toxic_flow_score + 0.3)

    def _close_all_positions(self: Any) -> None:
        """市价单平掉所有持仓（单向持仓模式）."""
        positions = self.cache.positions_open(instrument_id=self.config.instrument_id)
        if self.instrument is None:
            return
        for pos in positions:
            try:
                qty = abs(float(pos.quantity))
                if qty <= 0:
                    continue
                qty_obj = self.instrument.make_qty(qty)
                is_long = bool(getattr(pos, "is_long", False))
                order_side = OrderSide.SELL if is_long else OrderSide.BUY
                order = self.order_factory.market(
                    instrument_id=self.config.instrument_id,
                    order_side=order_side,
                    quantity=qty_obj,
                    reduce_only=True,
                )
                self.submit_order(order)
            except Exception as e:
                self.log.error(f"Failed to close position {pos}: {e}", color=LogColor.RED)

    def _update_position_state(self: Any) -> None:
        """更新持仓 USD 状态（单向净仓）."""
        positions = self.cache.positions_open(instrument_id=self.config.instrument_id)
        net_usd = 0.0
        net_qty = 0.0

        for pos in positions:
            qty = float(pos.quantity)  # 正=多, 负=空
            price = float(pos.avg_px_open)
            if pos.is_long:
                net_usd += abs(qty) * price
                net_qty += qty
            elif pos.is_short:
                net_usd -= abs(qty) * price
                net_qty -= abs(qty)  # short qty 为负

        self._net_position_usd = net_usd
        self._position_qty = net_qty

        ratio = abs(net_usd) / max(self.config.max_position_usd, 1.0)
        self.log.info(
            f"position_state net_usd={net_usd:.4f} net_qty={net_qty:.8f} ratio={ratio:.4f} open_positions={len(positions)}",
            color=LogColor.YELLOW,
        )

        if ratio >= self.config.kill_switch_limit and not self._kill_switch:
            self._kill_switch = True
            self._cancel_all_quotes()
            self.log.error(
                f"Kill switch activated: ratio={ratio:.2f} >= {self.config.kill_switch_limit}",
                color=LogColor.RED,
            )
            self._close_all_positions()
        elif ratio < self.config.hard_limit and self._kill_switch:
            self._kill_switch = False
            self.log.info("Kill switch reset", color=LogColor.GREEN)

    def on_position_opened(self: Any, event: PositionOpened) -> None:
        """仓位开启时更新持仓状态.

        Args:
            event: PositionOpened 事件实例.
        """
        BaseStrategy.on_position_opened(self, event)
        self._update_position_state()
        self._sync_net_tp_order()

    def on_position_changed(self: Any, event: PositionChanged) -> None:
        """仓位变化时更新持仓状态.

        Args:
            event: PositionChanged 事件实例.
        """
        BaseStrategy.on_position_changed(self, event)
        self._update_position_state()
        self._sync_net_tp_order()

    def on_position_closed(self: Any, event: PositionClosed) -> None:
        """仓位关闭时更新持仓状态.

        Args:
            event: PositionClosed 事件实例.
        """
        BaseStrategy.on_position_closed(self, event)
        self._update_position_state()
        self._sync_net_tp_order()
