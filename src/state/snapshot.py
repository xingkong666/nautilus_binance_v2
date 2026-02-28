"""状态快照.

定期保存系统状态, 用于崩溃恢复.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


class DecimalEncoder(json.JSONEncoder):
    """支持 Decimal 的 JSON 编码器."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


@dataclass
class PositionSnapshot:
    """仓位快照."""

    instrument_id: str
    side: str  # LONG / SHORT / FLAT
    quantity: str  # Decimal as string
    avg_entry_price: str
    unrealized_pnl: str
    realized_pnl: str


@dataclass
class SystemSnapshot:
    """系统状态快照."""

    timestamp_ns: int = field(default_factory=lambda: time.time_ns())
    positions: list[PositionSnapshot] = field(default_factory=list)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    account_balance: str = "0"
    strategy_state: dict[str, Any] = field(default_factory=dict)
    risk_state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class SnapshotManager:
    """状态快照管理器."""

    def __init__(self, snapshot_dir: Path) -> None:
        self._snapshot_dir = snapshot_dir
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)

    def save(self, snapshot: SystemSnapshot) -> Path:
        """保存快照到磁盘."""
        filename = f"snapshot_{snapshot.timestamp_ns}.json"
        filepath = self._snapshot_dir / filename
        with open(filepath, "w") as f:
            json.dump(asdict(snapshot), f, cls=DecimalEncoder, indent=2)

        # 同时保存一个 latest 符号链接
        latest = self._snapshot_dir / "latest.json"
        if latest.exists():
            latest.unlink()
        latest.symlink_to(filepath.name)

        logger.info("snapshot_saved", path=str(filepath))
        return filepath

    def load_latest(self) -> SystemSnapshot | None:
        """加载最新快照."""
        latest = self._snapshot_dir / "latest.json"
        if not latest.exists():
            logger.warning("no_snapshot_found")
            return None

        with open(latest) as f:
            data = json.load(f)

        positions = [PositionSnapshot(**p) for p in data.get("positions", [])]
        return SystemSnapshot(
            timestamp_ns=data["timestamp_ns"],
            positions=positions,
            open_orders=data.get("open_orders", []),
            account_balance=data.get("account_balance", "0"),
            strategy_state=data.get("strategy_state", {}),
            risk_state=data.get("risk_state", {}),
            metadata=data.get("metadata", {}),
        )

    def cleanup(self, keep_count: int = 10) -> None:
        """清理旧快照, 只保留最近 N 个."""
        snapshots = sorted(self._snapshot_dir.glob("snapshot_*.json"))
        for old in snapshots[:-keep_count]:
            old.unlink()
            logger.debug("snapshot_cleaned", path=str(old))
