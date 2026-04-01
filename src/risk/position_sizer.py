"""仓位管理.

根据风控规则和信号强度决定交易数量.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from src.monitoring.metrics import POSITION_SIZER_OUTPUT

logger = structlog.get_logger()


class PositionSizer:
    """仓位计算器.

    支持多种仓位策略:
    - fixed: 固定数量
    - risk_pct: 风险比例 (基于止损距离)
    - kelly: Kelly 准则
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize the position sizer.

        Args:
            config: Configuration values for the component.
        """
        self._mode = config.get("mode", "fixed")
        self._fixed_size = Decimal(str(config.get("fixed_size", "0.01")))
        self._risk_pct = config.get("risk_pct", 1.0)  # 每笔风险占总资金的百分比
        self._max_size = Decimal(str(config.get("max_size", "1.0")))

    def calculate(
        self,
        account_equity: Decimal,
        current_price: Decimal,
        stop_loss_distance: Decimal | None = None,
        signal_strength: float = 1.0,
    ) -> Decimal:
        """计算仓位大小.

        Args:
            account_equity: 账户权益
            current_price: 当前价格
            stop_loss_distance: 止损距离 (价格)
            signal_strength: 信号强度 (0-1)

        Returns:
            交易数量

        """
        if self._mode == "fixed":
            size = self._fixed_size

        elif self._mode == "risk_pct" and stop_loss_distance and stop_loss_distance > 0:
            # 基于风险比例: risk_amount = equity * risk_pct%
            # size = risk_amount / stop_loss_distance
            risk_amount = account_equity * Decimal(str(self._risk_pct / 100))
            size = risk_amount / stop_loss_distance

        else:
            size = self._fixed_size

        # 信号强度调整
        size = size * Decimal(str(signal_strength))

        # 上限
        size = min(size, self._max_size)

        logger.debug(
            "position_size_calculated",
            mode=self._mode,
            size=str(size),
            signal_strength=signal_strength,
        )

        # 记录仓位计算器输出分布
        POSITION_SIZER_OUTPUT.observe(float(size))

        return size
