"""算法执行.

TWAP / VWAP / 冰山等算法执行策略 (预留).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

import structlog

from src.execution.order_intent import OrderIntent

logger = structlog.get_logger()


class ExecAlgorithm(ABC):
    """执行算法基类."""

    @abstractmethod
    def split(self, intent: OrderIntent) -> list[OrderIntent]:
        """将大单拆分为多个小单.

        Args:
            intent: 原始订单意图

        Returns:
            拆分后的订单意图列表

        """


class TWAPAlgorithm(ExecAlgorithm):
    """时间加权平均价格算法 (TWAP).

    将大单均匀拆分到指定时间窗口内执行.
    """

    def __init__(self, slices: int = 5, interval_seconds: int = 60) -> None:
        """Initialize the twap algorithm.

        Args:
            slices: Slices.
            interval_seconds: Interval seconds.
        """
        self._slices = slices
        self._interval_seconds = interval_seconds

    def split(self, intent: OrderIntent) -> list[OrderIntent]:
        """均匀拆分.

        Args:
            intent: Intent.
        """
        slice_qty = intent.quantity / Decimal(self._slices)
        intents = []

        for i in range(self._slices):
            sliced = OrderIntent(
                instrument_id=intent.instrument_id,
                side=intent.side,
                quantity=slice_qty,
                order_type=intent.order_type,
                time_in_force=intent.time_in_force,
                strategy_id=intent.strategy_id,
                metadata={**intent.metadata, "algo": "twap", "slice": i + 1, "total_slices": self._slices},
            )
            intents.append(sliced)

        logger.info(
            "twap_split",
            instrument=intent.instrument_id,
            slices=self._slices,
            slice_qty=str(slice_qty),
        )
        return intents


class PassthroughAlgorithm(ExecAlgorithm):
    """直通算法, 不拆分."""

    def split(self, intent: OrderIntent) -> list[OrderIntent]:
        """Run split.

        Args:
            intent: Intent.

        Returns:
            list[OrderIntent]: Collected items returned by the operation.
        """
        return [intent]
