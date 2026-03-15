"""对账 + 恢复模块单元测试.

ReconciliationEngine：本地 vs 交易所仓位比对逻辑
RecoveryManager：快照加载 + 恢复流程
"""

from __future__ import annotations

import pytest

from src.core.events import EventBus, EventType
from src.state.reconciliation import ReconciliationEngine
from src.state.recovery import RecoveryManager
from src.state.snapshot import PositionSnapshot, SnapshotManager, SystemSnapshot

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus():
    bus = EventBus()
    yield bus
    bus.clear()


@pytest.fixture
def reconciler(event_bus):
    return ReconciliationEngine(event_bus=event_bus)


@pytest.fixture
def snapshot_dir(tmp_path):
    return tmp_path / "snapshots"


@pytest.fixture
def snapshot_mgr(snapshot_dir):
    return SnapshotManager(snapshot_dir=snapshot_dir)


@pytest.fixture
def recovery_mgr(snapshot_mgr):
    return RecoveryManager(snapshot_mgr=snapshot_mgr)


@pytest.fixture
def recovery_mgr_with_reconciler(snapshot_mgr, reconciler):
    return RecoveryManager(snapshot_mgr=snapshot_mgr, reconciler=reconciler)


# ---------------------------------------------------------------------------
# ReconciliationEngine — 完全匹配场景
# ---------------------------------------------------------------------------


class TestReconciliationMatch:
    def test_empty_positions_match(self, reconciler):
        """本地和交易所都为空时，对账通过。"""
        result = reconciler.reconcile(
            local_positions=[],
            exchange_positions=[],
        )
        assert result.matched is True
        assert len(result.mismatches) == 0

    def test_identical_positions_match(self, reconciler):
        """本地与交易所仓位完全一致时，对账通过。"""
        pos = {"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"}
        result = reconciler.reconcile(
            local_positions=[pos],
            exchange_positions=[pos],
        )
        assert result.matched is True
        assert len(result.mismatches) == 0

    def test_multiple_identical_positions_match(self, reconciler):
        """多个仓位完全一致时，对账通过。"""
        local = [
            {"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"},
            {"instrument_id": "ETHUSDT-PERP.BINANCE", "quantity": "0.5"},
        ]
        result = reconciler.reconcile(local_positions=local, exchange_positions=local)
        assert result.matched is True

    def test_match_does_not_publish_alert(self, reconciler, event_bus):
        """对账一致时，不发布 RISK_ALERT 事件。"""
        alerts = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)

        pos = {"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"}
        reconciler.reconcile(local_positions=[pos], exchange_positions=[pos])

        assert len(alerts) == 0


# ---------------------------------------------------------------------------
# ReconciliationEngine — 不匹配场景
# ---------------------------------------------------------------------------


class TestReconciliationMismatch:
    def test_local_only_position_detected(self, reconciler):
        """本地有但交易所没有的仓位，识别为 local_only。"""
        result = reconciler.reconcile(
            local_positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"}],
            exchange_positions=[],
        )
        assert result.matched is False
        assert len(result.mismatches) == 1
        assert result.mismatches[0]["type"] == "local_only"

    def test_exchange_only_position_detected(self, reconciler):
        """交易所有但本地没有的仓位，识别为 exchange_only。"""
        result = reconciler.reconcile(
            local_positions=[],
            exchange_positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"}],
        )
        assert result.matched is False
        assert len(result.mismatches) == 1
        assert result.mismatches[0]["type"] == "exchange_only"

    def test_quantity_mismatch_detected(self, reconciler):
        """仓位数量不一致，识别为 quantity_mismatch。"""
        result = reconciler.reconcile(
            local_positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"}],
            exchange_positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.02"}],
        )
        assert result.matched is False
        assert len(result.mismatches) == 1
        assert result.mismatches[0]["type"] == "quantity_mismatch"

    def test_mismatch_publishes_critical_alert(self, reconciler, event_bus):
        """对账不一致时发布 CRITICAL 级别 RISK_ALERT。"""
        alerts = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)

        reconciler.reconcile(
            local_positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"}],
            exchange_positions=[],
        )

        assert len(alerts) == 1
        assert alerts[0].level == "CRITICAL"
        assert alerts[0].rule_name == "reconciliation_mismatch"

    def test_multiple_mismatches_all_detected(self, reconciler):
        """多个不一致全部被检出。"""
        result = reconciler.reconcile(
            local_positions=[
                {"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"},
                {"instrument_id": "ETHUSDT-PERP.BINANCE", "quantity": "0.5"},
            ],
            exchange_positions=[
                {"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.02"},  # 数量不符
                {"instrument_id": "SOLUSDT-PERP.BINANCE", "quantity": "10.0"},  # 本地没有
            ],
        )
        # BTCUSDT: quantity_mismatch
        # ETHUSDT: local_only
        # SOLUSDT: exchange_only
        assert result.matched is False
        assert len(result.mismatches) == 3

    def test_mismatch_result_contains_both_sides(self, reconciler):
        """quantity_mismatch 结果中同时包含 local 和 exchange 数据。"""
        result = reconciler.reconcile(
            local_positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"}],
            exchange_positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.02"}],
        )
        m = result.mismatches[0]
        assert m["local"] is not None
        assert m["exchange"] is not None
        assert m["local"]["quantity"] == "0.01"
        assert m["exchange"]["quantity"] == "0.02"

    def test_local_only_mismatch_exchange_field_is_none(self, reconciler):
        """local_only 类型的 mismatch，exchange 字段为 None。"""
        result = reconciler.reconcile(
            local_positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"}],
            exchange_positions=[],
        )
        assert result.mismatches[0]["exchange"] is None

    def test_exchange_only_mismatch_local_field_is_none(self, reconciler):
        """exchange_only 类型的 mismatch，local 字段为 None。"""
        result = reconciler.reconcile(
            local_positions=[],
            exchange_positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"}],
        )
        assert result.mismatches[0]["local"] is None


