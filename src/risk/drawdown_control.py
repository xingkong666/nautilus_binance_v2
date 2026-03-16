"""回撤控制.

独立的回撤追踪和自动降仓逻辑.
"""

from __future__ import annotations

from decimal import Decimal

import structlog

logger = structlog.get_logger()


class DrawdownController:
    """回撤控制器.

    当回撤达到阈值时, 自动缩减仓位比例.
    """

    def __init__(
        self,
        warning_pct: float = 3.0,
        critical_pct: float = 5.0,
        reduce_factor: float = 0.5,
    ) -> None:
        """初始化回撤控制器.

        Args:
            warning_pct: 预警回撤阈值（%）。
            critical_pct: 严重回撤阈值（%）。
            reduce_factor: 回撤时仓位缩减比例，`0.5` 表示减半。

        """
        self._warning_pct = warning_pct
        self._critical_pct = critical_pct
        self._reduce_factor = reduce_factor
        self._peak_equity = Decimal(0)

    def update_equity(self, equity: Decimal) -> None:
        """更新权益峰值.

        Args:
            equity: Current account equity value.
        """
        if equity > self._peak_equity:
            self._peak_equity = equity

    @property
    def current_drawdown_pct(self) -> float:
        """当前回撤百分比."""
        if self._peak_equity <= 0:
            return 0.0
        return float((self._peak_equity - self._peak_equity) / self._peak_equity * 100)

    def get_size_multiplier(self, current_equity: Decimal) -> float:
        """根据回撤状态返回仓位乘数.

        Args:
            current_equity: Current equity.

        Returns:
            1.0 = 正常, 0.5 = 减半, 0.0 = 停止交易
        """
        if self._peak_equity <= 0:
            return 1.0

        dd_pct = float((self._peak_equity - current_equity) / self._peak_equity * 100)

        if dd_pct >= self._critical_pct:
            logger.warning("drawdown_critical", drawdown_pct=dd_pct)
            return 0.0  # 停止交易

        if dd_pct >= self._warning_pct:
            logger.warning("drawdown_warning", drawdown_pct=dd_pct)
            return self._reduce_factor

        return 1.0
