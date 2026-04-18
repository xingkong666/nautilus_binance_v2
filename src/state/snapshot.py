"""状态快照.

定期保存系统状态, 用于崩溃恢复.

写入策略：先写 .tmp 临时文件，再原子 rename，防止写入中途崩溃损坏快照。
加载策略：校验 JSON schema，损坏时自动 fallback 到上一个有效快照。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class DecimalEncoder(json.JSONEncoder):
    """支持 Decimal 的 JSON 编码器."""

    def default(self, o: Any) -> Any:
        """Run default.

        Args:
            o: O.

        Returns:
            Any: Result of default.
        """
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


@dataclass
class PositionSnapshot:
    """仓位快照."""

    instrument_id: str
    side: str  # 多头 / 空头 / 空仓
    quantity: str  # Decimal 字符串
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


def _validate_snapshot_schema(data: dict[str, Any]) -> None:
    """校验快照 JSON 的必要字段.

    Args:
        data: 已解析的快照 JSON 字典。

    Raises:
        ValueError: 缺少必要字段或类型不符。

    """
    if not isinstance(data.get("timestamp_ns"), int):
        raise ValueError("snapshot missing or invalid field: timestamp_ns (expected int)")
    if not isinstance(data.get("positions"), list):
        raise ValueError("snapshot missing or invalid field: positions (expected list)")
    if not isinstance(data.get("account_balance"), str):
        raise ValueError("snapshot missing or invalid field: account_balance (expected str)")


def _parse_snapshot(data: dict[str, Any]) -> SystemSnapshot:
    """将已校验的 JSON 字典转换为 SystemSnapshot.

    Args:
        data: 已解析并校验的快照字典。

    Returns:
        SystemSnapshot 实例。

    """
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


class SnapshotManager:
    """状态快照管理器."""

    def __init__(self, snapshot_dir: Path) -> None:
        """Initialize the snapshot manager.

        Args:
            snapshot_dir: Directory for snapshot.
        """
        self._snapshot_dir = snapshot_dir
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)

    def save(self, snapshot: SystemSnapshot) -> Path:
        """原子写入快照到磁盘（先写 .tmp，再 os.replace）.

        Args:
            snapshot: Snapshot payload to persist.

        Returns:
            写入的快照文件路径。

        """
        filename = f"snapshot_{snapshot.timestamp_ns}.json"
        filepath = self._snapshot_dir / filename
        tmp_path = filepath.with_suffix(".tmp")

        serialized = json.dumps(asdict(snapshot), cls=DecimalEncoder, indent=2)
        tmp_path.write_text(serialized)
        # os.replace 在 POSIX 上是原子操作；Windows 上也是原子替换
        os.replace(tmp_path, filepath)

        # 更新 latest 符号链接（先建新链接再替换旧链接，保证原子性）
        latest = self._snapshot_dir / "latest.json"
        latest_tmp = self._snapshot_dir / "latest.json.tmp"
        if latest_tmp.exists():
            latest_tmp.unlink()
        latest_tmp.symlink_to(filepath.name)
        os.replace(latest_tmp, latest)

        logger.info("snapshot_saved", path=str(filepath))
        return filepath

    def load_latest(self) -> SystemSnapshot | None:
        """加载最新快照，损坏时自动 fallback 到上一个有效快照.

        Returns:
            SystemSnapshot 或 None（无任何可用快照时）。

        """
        latest = self._snapshot_dir / "latest.json"
        if latest.exists():
            snapshot = self._try_load_file(latest)
            if snapshot is not None:
                return snapshot
            logger.warning("snapshot_latest_corrupted_trying_fallback")

        return self._load_fallback_snapshot()

    def _try_load_file(self, path: Path) -> SystemSnapshot | None:
        """尝试加载并校验单个快照文件.

        Args:
            path: 快照文件路径（可能是符号链接）。

        Returns:
            成功则返回 SystemSnapshot，失败返回 None。

        """
        try:
            data = json.loads(path.read_text())
            _validate_snapshot_schema(data)
            return _parse_snapshot(data)
        except (json.JSONDecodeError, ValueError, KeyError, OSError) as exc:
            logger.error("snapshot_load_failed", path=str(path), error=str(exc))
            return None

    def _load_fallback_snapshot(self) -> SystemSnapshot | None:
        """按时间戳倒序扫描快照文件，返回第一个有效的.

        Returns:
            最近一个有效的 SystemSnapshot，或 None。

        """
        candidates = sorted(self._snapshot_dir.glob("snapshot_*.json"), reverse=True)
        for candidate in candidates:
            snapshot = self._try_load_file(candidate)
            if snapshot is not None:
                logger.info("snapshot_fallback_loaded", path=str(candidate))
                return snapshot
        logger.warning("no_snapshot_found")
        return None

    def cleanup(self, keep_count: int = 10) -> None:
        """清理旧快照, 只保留最近 N 个.

        Args:
            keep_count: Number of historical snapshots to retain.
        """
        snapshots = sorted(self._snapshot_dir.glob("snapshot_*.json"))
        for old in snapshots[:-keep_count]:
            old.unlink()
            logger.debug("snapshot_cleaned", path=str(old))