# ---------------------------------------------------------------------------
# RecoveryManager — 快照恢复流程
# ---------------------------------------------------------------------------


class TestRecoveryManager:
    def test_recover_returns_none_when_no_snapshot(self, recovery_mgr):
        """无快照时，recover() 返回 None（冷启动）。"""
        result = recovery_mgr.recover()
        assert result is None

    def test_recover_cold_starts_from_exchange_truth(self, recovery_mgr):
        result = recovery_mgr.recover(
            exchange_positions=[
                {
                    "instrument_id": "ETHUSDC-PERP.BINANCE",
                    "side": "LONG",
                    "quantity": "0.165",
                    "entry_price": "2400.3",
                    "unrealized_pnl": "-51.4",
                    "leverage": "1",
                }
            ],
            account_balance="1234.56",
        )

        assert result is not None
        assert len(result.positions) == 1
        assert result.account_balance == "1234.56"
        assert result.metadata["recovery_source"] == "exchange_truth"
        assert result.metadata["recovery_action"] == "cold_start_from_exchange"

    def test_recover_loads_latest_snapshot(self, recovery_mgr, snapshot_mgr):
        """有快照时，recover() 返回最新快照内容。"""
        snapshot = SystemSnapshot(
            positions=[
                PositionSnapshot(
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    side="LONG",
                    quantity="0.01",
                    avg_entry_price="50000",
                    unrealized_pnl="0",
                    realized_pnl="0",
                )
            ],
            account_balance="10000",
        )
        snapshot_mgr.save(snapshot)

        result = recovery_mgr.recover()

        assert result is not None
        assert len(result.positions) == 1
        assert result.positions[0].instrument_id == "BTCUSDT-PERP.BINANCE"

    def test_recover_marks_needs_reconciliation(self, recovery_mgr, snapshot_mgr):
        """恢复后快照 metadata 中包含 needs_reconciliation=True。"""
        snapshot = SystemSnapshot(account_balance="10000")
        snapshot_mgr.save(snapshot)

        result = recovery_mgr.recover()

        assert result is not None
        assert result.metadata.get("needs_reconciliation") is True

    def test_recover_marks_recovery_source(self, recovery_mgr, snapshot_mgr):
        """恢复后 metadata 中包含 recovery_source 标记。"""
        snapshot = SystemSnapshot(account_balance="10000")
        snapshot_mgr.save(snapshot)

        result = recovery_mgr.recover()

        assert result.metadata.get("recovery_source") == "local_snapshot"

    def test_snapshot_age_is_positive(self, recovery_mgr, snapshot_mgr):
        """_snapshot_age_seconds 对正常快照返回正数。"""
        snapshot = SystemSnapshot(account_balance="10000")
        snapshot_mgr.save(snapshot)

        result = recovery_mgr.recover()

        assert result is not None
        age = RecoveryManager._snapshot_age_seconds(result)
        assert age >= 0

    def test_recover_multiple_saves_uses_latest(self, recovery_mgr, snapshot_mgr):
        """多次保存快照时，recover 加载最新一个。"""
        snap1 = SystemSnapshot(account_balance="5000")
        snapshot_mgr.save(snap1)

        import time as _time
        _time.sleep(0.01)  # 确保时间戳不同

        snap2 = SystemSnapshot(account_balance="9999")
        snapshot_mgr.save(snap2)

        result = recovery_mgr.recover()

        # 应加载到 latest（snap2）
        assert result is not None
        assert result.account_balance == "9999"

    def test_recover_with_matching_exchange_positions_confirms_snapshot(
        self,
        recovery_mgr_with_reconciler,
        snapshot_mgr,
    ):
        snapshot = SystemSnapshot(
            positions=[
                PositionSnapshot(
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    side="LONG",
                    quantity="0.01",
                    avg_entry_price="50000",
                    unrealized_pnl="0",
                    realized_pnl="0",
                )
            ],
            account_balance="10000",
        )
        snapshot_mgr.save(snapshot)

        result = recovery_mgr_with_reconciler.recover(
            exchange_positions=[
                {
                    "instrument_id": "BTCUSDT-PERP.BINANCE",
                    "side": "LONG",
                    "quantity": "0.01",
                }
            ]
        )

        assert result is not None
        assert result.metadata["reconciliation_matched"] is True
        assert result.metadata["needs_reconciliation"] is False
        assert result.metadata["recovery_action"] == "snapshot_confirmed"

    def test_recover_with_mismatch_replaces_positions_from_exchange(
        self,
        recovery_mgr_with_reconciler,
        snapshot_mgr,
    ):
        snapshot = SystemSnapshot(
            positions=[
                PositionSnapshot(
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    side="LONG",
                    quantity="0.01",
                    avg_entry_price="50000",
                    unrealized_pnl="0",
                    realized_pnl="0",
                )
            ],
            account_balance="10000",
        )
        snapshot_mgr.save(snapshot)

        result = recovery_mgr_with_reconciler.recover(
            exchange_positions=[
                {
                    "instrument_id": "BTCUSDT-PERP.BINANCE",
                    "side": "LONG",
                    "quantity": "0.02",
                    "entry_price": "50010",
                    "unrealized_pnl": "10",
                }
            ]
        )

        assert result is not None
        assert result.metadata["reconciliation_matched"] is False
        assert result.metadata["recovery_source"] == "exchange_truth"
        assert result.metadata["recovery_action"] == "positions_replaced_from_exchange"

    def test_recover_mismatch_does_not_publish_risk_alert(
        self,
        recovery_mgr_with_reconciler,
        snapshot_mgr,
        event_bus,
    ):
        snapshot = SystemSnapshot(
            positions=[
                PositionSnapshot(
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    side="LONG",
                    quantity="0.01",
                    avg_entry_price="50000",
                    unrealized_pnl="0",
                    realized_pnl="0",
                )
            ],
        )
        snapshot_mgr.save(snapshot)
        alerts = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)

        result = recovery_mgr_with_reconciler.recover(
            exchange_positions=[
                {
                    "instrument_id": "ETHUSDC-PERP.BINANCE",
                    "side": "LONG",
                    "quantity": "0.165",
                }
            ]
        )

        assert alerts == []
        assert result is not None
        assert result.positions[0].instrument_id == "ETHUSDC-PERP.BINANCE"
        assert result.positions[0].quantity == "0.165"


