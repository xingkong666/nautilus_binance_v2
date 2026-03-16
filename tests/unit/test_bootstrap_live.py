"""Tests for test bootstrap live."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.app.bootstrap import _bootstrap_live_state, _build_live_strategies, _build_live_strategy
from src.core.events import EventBus
from src.execution.ignored_instruments import IgnoredInstrumentRegistry
from src.state.snapshot import SystemSnapshot


def test_build_live_strategy_uses_event_bus_and_symbol_override() -> None:
    """Verify that build live strategy uses event bus and symbol override."""
    container = SimpleNamespace(event_bus=EventBus())
    strategy = _build_live_strategy(
        Path("/root/workSpace/nautilus_binance_v2/configs/strategies/vegas_tunnel.yaml"),
        container,
        symbol="ETHUSDT",
    )

    assert str(strategy.config.instrument_id) == "ETHUSDT-PERP.BINANCE"
    assert strategy._event_bus is container.event_bus


def test_build_live_strategies_creates_one_instance_per_symbol() -> None:
    """Verify that build live strategies creates one instance per symbol."""
    container = SimpleNamespace(event_bus=EventBus())

    strategies = _build_live_strategies(
        Path("/root/workSpace/nautilus_binance_v2/configs/strategies/vegas_tunnel.yaml"),
        container,
        symbols=["BTCUSDT", "ETHUSDT", "BTCUSDT"],
    )

    assert [str(strategy.config.instrument_id) for strategy in strategies] == [
        "BTCUSDT-PERP.BINANCE",
        "ETHUSDT-PERP.BINANCE",
    ]


class _SnapshotManagerStub:
    def __init__(self, snapshot: SystemSnapshot | None = None) -> None:
        self._snapshot = snapshot
        self.saved: list[SystemSnapshot] = []

    def load_latest(self) -> SystemSnapshot | None:
        return self._snapshot

    def save(self, snapshot: SystemSnapshot) -> None:
        self._snapshot = snapshot
        self.saved.append(snapshot)


def test_bootstrap_live_state_saves_exchange_truth_snapshot() -> None:
    """Verify that bootstrap live state saves exchange truth snapshot."""
    event_bus = EventBus()
    snapshot_manager = _SnapshotManagerStub()
    container = SimpleNamespace(
        event_bus=event_bus,
        snapshot_manager=snapshot_manager,
        ignored_instruments=IgnoredInstrumentRegistry(event_bus),
    )
    adapter = SimpleNamespace(
        fetch_account_snapshot=lambda: (
            [{"asset": "USDT", "walletBalance": "1000"}],
            [
                {
                    "symbol": "ETHUSDC",
                    "positionSide": "LONG",
                    "positionAmt": "0.165",
                    "entryPrice": "2400.3",
                    "unrealizedProfit": "-51.4",
                    "leverage": "1",
                }
            ],
        ),
        fetch_open_orders=lambda: [],
    )

    _bootstrap_live_state(container, adapter)

    assert len(snapshot_manager.saved) == 1
    saved = snapshot_manager.saved[0]
    assert saved.account_balance == "1000"
    assert saved.positions[0].instrument_id == "ETHUSDC-PERP.BINANCE"
    assert saved.metadata["recovery_action"] == "cold_start_from_exchange"


def test_bootstrap_live_state_marks_exchange_position_as_ignored() -> None:
    """Verify that bootstrap live state marks exchange position as ignored."""
    event_bus = EventBus()
    snapshot_manager = _SnapshotManagerStub()
    ignored = IgnoredInstrumentRegistry(event_bus)
    container = SimpleNamespace(
        event_bus=event_bus,
        snapshot_manager=snapshot_manager,
        ignored_instruments=ignored,
    )
    adapter = SimpleNamespace(
        fetch_account_snapshot=lambda: (
            [{"asset": "USDT", "walletBalance": "1000"}],
            [
                {
                    "symbol": "ETHUSDC",
                    "positionSide": "LONG",
                    "positionAmt": "0.165",
                    "entryPrice": "2400.3",
                    "unrealizedProfit": "-51.4",
                    "leverage": "1",
                }
            ],
        ),
        fetch_open_orders=lambda: [],
    )

    _bootstrap_live_state(container, adapter)

    assert ignored.is_ignored("ETHUSDC-PERP.BINANCE") is True


def test_bootstrap_live_state_marks_exchange_open_order_as_ignored() -> None:
    """Verify that bootstrap live state marks exchange open order as ignored."""
    event_bus = EventBus()
    snapshot_manager = _SnapshotManagerStub()
    ignored = IgnoredInstrumentRegistry(event_bus)
    container = SimpleNamespace(
        event_bus=event_bus,
        snapshot_manager=snapshot_manager,
        ignored_instruments=ignored,
    )
    adapter = SimpleNamespace(
        fetch_account_snapshot=lambda: ([{"asset": "USDT", "walletBalance": "1000"}], []),
        fetch_open_orders=lambda: [
            {
                "symbol": "BTCUSDT",
                "clientOrderId": "external-123",
            }
        ],
    )

    _bootstrap_live_state(container, adapter)

    assert ignored.is_ignored("BTCUSDT-PERP.BINANCE") is True
    assert snapshot_manager.saved[0].open_orders[0]["clientOrderId"] == "external-123"
