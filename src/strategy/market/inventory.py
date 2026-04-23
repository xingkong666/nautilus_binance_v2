"""Inventory management mixin for the market maker strategy."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled, PositionChanged, PositionClosed, PositionOpened

from src.strategy.base import BaseStrategy
from src.strategy.market.quote_engine import CancelReason


class InventoryMixin:
    """持仓管理类."""

    _last_fill_price: float | None
    _last_fill_side: str | None

    def _inventory_snapshot(self: Any) -> dict[str, float]:
        """获取持仓快照."""
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

    def _calc_quote_sizes(self: Any, base_qty: Decimal, adverse_side: str | None = None) -> tuple[Decimal, Decimal, bool, bool]:
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

    def on_order_filled(self: Any, event: OrderFilled) -> None:
        """处理订单成交事件."""
        self._last_fill_ts = self._utc_now()

        fill_price = float(event.last_px)
        fill_qty = float(event.last_qty)
        fill_side = "BUY" if event.order_side == OrderSide.BUY else "SELL"
        self._last_fill_price = fill_price
        self._last_fill_side = fill_side

        client_order_id = getattr(event, "client_order_id", None)

        # 1) Reduce fill
        if client_order_id is not None and client_order_id in self._reduce_to_lot:
            lot_id = self._reduce_to_lot[client_order_id]
            lot = self._inventory_lots.get(lot_id)
            if lot is not None:
                self._handle_reduce_fill(event, lot)
            return

        # 2) 只有 quote 池订单的 fill 才创建 lot
        is_quote_fill = client_order_id is not None and client_order_id in self._quote_order_ids
        if not is_quote_fill:
            self.log.info(
                f"Non-quote fill ignored for lot creation: oid={client_order_id} side={fill_side} qty={fill_qty:.8f} px={fill_price:.8f}",
                color=LogColor.YELLOW,
            )
            return

        # 3) Quote fill
        self.log.info(
            f"Quote filled: oid={client_order_id} side={fill_side} qty={fill_qty:.8f} px={fill_price:.8f}",
            color=LogColor.YELLOW,
        )

        # quote 成交后，可撤 quote 池避免继续被动吃货；reduce 池绝不跟着撤
        self._cancel_all_quotes(CancelReason.ORDER_FILLED)

        # 当前 oid 已成交，移出 quote 来源集合
        self._quote_order_ids.discard(client_order_id)

        lot = self._create_lot(event)
        self._place_reduce_order(lot)

        # m2m proxy pnl
        mid = self._last_microprice or 0.0
        if mid > 0:
            sign = 1.0 if fill_side == "BUY" else -1.0
            proxy_pnl = (mid - fill_price) * fill_qty * sign
            self._recent_fills.append((self._utc_now(), proxy_pnl))

        # toxic flow
        mid_now = self._last_microprice
        if mid_now is not None:
            drift = mid_now - fill_price
            if fill_side == "BUY" and drift < 0:
                self._toxic_flow_score = max(-1.0, self._toxic_flow_score - 0.3)
            elif fill_side == "SELL" and drift > 0:
                self._toxic_flow_score = min(1.0, self._toxic_flow_score + 0.3)

    def _close_all_positions(self: Any) -> None:
        """关闭所有持仓.

        注意：这里不清理 lot，由 reduce fill 驱动。
        """
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
        """更新持仓状态.

        注意：这里不清理 lot，由 reduce fill 驱动。
        """
        positions = self.cache.positions_open(instrument_id=self.config.instrument_id)
        net_usd = 0.0
        net_qty = 0.0

        for pos in positions:
            qty = float(pos.quantity)
            price = float(pos.avg_px_open)
            if pos.is_long:
                net_usd += abs(qty) * price
                net_qty += qty
            elif pos.is_short:
                net_usd -= abs(qty) * price
                net_qty -= abs(qty)

        self._net_position_usd = net_usd
        self._position_qty = net_qty

        ratio = abs(net_usd) / max(self.config.max_position_usd, 1.0)
        self.log.info(
            f"position_state net_usd={net_usd:.4f} net_qty={net_qty:.8f} ratio={ratio:.4f} open_positions={len(positions)}",
            color=LogColor.YELLOW,
        )

        if ratio >= self.config.kill_switch_limit and not self._kill_switch:
            self._kill_switch = True
            self._cancel_all_orders(CancelReason.KILL_SWITCH)
            self.log.error(
                f"Kill switch activated: ratio={ratio:.2f} >= {self.config.kill_switch_limit}",
                color=LogColor.RED,
            )
            self._close_all_positions()
        elif ratio < self.config.hard_limit and self._kill_switch:
            self._kill_switch = False
            self.log.info("Kill switch reset", color=LogColor.GREEN)

    def on_position_opened(self: Any, event: PositionOpened) -> None:
        """处理持仓开仓事件.

        Args:
            event: 持仓开仓事件
        """
        BaseStrategy.on_position_opened(self, event)
        self._update_position_state()

    def on_position_changed(self: Any, event: PositionChanged) -> None:
        """处理持仓变更事件.

        Args:
            event: 持仓变更事件
        """
        BaseStrategy.on_position_changed(self, event)
        self._update_position_state()

    def on_position_closed(self: Any, event: PositionClosed) -> None:
        """处理持仓关闭事件.

        这里只更新持仓；lot 是否关闭由 reduce fill 驱动。
        """
        BaseStrategy.on_position_closed(self, event)
        self._update_position_state()

        # 防止 reduce 丢失
        self._ensure_all_open_lots_protected()
