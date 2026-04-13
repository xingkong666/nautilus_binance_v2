"""崩溃恢复.

恢复优先级: snapshot → exchange truth → local cache → reconcile

增强版（v2）：
- RecoveryReport 数据类，包含恢复来源、对账结果、孤立订单、建议动作
- recover() 返回 RecoveryReport（原 SystemSnapshot | None 仍可从 report.snapshot 取得）
- recommended_action 三级：proceed / manual_review / halt
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from src.state.reconciliation import ReconciliationEngine, ReconciliationResult
from src.state.snapshot import PositionSnapshot, SnapshotManager, SystemSnapshot

logger = structlog.get_logger(__name__)

# mismatch 数量超过此阈值时 recommended_action = "halt"
_HALT_MISMATCH_THRESHOLD = 5


@dataclass
class RecoveryReport:
    """恢复流程执行报告.

    Attributes:
        snapshot_age_sec: 快照年龄（秒）；冷启动时为 0.0。
        recovery_source: 恢复来源标识。
            "local_snapshot" | "exchange_truth" | "cold_start" | "none"
        reconciliation_matched: 对账是否完全匹配。
        mismatch_count: 对账不一致条目数。
        mismatches: 不一致详情列表。
        orphan_orders: 孤立挂单列表（交易所有但本系统不知道的）。
        recommended_action: 建议后续动作。
            "proceed" — 正常继续启动
            "manual_review" — 有少量不一致，记录日志后继续但建议人工检查
            "halt" — 不一致过多，建议阻断启动等待人工处理
        snapshot: 恢复后的系统快照，可为 None（无任何可用快照时）。

    """

    snapshot_age_sec: float
    recovery_source: str
    reconciliation_matched: bool
    mismatch_count: int
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    orphan_orders: list[dict[str, Any]] = field(default_factory=list)
    recommended_action: str = "proceed"
    snapshot: SystemSnapshot | None = None


def _compute_recommended_action(
    mismatch_count: int,
    orphan_count: int,
    halt_threshold: int = _HALT_MISMATCH_THRESHOLD,
) -> str:
    """根据不一致数量计算建议动作."""
    if mismatch_count > halt_threshold:
        return "halt"
    if mismatch_count > 0 or orphan_count > 0:
        return "manual_review"
    return "proceed"


class RecoveryManager:
    """崩溃恢复管理器."""

    def __init__(
        self,
        snapshot_mgr: SnapshotManager,
        reconciler: ReconciliationEngine | None = None,
        halt_mismatch_threshold: int = _HALT_MISMATCH_THRESHOLD,
    ) -> None:
        """Initialize the recovery manager.

        Args:
            snapshot_mgr: Snapshot mgr.
            reconciler: Reconciler.
            halt_mismatch_threshold: mismatch 数量超过此值时 recommended_action="halt"。

        """
        self._snapshot_mgr = snapshot_mgr
        self._reconciler = reconciler
        self._halt_threshold = halt_mismatch_threshold

    def recover(
        self,
        exchange_positions: list[dict[str, str]] | None = None,
        account_balance: str | None = None,
        exchange_open_orders: list[dict[str, Any]] | None = None,
        known_client_order_ids: set[str] | None = None,
    ) -> RecoveryReport:
        """执行恢复流程，返回 RecoveryReport.

        1. 尝试加载本地快照
        2. 如果有快照，与交易所对账
        3. 以交易所为准修正本地状态

        Args:
            exchange_positions: 交易所仓位列表。
            account_balance: 账户余额字符串（来自交易所）。
            exchange_open_orders: 交易所当前挂单（用于孤立订单检测）。
            known_client_order_ids: 本系统已知的 clientOrderId 集合。

        Returns:
            RecoveryReport，含 snapshot 字段（可能为 None）。

        """
        # Step 1: 加载本地快照
        snapshot = self._snapshot_mgr.load_latest()

        if snapshot is None:
            if exchange_positions is None:
                logger.warning("recovery_no_snapshot", action="cold_start")
                return RecoveryReport(
                    snapshot_age_sec=0.0,
                    recovery_source="none",
                    reconciliation_matched=True,
                    mismatch_count=0,
                    recommended_action="proceed",
                    snapshot=None,
                )

            snapshot = self._build_exchange_snapshot(exchange_positions, account_balance)
            logger.info(
                "recovery_cold_start_from_exchange",
                positions=len(snapshot.positions),
                account_balance=snapshot.account_balance,
            )
            return RecoveryReport(
                snapshot_age_sec=0.0,
                recovery_source="exchange_truth",
                reconciliation_matched=True,
                mismatch_count=0,
                recommended_action="proceed",
                snapshot=snapshot,
            )

        age_sec = self._snapshot_age_seconds(snapshot)
        logger.info("recovery_snapshot_loaded", age_seconds=age_sec, positions=len(snapshot.positions))

        snapshot.metadata["needs_reconciliation"] = True
        snapshot.metadata["recovery_source"] = "local_snapshot"
        snapshot.metadata["recovery_action"] = "awaiting_reconciliation"
        if account_balance is not None:
            snapshot.account_balance = account_balance

        if exchange_positions is None or self._reconciler is None:
            return RecoveryReport(
                snapshot_age_sec=age_sec,
                recovery_source="local_snapshot",
                reconciliation_matched=False,
                mismatch_count=0,
                recommended_action="manual_review",
                snapshot=snapshot,
            )

        result = self._reconciler.reconcile(
            local_positions=self._snapshot_positions_to_dicts(snapshot),
            exchange_positions=exchange_positions,
            exchange_open_orders=exchange_open_orders,
            known_client_order_ids=known_client_order_ids,
            publish_alerts=False,
        )
        self._apply_reconciliation(snapshot, result)

        action = _compute_recommended_action(
            mismatch_count=len(result.mismatches),
            orphan_count=len(result.orphan_orders),
            halt_threshold=self._halt_threshold,
        )

        return RecoveryReport(
            snapshot_age_sec=age_sec,
            recovery_source=snapshot.metadata.get("recovery_source", "local_snapshot"),
            reconciliation_matched=result.matched,
            mismatch_count=len(result.mismatches),
            mismatches=list(result.mismatches),
            orphan_orders=list(result.orphan_orders),
            recommended_action=action,
            snapshot=snapshot,
        )

    @staticmethod
    def _snapshot_age_seconds(snapshot: SystemSnapshot) -> float:
        """计算快照年龄."""
        import time

        return (time.time_ns() - snapshot.timestamp_ns) / 1e9

    @staticmethod
    def _snapshot_positions_to_dicts(snapshot: SystemSnapshot) -> list[dict[str, str]]:
        return [
            {
                "instrument_id": position.instrument_id,
                "side": position.side,
                "quantity": position.quantity,
            }
            for position in snapshot.positions
        ]

    @staticmethod
    def _dict_to_position_snapshot(position: dict[str, str]) -> PositionSnapshot:
        return PositionSnapshot(
            instrument_id=str(position.get("instrument_id", "")),
            side=str(position.get("side", "BOTH")),
            quantity=str(position.get("quantity", "0")),
            avg_entry_price=str(position.get("avg_entry_price", position.get("entry_price", "0"))),
            unrealized_pnl=str(position.get("unrealized_pnl", "0")),
            realized_pnl=str(position.get("realized_pnl", "0")),
        )

    def _build_exchange_snapshot(
        self,
        exchange_positions: list[dict[str, str]],
        account_balance: str | None,
    ) -> SystemSnapshot:
        snapshot = SystemSnapshot(
            positions=[self._dict_to_position_snapshot(position) for position in exchange_positions],
            account_balance=account_balance or "0",
        )
        snapshot.metadata["needs_reconciliation"] = False
        snapshot.metadata["reconciliation_matched"] = True
        snapshot.metadata["reconciliation_mismatch_count"] = 0
        snapshot.metadata["recovery_source"] = "exchange_truth"
        snapshot.metadata["recovery_action"] = "cold_start_from_exchange"
        return snapshot

    def _apply_reconciliation(self, snapshot: SystemSnapshot, result: ReconciliationResult) -> None:
        snapshot.metadata["reconciliation_matched"] = result.matched
        snapshot.metadata["reconciliation_mismatch_count"] = len(result.mismatches)
        snapshot.metadata["orphan_order_count"] = len(result.orphan_orders)

        if result.matched:
            snapshot.metadata["needs_reconciliation"] = False
            snapshot.metadata["recovery_action"] = "snapshot_confirmed"
            return

        snapshot.positions = [self._dict_to_position_snapshot(position) for position in result.exchange_positions]
        snapshot.metadata["needs_reconciliation"] = False
        snapshot.metadata["recovery_source"] = "exchange_truth"
        snapshot.metadata["recovery_action"] = "positions_replaced_from_exchange"
