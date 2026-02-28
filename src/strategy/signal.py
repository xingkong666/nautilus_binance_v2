"""信号定义.

策略产出的标准化信号格式.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.core.events import SignalDirection


@dataclass(frozen=True)
class TradeSignal:
    """交易信号 (策略 → 执行引擎).

    Attributes:
        instrument_id: 交易对
        direction: 信号方向
        strength: 信号强度 (0.0-1.0)
        suggested_size: 建议仓位大小 (可选, 由 position_sizer 最终决定)
        stop_loss: 建议止损价 (可选)
        take_profit: 建议止盈价 (可选)
        metadata: 附加信息 (如指标值、bar 信息等)
    """

    instrument_id: str
    direction: SignalDirection
    strength: float = 1.0
    suggested_size: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_entry(self) -> bool:
        """是否为入场信号."""
        return self.direction in (SignalDirection.LONG, SignalDirection.SHORT)

    @property
    def is_exit(self) -> bool:
        """是否为出场信号."""
        return self.direction == SignalDirection.FLAT
