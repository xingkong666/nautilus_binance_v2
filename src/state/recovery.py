"""崩溃恢复.

恢复优先级: snapshot → exchange truth → local cache → reconcile
"""

from __future__ import annotations

import structlog

from src.state.snapshot import SnapshotManager, SystemSnapshot

logger = structlog.get_logger()


class RecoveryManager:
    """崩溃恢复管理器."""

    def __init__(self, snapshot_mgr: SnapshotManager) -> None:
        self._snapshot_mgr = snapshot_mgr

    def recover(self) -> SystemSnapshot | None:
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
            logger.warning("recovery_no_snapshot", action="cold_start")
            return None

        age_sec = self._snapshot_age_seconds(snapshot)
        logger.info("recovery_snapshot_loaded", age_seconds=age_sec, positions=len(snapshot.positions))

        # Step 2: 标记需要与交易所对账
        # 实际对账逻辑在 reconciliation.py
        snapshot.metadata["needs_reconciliation"] = True
        snapshot.metadata["recovery_source"] = "local_snapshot"

        return snapshot

    @staticmethod
    def _snapshot_age_seconds(snapshot: SystemSnapshot) -> float:
        """计算快照年龄."""
        import time

        return (time.time_ns() - snapshot.timestamp_ns) / 1e9
