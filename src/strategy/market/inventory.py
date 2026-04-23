"""Inventory management mixin for the market maker strategy."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled
from src.strategy.market.quote_engine import CancelReason


class InventoryMixin:
    """双向持仓库存管理类（基于 lot 聚合，而非净仓）."""

    _last_fill_price: float | None
    _last_fill_side: str | None

    def _inventory_snapshot(self: Any) -> dict[str, float]:
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
                net_usd: 多头 - 空头（仅观测用途，不作为风控主驱动）
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
        net_usd = long_usd - short_usd
        max_pos = max(float(self.config.max_position_usd), 1.0)

        long_ratio = long_usd / max_pos
        short_ratio = short_usd / max_pos
        gross_ratio = gross_usd / max_pos

        if gross_usd > 1e-9:
            imbalance = (long_usd - short_usd) / gross_usd
        else:
            imbalance = 0.0

        return {
            "long_ratio": long_ratio,
            "short_ratio": short_ratio,
            "gross_ratio": gross_ratio,
            "imbalance": imbalance,
            "long_usd": long_usd,
            "short_usd": short_usd,
            "gross_usd": gross_usd,
            "net_usd": net_usd,
            "long_qty": long_qty,
            "short_qty": short_qty,
        }

    def _calc_quote_sizes(self: Any, base_qty: Decimal, adverse_side: str | None = None) -> tuple[Decimal, Decimal, bool, bool]:
        """双向持仓下的 bid/ask 数量控制.

        规则：
        - 多仓越大，越压缩 bid（避免继续加多）
        - 空仓越大，越压缩 ask（避免继续加空）
        - gross 越大，双边都缩量
        - 某一侧库存过重时，仅关闭该侧开仓能力
        - quote 池不承担 reduce_only 语义，因此始终返回 False/False

        Args:
            base_qty: 基础下单数量.
            adverse_side: 若为 "BUY" 则禁 bid；若为 "SELL" 则禁 ask.

        Returns:
            tuple[Decimal, Decimal, bool, bool]:
                (bid_qty, ask_qty, bid_reduce_only, ask_reduce_only)
        """
        if self.instrument is None:
            return base_qty, base_qty, False, False

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

        return bid_qty, ask_qty, False, False

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

            # reduce 成交后重新检查 lot 聚合风险
            self._check_lot_risk()
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

        self._check_lot_risk()

    def _check_lot_risk(self: Any) -> None:
        """基于 lot 聚合风险检查 kill switch."""
        inv = self._inventory_snapshot()

        self.log.info(
            "inventory_state "
            f"long_usd={inv['long_usd']:.4f} "
            f"short_usd={inv['short_usd']:.4f} "
            f"gross_usd={inv['gross_usd']:.4f} "
            f"net_usd={inv['net_usd']:.4f} "
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

    def _flatten_all_lots(self: Any) -> None:
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

                order = self.order_factory.market(
                    instrument_id=self.config.instrument_id,
                    order_side=order_side,
                    quantity=qty_obj,
                    reduce_only=True,
                )
                self.submit_order(order)
                flattened += 1
            except Exception as e:
                self.log.error(f"Failed to flatten lot {lot.lot_id}: {e}", color=LogColor.RED)

        if flattened > 0:
            self.log.warning(f"Flatten requested for {flattened} open lots", color=LogColor.YELLOW)