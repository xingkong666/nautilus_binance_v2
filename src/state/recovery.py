"""崩溃恢复.

恢复优先级: snapshot → exchange truth → local cache → reconcile
"""

from __future__ import annotations

import structlog

from src.state.reconciliation import ReconciliationEngine, ReconciliationResult
from src.state.snapshot import PositionSnapshot, SnapshotManager, SystemSnapshot

logger = structlog.get_logger()


class RecoveryManager:
    """崩溃恢复管理器."""

    def __init__(
        self,
        snapshot_mgr: SnapshotManager,
        reconciler: ReconciliationEngine | None = None,
    ) -> None:
        self._snapshot_mgr = snapshot_mgr
        self._reconciler = reconciler

    def recover(
        self,
        exchange_positions: list[dict[str, str]] | None = None,
        account_balance: str | None = None,
    ) -> SystemSnapshot | None:
        """执行恢复流程.

        1. 尝试加载本地快照
        2. 如果有快照, 与交易所对账
        3. 以交易所为准修正本地状态

        Returns:
            恢复后的快照, 如果无快照则返回 None
        """
        # Step 1: 加载本地快照
        snapshot = self._snapshot_mgr.load_latest()
        if snapshot is None:
            if exchange_positions is None:
                logger.warning("recovery_no_snapshot", action="cold_start")
                return None
            snapshot = self._build_exchange_snapshot(exchange_positions, account_balance)
            logger.info(
                "recovery_cold_start_from_exchange",
                positions=len(snapshot.positions),
                account_balance=snapshot.account_balance,
            )
            return snapshot

        age_sec = self._snapshot_age_seconds(snapshot)
        logger.info("recovery_snapshot_loaded", age_seconds=age_sec, positions=len(snapshot.positions))

        snapshot.metadata["needs_reconciliation"] = True
        snapshot.metadata["recovery_source"] = "local_snapshot"
        snapshot.metadata["recovery_action"] = "awaiting_reconciliation"
        if account_balance is not None:
            snapshot.account_balance = account_balance

        if exchange_positions is None or self._reconciler is None:
            return snapshot

        result = self._reconciler.reconcile(
            local_positions=self._snapshot_positions_to_dicts(snapshot),
            exchange_positions=exchange_positions,
            publish_alerts=False,
        )
        self._apply_reconciliation(snapshot, result)

        return snapshot

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

        if result.matched:
            snapshot.metadata["needs_reconciliation"] = False
            snapshot.metadata["recovery_action"] = "snapshot_confirmed"
            return

        snapshot.positions = [
            self._dict_to_position_snapshot(position)
            for position in result.exchange_positions
        ]
        snapshot.metadata["needs_reconciliation"] = False
        snapshot.metadata["recovery_source"] = "exchange_truth"
        snapshot.metadata["recovery_action"] = "positions_replaced_from_exchange"