# ---------------------------------------------------------------------------
# SnapshotManager 基础测试（服务于上层恢复路径）
# ---------------------------------------------------------------------------


class TestSnapshotManager:
    def test_save_creates_file(self, snapshot_mgr, snapshot_dir):
        """save() 在磁盘上创建快照文件。"""
        snapshot = SystemSnapshot(account_balance="10000")
        path = snapshot_mgr.save(snapshot)
        assert path.exists()

    def test_save_creates_latest_symlink(self, snapshot_mgr, snapshot_dir):
        """save() 同时更新 latest.json 符号链接。"""
        snapshot = SystemSnapshot(account_balance="10000")
        snapshot_mgr.save(snapshot)
        assert (snapshot_dir / "latest.json").exists()

    def test_load_latest_roundtrip(self, snapshot_mgr):
        """保存后再加载，内容一致（positions 和 account_balance）。"""
        original = SystemSnapshot(
            positions=[
                PositionSnapshot(
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    side="LONG",
                    quantity="0.01",
                    avg_entry_price="50000",
                    unrealized_pnl="100",
                    realized_pnl="0",
                )
            ],
            account_balance="10500",
        )
        snapshot_mgr.save(original)
        loaded = snapshot_mgr.load_latest()

        assert loaded is not None
        assert loaded.account_balance == "10500"
        assert len(loaded.positions) == 1
        assert loaded.positions[0].quantity == "0.01"

    def test_load_latest_returns_none_when_empty(self, snapshot_mgr):
        """没有任何快照时，load_latest() 返回 None。"""
        result = snapshot_mgr.load_latest()
        assert result is None
