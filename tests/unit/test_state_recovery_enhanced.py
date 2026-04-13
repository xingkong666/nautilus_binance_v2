"""State Recovery Enhanced — Unit Tests.

Covers:
  1. Atomic snapshot write (.tmp → rename)
  2. Corrupted latest.json → fallback to previous snapshot
  3. Schema validation raises ValueError on missing fields
  4. SnapshotScheduler triggers state_provider on interval
  5. SnapshotScheduler implements Watchable (last_heartbeat_ns updates)
  6. ReconciliationEngine quantity tolerance (0.001% → matched)
  7. ReconciliationEngine orphan order detection
  8. RecoveryReport fields and recommended_action logic
  9. RecoveryManager recommended_action="halt" when mismatch > 5
 10. LiveSupervisor DEGRADED triggers _attempt_recovery
 11. LiveSupervisor recovery succeeds → state=RUNNING, error_count=0
 12. LiveSupervisor recovery exhausted → stop_event set
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.state.recovery import RecoveryManager, RecoveryReport, _compute_recommended_action
from src.state.snapshot import SnapshotManager, SystemSnapshot, _validate_snapshot_schema
from src.state.snapshot_scheduler import SnapshotScheduler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot_manager(tmp_dir: Path) -> SnapshotManager:
    return SnapshotManager(snapshot_dir=tmp_dir)


def _make_snapshot() -> SystemSnapshot:
    return SystemSnapshot(account_balance="1000")


# ---------------------------------------------------------------------------
# Test 1 — Atomic snapshot write (.tmp → rename)
# ---------------------------------------------------------------------------


def test_snapshot_atomic_write() -> None:
    """.tmp file is removed after successful save; only the final file exists."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_snapshot_manager(Path(d))
        snap = _make_snapshot()
        saved_path = mgr.save(snap)

        assert saved_path.exists(), "Snapshot file must exist after save"
        tmp = saved_path.with_suffix(".tmp")
        assert not tmp.exists(), ".tmp file must be removed after atomic rename"

        # Content must be valid JSON
        data = json.loads(saved_path.read_text())
        assert data["account_balance"] == "1000"


# ---------------------------------------------------------------------------
# Test 2 — Corrupted latest.json → fallback to previous snapshot
# ---------------------------------------------------------------------------


def test_snapshot_load_corrupted_fallback() -> None:
    """Corrupted latest.json → load_latest() returns the previous valid snapshot."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_snapshot_manager(Path(d))

        # Save a valid snapshot first
        snap1 = SystemSnapshot(account_balance="999")
        mgr.save(snap1)

        # Corrupt latest.json
        latest = Path(d) / "latest.json"
        latest.unlink(missing_ok=True)
        latest.write_text("{invalid json!!}")  # corrupt

        result = mgr.load_latest()
        assert result is not None, "Should fall back to a valid snapshot"
        assert result.account_balance == "999"


# ---------------------------------------------------------------------------
# Test 3 — Schema validation raises ValueError on missing fields
# ---------------------------------------------------------------------------


def test_snapshot_schema_validation_missing_timestamp() -> None:
    """Missing timestamp_ns → ValueError."""
    with pytest.raises(ValueError, match="timestamp_ns"):
        _validate_snapshot_schema({"positions": [], "account_balance": "0"})


def test_snapshot_schema_validation_missing_positions() -> None:
    """Missing positions → ValueError."""
    with pytest.raises(ValueError, match="positions"):
        _validate_snapshot_schema({"timestamp_ns": 123, "account_balance": "0"})


def test_snapshot_schema_validation_missing_balance() -> None:
    """Missing account_balance → ValueError."""
    with pytest.raises(ValueError, match="account_balance"):
        _validate_snapshot_schema({"timestamp_ns": 123, "positions": []})


def test_snapshot_schema_validation_ok() -> None:
    """Valid schema → no exception."""
    _validate_snapshot_schema({"timestamp_ns": 1, "positions": [], "account_balance": "0"})


# ---------------------------------------------------------------------------
# Test 4 — SnapshotScheduler triggers state_provider on interval
# ---------------------------------------------------------------------------


def test_snapshot_scheduler_triggers() -> None:
    """Scheduler calls state_provider at least twice within 1.5x interval."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_snapshot_manager(Path(d))
        calls: list[int] = []

        def _provider() -> SystemSnapshot:
            calls.append(1)
            return SystemSnapshot()

        scheduler = SnapshotScheduler(mgr, _provider, interval_sec=0.2)
        scheduler.start()
        time.sleep(0.65)
        scheduler.stop()

        assert len(calls) >= 2, f"Expected >=2 snapshot calls, got {len(calls)}"


