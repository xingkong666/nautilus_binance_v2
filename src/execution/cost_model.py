"""成本模型.

计算交易成本 (手续费 + 滑点).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

logger = structlog.get_logger()


class CostModel:
    """交易成本计算器."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._maker_fee_bps = Decimal(str(config.get("maker_fee_bps", 2)))
        self._taker_fee_bps = Decimal(str(config.get("taker_fee_bps", 4)))

    def estimate_cost(
        self,
        quantity: Decimal,
        price: Decimal,
        is_maker: bool = False,
        slippage_bps: Decimal = Decimal(0),
    ) -> Decimal:
        """估算交易总成本.

        Args:
            quantity: 交易数量
            price: 价格
            is_maker: 是否为 maker
            slippage_bps: 预估滑点 (bps)

        Returns:
            总成本 (USDT)
        """
        order_value = quantity * price
        fee_bps = self._maker_fee_bps if is_maker else self._taker_fee_bps
        total_bps = fee_bps + slippage_bps

        cost = order_value * total_bps / Decimal(10000)

        logger.debug(
            "cost_estimated",
            order_value=str(order_value),
            fee_bps=str(fee_bps),
            slippage_bps=str(slippage_bps),
            total_cost=str(cost),
        )

        return cost
