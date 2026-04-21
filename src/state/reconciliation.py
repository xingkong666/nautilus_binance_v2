"""对账模块.

定期将本地状态与交易所真实状态对比, 发现不一致时触发告警和修复.

增强版（v2）：
- 数量比对改为相对容差（默认 0.01%），避免浮点误报
- 新增孤立挂单检测（exchange_open_orders 中未被本系统创建的订单）
- ReconciliationResult 新增 orphan_orders 字段（向后兼容，默认空列表）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from src.core.events import EventBus, RiskAlertEvent

logger = structlog.get_logger(__name__)

# 仓位数量相对容差（0.01% = 0.0001）
_QUANTITY_TOLERANCE = Decimal("0.0001")


@dataclass
class ReconciliationResult:
    """对账结果."""

    matched: bool
    local_positions: list[dict[str, Any]]
    exchange_positions: list[dict[str, Any]]
    mismatches: list[dict[str, Any]]
    orphan_orders: list[dict[str, Any]] = field(default_factory=list)
    pending_cancel_orders: list[dict[str, Any]] = field(default_factory=list)


class ReconciliationEngine:
    """对账引擎."""

    def __init__(self, event_bus: EventBus) -> None:
        """Initialize the reconciliation engine.

        Args:
            event_bus: Event bus used for cross-module communication.
        """
        self._event_bus = event_bus

    def reconcile(
        self,
        local_positions: list[dict[str, Any]],
        exchange_positions: list[dict[str, Any]],
        exchange_open_orders: list[dict[str, Any]] | None = None,
        known_client_order_ids: set[str] | None = None,
        pending_cancel_orders: list[dict[str, Any]] | None = None,
        publish_alerts: bool = True,
    ) -> ReconciliationResult:
        """执行对账.

        Args:
            local_positions: 本地仓位列表。
            exchange_positions: 交易所仓位列表。
            exchange_open_orders: 交易所当前挂单列表（可选）。传入后会检测孤立订单。
            known_client_order_ids: 本系统已知的客户端订单 ID 集合（可选）。
                未在此集合中的交易所挂单视为孤立订单。
            pending_cancel_orders: PENDING_CANCEL 状态的订单列表（可选）。
            publish_alerts: 出现不一致时是否向 EventBus 发布风控告警。

        Returns:
            对账结果。

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

        # 孤立挂单检测
        orphan_orders = self._detect_orphan_orders(
            exchange_open_orders=exchange_open_orders or [],
            known_client_order_ids=known_client_order_ids or set(),
        )

        # 处理 PENDING_CANCEL 残留订单
        pending_cancel_orders = pending_cancel_orders or []
        if pending_cancel_orders:
            logger.warning("pending_cancel_residual_detected", count=len(pending_cancel_orders), orders=pending_cancel_orders)
            if publish_alerts:
                self._event_bus.publish(
                    RiskAlertEvent(
                        level="WARNING",
                        rule_name="pending_cancel_residual",
                        message=f"发现 {len(pending_cancel_orders)} 笔 PENDING_CANCEL 残留订单",
                        details={"orders": pending_cancel_orders},
                    )
                )

        result = ReconciliationResult(
            matched=len(mismatches) == 0,
            local_positions=local_positions,
            exchange_positions=exchange_positions,
            mismatches=mismatches,
            orphan_orders=orphan_orders,
            pending_cancel_orders=pending_cancel_orders,
        )

        if not result.matched or orphan_orders:
            logger.error(
                "reconciliation_mismatch",
                mismatch_count=len(mismatches),
                orphan_order_count=len(orphan_orders),
                details=mismatches,
            )
            if publish_alerts:
                self._event_bus.publish(
                    RiskAlertEvent(
                        level="CRITICAL",
                        rule_name="reconciliation_mismatch",
                        message=f"对账不一致: {len(mismatches)} 笔，孤立挂单: {len(orphan_orders)} 笔",
                        details={"mismatches": mismatches, "orphan_orders": orphan_orders},
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
        """相对容差对比（容差 0.01%），比严格等值更健壮."""
        try:
            lv = Decimal(str(left))
            r = Decimal(str(right))
        except (InvalidOperation, ValueError, TypeError):
            return left == right

        if lv == 0 and r == 0:
            return True
        denom = max(abs(lv), abs(r))
        if denom == 0:
            return True
        return abs(lv - r) / denom <= _QUANTITY_TOLERANCE

    @staticmethod
    def _detect_orphan_orders(
        exchange_open_orders: list[dict[str, Any]],
        known_client_order_ids: set[str],
    ) -> list[dict[str, Any]]:
        """检测孤立挂单（交易所有但本系统不知道的）."""
        if not exchange_open_orders:
            return []

        orphans = []
        for order in exchange_open_orders:
            client_id = str(order.get("clientOrderId", "")).strip()
            if not client_id or client_id not in known_client_order_ids:
                orphans.append(order)
        return orphans