# ---------------------------------------------------------------------------
# Test 5 — SnapshotScheduler implements Watchable (last_heartbeat_ns updates)
# ---------------------------------------------------------------------------


def test_snapshot_scheduler_is_watchable() -> None:
    """last_heartbeat_ns must increase after each successful snapshot."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_snapshot_manager(Path(d))

        scheduler = SnapshotScheduler(mgr, lambda: SystemSnapshot(), interval_sec=0.1)
        initial_hb = scheduler.last_heartbeat_ns

        scheduler.start()
        time.sleep(0.35)
        scheduler.stop()

        assert scheduler.last_heartbeat_ns > initial_hb, "last_heartbeat_ns should advance after snapshots"
        assert not scheduler.is_running


# ---------------------------------------------------------------------------
# Test 6 — ReconciliationEngine quantity tolerance (0.001% → matched)
# ---------------------------------------------------------------------------


def test_reconciliation_quantity_tolerance() -> None:
    """A 0.001% quantity difference must be treated as matched."""
    from src.state.reconciliation import ReconciliationEngine

    bus = MagicMock()
    engine = ReconciliationEngine(event_bus=bus)

    local = [{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "1.00000", "side": "LONG"}]
    # 0.00001 / 1.00000 = 0.001% < tolerance 0.01%
    exchange = [{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "1.00001", "side": "LONG"}]

    result = engine.reconcile(local_positions=local, exchange_positions=exchange)
    assert result.matched, "Quantities within tolerance should match"
    assert result.mismatches == []


# ---------------------------------------------------------------------------
# Test 7 — ReconciliationEngine orphan order detection
# ---------------------------------------------------------------------------


def test_reconciliation_orphan_orders() -> None:
    """Exchange order not in known_client_order_ids → appears in orphan_orders."""
    from src.state.reconciliation import ReconciliationEngine

    bus = MagicMock()
    engine = ReconciliationEngine(event_bus=bus)

    exchange_open_orders = [
        {"clientOrderId": "unknown-123", "symbol": "BTCUSDT"},
        {"clientOrderId": "known-456", "symbol": "ETHUSDT"},
    ]
    known_ids = {"known-456"}

    result = engine.reconcile(
        local_positions=[],
        exchange_positions=[],
        exchange_open_orders=exchange_open_orders,
        known_client_order_ids=known_ids,
    )
    assert len(result.orphan_orders) == 1
    assert result.orphan_orders[0]["clientOrderId"] == "unknown-123"


# ---------------------------------------------------------------------------
# Test 8 — RecoveryReport fields and recommended_action logic
# ---------------------------------------------------------------------------


def test_recovery_report_fields() -> None:
    """RecoveryReport is a well-formed dataclass with correct recommended_action."""
    report = RecoveryReport(
        snapshot_age_sec=30.0,
        recovery_source="local_snapshot",
        reconciliation_matched=True,
        mismatch_count=0,
        mismatches=[],
        orphan_orders=[],
        recommended_action="proceed",
        snapshot=None,
    )
    assert report.snapshot_age_sec == 30.0
    assert report.recovery_source == "local_snapshot"
    assert report.recommended_action == "proceed"


def test_compute_recommended_action_proceed() -> None:
    """0 mismatches, 0 orphans → proceed."""
    assert _compute_recommended_action(0, 0, halt_threshold=5) == "proceed"


def test_compute_recommended_action_manual_review_mismatches() -> None:
    """2 mismatches below halt threshold → manual_review."""
    assert _compute_recommended_action(2, 0, halt_threshold=5) == "manual_review"


def test_compute_recommended_action_manual_review_orphans() -> None:
    """0 mismatches but 3 orphan orders → manual_review."""
    assert _compute_recommended_action(0, 3, halt_threshold=5) == "manual_review"


def test_compute_recommended_action_halt() -> None:
    """mismatch_count > halt_threshold → halt."""
    assert _compute_recommended_action(6, 0, halt_threshold=5) == "halt"


# ---------------------------------------------------------------------------
# Test 9 — RecoveryManager recommended_action="halt" when mismatch > 5
# ---------------------------------------------------------------------------


def test_recovery_halt_on_many_mismatches() -> None:
    """mismatch_count > 5 → recommended_action='halt'."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_snapshot_manager(Path(d))

        # Save a snapshot so recovery has something to load
        snap = SystemSnapshot(account_balance="500")
        mgr.save(snap)

        # 6 mismatching exchange positions not in local snapshot
        exchange_positions = [
            {"instrument_id": f"TOKEN{i}-PERP.BINANCE", "quantity": "1.0", "side": "LONG"} for i in range(6)
        ]

        mock_reconciler = MagicMock()
        mock_result = MagicMock()
        mock_result.matched = False
        mock_result.mismatches = [{"instrument_id": f"TOKEN{i}"} for i in range(6)]
        mock_result.orphan_orders = []
        mock_reconciler.reconcile.return_value = mock_result

        recovery = RecoveryManager(snapshot_mgr=mgr, reconciler=mock_reconciler)
        report = recovery.recover(
            exchange_positions=exchange_positions,
            account_balance="500",
        )

        assert report.recommended_action == "halt"
        assert report.mismatch_count == 6


