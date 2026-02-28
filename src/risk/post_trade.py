"""事后风控.

交易完成后的 PnL 归因、滑点分析等.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class TradeAnalysis:
    """单笔交易分析."""

    instrument_id: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal
    fees: Decimal
    slippage_bps: float  # 滑点 (basis points)
    duration_seconds: float


@dataclass
class PostTradeReport:
    """事后风控报告."""

    period: str  # 如 "2025-11-01"
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: Decimal = Decimal(0)
    total_fees: Decimal = Decimal(0)
    avg_slippage_bps: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    trades: list[TradeAnalysis] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def net_pnl(self) -> Decimal:
        return self.total_pnl - self.total_fees


class PostTradeAnalyzer:
    """事后风控分析器."""

    def __init__(self) -> None:
        self._trades: list[TradeAnalysis] = []

    def record_trade(self, trade: TradeAnalysis) -> None:
        """记录一笔交易."""
        self._trades.append(trade)
        logger.info(
            "trade_recorded",
            instrument=trade.instrument_id,
            side=trade.side,
            pnl=str(trade.pnl),
            slippage_bps=trade.slippage_bps,
        )

    def generate_report(self, period: str) -> PostTradeReport:
        """生成事后分析报告."""
        if not self._trades:
            return PostTradeReport(period=period)

        winning = [t for t in self._trades if t.pnl > 0]
        losing = [t for t in self._trades if t.pnl <= 0]

        report = PostTradeReport(
            period=period,
            total_trades=len(self._trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            total_pnl=sum(t.pnl for t in self._trades),
            total_fees=sum(t.fees for t in self._trades),
            avg_slippage_bps=sum(t.slippage_bps for t in self._trades) / len(self._trades),
            trades=self._trades.copy(),
        )

        logger.info(
            "post_trade_report",
            period=period,
            trades=report.total_trades,
            pnl=str(report.total_pnl),
            win_rate=f"{report.win_rate:.1%}",
        )

        return report

    def clear(self) -> None:
        """清空交易记录."""
        self._trades.clear()
