"""Tests for test account sync."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from src.core.events import EventBus, EventType
from src.execution.ignored_instruments import IgnoredInstrumentRegistry
from src.live.account_sync import AccountBalance, AccountSync, PositionSnapshot
from src.state.snapshot import PositionSnapshot as SnapshotPosition
from src.state.snapshot import SystemSnapshot


class _SnapshotManagerStub:
    def __init__(self, snapshot: SystemSnapshot | None = None) -> None:
        self._snapshot = snapshot

    def load_latest(self) -> SystemSnapshot | None:
        return self._snapshot


class _CacheStub:
    def __init__(self, client_order_ids_open: list[str] | None = None) -> None:
        self._client_order_ids_open = client_order_ids_open or []

    def client_order_ids_open(self):
        return self._client_order_ids_open


class _RedisStub:
    def __init__(self) -> None:
        self.is_available = True
        self.hashes: dict[str, dict[str, str]] = {}
        self.expirations: dict[str, int] = {}

    def hset(self, name: str, mapping: dict[str, str]) -> int:
        self.hashes[name] = dict(mapping)
        return len(mapping)

    def expire(self, name: str, seconds: int) -> bool:
        self.expirations[name] = seconds
        return True


class _RealTimeRiskMonitorStub:
    def __init__(self) -> None:
        self.initialized: list[Decimal] = []
        self.updated: list[Decimal] = []

    def initialize(self, equity: Decimal) -> None:
        self.initialized.append(equity)

    def update(self, current_equity: Decimal) -> list[str]:
        self.updated.append(current_equity)
        return []


def _make_container(snapshot: SystemSnapshot | None = None):
    event_bus = EventBus()
    return SimpleNamespace(
        event_bus=event_bus,
        snapshot_manager=_SnapshotManagerStub(snapshot),
        binance_adapter=None,
        ignored_instruments=IgnoredInstrumentRegistry(event_bus),
        redis_client=None,
        real_time_risk_monitor=None,
    )


def test_sync_once_reconciles_match_and_publishes_event() -> None:
    """Verify that sync once reconciles match and publishes event."""
    snapshot = SystemSnapshot(
        positions=[
            SnapshotPosition(
                instrument_id="BTCUSDT-PERP.BINANCE",
                side="LONG",
                quantity="0.01",
                avg_entry_price="50000",
                unrealized_pnl="0",
                realized_pnl="0",
            )
        ]
    )
    container = _make_container(snapshot)
    reconciliations = []
    container.event_bus.subscribe(EventType.RECONCILIATION, reconciliations.append)
    sync = AccountSync(
        container=container,
        exchange_snapshot_provider=lambda: (
            [
                AccountBalance(
                    asset="USDT",
                    wallet_balance=Decimal("1000"),
                    available_balance=Decimal("900"),
                    unrealized_pnl=Decimal("0"),
                )
            ],
            [
                PositionSnapshot(
                    symbol="BTCUSDT",
                    side="LONG",
                    quantity=Decimal("0.01"),
                    entry_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=5,
                )
            ],
        ),
    )

    result = sync.sync_once()

    assert result.success is True
    assert result.reconciliation_matched is True
    assert result.mismatch_count == 0
    assert len(reconciliations) == 1
    assert reconciliations[0].payload["reconciliation_matched"] is True


def test_sync_once_detects_mismatch_and_emits_risk_alert() -> None:
    """Verify that sync once detects mismatch and emits risk alert."""
    snapshot = SystemSnapshot(
        positions=[
            SnapshotPosition(
                instrument_id="BTCUSDT-PERP.BINANCE",
                side="LONG",
                quantity="0.01",
                avg_entry_price="50000",
                unrealized_pnl="0",
                realized_pnl="0",
            )
        ]
    )
    container = _make_container(snapshot)
    alerts = []
    container.event_bus.subscribe(EventType.RISK_ALERT, alerts.append)
    sync = AccountSync(
        container=container,
        exchange_snapshot_provider=lambda: (
            [],
            [
                PositionSnapshot(
                    symbol="BTCUSDT",
                    side="LONG",
                    quantity=Decimal("0.02"),
                    entry_price=Decimal("50010"),
                    unrealized_pnl=Decimal("10"),
                    leverage=5,
                )
            ],
        ),
    )

    result = sync.sync_once()

    assert result.success is True
    assert result.reconciliation_matched is False
    assert result.mismatch_count == 1
    assert any(alert.rule_name == "reconciliation_mismatch" for alert in alerts)


def test_sync_once_fails_without_provider() -> None:
    """Verify that sync once fails without provider."""
    container = _make_container()
    sync = AccountSync(container=container)

    result = sync.sync_once()

    assert result.success is False
    assert "exchange_snapshot_provider_unavailable" in result.error


def test_sync_once_marks_exchange_symbol_as_ignored() -> None:
    """Verify that sync once marks exchange symbol as ignored."""
    container = _make_container()
    sync = AccountSync(
        container=container,
        exchange_snapshot_provider=lambda: (
            [],
            [
                PositionSnapshot(
                    symbol="ETHUSDC",
                    side="LONG",
                    quantity=Decimal("0.165"),
                    entry_price=Decimal("2400.3"),
                    unrealized_pnl=Decimal("-51.4"),
                    leverage=1,
                )
            ],
        ),
    )

    result = sync.sync_once()

    assert result.success is True
    assert container.ignored_instruments.is_ignored("ETHUSDC-PERP.BINANCE") is True


def test_sync_once_ignores_external_open_order_but_not_known_local_open_order() -> None:
    """Verify that sync once ignores external open order but not known local open order."""
    container = _make_container()
    container.binance_adapter = SimpleNamespace(
        fetch_open_orders=lambda: [
            {"symbol": "BTCUSDT", "clientOrderId": "local-1"},
            {"symbol": "ETHUSDC", "clientOrderId": "external-1"},
        ],
        node=SimpleNamespace(cache=_CacheStub(client_order_ids_open=["local-1"])),
    )
    sync = AccountSync(
        container=container,
        exchange_snapshot_provider=lambda: ([], []),
    )

    result = sync.sync_once()

    assert result.success is True
    assert container.ignored_instruments.is_ignored("BTCUSDT-PERP.BINANCE") is False
    assert container.ignored_instruments.is_ignored("ETHUSDC-PERP.BINANCE") is True


def test_sync_once_uses_container_redis_client_for_balance_and_position_cache() -> None:
    """Verify that sync once writes account snapshot to container redis client."""
    container = _make_container()
    redis_client = _RedisStub()
    container.redis_client = redis_client
    sync = AccountSync(
        container=container,
        interval_sec=30.0,
        exchange_snapshot_provider=lambda: (
            [
                AccountBalance(
                    asset="USDT",
                    wallet_balance=Decimal("1000"),
                    available_balance=Decimal("900"),
                    unrealized_pnl=Decimal("5"),
                )
            ],
            [
                PositionSnapshot(
                    symbol="BTCUSDT",
                    side="LONG",
                    quantity=Decimal("0.01"),
                    entry_price=Decimal("50000"),
                    unrealized_pnl=Decimal("10"),
                    leverage=5,
                )
            ],
        ),
    )

    result = sync.sync_once()

    assert result.success is True
    assert redis_client.hashes["nautilus:account:balance:USDT"]["wallet_balance"] == "1000"
    assert redis_client.hashes["nautilus:account:position:BTCUSDT:LONG"]["quantity"] == "0.01"
    assert redis_client.expirations["nautilus:account:balance:USDT"] == 35
    assert redis_client.expirations["nautilus:account:position:BTCUSDT:LONG"] == 35


def test_sync_once_updates_real_time_risk_monitor_from_usdt_balance() -> None:
    """Verify that sync once initializes and updates real time risk monitor."""
    container = _make_container()
    monitor = _RealTimeRiskMonitorStub()
    container.real_time_risk_monitor = monitor
    sync = AccountSync(
        container=container,
        exchange_snapshot_provider=lambda: (
            [
                AccountBalance(
                    asset="USDT",
                    wallet_balance=Decimal("1000"),
                    available_balance=Decimal("900"),
                    unrealized_pnl=Decimal("0"),
                )
            ],
            [],
        ),
    )

    result = sync.sync_once()

    assert result.success is True
    assert monitor.initialized == [Decimal("1000")]
    assert monitor.updated == [Decimal("1000")]
