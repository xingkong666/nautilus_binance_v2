"""滑点模型.

估算和追踪订单滑点.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class SlippageModel:
    """滑点估算模型."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize the slippage model.

        Args:
            config: Configuration values for the component.
        """
        self._model_type = config.get("model", "fixed")
        self._fixed_bps = Decimal(str(config.get("fixed_bps", 2)))

    def estimate_slippage_bps(
        self,
        quantity: Decimal,
        price: Decimal,
        volume_24h: Decimal | None = None,
    ) -> Decimal:
        """估算滑点 (basis points).

        Args:
            quantity: 交易数量
            price: 当前价格
            volume_24h: 24h 交易量 (用于 volume_based 模型)

        Returns:
            估算滑点 (bps)

        """
        if self._model_type == "fixed":
            return self._fixed_bps

        if self._model_type == "volume_based" and volume_24h and volume_24h > 0:
            # 订单占 24h 交易量的比例越大, 滑点越大
            order_value = quantity * price
            ratio = order_value / volume_24h
            return self._fixed_bps + Decimal(str(float(ratio) * 100))

        return self._fixed_bps

    @staticmethod
    def calculate_actual_slippage_bps(
        expected_price: Decimal,
        actual_price: Decimal,
    ) -> float:
        """计算实际滑点.

        Args:
            expected_price: 预期成交价
            actual_price: 实际成交价

        Returns:
            实际滑点 (bps)

        """
        if expected_price == 0:
            return 0.0
        return float(abs(actual_price - expected_price) / expected_price * 10000)
