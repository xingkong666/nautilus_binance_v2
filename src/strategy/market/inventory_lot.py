"""库存 Lot 数据模型 — 追踪每笔 Quote 成交对应的库存和 Reduce 订单."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientOrderId


class LotStatus:
    """Lot 生命周期状态."""

    OPEN = "OPEN"  # 刚由 quote fill 创建，尚未有有效 reduce
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
    reduce_order_id: ClientOrderId | None = None  # 对应的 Reduce 订单ID
    status: str = field(default=LotStatus.OPEN)
    reduce_version: int = 0  # 每次补挂递增，避免旧单事件串扰
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
