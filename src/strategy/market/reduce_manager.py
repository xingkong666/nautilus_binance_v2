"""Reduce/TP 订单池管理 — 库存缩量单生命周期."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, cast

from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId, PositionId

from src.strategy.market.inventory_lot import InventoryLot, LotStatus
from src.strategy.market.quote_engine import CancelReason


class ReduceManagerMixin:
    """封装 Reduce/TP 池的 lot 管理和 reduce 订单生命周期."""

    def _next_lot_id(self: Any, event: OrderFilled) -> str:
        """生成 fill 级唯一 lot_id."""
        self._lot_seq += 1
        oid = getattr(event, "client_order_id", None)
        ts = getattr(event, "ts_event", None)
        return f"lot:{oid or 'na'}:{ts or 'na'}:{self._lot_seq}"

    def _create_lot(self: Any, event: OrderFilled) -> InventoryLot:
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
            f"Lot created: id={lot.lot_id} side={event.order_side.name} qty={lot.filled_qty} entry={lot.entry_price:.8f}",
            color=LogColor.GREEN,
        )
        return lot

    def _resolve_lot_position_id(self: Any, lot: InventoryLot) -> PositionId | None:
        """解析 lot 对应的持仓 ID，供 reduce_only 订单绑定使用."""
        if lot.position_id is not None:
            return lot.position_id

        positions_open = getattr(self.cache, "positions_open", None)
        if not callable(positions_open):
            self.log.error(f"Cannot resolve position_id for lot={lot.lot_id}: cache.positions_open unavailable", color=LogColor.RED)
            return None

        try:
            candidates = []
            for position in cast(Callable[[], list[Any]], positions_open)():
                if getattr(position, "instrument_id", None) != self.config.instrument_id:
                    continue

                if lot.side == OrderSide.BUY and not getattr(position, "is_long", False):
                    continue
                if lot.side == OrderSide.SELL and getattr(position, "is_long", False):
                    continue

                candidates.append(position)
        except Exception as exc:
            self.log.error(
                f"Failed to resolve position_id for lot={lot.lot_id}: {exc}",
                color=LogColor.RED,
            )
            return None

        if len(candidates) != 1:
            self.log.error(
                f"Cannot resolve position_id for lot={lot.lot_id}: matched_positions={len(candidates)} side={lot.side.name}",
                color=LogColor.RED,
            )
            return None

        position_id = getattr(candidates[0], "id", None)
        if position_id is None:
            self.log.error(f"Resolved position missing id for lot={lot.lot_id}", color=LogColor.RED)
            return None

        lot.position_id = position_id
        self.log.info(f"Resolved position_id for lot={lot.lot_id}: {position_id}", color=LogColor.BLUE)
        return position_id

    def _calc_reduce_price(self: Any, lot: InventoryLot) -> tuple[OrderSide, float] | None:
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

    def _place_reduce_order(self: Any, lot: InventoryLot) -> ClientOrderId | None:
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
            lot.status = LotStatus.PROTECTED
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

    def _ensure_reduce_for_lot(self: Any, lot: InventoryLot) -> None:
        """Lot 只要还开着，就必须有 reduce."""
        if not lot.is_open():
            return
        # 刚补挂过，给 cache / adapter 一个收敛窗口，避免重复补单
        if lot.last_reduce_submit_at is not None:
            elapsed_ms = (self._utc_now() - lot.last_reduce_submit_at).total_seconds() * 1000
            if elapsed_ms < 500:
                return

        if lot.reduce_order_id is not None:
            order = self.cache.order(lot.reduce_order_id)
            if order is not None and order.is_open and not order.is_pending_cancel:
                return

        lot.reduce_order_id = None
        lot.status = LotStatus.OPEN
        self._place_reduce_order(lot)

    def _ensure_all_open_lots_protected(self: Any) -> None:
        """为所有开着的 lot 确保有 reduce."""
        for lot in list(self._inventory_lots.values()):
            if lot.is_open():
                self._ensure_reduce_for_lot(lot)

    def _handle_reduce_fill(self: Any, event: OrderFilled, lot: InventoryLot) -> None:
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

    def _handle_reduce_canceled(self: Any, client_order_id: ClientOrderId, reason: CancelReason | None) -> None:
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

    def _cancel_reduce_orders(self: Any, reason: CancelReason) -> None:
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

    def _cancel_all_orders(self: Any, reason: CancelReason) -> None:
        """撤销所有订单（Quote 池 + Reduce 池）."""
        self._cancel_all_quotes(reason)
        self._cancel_reduce_orders(reason)

    def _prune_reduce_orders(self: Any) -> None:
        """只做脏状态修剪，不主动关闭 lot."""
        stale_oids: list[ClientOrderId] = []
        for oid in list(self._reduce_to_lot.keys()):
            order = self.cache.order(oid)
            if order is None or not order.is_open:
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
