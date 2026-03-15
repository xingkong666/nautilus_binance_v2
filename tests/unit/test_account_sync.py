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


def _make_container(snapshot: SystemSnapshot | None = None):
    event_bus = EventBus()
    return SimpleNamespace(
        event_bus=event_bus,
        snapshot_manager=_SnapshotManagerStub(snapshot),
        binance_adapter=None,
        ignored_instruments=IgnoredInstrumentRegistry(event_bus),
    )


def test_sync_once_reconciles_match_and_publishes_event() -> None:
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
    container = _make_container()
    sync = AccountSync(container=container)

    result = sync.sync_once()

    assert result.success is False
    assert "exchange_snapshot_provider_unavailable" in result.error


def test_sync_once_marks_exchange_symbol_as_ignored() -> None:
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