# ---------------------------------------------------------------------------
# Test 10 — LiveSupervisor DEGRADED triggers _attempt_recovery
# ---------------------------------------------------------------------------


def test_supervisor_degraded_attempts_recovery() -> None:
    """_on_circuit_breaker must schedule _attempt_recovery when loop is running."""
    import asyncio as _asyncio

    from src.core.events import Event, EventType
    from src.live.supervisor import LiveSupervisor, SupervisorState

    container = MagicMock()
    sup = LiveSupervisor(container, max_recovery_attempts=2, recovery_backoff_base_sec=0.01)

    event = MagicMock(spec=Event)
    event.payload = {}
    event.event_type = EventType.CIRCUIT_BREAKER

    with patch.object(_asyncio, "run_coroutine_threadsafe") as mock_rcts:
        # Simulate a running event loop
        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True
        sup._loop = mock_loop

        sup._on_circuit_breaker(event)

        assert sup.state == SupervisorState.DEGRADED
        assert sup._error_count == 1
        mock_loop.is_running.assert_called()
        mock_rcts.assert_called_once()
        # First arg to run_coroutine_threadsafe must be a coroutine
        coro = mock_rcts.call_args[0][0]
        # Clean up the coroutine to avoid RuntimeWarning
        coro.close()


# ---------------------------------------------------------------------------
# Test 11 — LiveSupervisor recovery succeeds → state=RUNNING, error_count=0
# ---------------------------------------------------------------------------


def test_supervisor_recovery_succeeds_resets_state() -> None:
    """Successful _restart_adapter → state=RUNNING and error_count reset to 0."""
    from src.live.supervisor import LiveSupervisor, SupervisorState

    container = MagicMock()
    sup = LiveSupervisor(container, max_recovery_attempts=3, recovery_backoff_base_sec=0.001)
    sup._state = SupervisorState.DEGRADED
    sup._error_count = 1

    # Patch _restart_adapter to succeed immediately
    async def _ok_restart() -> None:
        pass

    sup._restart_adapter = _ok_restart  # type: ignore[method-assign]

    asyncio.run(sup._attempt_recovery())

    assert sup.state == SupervisorState.RUNNING
    assert sup._error_count == 0


# ---------------------------------------------------------------------------
# Test 12 — LiveSupervisor recovery exhausted → stop_event set
# ---------------------------------------------------------------------------


def test_supervisor_recovery_exhausted_stops() -> None:
    """All recovery attempts fail → _stop_event.set() is called."""
    from src.live.supervisor import LiveSupervisor, SupervisorState

    container = MagicMock()
    sup = LiveSupervisor(container, max_recovery_attempts=2, recovery_backoff_base_sec=0.001)
    sup._state = SupervisorState.DEGRADED

    call_count = 0

    async def _fail_restart() -> None:
        nonlocal call_count
        call_count += 1
        raise ConnectionError("adapter down")

    sup._restart_adapter = _fail_restart  # type: ignore[method-assign]

    asyncio.run(sup._attempt_recovery())

    assert sup._stop_event.is_set(), "_stop_event must be set after exhausted recovery"
    assert call_count == 2, f"Expected 2 restart attempts, got {call_count}"
