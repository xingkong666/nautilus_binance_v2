"""订单意图.

策略信号转化为标准化的订单意图, 经风控审核后由 order_router 执行.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.core.events import SignalDirection


@dataclass(frozen=True)
class OrderIntent:
    """订单意图 (策略信号 → 风控审核 → 执行).

    这是策略和执行引擎之间的标准接口.
    """

    instrument_id: str
    side: str  # BUY / SELL
    quantity: Decimal
    order_type: str = "MARKET"  # MARKET / LIMIT / MARKET_IF_TOUCHED
    price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    time_in_force: str = "GTC"
    reduce_only: bool = False
    strategy_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_signal(
        cls,
        instrument_id: str,
        direction: SignalDirection,
        quantity: Decimal,
        strategy_id: str = "",
        **kwargs: Any,
    ) -> OrderIntent:
        """从信号方向创建订单意图.

        Args:
            instrument_id: Instrument identifier to target.
            direction: Signal direction for the order intent.
            quantity: Order quantity to use.
            strategy_id: Strategy identifier associated with the order.
            **kwargs: Kwargs.
        """
        if direction == SignalDirection.LONG:
            side = "BUY"
        elif direction == SignalDirection.SHORT:
            side = "SELL"
        else:
            # FLAT → 平仓
            side = "SELL"  # 具体方向由 router 根据当前持仓判断
            kwargs["reduce_only"] = True

        return cls(
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            strategy_id=strategy_id,
            **kwargs,
        )
