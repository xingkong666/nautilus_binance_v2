"""对账模块.

定期将本地状态与交易所真实状态对比, 发现不一致时触发告警和修复.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from src.core.events import EventBus, RiskAlertEvent

logger = structlog.get_logger()


@dataclass
class ReconciliationResult:
    """对账结果."""

    matched: bool
    local_positions: list[dict[str, Any]]
    exchange_positions: list[dict[str, Any]]
    mismatches: list[dict[str, Any]]


class ReconciliationEngine:
    """对账引擎."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus

    def reconcile(
        self,
        local_positions: list[dict[str, Any]],
        exchange_positions: list[dict[str, Any]],
        publish_alerts: bool = True,
    ) -> ReconciliationResult:
        """执行对账.

        Args:
            local_positions: 本地仓位列表
            exchange_positions: 交易所仓位列表

        Returns:
            对账结果
        """
        local_map = {self._position_key(p): p for p in local_positions}
        exchange_map = {self._position_key(p): p for p in exchange_positions}

        mismatches: list[dict[str, Any]] = []

        # 检查本地有但交易所没有的
        for position_key, local_pos in local_map.items():
            inst_id = str(local_pos["instrument_id"])
            if position_key not in exchange_map:
                mismatches.append(
                    {
                        "instrument_id": inst_id,
                        "side": local_pos.get("side", "BOTH"),
                        "type": "local_only",
                        "local": local_pos,
                        "exchange": None,
                    }
                )
            elif not self._quantities_match(local_pos.get("quantity"), exchange_map[position_key].get("quantity")):
                mismatches.append(
                    {
                        "instrument_id": inst_id,
                        "side": local_pos.get("side", "BOTH"),
                        "type": "quantity_mismatch",
                        "local": local_pos,
                        "exchange": exchange_map[position_key],
                    }
                )

        # 检查交易所有但本地没有的
        for position_key, ex_pos in exchange_map.items():
            inst_id = str(ex_pos["instrument_id"])
            if position_key not in local_map:
                mismatches.append(
                    {
                        "instrument_id": inst_id,
                        "side": ex_pos.get("side", "BOTH"),
                        "type": "exchange_only",
                        "local": None,
                        "exchange": ex_pos,
                    }
                )

        result = ReconciliationResult(
            matched=len(mismatches) == 0,
            local_positions=local_positions,
            exchange_positions=exchange_positions,
            mismatches=mismatches,
        )

        if not result.matched:
            logger.error("reconciliation_mismatch", mismatch_count=len(mismatches), details=mismatches)
            if publish_alerts:
                self._event_bus.publish(
                    RiskAlertEvent(
                        level="CRITICAL",
                        rule_name="reconciliation_mismatch",
                        message=f"对账不一致: {len(mismatches)} 笔",
                        details={"mismatches": mismatches},
                    )
                )
        else:
            logger.info("reconciliation_ok", position_count=len(local_positions))

        return result

    @staticmethod
    def _position_key(position: dict[str, Any]) -> str:
        instrument_id = str(position.get("instrument_id", ""))
        side = str(position.get("side", "BOTH")).upper()
        if side and side != "BOTH":
            return f"{instrument_id}:{side}"
        return instrument_id

    @staticmethod
    def _quantities_match(left: Any, right: Any) -> bool:
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except (InvalidOperation, ValueError, TypeError):
            return left == right
